"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, max_length=100, pattern=r"^[\w.:-]+$")


class ChatOut(BaseModel):
    thread_id: str
    message_id: int | None
    intent: str | None
    behavior: str
    answer: str
    citations: list[str] = []
    confidence: float | None = None
    handoff_pending: bool = False


class HumanReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class FeedbackIn(BaseModel):
    thread_id: str = Field(max_length=100)
    message_id: int | None = None
    rating: Literal[1, -1] = Field(description="1 = thumbs up, -1 = thumbs down")
    comment: str | None = Field(default=None, max_length=2000)
