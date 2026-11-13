"""Offline tests for the answer pipeline: fake retriever + fake LLMs, no network."""

import json

import pytest

from bot.llm.base import ChatResponse
from bot.prompts import render_prompt
from bot.rag.answer import Answerer, format_sources
from bot.rag.verify import facts_in_text, split_claims, verify_answer
from bot.retrieval.hybrid import Hit

TARIFF = "Service ID: SRV-003; Routine Ultrasonic Scaling & Polishing; 2026 Fee (INR): ₹1,800"
ARCHIVED = "Service ID: SRV-003; Routine Ultrasonic Scaling & Polishing; 2025 Fee (INR): ₹1,500"


def _hit(i: int, content: str, status: str = "active", doc: str = "DENT-019") -> Hit:
    return Hit(i, f"{doc}#sec{i}", doc, "Fee Schedule", "Tariff", content, status, 0.03, 1, 1)


class FakeRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.queries: list[str] = []

    def search(self, query, **_):
        self.queries.append(query)
        return self.hits


class ScriptedLLM:
    """Stands in for bot.llm.client.LLM: returns scripted JSON replies in order."""

    def __init__(self, *replies: dict):
        self.replies = list(replies)
        self.calls: list[tuple[str, list]] = []

    def chat_json(self, system, messages, schema, **_):
        self.calls.append((system, messages))
        reply = self.replies.pop(0)
        return schema.model_validate(reply), ChatResponse(json.dumps(reply), "fake", "fake")


def _answerer(hits, generator, verifier=None):
    return Answerer(
        retriever=FakeRetriever(hits), generator=generator, verifier=verifier or ScriptedLLM()
    )


# --------------------------------------------------------------------------- verification


def test_split_claims_separates_cited_and_uncited():
    claims, uncited = split_claims(
        "Scaling costs ₹1,800 [S1]. Happy to help! A crown is ₹9,500 per tooth."
    )
    assert [c.source_ids for c in claims] == [["S1"]]
    assert uncited == ["A crown is ₹9,500 per tooth."]


def test_deterministic_check_catches_invented_price_and_code():
    assert facts_in_text("Scaling is ₹1,800 [S1].", TARIFF) == (True, "")
    ok, why = facts_in_text("Scaling is ₹1,600 [S1].", TARIFF)
    assert not ok and "1,600" in why
    ok, why = facts_in_text("Use code SRV-099 [S1].", TARIFF)
    assert not ok and "SRV-099" in why


def test_verify_marks_unknown_source_without_llm_call():
    llm = ScriptedLLM()
    v = verify_answer("Scaling is ₹1,800 [S9].", {"S1": TARIFF}, llm=llm)
    assert v.n_supported == 0 and "unknown source" in v.claims[0].reason
    assert llm.calls == []


def test_verify_uses_llm_for_claims_passing_deterministic_check():
    llm = ScriptedLLM({"results": [{"claim": 1, "supported": True, "reason": "listed"}]})
    v = verify_answer("Scaling is ₹1,800 [S1].", {"S1": TARIFF}, llm=llm)
    assert v.support_ratio == 1.0 and v.llm_checked


# --------------------------------------------------------------------------- pipeline


def test_answer_maps_citations_to_documents_and_verifies():
    gen = ScriptedLLM(
        {"behavior": "answer", "answer": "Scaling costs ₹1,800 [S1].", "citations": ["S1"]}
    )
    ver = ScriptedLLM({"results": [{"claim": 1, "supported": True}]})
    r = _answerer([_hit(1, TARIFF)], gen, ver).answer("price of scaling?")
    assert r.behavior == "answer"
    assert r.answer == "Scaling costs ₹1,800 [DENT-019]."
    assert r.citations == ["DENT-019#sec1"]
    assert r.confidence == 1.0 and r.fallback_reason is None


def test_invented_price_triggers_fallback():
    gen = ScriptedLLM(
        {"behavior": "answer", "answer": "Scaling costs ₹999 [S1].", "citations": ["S1"]}
    )
    r = _answerer([_hit(1, TARIFF)], gen).answer("price of scaling?")
    assert r.behavior == "abstain" and r.citations == []
    assert "+91-20-5550-0140" in r.answer and "999" not in r.answer
    assert "999" in r.fallback_reason


def test_uncited_grounded_reply_falls_back():
    gen = ScriptedLLM({"behavior": "answer", "answer": "Scaling costs ₹1,800.", "citations": []})
    r = _answerer([_hit(1, TARIFF)], gen).answer("price of scaling?")
    assert r.behavior == "abstain" and r.fallback_reason == "grounded reply has no citations"


def test_failed_emergency_reply_keeps_emergency_routing():
    gen = ScriptedLLM(
        {"behavior": "handoff", "answer": "Go to Some Other Hospital [S1].", "citations": ["S1"]}
    )
    ver = ScriptedLLM(
        {"results": [{"claim": 1, "supported": False, "reason": "hospital not in source"}]}
    )
    r = _answerer(
        [_hit(1, "ESC-01: Jupiter Hospital Baner or Manipal Hospital Baner")], gen, ver
    ).answer("face swelling and fever")
    assert r.behavior == "handoff"
    assert "112" in r.answer and "Manipal Hospital" in r.answer


def test_refusal_needs_no_citations_or_verification():
    gen = ScriptedLLM(
        {"behavior": "refuse", "answer": "I can only help with BrightSmile questions."}
    )
    ver = ScriptedLLM()
    r = _answerer([_hit(1, TARIFF)], gen, ver).answer("write me a scraper")
    assert r.behavior == "refuse" and r.verification is None and ver.calls == []


def test_archived_sources_are_flagged_and_query_override_used():
    hits = [_hit(1, TARIFF), _hit(2, ARCHIVED, status="archived", doc="DENT-020")]
    block, texts = format_sources(hits)
    assert 'status="ARCHIVED"' in block and set(texts) == {"S1", "S2"}

    gen = ScriptedLLM({"behavior": "refuse", "answer": "No."})
    answerer = _answerer(hits, gen)
    answerer.answer("and for kids?", query="price of a kids consultation")
    assert answerer.retriever.queries == ["price of a kids consultation"]


def test_answer_prompt_renders_clinic_details():
    p = render_prompt("answer", clinic_name="X Clinic", reception="+1", booking_url="https://b")
    assert "X Clinic" in p and "{" + "reception}" not in p
    with pytest.raises(KeyError):
        render_prompt("answer", not_a_placeholder="x")
