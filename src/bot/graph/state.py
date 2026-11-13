"""Conversation state carried (and checkpointed) by the LangGraph graph."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

Intent = Literal["support", "lead", "handoff", "greeting", "out_of_scope"]


class Turn(TypedDict):
    role: Literal["user", "assistant", "human_agent"]
    content: str


class Reply(TypedDict, total=False):
    behavior: str  # answer | abstain | clarify | refuse | handoff
    answer: str
    citations: list[str]
    confidence: float | None
    fallback_reason: str | None
    repaired: bool


LeadStage = Literal["collecting", "awaiting_consent", "saved", "declined"]


class Lead(TypedDict, total=False):
    need: str
    timeline: str
    budget: str
    # name/contact are only ever stored here after the patient has consented.
    name: str
    contact: str
    lead_id: str


class ChatState(TypedDict, total=False):
    thread_id: str
    # Full transcript; nodes return only the new turns and the reducer appends them.
    messages: Annotated[list[Turn], operator.add]
    question: str  # the latest user message
    standalone: str  # rewritten, self-contained version used for retrieval
    intent: Intent
    reply: Reply  # the assistant's reply for this turn (with grounding metadata)
    # Persist across turns (not reset by new_turn):
    lead: Lead
    lead_stage: LeadStage | None
    handoff_pending: bool
