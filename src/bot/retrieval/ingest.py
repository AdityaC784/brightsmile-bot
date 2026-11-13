"""Load -> chunk (both strategies) -> embed -> replace the tenant's chunks in Postgres.

    python -m bot.retrieval.ingest

Both chunkers are always indexed (rows differ by the `chunker` column) so the chunking
experiment can switch strategies without re-ingesting. Re-running is idempotent and, thanks to
the embedding cache, free when the KB hasn't changed.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
import structlog
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from bot.config import ROOT, get_settings, load_yaml
from bot.llm.embeddings import Embedder
from bot.retrieval.chunking import Chunk, chunk_document
from bot.retrieval.loaders import load_document

log = structlog.get_logger(__name__)
STRATEGIES = ("heading", "fixed")


@dataclass
class IngestResult:
    kb_version: str
    documents: int
    chunks: dict[str, int]
    seconds: float


def embedding_text(c: Chunk, status: str) -> str:
    """Text that gets embedded: title + section give the vector its context."""
    flag = "[ARCHIVED — superseded, not current]\n" if status == "archived" else ""
    return f"{flag}{c.title}\n{c.section}\n{c.content}".strip()


def kb_version(kb_dir: Path, manifest: dict, files: dict[str, Path]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(manifest, sort_keys=True).encode())
    for name in sorted(files):
        h.update(name.encode())
        h.update(files[name].read_bytes())
    h.update(json.dumps(load_yaml("retrieval")["chunking"], sort_keys=True).encode())
    h.update(json.dumps(load_yaml("models")["embeddings"], sort_keys=True).encode())
    return h.hexdigest()[:12]


def build_chunks() -> tuple[str, dict, list[tuple[Chunk, dict]]]:
    rcfg = load_yaml("retrieval")
    kb_dir = ROOT / rcfg["kb_dir"]
    manifest = json.loads((ROOT / rcfg["manifest"]).read_text(encoding="utf-8"))
    files = {p.name: p for p in kb_dir.rglob("*") if p.is_file() and p.name != "manifest.json"}

    out: list[tuple[Chunk, dict]] = []
    for meta in manifest["documents"]:
        path = files.get(meta["filename"])
        if path is None:
            raise FileNotFoundError(f"{meta['doc_id']}: {meta['filename']} listed but missing")
        doc = load_document(path, meta)
        for strategy in STRATEGIES:
            for c in chunk_document(doc, strategy, rcfg["chunking"]):
                out.append((c, meta))
    return kb_version(kb_dir, manifest, files), manifest, out


def ingest(database_url: str | None = None) -> IngestResult:
    t0 = time.perf_counter()
    settings = get_settings()
    version, manifest, items = build_chunks()
    log.info("ingest.chunked", kb_version=version, chunks=len(items))

    vectors = Embedder().embed([embedding_text(c, m["status"]) for c, m in items])

    with psycopg.connect(database_url or settings.database_url) as conn:
        register_vector(conn)
        with conn.transaction():
            conn.execute("DELETE FROM chunks WHERE tenant_id = %s", (settings.tenant_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO chunks (tenant_id, doc_id, source_doc_id, title, section, content,
                           category, status, chunker, metadata, content_hash, embedding, kb_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (
                            settings.tenant_id,
                            c.doc_id,
                            c.source_doc_id,
                            c.title,
                            c.section,
                            c.content,
                            m["category"],
                            m["status"],
                            c.chunker,
                            Jsonb(c.metadata),
                            hashlib.sha256(c.content.encode()).hexdigest(),
                            v,
                            version,
                        )
                        for (c, m), v in zip(items, vectors, strict=True)
                    ],
                )
        conn.execute("ANALYZE chunks")

    counts = {s: sum(1 for c, _ in items if c.chunker == s) for s in STRATEGIES}
    return IngestResult(version, len(manifest["documents"]), counts, time.perf_counter() - t0)


if __name__ == "__main__":
    r = ingest()
    print(
        f"Ingested {r.documents} documents -> {r.chunks} chunks "
        f"(kb_version={r.kb_version}) in {r.seconds:.1f}s"
    )
