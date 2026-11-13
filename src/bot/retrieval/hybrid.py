"""Hybrid retrieval: Postgres full-text + pgvector HNSW, fused with Reciprocal Rank Fusion.

Modes (configs/retrieval.yaml -> search.mode, or per call):
  hybrid  — weighted RRF over both lists (search.lexical_weight); FULL OUTER JOIN keeps
            hits found by only one side
  lexical — full-text only (ts_rank_cd; NOT true BM25)
  dense   — vector only (cosine distance)

Archived chunks (e.g. DENT-020, 2025 prices) stay retrievable but their score is multiplied by
`archived_penalty`, so current documents win ties and the generator can still explain old prices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import psycopg
from pgvector.psycopg import register_vector

from bot.config import get_settings, load_yaml
from bot.llm.embeddings import Embedder

Mode = Literal["hybrid", "lexical", "dense"]


@dataclass
class Hit:
    id: int
    doc_id: str
    source_doc_id: str
    title: str
    section: str
    content: str
    status: str
    score: float
    lexical_rank: int | None
    dense_rank: int | None


def lexical_query(text: str) -> str:
    """Turn a natural-language question into an OR query for websearch_to_tsquery.

    websearch_to_tsquery ANDs bare words, which makes conversational questions match almost
    nothing. OR-ing the terms lets ts_rank_cd reward chunks that cover more of them, while a
    hyphenated identifier like SRV-005A stays one term (parsed as an exact phrase).
    """
    terms = re.findall(r"[\w₹]+(?:[-/][\w]+)*", text)
    return " or ".join(t for t in terms if t.lower() != "or")


_SQL = """
WITH lexical AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(fts, q) DESC, id) AS rank
    FROM chunks, websearch_to_tsquery('english', %(lex)s) AS q
    WHERE %(use_lex)s AND tenant_id = %(tenant)s AND chunker = %(chunker)s AND fts @@ q
    ORDER BY ts_rank_cd(fts, q) DESC, id
    LIMIT %(n)s
),
semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(vec)s::vector, id) AS rank
    FROM chunks
    WHERE %(use_dense)s AND tenant_id = %(tenant)s AND chunker = %(chunker)s
    ORDER BY embedding <=> %(vec)s::vector, id
    LIMIT %(n)s
)
SELECT c.id, c.doc_id, c.source_doc_id, c.title, c.section, c.content, c.status,
       (%(w_lex)s * coalesce(1.0 / (%(k)s + l.rank), 0.0) + coalesce(1.0 / (%(k)s + s.rank), 0.0))
         * CASE WHEN c.status = 'archived' THEN %(penalty)s ELSE 1.0 END AS score,
       l.rank AS lexical_rank, s.rank AS dense_rank
FROM lexical l
FULL OUTER JOIN semantic s USING (id)
JOIN chunks c ON c.id = coalesce(l.id, s.id)
ORDER BY score DESC, c.id
LIMIT %(top_k)s
"""


class HybridRetriever:
    def __init__(self, database_url: str | None = None, embedder: Embedder | None = None):
        self.cfg = load_yaml("retrieval")
        self.database_url = database_url or get_settings().database_url
        self._embedder = embedder

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder()
        return self._embedder

    def search(
        self,
        query: str,
        *,
        mode: Mode | None = None,
        top_k: int | None = None,
        chunker: str | None = None,
        lexical_weight: float | None = None,
    ) -> list[Hit]:
        s = self.cfg["search"]
        mode = mode or s["mode"]
        use_lex = mode in ("hybrid", "lexical")
        use_dense = mode in ("hybrid", "dense")
        dims = load_yaml("models")["embeddings"]["dimensions"]
        vec = self.embedder.embed_query(query) if use_dense else [0.0] * dims

        params = {
            "lex": lexical_query(query) if use_lex else "",
            "vec": vec,
            "use_lex": use_lex,
            "use_dense": use_dense,
            "tenant": get_settings().tenant_id,
            "chunker": chunker or self.cfg["chunking"]["strategy"],
            "n": s["candidates_per_list"],
            "k": s["rrf_k"],
            # Weighted RRF: the full-text list counts this much relative to the dense list.
            "w_lex": s.get("lexical_weight", 1.0) if lexical_weight is None else lexical_weight,
            "penalty": s["archived_penalty"],
            "top_k": top_k or s["top_k"],
        }
        with psycopg.connect(self.database_url) as conn:
            register_vector(conn)
            with conn.transaction():
                # Filtered HNSW scans can return fewer rows than LIMIT; iterative scan
                # (pgvector >= 0.8) keeps searching until enough rows pass the filter.
                conn.execute("SET LOCAL hnsw.ef_search = 100")
                conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                rows = conn.execute(_SQL, params).fetchall()
        return [Hit(*r[:7], float(r[7]), r[8], r[9]) for r in rows]
