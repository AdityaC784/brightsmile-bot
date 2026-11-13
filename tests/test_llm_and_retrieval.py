import psycopg
import pytest
from pydantic import BaseModel

from bot.config import get_settings
from bot.llm import client as client_mod
from bot.llm.base import ChatRequest, ChatResponse, LLMError, Message
from bot.llm.cache import DiskCache, request_key
from bot.llm.client import LLM, _extract_json
from bot.llm.embeddings import l2_normalize
from bot.llm.providers import is_retryable
from bot.retrieval.hybrid import HybridRetriever, lexical_query

# --------------------------------------------------------------------------- LLM layer


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[ChatRequest] = []

    def complete(self, req: ChatRequest) -> ChatResponse:
        self.calls.append(req)
        return ChatResponse(text=self.replies.pop(0), provider="fake", model=req.model)

    def list_models(self) -> list[str]:
        return ["fake-1"]


@pytest.fixture
def fake_llm(monkeypatch, tmp_path):
    def make(replies, use_cache=False):
        fake = FakeProvider(replies)
        monkeypatch.setattr(client_mod, "_provider", lambda _name, _key=None: fake)
        llm = LLM("test", {"provider": "fake", "model": "fake-1", "max_tokens": 50}, use_cache)
        llm._cache = DiskCache(tmp_path, "chat")
        return llm, fake

    return make


class Verdict(BaseModel):
    ok: bool
    reason: str


def test_chat_json_parses_fenced_output(fake_llm):
    llm, _ = fake_llm(['```json\n{"ok": true, "reason": "fine"}\n```'])
    parsed, _ = llm.chat_json("judge", [Message("user", "x")], Verdict)
    assert parsed == Verdict(ok=True, reason="fine")


def test_chat_json_repairs_once_then_fails(fake_llm):
    llm, fake = fake_llm(['{"ok": "maybe"}', '{"ok": false, "reason": "fixed"}'])
    parsed, _ = llm.chat_json("judge", [Message("user", "x")], Verdict)
    assert parsed.reason == "fixed" and len(fake.calls) == 2

    llm, _ = fake_llm(["nope", "still nope"])
    with pytest.raises(LLMError):
        llm.chat_json("judge", [Message("user", "x")], Verdict)


def test_cache_returns_identical_response_without_calling_provider(fake_llm):
    llm, fake = fake_llm(["first", "second"], use_cache=True)
    a = llm.chat("sys", [Message("user", "same")])
    b = llm.chat("sys", [Message("user", "same")])
    assert (a.text, b.text, b.cached, len(fake.calls)) == ("first", "first", True, 1)
    c = llm.chat("sys", [Message("user", "different")])
    assert c.text == "second"


def test_request_key_is_order_independent():
    assert request_key({"a": 1, "b": 2}) == request_key({"b": 2, "a": 1})


def test_extract_json_tolerates_prose():
    assert _extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'


class _StatusErr(Exception):
    def __init__(self, status):
        self.status_code = status


def test_retry_policy():
    assert is_retryable(_StatusErr(429)) and is_retryable(_StatusErr(503))
    assert not is_retryable(_StatusErr(400)) and not is_retryable(_StatusErr(401))


def test_l2_normalize():
    v = l2_normalize([3.0, 4.0])
    assert v == pytest.approx([0.6, 0.8])


# --------------------------------------------------------------------------- retrieval


def test_lexical_query_ors_terms_and_keeps_identifiers():
    q = lexical_query("How much is SRV-005A or a molar RCT?")
    assert q == "How or much or is or SRV-005A or a or molar or RCT"


def _db_has_chunks() -> bool:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=2) as conn:
            return conn.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
    except Exception:
        return False


@pytest.mark.skipif(not _db_has_chunks(), reason="needs `docker compose up db` + ingest")
def test_lexical_search_finds_exact_identifier():
    """Integration: full-text side alone must surface the doc defining SRV-005A (no API call)."""
    hits = HybridRetriever().search("SRV-005A", mode="lexical", top_k=5)
    assert hits and hits[0].source_doc_id == "DENT-014"
    assert all(h.lexical_rank is not None and h.dense_rank is None for h in hits)


def test_daily_quota_errors_are_not_retried():
    from bot.llm.providers import is_daily_quota

    tpd = _StatusErr(429)
    tpd.args = ("Rate limit reached ... on tokens per day (TPD): Limit 200000",)
    assert is_daily_quota(tpd) and not is_retryable(tpd)
    tpm = _StatusErr(429)
    tpm.args = ("Rate limit reached ... on tokens per minute (TPM): Limit 8000",)
    assert not is_daily_quota(tpm) and is_retryable(tpm)


@pytest.mark.skipif(not _db_has_chunks(), reason="needs `docker compose up db` + ingest")
def test_reference_expansion_fetches_named_rows():
    """A retrieved package row names REF-03; expansion must add that refund-rule row once."""
    from bot.retrieval.hybrid import Hit

    r = HybridRetriever()
    pkg = Hit(
        0,
        "DENT-023#PKG-002",
        "DENT-023",
        "Packages",
        "PKG-002",
        "refund policy code: REF-03; REF-04",
        "active",
        0.03,
        1,
        1,
    )
    out = r.expand_references([pkg], limit=3)
    ids = [h.doc_id for h in out]
    assert ids[0] == "DENT-023#PKG-002"
    assert "DENT-024#REF-03" in ids and "DENT-024#REF-04" in ids
    assert len(ids) == len(set(ids))
    assert r.expand_references([pkg], limit=0) == [pkg]


@pytest.mark.skipif(not _db_has_chunks(), reason="needs `docker compose up db` + ingest")
def test_tariff_expansion_uses_row_chunks_not_prose():
    from bot.retrieval.hybrid import Hit

    r = HybridRetriever()
    pkg = Hit(
        0,
        "DENT-023#PKG-002",
        "DENT-023",
        "Packages",
        "PKG-002",
        "included service ids: 1x SRV-005A; 1x SRV-006A; 2x SRV-002",
        "active",
        0.03,
        1,
        1,
    )
    out = r.expand_tariff([pkg], "", limit=2)
    added = out[1:]
    assert added and all(h.source_doc_id == "DENT-019" for h in added)
    assert any("SRV-005A" in h.content and "6,800" in h.content for h in added)
    # Codes mentioned only in prose chunks don't trigger expansion.
    prose = Hit(
        1, "DENT-004#mapping", "DENT-004", "Team", "Map", "SRV-005A SRV-002", "active", 0.03, 1, 1
    )
    assert r.expand_tariff([prose], "", limit=2) == [prose]
