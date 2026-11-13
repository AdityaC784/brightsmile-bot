"""Answer one question: retrieve -> (abstain on weak retrieval) -> generate -> verify -> respond.

    from bot.rag.answer import Answerer
    result = Answerer().answer("How much is a molar root canal?")

This is the support path the LangGraph graph will call; it has no conversation state of its own
(history is passed in).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, field_validator

from bot.config import load_yaml
from bot.llm.base import Message
from bot.llm.client import LLM, get_llm
from bot.prompts import render_prompt
from bot.rag.verify import CITE, Verification, verify_answer
from bot.retrieval.hybrid import Hit, HybridRetriever

Behavior = Literal["answer", "abstain", "clarify", "refuse", "handoff"]
# Behaviours whose reply must be grounded in sources (refusals need no citations).
GROUNDED = {"answer", "clarify", "handoff"}


class GeneratorOutput(BaseModel):
    behavior: Behavior
    answer: str
    citations: list[str] = []

    @field_validator("behavior", mode="before")
    @classmethod
    def _lower(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v


@dataclass
class AnswerResult:
    question: str
    behavior: Behavior
    answer: str  # what the patient sees; [S#] markers replaced by [DENT-xxx]
    raw_answer: str  # model output with [S#] markers
    citations: list[str]  # cited chunk ids (doc_id anchors)
    hits: list[Hit]
    verification: Verification | None
    fallback_reason: str | None = None  # set when the reply was replaced by the fallback
    latency_ms: int = 0
    cached: bool = False  # generator reply came from the LLM cache (latency not meaningful)
    tokens_in: int = 0
    tokens_out: int = 0
    timings: dict[str, int] = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return self.behavior in ("abstain", "refuse")

    @property
    def confidence(self) -> float | None:
        """Share of cited claims that passed verification (None if nothing was verified)."""
        return self.verification.support_ratio if self.verification else None


def format_sources(hits: list[Hit]) -> tuple[str, dict[str, str]]:
    """Number sources S1..Sn; return the prompt block and an S-id -> text map for verification."""
    parts, texts = [], {}
    for i, h in enumerate(hits, 1):
        sid = f"S{i}"
        status = "ARCHIVED" if h.status == "archived" else "active"
        texts[sid] = h.content
        parts.append(
            f'<source id="{sid}" doc="{h.source_doc_id}" status="{status}" '
            f'title="{h.title}" section="{h.section}">\n{h.content}\n</source>'
        )
    return "<sources>\n" + "\n".join(parts) + "\n</sources>", texts


def _history_block(history: list[Message]) -> str:
    if not history:
        return ""
    lines = [f"{m.role}: {m.content}" for m in history[-6:]]
    return "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\n"


class Answerer:
    def __init__(
        self,
        retriever: HybridRetriever | None = None,
        generator: LLM | None = None,
        verifier: LLM | None = None,
        use_cache: bool | None = None,
    ):
        self.retriever = retriever or HybridRetriever()
        self._generator = generator
        self._verifier = verifier
        self.use_cache = use_cache
        self.cfg = load_yaml("generation")
        self.search_cfg = load_yaml("retrieval")

    @cached_property
    def generator(self) -> LLM:
        return self._generator or get_llm("generator", use_cache=self.use_cache)

    @cached_property
    def verifier(self) -> LLM:
        return self._verifier or get_llm("utility", use_cache=self.use_cache)

    @cached_property
    def system_prompt(self) -> str:
        c = self.cfg["clinic"]
        return render_prompt(
            "answer", clinic_name=c["name"], reception=c["reception"], booking_url=c["booking_url"]
        )

    def _fallback(self, result: AnswerResult, reason: str) -> AnswerResult:
        if result.behavior == "handoff":
            # An emergency stays an emergency: give the fixed, KB-sourced routing text.
            result.answer = self.cfg["emergency_fallback_answer"].strip()
        else:
            result.behavior = "abstain"
            result.answer = self.cfg["fallback_answer"].strip()
        result.citations = []
        result.fallback_reason = reason
        return result

    def answer(
        self, question: str, history: list[Message] | None = None, *, query: str | None = None
    ) -> AnswerResult:
        """`query` overrides the retrieval query (e.g. a rewritten standalone question)."""
        t0 = time.perf_counter()
        timings: dict[str, int] = {}

        hits = self.retriever.search(query or question)
        timings["retrieve_ms"] = int((time.perf_counter() - t0) * 1000)

        sources_block, source_texts = format_sources(hits)
        user_msg = f"{sources_block}\n\n{_history_block(history or [])}Patient question: {question}"
        t1 = time.perf_counter()
        out, resp = self.generator.chat_json(
            self.system_prompt, [Message("user", user_msg)], GeneratorOutput
        )
        timings["generate_ms"] = int((time.perf_counter() - t1) * 1000)

        sid_to_doc = {f"S{i}": h.doc_id for i, h in enumerate(hits, 1)}
        sid_to_src = {f"S{i}": h.source_doc_id for i, h in enumerate(hits, 1)}
        cited = list(dict.fromkeys(CITE.findall(out.answer) + out.citations))
        result = AnswerResult(
            question=question,
            behavior=out.behavior,
            answer=CITE.sub(lambda m: f"[{sid_to_src.get(m.group(1), m.group(1))}]", out.answer),
            raw_answer=out.answer,
            citations=[sid_to_doc[s] for s in cited if s in sid_to_doc],
            hits=hits,
            verification=None,
            cached=resp.cached,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            timings=timings,
        )

        min_score = self.search_cfg["abstention"]["min_top_score"]
        top = hits[0].score if hits else 0.0
        if out.behavior in GROUNDED and top < min_score:
            self._fallback(result, f"top retrieval score {top:.4f} < {min_score}")

        vcfg = self.cfg["verification"]
        if vcfg["enabled"] and result.fallback_reason is None and out.behavior in GROUNDED:
            t2 = time.perf_counter()
            v = verify_answer(out.answer, source_texts, llm=self.verifier)
            timings["verify_ms"] = int((time.perf_counter() - t2) * 1000)
            result.verification = v
            if v.n_claims == 0:
                self._fallback(result, "grounded reply has no citations")
            elif (v.support_ratio or 0.0) < vcfg["min_support_ratio"]:
                bad = [c.reason for c in v.claims if not c.supported]
                self._fallback(result, f"unsupported claims: {bad}")

        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        return result
