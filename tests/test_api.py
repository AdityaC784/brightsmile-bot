"""HTTP API tests: injected graph built from scripted LLMs; turn logging hits the real DB."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from tests.test_graph import FakeAnswerer, ScriptedUtility, _db_ok

from bot.api.main import create_app
from bot.api.turns import redact
from bot.config import get_settings
from bot.graph.builder import build_graph
from bot.graph.nodes import Nodes

pytestmark = pytest.mark.skipif(not _db_ok(), reason="needs `docker compose up db`")

ADMIN = "test-admin-key"


@pytest.fixture
def admin_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", ADMIN)
    return {"X-Admin-Key": ADMIN}


def _client(*utility_replies):
    graph = build_graph(
        Nodes(utility=ScriptedUtility(*utility_replies), answerer=FakeAnswerer()),
        checkpointer=InMemorySaver(),
    )
    return TestClient(create_app(graph))


def _tid() -> str:
    return f"test-{uuid.uuid4().hex[:10]}"


def _db():
    import psycopg

    return psycopg.connect(get_settings().database_url)


# --------------------------------------------------------------------------- redaction


def test_redact_masks_patient_contacts_but_keeps_clinic_numbers():
    text = "Call me on 98765 43210 or +91-98765-00000, mail a.b@x.com; clinic +91-20-5550-0140"
    out = redact(text)
    assert "98765" not in out and "a.b@x.com" not in out
    assert out.count("[phone]") == 2 and "[email]" in out
    assert "+91-20-5550-0140" in out


# --------------------------------------------------------------------------- chat


def test_chat_json_reply_and_turn_logged_with_contact_redacted():
    tid = _tid()
    with _client({"intent": "support"}) as c:
        r = c.post(
            "/v1/chat?stream=false",
            json={"thread_id": tid, "message": "Price of scaling? my no. 9876543210"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == tid and body["behavior"] == "answer"
    assert body["answer"].startswith("grounded answer to:") and body["message_id"]
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE thread_id = %s ORDER BY id", (tid,)
        ).fetchall()
    assert [r[0] for r in rows] == ["user", "assistant"]
    assert "9876543210" not in rows[0][1] and "[phone]" in rows[0][1]


def test_chat_streams_status_answer_chunks_and_done():
    tid = _tid()
    with _client({"intent": "support"}) as c:
        r = c.post("/v1/chat", json={"thread_id": tid, "message": "Is the clinic open Sunday?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [
        line.split(":", 1)[1].strip() for line in r.text.splitlines() if line.startswith("event:")
    ]
    assert events[0] == "status" and events[-1] == "done" and "answer" in events
    data = [
        json.loads(line.split(":", 1)[1])
        for line in r.text.splitlines()
        if line.startswith("data:")
    ]
    assert data[-1]["thread_id"] == tid and "answer" not in data[-1]
    streamed = " ".join(d["text"] for d in data[1:-1])
    assert streamed.startswith("grounded answer to:")


def test_provider_failure_returns_fallback_not_500():
    class Broken:
        def invoke(self, *a, **k):
            raise RuntimeError("provider down")

    with TestClient(create_app(Broken())) as c:
        r = c.post("/v1/chat?stream=false", json={"message": "hello"})
    assert r.status_code == 200
    assert r.json()["behavior"] == "abstain" and "+91-20-5550-0140" in r.json()["answer"]


def test_invalid_input_rejected():
    with _client() as c:
        assert c.post("/v1/chat", json={"message": ""}).status_code == 422
        assert c.post("/v1/chat", json={"message": "x", "thread_id": "bad id!"}).status_code == 422
        assert c.post("/v1/feedback", json={"thread_id": "t", "rating": 5}).status_code == 422


# --------------------------------------------------------------------------- admin & handoff


def test_admin_endpoints_require_key(admin_key):
    with _client() as c:
        assert c.get("/v1/leads").status_code == 401
        assert c.get("/v1/leads", headers={"X-Admin-Key": "wrong"}).status_code == 401
        assert c.get("/v1/leads", headers=admin_key).status_code == 200


def test_admin_disabled_when_key_is_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", "change-me")
    with _client() as c:
        assert c.get("/v1/leads", headers={"X-Admin-Key": "change-me"}).status_code == 503


def test_staff_chat_several_replies_then_end(admin_key):
    tid = _tid()
    with _client({"intent": "handoff"}, {"intent": "support"}) as c:
        c.get("/v1/handoffs", headers=admin_key)  # a staff member has the staff page open
        r = c.post(
            "/v1/chat?stream=false",
            json={"thread_id": tid, "message": "I want to speak to the manager"},
        )
        assert r.json()["handoff_pending"] is True
        listed = next(
            h for h in c.get("/v1/handoffs", headers=admin_key).json() if h["thread_id"] == tid
        )
        assert listed["waiting"] is True

        reply = lambda text: c.post(  # noqa: E731
            f"/v1/handoff/{tid}/reply", headers=admin_key, json={"message": text}
        )
        assert reply("Hi, Vikram here. How can I help?").json()["status"] == "sent"
        relayed = c.post(
            "/v1/chat?stream=false", json={"thread_id": tid, "message": "About my bill"}
        )
        assert relayed.json()["behavior"] == "staff" and relayed.json()["answer"] == ""
        assert relayed.json()["handoff_pending"] is True
        assert reply("Let me check that for you.").status_code == 200  # several replies

        assert c.post(f"/v1/handoff/{tid}/end", headers=admin_key).json()["status"] == "ended"
        assert reply("too late").status_code == 409
        assert c.post(f"/v1/handoff/{tid}/end", headers=admin_key).status_code == 200  # idempotent
        assert not any(
            p["thread_id"] == tid for p in c.get("/v1/handoffs", headers=admin_key).json()
        )
    with _db() as conn:
        src = conn.execute("SELECT source FROM candidates WHERE thread_id = %s", (tid,)).fetchall()
    assert src.count(("handoff",)) == 1  # only the turn that started it


def test_thumbs_down_creates_review_candidate():
    tid = _tid()
    with _client({"intent": "support"}) as c:
        mid = c.post(
            "/v1/chat?stream=false", json={"thread_id": tid, "message": "Price of a crown?"}
        ).json()["message_id"]
        r = c.post(
            "/v1/feedback",
            json={
                "thread_id": tid,
                "message_id": mid,
                "rating": -1,
                "comment": "wrong price, call me 9876543210",
            },
        )
    assert r.status_code == 200
    with _db() as conn:
        cand = conn.execute(
            "SELECT source, question FROM candidates WHERE thread_id = %s", (tid,)
        ).fetchone()
        comment = conn.execute(
            "SELECT comment FROM feedback WHERE thread_id = %s", (tid,)
        ).fetchone()[0]
    assert cand == ("thumbs_down", "Price of a crown?")
    assert "9876543210" not in comment


def test_health():
    with _client() as c:
        r = c.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["chunks"] >= 0  # CI test job runs without ingest


def test_chat_page_served_with_clinic_facts_from_config():
    with _client() as c:
        r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "{{" not in r.text
    from bot.config import load_yaml

    assert load_yaml("generation")["clinic"]["name"] in r.text and "/v1/chat" in r.text


def test_staff_messages_and_end_reach_the_patient_poll(admin_key):
    tid = _tid()
    with _client({"intent": "handoff"}) as c:
        c.get("/v1/handoffs", headers=admin_key)
        r = c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
        after = r.json()["message_id"]
        poll = lambda a: c.get(f"/v1/chat/{tid}/staff-replies", params={"after": a}).json()  # noqa: E731
        assert poll(after) == {"pending": True, "replies": [], "staff_online": True}
        listed = next(
            h for h in c.get("/v1/handoffs", headers=admin_key).json() if h["thread_id"] == tid
        )
        assert [m["role"] for m in listed["recent"]][:1] == ["user"]

        c.post(f"/v1/handoff/{tid}/reply", headers=admin_key, json={"message": "Hi, Vikram here."})
        got = poll(after)
        assert got["pending"] is True
        assert [(m["kind"], m["text"]) for m in got["replies"]] == [("staff", "Hi, Vikram here.")]
        after = got["replies"][-1]["id"]
        assert poll(after)["replies"] == []

        c.post(f"/v1/handoff/{tid}/end", headers=admin_key)
        got = poll(after)
        assert got["pending"] is False and [m["kind"] for m in got["replies"]] == ["end"]
        assert c.get("/v1/chat/bad id!/staff-replies").status_code == 422


def test_patient_poll_ends_the_staff_chat_after_the_timeout(admin_key, monkeypatch):
    import time as _time

    tid = _tid()
    with _client({"intent": "handoff"}) as c:
        c.get("/v1/handoffs", headers=admin_key)
        r = c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
        after = r.json()["message_id"]
        later = _time.time() + 241
        monkeypatch.setattr("bot.api.staff.time.time", lambda: later)
        got = c.get(f"/v1/chat/{tid}/staff-replies", params={"after": after}).json()
    assert got["pending"] is False
    assert [m["kind"] for m in got["replies"]] == ["end"]
    assert "call reception" in got["replies"][0]["text"]


def test_staff_page_served():
    with _client() as c:
        r = c.get("/staff")
    assert r.status_code == 200 and "/v1/handoffs" in r.text and "{{" not in r.text
    assert 'window.open("/leads"' in r.text


def test_leads_page_served():
    with _client() as c:
        r = c.get("/leads")
    assert r.status_code == 200 and "/v1/leads" in r.text and "{{" not in r.text


def test_nobody_online_gets_the_offline_message_at_once(admin_key):
    tid = _tid()
    with _client({"intent": "handoff"}) as c:  # no staff page open
        r = c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
        body = r.json()
        assert body["handoff_pending"] is False and "Nobody from our team" in body["answer"]
        assert "+91-20-5550-0140" in body["answer"]
        assert not any(
            h["thread_id"] == tid for h in c.get("/v1/handoffs", headers=admin_key).json()
        )


def test_end_removes_a_chat_whose_state_is_gone(admin_key):
    tid = _tid()
    with _client({"intent": "handoff"}) as c:
        c.get("/v1/handoffs", headers=admin_key)
        c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
    with _client() as c:  # new app, empty conversation memory: the DB row is all that's left
        assert any(h["thread_id"] == tid for h in c.get("/v1/handoffs", headers=admin_key).json())
        assert c.post(f"/v1/handoff/{tid}/end", headers=admin_key).status_code == 200
        assert not any(
            h["thread_id"] == tid for h in c.get("/v1/handoffs", headers=admin_key).json()
        )


def test_staff_list_shows_patient_presence_and_closes_chats_they_left(admin_key, monkeypatch):
    import time as _time

    tid = _tid()
    with _client({"intent": "handoff"}) as c:
        c.get("/v1/handoffs", headers=admin_key)
        r = c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
        c.post(
            f"/v1/handoff/{tid}/reply", headers=admin_key, json={"message": "Hi!"}
        )  # not waiting
        c.get(
            f"/v1/chat/{tid}/staff-replies",
            params={"after": r.json()["message_id"], "hidden": True},
        )
        mine = lambda: next(  # noqa: E731
            (h for h in c.get("/v1/handoffs", headers=admin_key).json() if h["thread_id"] == tid),
            None,
        )
        assert mine()["patient"] == "online"  # they just wrote from a visible tab
        now = _time.time()
        monkeypatch.setattr("bot.api.staff.time.time", lambda: now + 30)
        assert mine()["patient"] == "away"  # only the hidden tab still polls (slowly)
        monkeypatch.setattr("bot.api.staff.time.time", lambda: now + 95)
        assert mine()["patient"] == "left"
        monkeypatch.setattr("bot.api.staff.time.time", lambda: now + 301)
        assert mine() is None  # gone 5 minutes: closed
        got = c.get(f"/v1/chat/{tid}/staff-replies", params={"after": 0}).json()
        assert got["pending"] is False and "you were away" in got["replies"][-1]["text"]


def test_staff_logging_out_closes_open_chats_and_new_requests_get_offline(admin_key, monkeypatch):
    import time as _time

    tid = _tid()
    staff_tab = {**admin_key, "X-Staff-Session": "tab-1"}
    with _client({"intent": "handoff"}, {"intent": "handoff"}) as c:
        c.get("/v1/handoffs", headers=staff_tab)
        r = c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
        after = r.json()["message_id"]
        c.post(f"/v1/handoff/{tid}/reply", headers=staff_tab, json={"message": "Hi!"})
        assert c.post("/v1/staff/offline", headers=staff_tab).status_code == 200

        poll = lambda: c.get(f"/v1/chat/{tid}/staff-replies", params={"after": after}).json()  # noqa: E731
        assert poll()["pending"] is True  # grace period: a page reload would ping again
        now = _time.time()
        monkeypatch.setattr("bot.api.staff.time.time", lambda: now + 11)
        got = poll()
        assert got["pending"] is False and got["staff_online"] is False
        assert "gone offline" in got["replies"][-1]["text"]

        again = c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "Staff please"})
        assert again.json()["handoff_pending"] is False
        assert "Nobody from our team" in again.json()["answer"]


def test_history_lets_a_reopened_page_show_the_conversation(admin_key):
    tid = _tid()
    with _client({"intent": "support"}, {"intent": "handoff"}) as c:
        c.get("/v1/handoffs", headers=admin_key)
        c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "Price of scaling?"})
        c.post("/v1/chat?stream=false", json={"thread_id": tid, "message": "A person please"})
        c.post(f"/v1/handoff/{tid}/reply", headers=admin_key, json={"message": "Hi, Vikram here."})
        h = c.get(f"/v1/chat/{tid}/history").json()
        assert h["pending"] is True and h["staff_online"] is True
        assert [m["role"] for m in h["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "human_agent",
        ]
        assert h["messages"][1]["citations"]  # the bot answer keeps its sources
        c.post(f"/v1/handoff/{tid}/end", headers=admin_key)  # don't leave it on the staff list
        assert c.get("/v1/chat/bad id!/history").status_code == 422
        assert c.get(f"/v1/chat/{_tid()}/history").json()["messages"] == []
