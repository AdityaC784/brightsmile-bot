"""FastAPI service (plan §6).

    uv run uvicorn bot.api.main:app --port 8000      # make serve

POST /v1/chat                        reply, streamed over SSE (or JSON with ?stream=false)
GET  /v1/chat/{thread_id}/staff-replies  staff replies newer than ?after=<message id> (+ presence)
GET  /v1/chat/{thread_id}/history   the conversation so far (page refresh / second tab)
POST /v1/feedback                    thumbs up/down (+ comment) -> review candidates
POST /v1/handoff/{thread_id}/reply   staff message (staff keep the chat until they end it) [admin]
POST /v1/handoff/{thread_id}/end     end the staff chat; the assistant takes over      [admin]
GET  /v1/handoffs                    conversations held by staff (marks staff online) [admin]
POST /v1/staff/offline               this staff page was locked or closed              [admin]
GET  /v1/leads, DELETE /v1/leads/{id}                                                 [admin]
POST /v1/ingest                      re-index the KB                                  [admin]
GET  /v1/health                      KB version, chunk count, DB status
GET  /                              chat page (static/index.html)
GET  /staff                         staff page: handoffs + leads (static/staff.html; uses the admin key)

Streaming note: replies are verified before they are shown, so the stream sends a status
event, then the *verified* answer in chunks, then a `done` event with citations. Streaming raw
model tokens would show text that might still be rejected by the citation verifier.
"""

from __future__ import annotations

import html
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from bot.api import staff, turns
from bot.api.schemas import ChatIn, ChatOut, FeedbackIn, HumanReplyIn
from bot.config import get_settings, load_yaml
from bot.graph.builder import build_graph, new_turn

log = structlog.get_logger("bot.api")
DEFAULT_ADMIN_KEYS = {"", "change-me"}
STATIC = Path(__file__).parent / "static"
THREAD_ID = r"^[\w.:-]{1,100}$"


