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
    repaired: bool = False  # the generator got a second attempt after failing verification
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

    def _fallback(
        self, result: AnswerResult, reason: str, *, emergency: bool = False
    ) -> AnswerResult:
        if emergency or result.behavior == "handoff":
            # An emergency stays an emergency: give the fixed, KB-sourced routing text.
            result.answer = self.cfg["emergency_fallback_answer"].strip()
        else:
            result.behavior = "abstain"
            result.answer = self.cfg["fallback_answer"].strip()
        result.citations = []
        result.fallback_reason = reason
        return result

    def _apply_output(self, result: AnswerResult, out: GeneratorOutput) -> None:
        """Copy a generator output into the result, mapping [S#] markers to documents."""
        sid_to_doc = {f"S{i}": h.doc_id for i, h in enumerate(result.hits, 1)}
        sid_to_src = {f"S{i}": h.source_doc_id for i, h in enumerate(result.hits, 1)}
        cited = list(dict.fromkeys(CITE.findall(out.answer) + out.citations))
        result.behavior = out.behavior
        result.raw_answer = out.answer
        result.answer = CITE.sub(
            lambda m: f"[{sid_to_src.get(m.group(1), m.group(1))}]", out.answer
        )
        result.citations = [sid_to_doc[s] for s in cited if s in sid_to_doc]

    def _verify(
        self, out: GeneratorOutput, sources: dict[str, str]
    ) -> tuple[Verification, str | None]:
        """Verify a grounded reply; return the verification and a failure reason (None = ok)."""
        v = verify_answer(out.answer, sources, llm=self.verifier)
        if v.n_claims == 0:
            return v, "grounded reply has no citations"
        if (v.support_ratio or 0.0) < self.cfg["verification"]["min_support_ratio"]:
            return v, f"unsupported claims: {[c.reason for c in v.claims if not c.supported]}"
        return v, None

    @staticmethod
    def _repair_feedback(v: Verification) -> str:
        if v.n_claims == 0:
            problems = "- Your reply has no [S#] citations. Every fact must cite its source."
        else:
            problems = "\n".join(f'- "{c.text}" -> {c.reason}' for c in v.claims if not c.supported)
        return (
            "A fact-checker compared your reply with the sources and found these problems:\n"
            f"{problems}\n\n"
            "Rewrite your reply to fix them: cite the source that actually contains each fact, "
            "restore any condition exactly as the source states it, and remove anything the "
            "sources do not support. If the sources cannot answer the question, abstain. "
            "Return the same JSON format."
        )

    def answer(
        self, question: str, history: list[Message] | None = None, *, query: str | None = None
    ) -> AnswerResult:
        """`query` overrides the retrieval query (e.g. a rewritten standalone question)."""
        t0 = time.perf_counter()
        timings: dict[str, int] = {}

        hits = self.retriever.search(query or question)
        expand = getattr(self.retriever, "expand_references", None)
        if expand is not None:
            hits = expand(hits)
        timings["retrieve_ms"] = int((time.perf_counter() - t0) * 1000)

        sources_block, source_texts = format_sources(hits)
        user_msg = f"{sources_block}\n\n{_history_block(history or [])}Patient question: {question}"
        messages = [Message("user", user_msg)]
        t1 = time.perf_counter()
        out, resp = self.generator.chat_json(self.system_prompt, messages, GeneratorOutput)
        timings["generate_ms"] = int((time.perf_counter() - t1) * 1000)

        result = AnswerResult(
            question=question,
            behavior=out.behavior,
            answer="",
            raw_answer="",
            citations=[],
            hits=hits,
            verification=None,
            cached=resp.cached,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            timings=timings,
        )
        self._apply_output(result, out)

        min_score = self.search_cfg["abstention"]["min_top_score"]
        top = hits[0].score if hits else 0.0
        if out.behavior in GROUNDED and top < min_score:
            self._fallback(result, f"top retrieval score {top:.4f} < {min_score}")

        vcfg = self.cfg["verification"]
        # An emergency stays an emergency: if any attempt was a handoff, a failed reply falls back
        # to the fixed emergency text, never to "call reception during office hours".
        emergency = out.behavior == "handoff"
        if vcfg["enabled"] and result.fallback_reason is None and out.behavior in GROUNDED:
            t2 = time.perf_counter()
            v, failure = self._verify(out, source_texts)
            # One repair attempt (configurable): show the generator exactly what failed.
            for _ in range(vcfg.get("repair_attempts", 0)):
                if failure is None:
                    break
                messages = [
                    *messages,
                    Message("assistant", out.model_dump_json()),
                    Message("user", self._repair_feedback(v)),
                ]
                out, resp = self.generator.chat_json(self.system_prompt, messages, GeneratorOutput)
                result.tokens_in += resp.tokens_in
                result.tokens_out += resp.tokens_out
                result.repaired = True
                self._apply_output(result, out)
                emergency = emergency or out.behavior == "handoff"
                if out.behavior not in GROUNDED:  # it chose to abstain/refuse: nothing to verify
                    v, failure = None, None
                    if emergency:  # never let a repair downgrade an emergency to an abstention
                        failure = "repair dropped the emergency handoff"
                    break
                v, failure = self._verify(out, source_texts)
            timings["verify_ms"] = int((time.perf_counter() - t2) * 1000)
            result.verification = v
            if failure:
                self._fallback(result, failure, emergency=emergency)
                if emergency:
                    result.behavior = "handoff"

        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        return result
