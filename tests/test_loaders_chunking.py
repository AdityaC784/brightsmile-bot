import json
import re
from functools import cache
from pathlib import Path

import pytest

from bot.config import ROOT, load_yaml
from bot.retrieval.chunking import chunk_document, chunk_fixed, chunk_heading, count_tokens
from bot.retrieval.loaders import Block, LoadedDoc, clean, load_csv, load_document, load_markdown

KB = ROOT / "data" / "kb"


@cache
def _manifest() -> dict:
    return json.loads((KB / "manifest.json").read_text(encoding="utf-8"))


@cache
def _all_docs() -> tuple[LoadedDoc, ...]:
    files = {p.name: p for p in KB.rglob("*") if p.is_file()}
    return tuple(load_document(files[d["filename"]], d) for d in _manifest()["documents"])


def _doc(doc_id: str) -> LoadedDoc:
    return next(d for d in _all_docs() if d.doc_id == doc_id)


# --------------------------------------------------------------------------- loaders (unit)


def test_clean_rejoins_wrapped_identifiers():
    assert clean("SRV-\n005A") == "SRV-005A"
    assert clean("Digital X-\n Ray") == "Digital X-Ray"
    assert clean("a  -\nb") == "a -\nb".replace("\n", " ")  # spaced dash is a real dash


def test_markdown_tables_render_as_self_contained_rows(tmp_path: Path):
    p = tmp_path / "t.md"
    p.write_text(
        "---\ntitle: T\n---\n# T\n## Fees\n| Code | Fee |\n| :--- | ---: |\n| `SRV-001` | **₹600** |\n",
        encoding="utf-8",
    )
    blocks = load_markdown(p)
    assert [b.kind for b in blocks] == ["heading", "heading", "table"]
    assert blocks[2].rows == ["Code: SRV-001; Fee: ₹600"]


def test_csv_handles_bom_and_one_block_per_row(tmp_path: Path):
    p = tmp_path / "t.csv"
    p.write_text("﻿item_id,price_inr\nA-1,100\nA-2,200\n", encoding="utf-8")
    blocks = load_csv(p)
    assert [b.key for b in blocks] == ["A-1", "A-2"]
    assert blocks[0].text == "item id: A-1; price inr: 100"


# --------------------------------------------------------------------------- loaders (real KB)


def test_every_kb_document_loads_with_content():
    for d in _all_docs():
        assert d.blocks, f"{d.doc_id} produced no blocks"


def test_no_broken_service_identifiers_anywhere():
    """PDF cell wrapping must not split exact IDs (the exact-identifier retrieval trap)."""
    broken = re.compile(r"\b(SRV|DENT|PKG|POL|REF|ESC|STF|EQP)- \d")
    for d in _all_docs():
        for b in d.blocks:
            for text in [b.text, *b.rows]:
                assert not broken.search(text), f"{d.doc_id}: {text[:120]}"


def test_pricing_tables_complete_and_metadata_table_skipped():
    for doc_id in ("DENT-019", "DENT-020"):
        rows = [r for b in _doc(doc_id).blocks if b.kind == "table" for r in b.rows]
        srv_rows = [r for r in rows if r.startswith("Service ID: SRV-")]
        assert len(srv_rows) == 18, doc_id  # manifest: 15 changed + 3 unchanged services
        assert not any(r.lower().startswith("metadata field") for r in rows)
    current = [r for b in _doc("DENT-019").blocks for r in b.rows]
    assert any("SRV-005A" in r and "₹6,800" in r for r in current)


# --------------------------------------------------------------------------- chunking


@pytest.mark.parametrize("strategy", ["heading", "fixed"])
def test_chunk_ids_unique_and_nonempty(strategy):
    cfg = load_yaml("retrieval")["chunking"]
    ids = []
    for d in _all_docs():
        chunks = chunk_document(d, strategy, cfg)
        assert chunks, f"{d.doc_id}: no {strategy} chunks"
        assert all(c.content.strip() for c in chunks)
        assert all(c.source_doc_id == d.doc_id for c in chunks)
        ids += [c.doc_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_heading_chunks_respect_max_tokens():
    cfg = load_yaml("retrieval")["chunking"]["heading"]
    for d in _all_docs():
        for c in chunk_heading(d, **cfg):
            # A single paragraph/row longer than the limit can't be split further.
            longest_line = max(count_tokens(x) for x in c.content.split("\n"))
            assert c.tokens <= cfg["max_tokens"] + longest_line, c.doc_id


def test_csv_documents_chunk_one_per_row():
    doc = _doc("DENT-023")
    chunks = chunk_heading(doc)
    assert [c.doc_id for c in chunks] == [f"DENT-023#PKG-00{i}" for i in range(1, 5)]


def test_heading_chunks_carry_section_path():
    doc = LoadedDoc(
        meta={"doc_id": "X-1", "title": "Doc Title"},
        path=Path("x.md"),
        blocks=[
            Block("heading", "Doc Title", level=1),
            Block("heading", "1. Fees", level=2),
            Block("heading", "1.1 Late fee", level=3),
            Block("text", "A late cancellation costs INR 300. " * 10),
        ],
    )
    (c,) = chunk_heading(doc, max_tokens=450, min_tokens=5)
    assert c.section == "1. Fees > 1.1 Late fee"
    assert c.doc_id == "X-1#1-1-late-fee"
    assert c.content.startswith("1.1 Late fee\n")


def test_fixed_chunks_overlap():
    doc = LoadedDoc(
        meta={"doc_id": "X-2", "title": "T"},
        path=Path("x.md"),
        blocks=[Block("text", " ".join(f"w{i}" for i in range(1000)))],
    )
    chunks = chunk_fixed(doc, size_tokens=100, overlap_tokens=20)
    assert len(chunks) > 1
    first, second = chunks[0].content.split(), chunks[1].content.split()
    assert set(first) & set(second), "consecutive windows should overlap"
    assert all(c.tokens <= 100 for c in chunks)
