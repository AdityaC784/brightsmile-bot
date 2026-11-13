# BrightSmile Dental Care — Support & Lead-Gen Chatbot with an Eval Gate

A website chatbot for a (fictional) dental clinic in Pune that answers patient questions from a
36-document knowledge base **with citations**, abstains when the KB doesn't say, captures leads
**only with consent**, hands off to a human when needed — and is protected by a **CI eval gate**
that blocks any PR that makes answers worse.

> Status: Day 0 — scaffolding. See [`docs/PLAN.md`](docs/PLAN.md) for the full plan.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI + SSE streaming |
| Orchestration | LangGraph (Postgres checkpointer, human-in-the-loop interrupt) |
| Storage | Postgres 16 + pgvector (chunks, leads, feedback, eval results, graph state) |
| Retrieval | Hybrid: Postgres full-text + pgvector HNSW, merged with Reciprocal Rank Fusion |
| Generator | Qwen 3.8 27B on Groq |
| Utility (classify / rewrite / slots) | GPT-OSS 20B on Groq |
| Judge | Gemini (different family than the generator, temperature 0) |
| Embeddings | Gemini embeddings (free tier), 768-dim |

All model IDs live in [`configs/models.yaml`](configs/models.yaml).

> **Note:** Postgres full-text search (`ts_rank_cd`) is **not** true BM25 — it has no IDF
> saturation or document-length normalisation in the BM25 sense. It's the lexical half of the hybrid.

## Knowledge base

`data/kb/` — 36 documents in mixed formats (11 PDF, 10 DOCX, 10 Markdown, 5 CSV) plus
`manifest.json`. Built-in retrieval traps:

- Exact identifiers: `SRV-005A`, `POL-CAN-01`, `PKG-002`, `FRM-CON-01`, `ESC-01`…
- Superseded pricing: `DENT-020` (2025, archived) next to `DENT-019` (2026, current)
- Multi-source facts (e.g. package price + refund rule)
- Tables (hours, tariffs, packages)
- Deliberately unanswerable questions (see `DENT-036` §2)

## Quick start

```bash
cp .env.example .env          # fill in API keys
uv sync                       # install deps
docker compose up -d --wait db
uv run python -m bot.db.migrate
uv run pytest -q
```

`make` targets wrap the same commands (`make db-up migrate test`). On Windows without `make`,
run the `uv run …` line from the corresponding target in the [`Makefile`](Makefile).

## Results

_To be filled with measured numbers (`docs/results.md`)._

## Known limitations

_To be written._
