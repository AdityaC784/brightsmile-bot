"""LLM judge (Gemini, temperature 0): grades a bot reply against the hand-written golden case.

One call per case returns behaviour, correctness and faithfulness, keeping Gemini free-tier
usage to one request per case. Responses are cached by prompt hash, so re-running an unchanged
case costs nothing and gives the identical grade.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from bot.llm.base import Message
from bot.llm.client import LLM, get_llm
from bot.prompts import load_prompt
from bot.rag.answer import AnswerResult
from evals.golden_set import GoldenCase


class Verdict(BaseModel):
    behavior_ok: bool
    correct: bool
    faithful: bool
    missing: list[str] = []
    violations: list[str] = []
    reason: str = ""


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
        load_prompt("judge"), [Message("user", build_judge_input(case, result))], Verdict
    )
    return verdict
