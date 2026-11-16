"""Graph nodes. Each takes the state and returns a partial update.

Dependencies (LLMs, the answerer) are injected through `Nodes(...)` so tests can use fakes.
"""

from __future__ import annotations

import time
from functools import cached_property
from typing import Literal

from langgraph.graph import END
from pydantic import BaseModel, field_validator

from bot.config import load_yaml
from bot.graph.state import ChatState, Intent, Turn
from bot.leads.store import save_lead
from bot.llm.base import Message
from bot.llm.client import LLM, get_llm
from bot.prompts import clinic_prompt, load_prompt
from bot.rag.answer import Answerer

HISTORY_TURNS = 6  # conversation window given to the classifier, rewriter and generator


class _Intent(BaseModel):
    intent: Intent
    emergency: bool = False
    reason: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _norm(cls, v: object) -> object:
        return v.strip().lower().replace(" ", "_") if isinstance(v, str) else v

    @field_validator("emergency", mode="before")
    @classmethod
    def _flag(cls, v: object) -> object:
        return v.strip().lower() in ("true", "yes", "1") if isinstance(v, str) else bool(v)


class _Standalone(BaseModel):
    standalone: str


class _LeadExtract(BaseModel):
    need: str | None = None
    timeline: str | None = None
    budget: str | None = None
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    consent: Literal["yes", "no", "unclear"] = "unclear"
    question: str | None = None

    @field_validator("consent", mode="before")
    @classmethod
    def _norm(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else "unclear"


def _history(state: ChatState) -> list[Turn]:
    """Prior turns, excluding the current user message (the last one)."""
    return list(state.get("messages", []))[:-1][-HISTORY_TURNS:]


def _transcript(turns: list[Turn]) -> str:
    return "\n".join(f"{t['role']}: {t['content']}" for t in turns) or "(none)"


class Nodes:
    def __init__(
        self,
        utility: LLM | None = None,
        answerer: Answerer | None = None,
    ):
        self._utility = utility
        self._answerer = answerer
        self.cfg = load_yaml("generation")

    @cached_property
    def utility(self) -> LLM:
        return self._utility or get_llm("utility")

    @cached_property
    def answerer(self) -> Answerer:
        return self._answerer or Answerer()

    # -------------------------------------------------------------------- routing

    def classify(self, state: ChatState) -> dict:
        prompt = (
            f"Recent conversation:\n{_transcript(_history(state))}\n\n"
            f"Latest patient message:\n{state['question']}"
        )
        parsed, _ = self.utility.chat_json(
            clinic_prompt("classify"), [Message("user", prompt)], _Intent
        )
        return {"intent": parsed.intent, "emergency": parsed.emergency}

    @staticmethod
    def route(
        state: ChatState,
    ) -> Literal["rewrite", "lead", "handoff", "greet", "refuse", "staff"]:
        intent = state.get("intent", "support")
        emergency = state.get("emergency", False)
        # Staff hold the conversation: messages go to them, except red-flag symptoms, which
        # the bot answers at once (staff may be slow; an emergency can't wait).
        if state.get("handoff_pending"):
            return "rewrite" if emergency else "staff"
        # Asking for a person with a red-flag symptom: answer first, then hand off.
        if intent == "handoff" and emergency:
            return "rewrite"
        # Mid-enquiry, keep the patient in the lead flow (it answers questions too), unless
        # they ask for a human or go off-topic.
        if state.get("lead_stage") in ("collecting", "awaiting_consent") and intent in (
            "support",
            "lead",
            "greeting",
        ):
            return "lead"
        return {
            "support": "rewrite",
            "lead": "lead",
            "handoff": "handoff",
            "greeting": "greet",
            "out_of_scope": "refuse",
        }.get(state.get("intent", "support"), "rewrite")

    # -------------------------------------------------------------------- support path

    def rewrite(self, state: ChatState) -> dict:
        history = _history(state)
        if not history:  # first message: nothing to resolve
            return {"standalone": state["question"]}
        prompt = (
            f"Recent conversation:\n{_transcript(history)}\n\n"
            f"Latest patient message:\n{state['question']}"
        )
        parsed, _ = self.utility.chat_json(
            load_prompt("rewrite"), [Message("user", prompt)], _Standalone
        )
        return {"standalone": parsed.standalone.strip() or state["question"]}

    def support(self, state: ChatState) -> dict:
        history = [
            Message("assistant" if t["role"] != "user" else "user", t["content"])
            for t in _history(state)
        ]
        r = self.answerer.answer(
            state["question"], history, query=state.get("standalone") or state["question"]
        )
        v = getattr(r, "verification", None)
        reply = {
            "behavior": r.behavior,
            "answer": r.answer,
            "citations": r.citations,
            "confidence": r.confidence,
            "fallback_reason": r.fallback_reason,
            "repaired": r.repaired,
            # Per-turn metrics for logging (plan §3: latency, tokens, retrieval ids, verifier).
            "retrieved_ids": [h.doc_id for h in getattr(r, "hits", [])],
            "verifier": {"claims": v.n_claims, "supported": v.n_supported} if v else None,
            "latency_ms": getattr(r, "latency_ms", 0),
            "tokens_in": getattr(r, "tokens_in", 0),
            "tokens_out": getattr(r, "tokens_out", 0),
        }
        return {"reply": reply, "messages": [Turn(role="assistant", content=r.answer)]}

    # -------------------------------------------------------------------- fixed replies

    def _fixed(self, behavior: str, text: str) -> dict:
        return {
            "reply": {"behavior": behavior, "answer": text, "citations": []},
            "messages": [Turn(role="assistant", content=text)],
        }

    def greet(self, state: ChatState) -> dict:
        c = self.cfg["clinic"]
        return self._fixed(
            "answer",
            f"Hello! I'm the {c['name']} assistant. I can help with our services, prices, "
            "appointments, insurance, clinic policies and aftercare. What would you like to know?",
        )

    def refuse(self, state: ChatState) -> dict:
        c = self.cfg["clinic"]
        return self._fixed(
            "refuse",
            f"Sorry, I can only help with questions about {c['name']}: our services, prices, "
            "appointments, policies and dental aftercare.",
        )

    # -------------------------------------------------------------------- human handoff

    @staticmethod
    def after_support(state: ChatState) -> str:
        """After the bot answered a red-flag message that also asked for a person: hand off."""
        if state.get("intent") == "handoff" and not state.get("handoff_pending"):
            return "handoff"
        return END

    def handoff(self, state: ChatState) -> dict:
        """Start a staff conversation: acknowledge and flag the thread. Staff reply through the
        API; until they end it (or it times out) patient messages go to them (`staff`)."""
        c = self.cfg["clinic"]
        online = state.get("staff_online", True)
        if online:
            ack = (
                "I've passed your message to our team and a staff member will reply here. If "
                f"it's urgent, you can also reach reception directly on {c['reception']}."
            )
        else:  # nobody has the staff page open: don't make the patient wait for nothing
            ack = self.cfg["handoff"]["offline_text"].strip().format(**c)
        prior = state.get("reply") or {}  # the bot's answer earlier in this turn, if any
        reply = {**prior, "behavior": prior.get("behavior", "handoff"), "handoff_started": online}
        reply["answer"] = f"{prior['answer']}\n\n{ack}" if prior.get("answer") else ack
        reply.setdefault("citations", [])
        out = {"reply": reply, "messages": [Turn(role="assistant", content=ack)]}
        if online:
            out |= {"handoff_pending": True, "staff_waiting_since": time.time()}
        return out

    def staff(self, state: ChatState) -> dict:
        """Staff hold the conversation: keep the message for them; the bot stays silent."""
        return {
            "reply": {"behavior": "staff", "answer": "", "citations": []},
            "staff_waiting_since": state.get("staff_waiting_since") or time.time(),
        }

    # -------------------------------------------------------------------- lead capture

    def lead(self, state: ChatState) -> dict:
        """Slot-filling with consent. Helps first, asks for contact only after the need is
        known, and saves ONLY after explicit consent with a name and a phone number (email is
        optional; both are stored in the lead's contact field)."""
        c = self.cfg["clinic"]
        consent_text = self.cfg["lead"]["consent_text"].strip()
        prompt = (
            f"Recent conversation:\n{_transcript(_history(state))}\n\n"
            f"Latest patient message:\n{state['question']}"
        )
        ex, _ = self.utility.chat_json(
            load_prompt("lead_extract"), [Message("user", prompt)], _LeadExtract
        )
        slots = dict(state.get("lead") or {})
        for k in ("need", "timeline", "budget"):
            if getattr(ex, k):
                slots[k] = getattr(ex, k)
        stage = state.get("lead_stage") or "collecting"
        # Personal data is kept only after the consent text has been shown (awaiting_consent)
        # AND the patient agreed. Details volunteered earlier are re-read from the conversation
        # once they say yes, so the recorded consent_text is what they actually saw.
        consented = stage == "awaiting_consent" and ex.consent == "yes"
        if consented:
            for k in ("name", "phone", "email"):
                if getattr(ex, k):
                    slots[k] = getattr(ex, k)

        parts: list[str] = []
        citations: list[str] = []
        if ex.question:  # help first
            r = self.answerer.answer(ex.question, [], query=ex.question)
            parts.append(r.answer)
            citations = r.citations

        if ex.consent == "no":
            for k in ("name", "phone", "email"):
                slots.pop(k, None)
            stage = "declined"
            parts.append(
                "No problem, I won't take any personal details. If you change your mind, you can "
                f"book at {c['booking_url']} or call reception on {c['reception']}."
            )
        elif not slots.get("need"):
            parts.append("Happy to help you get started. Which treatment are you interested in?")
        elif consented and slots.get("name") and slots.get("phone"):
            try:
                slots["lead_id"] = save_lead(
                    state.get("thread_id", ""),
                    name=slots["name"],
                    contact=", ".join(v for v in (slots["phone"], slots.get("email")) if v),
                    need=slots.get("need"),
                    timeline=slots.get("timeline"),
                    budget=slots.get("budget"),
                    consent_text=consent_text,
                    consented=True,
                )
                stage = "saved"
                parts.append(
                    f"Thank you, {slots['name']}. Our team will contact you about "
                    f"{slots['need']} using the details you shared. You can ask us to delete them "
                    "at any time."
                )
            except Exception:
                parts.append(
                    "Sorry, I couldn't save your details just now. Please book at "
                    f"{c['booking_url']} or call reception on {c['reception']}."
                )
        elif stage != "awaiting_consent":
            stage = "awaiting_consent"
            ask_when = (
                "" if slots.get("timeline") else " Also, roughly when would you like to come in?"
            )
            parts.append(consent_text + ask_when)
        else:  # agreed but something is missing, or consent still unclear
            missing = [k for k in ("name", "phone") if not slots.get(k)]
            if consented and missing:
                parts.append(
                    "Thanks! Could you also share your "
                    + " and ".join("phone number" if m == "phone" else m for m in missing)
                    + "?"
                )
            else:
                parts.append(
                    "Just to confirm: may our team contact you about this? If yes, please reply "
                    "with your name and phone number (an email address is optional)."
                )

        text = "\n\n".join(parts)
        return {
            "lead": slots,
            "lead_stage": stage,
            "reply": {"behavior": "answer", "answer": text, "citations": citations},
            "messages": [Turn(role="assistant", content=text)],
        }
