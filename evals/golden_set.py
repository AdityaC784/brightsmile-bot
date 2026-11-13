"""Load and validate the golden set (evals/golden/v1.jsonl)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from bot.config import ROOT

GOLDEN_PATH = ROOT / "evals" / "golden" / "v1.jsonl"

CaseType = Literal[
    "lookup", "exact_id", "multi_source", "followup", "unanswerable", "ambiguous", "safety"
]
Behavior = Literal["answer", "abstain", "clarify", "refuse", "handoff"]


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class GoldenCase(BaseModel):
    id: str
    dataset_version: str
    split: Literal["dev", "holdout"]
    type: CaseType
    question: str
    gold_doc_ids: list[str]
    expected_behavior: Behavior
    must_abstain: bool
    golden_answer: str
    # Key facts/behaviours the judge checks individually (optional; set on abstain/refuse/clarify).
    must_include: list[str] = Field(default_factory=list)
    # Anything here makes the reply wrong (e.g. a ₹ price for a service the clinic doesn't list).
    must_not_include: list[str] = Field(default_factory=list)
    notes: str = ""
    history: list[Turn] = Field(default_factory=list)
    standalone_question: str | None = None

    @field_validator("gold_doc_ids")
    @classmethod
    def _doc_ids(cls, v: list[str]) -> list[str]:
        for d in v:
            if not re.fullmatch(r"DENT-\d{3}", d):
                raise ValueError(f"gold_doc_ids must be document-level DENT-xxx, got {d!r}")
        return v

    @property
    def retrieval_query(self) -> str:
        """Query used for retrieval-only evals: the oracle standalone rewrite when present."""
        return self.standalone_question or self.question


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenCase]:
    cases = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(GoldenCase.model_validate(json.loads(line)))
            except Exception as e:
                raise ValueError(f"{path.name}:{n}: {e}") from e
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in golden set")
    return cases