def _open_pool_checkpointer():
    """PostgresSaver backed by a connection pool (safe for concurrent requests)."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=get_settings().database_url,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    cp = PostgresSaver(pool)
    cp.setup()
    return pool, cp


def create_app(graph=None, *, persist_turns: bool = True) -> FastAPI:
    """`graph` lets tests inject a graph built from fakes; otherwise one is built at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = None
        if graph is None:
            pool, cp = _open_pool_checkpointer()
            app.state.graph = build_graph(checkpointer=cp)
        else:
            app.state.graph = graph
        if get_settings().admin_api_key in DEFAULT_ADMIN_KEYS:
            log.warning("admin.key_not_set", hint="set ADMIN_API_KEY; admin endpoints disabled")
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="BrightSmile Dental Care assistant", version="0.1.0", lifespan=lifespan)
    app.state.persist_turns = persist_turns
    app.state.presence = staff.Presence()
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"]
    )

    def require_admin(x_admin_key: str = Header(default="")) -> None:
        expected = get_settings().admin_api_key
        if expected in DEFAULT_ADMIN_KEYS:
            raise HTTPException(503, "admin endpoints disabled: ADMIN_API_KEY is not configured")
        if not secrets.compare_digest(x_admin_key, expected):
            raise HTTPException(401, "invalid admin key")

    # ------------------------------------------------------------------ chat

    async def _run_turn(request: Request, body: ChatIn) -> ChatOut:
        thread_id = body.thread_id or f"web-{uuid.uuid4().hex[:12]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        t0 = time.perf_counter()
        persist, presence = request.app.state.persist_turns, request.app.state.presence
        presence.patient_ping(thread_id)
        try:  # staff didn't answer in time: the assistant takes this message
            await run_in_threadpool(
                staff.expire,
                request.app.state.graph,
                thread_id,
                persist=persist,
                presence=presence,
            )
        except Exception as e:
            log.error("staff.expire_failed", thread_id=thread_id, error=type(e).__name__)
        turn = new_turn(thread_id, body.message, staff_online=presence.staff_online())
        try:
            out = await run_in_threadpool(request.app.state.graph.invoke, turn, cfg)
        except Exception as e:  # provider outage, quota, DB error: never leave the user hanging
            log.error("chat.failed", thread_id=thread_id, error=type(e).__name__)
            fallback = load_yaml("generation")["fallback_answer"].strip()
            return ChatOut(
                thread_id=thread_id,
                message_id=None,
                intent=None,
                behavior="abstain",
                answer=fallback,
            )
        reply = out.get("reply") or {}
        pending = bool(out.get("handoff_pending"))
        message_id = None
        if request.app.state.persist_turns:
            try:
                message_id = await run_in_threadpool(
                    turns.log_turn, thread_id, body.message, out, pending
                )
            except Exception as e:  # logging must not break the reply
                log.error("turn.log_failed", thread_id=thread_id, error=type(e).__name__)
        log.info(
            "chat.turn",
            thread_id=thread_id,
            intent=out.get("intent"),
            behavior=reply.get("behavior"),
            handoff=pending,
            ms=int((time.perf_counter() - t0) * 1000),
        )
        return ChatOut(
            thread_id=thread_id,
            message_id=message_id,
            intent=out.get("intent"),
            behavior=reply.get("behavior", "answer"),
            answer=reply.get("answer", ""),
            citations=reply.get("citations", []),
            confidence=reply.get("confidence"),
            handoff_pending=pending,
        )

    @app.post("/v1/chat", response_model=None)
    async def chat(request: Request, body: ChatIn, stream: bool = True):
        if not stream:
            return await _run_turn(request, body)

        async def events():
            yield {"event": "status", "data": json.dumps({"status": "thinking"})}
            result = await _run_turn(request, body)
            words = result.answer.split(" ")
            for i in range(0, len(words), 8):  # verified answer, in small chunks
                yield {"event": "answer", "data": json.dumps({"text": " ".join(words[i : i + 8])})}
            yield {"event": "done", "data": result.model_dump_json(exclude={"answer"})}

        return EventSourceResponse(events())

    @app.get("/v1/chat/{thread_id}/staff-replies")
    async def staff_replies(
        request: Request,
        thread_id: str = PathParam(pattern=THREAD_ID),
        after: int = 0,
        hidden: bool = False,
    ):
        """Polled by the chat page while staff hold the conversation (also its presence ping).
        The thread id is a random per-browser value, so it doubles as the capability to read
        this thread."""
        persist, presence = request.app.state.persist_turns, request.app.state.presence
        presence.patient_ping(thread_id, hidden)
        await run_in_threadpool(
            staff.expire, request.app.state.graph, thread_id, persist=persist, presence=presence
        )
        out = await run_in_threadpool(turns.staff_replies, thread_id, after)
        return {**out, "staff_online": presence.staff_online()}

    @app.get("/v1/chat/{thread_id}/history")
    async def chat_history(request: Request, thread_id: str = PathParam(pattern=THREAD_ID)):
        """Lets a reopened page (refresh, duplicated tab) show the conversation and whether
        staff hold it. Same capability as staff-replies: the random thread id."""
        graph, presence = request.app.state.graph, request.app.state.presence
        presence.patient_ping(thread_id)
        persist = request.app.state.persist_turns
        await run_in_threadpool(staff.expire, graph, thread_id, persist=persist, presence=presence)
        messages = await run_in_threadpool(turns.history, thread_id)
        pending = await run_in_threadpool(staff.in_staff_mode, graph, thread_id)
        return {"messages": messages, "pending": pending, "staff_online": presence.staff_online()}

    # ------------------------------------------------------------------ feedback & handoff

    @app.post("/v1/feedback")
    async def feedback(body: FeedbackIn):
        fid = await run_in_threadpool(
            turns.save_feedback, body.thread_id, body.message_id, body.rating, body.comment
        )
        return {"id": fid}

    @app.get("/v1/handoffs", dependencies=[Depends(require_admin)])
    async def handoffs(request: Request):
        """Open staff chats. Called by the staff page every 5 s, which marks staff online."""
        graph, presence = request.app.state.graph, request.app.state.presence
        presence.staff_ping(request.headers.get("x-staff-session", "default"))

        def load():
            for h in turns.pending_handoffs():  # close chats that timed out or were left
                staff.expire(graph, h["thread_id"], presence=presence)
            items = turns.pending_handoffs()
            for h in items:
                h["patient"], h["patient_seen_seconds"] = presence.patient_status(h["thread_id"])
            return items

        return await run_in_threadpool(load)

    @app.post("/v1/staff/offline", dependencies=[Depends(require_admin)])
    async def staff_offline(request: Request):
        request.app.state.presence.staff_leave(request.headers.get("x-staff-session", "default"))
        return {"status": "offline"}

    async def _require_staff_mode(request: Request, thread_id: str) -> None:
        if not await run_in_threadpool(staff.in_staff_mode, request.app.state.graph, thread_id):
            raise HTTPException(409, "staff are not holding this conversation")

    @app.post("/v1/handoff/{thread_id}/reply", dependencies=[Depends(require_admin)])
    async def handoff_reply(request: Request, thread_id: str, body: HumanReplyIn):
        await _require_staff_mode(request, thread_id)
        await run_in_threadpool(
            staff.reply,
            request.app.state.graph,
            thread_id,
            body.message,
            persist=request.app.state.persist_turns,
        )
        return {"thread_id": thread_id, "status": "sent"}

    @app.post("/v1/handoff/{thread_id}/end", dependencies=[Depends(require_admin)])
    async def handoff_end(request: Request, thread_id: str):
        """Always takes the chat off the staff list, even if its conversation state is gone."""
        await run_in_threadpool(
            staff.end,
            request.app.state.graph,
            thread_id,
            persist=request.app.state.persist_turns,
            presence=request.app.state.presence,
        )
        return {"thread_id": thread_id, "status": "ended"}

    # ------------------------------------------------------------------ admin data

    @app.get("/v1/leads", dependencies=[Depends(require_admin)])
    async def leads():
        def q():
            with psycopg.connect(get_settings().database_url) as conn:
                rows = conn.execute(
                    """SELECT id, thread_id, need, budget, timeline, name, contact, consent_at,
                              created_at FROM leads ORDER BY created_at DESC"""
                ).fetchall()
            keys = [
                "id",
                "thread_id",
                "need",
                "budget",
                "timeline",
                "name",
                "contact",
                "consent_at",
                "created_at",
            ]
            return [
                {
                    k: (str(v) if k in ("id", "consent_at", "created_at") else v)
                    for k, v in zip(keys, r, strict=True)
                }
                for r in rows
            ]

        return await run_in_threadpool(q)

    @app.delete("/v1/leads/{lead_id}", dependencies=[Depends(require_admin)])
    async def delete_lead(lead_id: uuid.UUID):
        from bot.leads.store import delete_lead as _delete

        if not await run_in_threadpool(_delete, str(lead_id)):
            raise HTTPException(404, "lead not found")
        return {"deleted": str(lead_id)}

    @app.post("/v1/ingest", dependencies=[Depends(require_admin)])
    async def ingest():
        from bot.retrieval.ingest import ingest as _ingest

        r = await run_in_threadpool(_ingest)
        return {"kb_version": r.kb_version, "documents": r.documents, "chunks": r.chunks}

    def _page(name: str) -> str:
        page = (STATIC / name).read_text(encoding="utf-8")
        clinic = load_yaml("generation")["clinic"]
        for key in ("name", "reception", "emergency"):
            field = "clinic_name" if key == "name" else key
            page = page.replace("{{" + field + "}}", html.escape(clinic[key]))
        timeout = load_yaml("generation")["handoff"]["staff_timeout_seconds"]
        return page.replace("{{staff_timeout_minutes}}", f"{timeout / 60:g}")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def chat_page():
        return _page("index.html")

    @app.get("/staff", response_class=HTMLResponse, include_in_schema=False)
    async def staff_page():
        return _page("staff.html")

    @app.get("/v1/health")
    async def health():
        def q():
            with psycopg.connect(get_settings().database_url, connect_timeout=3) as conn:
                row = conn.execute(
                    "SELECT count(*), max(kb_version) FROM chunks WHERE chunker = %s",
                    (load_yaml("retrieval")["chunking"]["strategy"],),
                ).fetchone()
            return {"status": "ok", "chunks": row[0], "kb_version": row[1]}

        try:
            return await run_in_threadpool(q)
        except Exception as e:
            raise HTTPException(503, f"database unavailable: {type(e).__name__}") from e

    return app


app = create_app()
