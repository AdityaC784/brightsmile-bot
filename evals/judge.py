"""LLM judge (Gemini, temperature 0): grades a bot reply against the hand-written golden case.

The judge fills an item-by-item checklist (key facts, must_include, must_not_include,
contradictions); `correct` is then computed here in code, not taken from the model. Validation
against hand labels showed a holistic "correct" flag was too lenient (kappa 0.36): it passed
replies with a missing required fact or a contradicted rule.

One call per case keeps Gemini free-tier usage to one request per case; responses are cached by
prompt hash, so re-running an unchanged case costs nothing and gives the identical grade.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, computed_field

from bot.llm.base import Message
from bot.llm.client import LLM, get_llm
from bot.prompts import clinic_prompt
from bot.rag.answer import AnswerResult
from evals.golden_set import GoldenCase


class KeyFact(BaseModel):
    fact: str
    present: bool


class IncludeCheck(BaseModel):
    item: str
    satisfied: bool


class ExcludeCheck(BaseModel):
    item: str
    violated: bool


class Verdict(BaseModel):
    key_facts: list[KeyFact] = []
    must_include: list[IncludeCheck] = []
    must_not_include: list[ExcludeCheck] = []
    contradictions: list[str] = []
    behavior_ok: bool
    faithful: bool
    reason: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def missing(self) -> list[str]:
        return [k.fact for k in self.key_facts if not k.present] + [
            m.item for m in self.must_include if not m.satisfied
        ]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def violations(self) -> list[str]:
        return [m.item for m in self.must_not_include if m.violated] + self.contradictions

    @computed_field  # type: ignore[prop-decorator]
    @property
    def correct(self) -> bool:
        return self.behavior_ok and not self.missing and not self.violations


def build_judge_input(case: GoldenCase, result: AnswerResult) -> str:
    # The bot's own behaviour label is deliberately NOT shown: the judge must grade the reply
    # text, not compare labels (an early run failed a correct refusal labelled "abstain").
    sources = "\n".join(
        f'<source id="S{i}" doc="{h.source_doc_id}" status="{h.status}">\n{h.content}\n</source>'
        for i, h in enumerate(result.hits, 1)
    )
    history = "\n".join(f"{t.role}: {t.content}" for t in case.history) or "(none)"
    payload = {
        "expected_behavior": case.expected_behavior,
        "must_include": case.must_include,
        "must_not_include": case.must_not_include,
    }
    return (
        f"## Earlier conversation\n{history}\n\n"
        f"## Question\n{case.question}\n\n"
        f"## Grading inputs\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"## reference_answer\n{case.golden_answer}\n\n"
        f"## sources (what the bot was given)\n{sources}\n\n"
        f"## bot_reply\n{result.answer}"
    )


def judge(case: GoldenCase, result: AnswerResult, llm: LLM | None = None) -> Verdict:
    llm = llm or get_llm("judge")
    verdict, _ = llm.chat_json(
        clinic_prompt("judge"), [Message("user", build_judge_input(case, result))], Verdict
    )
    # The model must return one check per given item (in order, possibly reworded). Only items
    # beyond what it returned count as skipped -> failed. Matching by text was wrong: a reworded
    # item got re-added as unsatisfied and failed correct replies.
    for item in case.must_include[len(verdict.must_include) :]:
        verdict.must_include.append(IncludeCheck(item=item, satisfied=False))
    return verdict
