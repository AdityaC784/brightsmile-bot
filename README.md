# BrightSmile Dental Care — Support & Lead-Gen Chatbot with an Eval Gate

A website chatbot for a (fictional) dental clinic in Pune that answers patient questions from a
36-document knowledge base **with citations**, abstains when the KB doesn't say, captures leads
**only with consent**, hands off to a human when needed — and is protected by a **CI eval gate**
that blocks any PR that makes answers worse.

> Status: retrieval, grounded answering, LangGraph flow, FastAPI service and the CI gate are
> built. Answer tuning is **frozen**; next: re-measure, noise measurement, golden set → 50,
> baseline. Plan: [`docs/PLAN.md`](docs/PLAN.md) · measured results: [`docs/results.md`](docs/results.md).

## How it answers

```
question ─► classify intent ─► rewrite follow-up ─► hybrid retrieval (+ one-hop references)
        ─► generate JSON reply with [S#] citations ─► verify (₹ amounts / codes in cited
           source + LLM claim check) ─► one repair attempt ─► reply, or safe fallback
           (emergencies always fall back to fixed first-aid + routing text)
```

Lead capture asks for consent before taking any details and saves only after an explicit yes.
Human handoff pauses the graph (`interrupt`) until staff reply via the API.

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI; replies streamed over SSE *after* verification |
| Orchestration | LangGraph (Postgres checkpointer, human-in-the-loop interrupt) |
| Storage | Postgres 16 + pgvector (chunks, conversations, leads, feedback, eval results, graph state) |
| Retrieval | Hybrid: Postgres full-text + pgvector HNSW, weighted Reciprocal Rank Fusion |
| Generator | Qwen 3.8 27B on Groq |
| Utility (classify / rewrite / slots / citation verifier) | GPT-OSS 20B on Groq |
| Judge (evals only) | GPT-OSS 120B on Groq, temperature 0, checklist grading — validated against hand labels |
| Embeddings | Gemini `gemini-embedding-001`, 768-dim |

All model IDs live in [`configs/models.yaml`](configs/models.yaml); clinic contact details used
in prompts live in [`configs/generation.yaml`](configs/generation.yaml).

> **Note:** Postgres full-text search (`ts_rank_cd`) is **not** true BM25 — it has no IDF
> saturation or document-length normalisation in the BM25 sense. It's the lexical half of the hybrid.

## Knowledge base

`data/kb/` — 36 documents in mixed formats (11 PDF, 10 DOCX, 10 Markdown, 5 CSV) plus
`manifest.json`. Built-in retrieval traps: exact identifiers (`SRV-005A`, `POL-CAN-01`,
`PKG-002`…), superseded pricing (`DENT-020` 2025 next to `DENT-019` 2026), multi-source facts,
tables, and deliberately unanswerable questions (`DENT-036` §2).

## Run it

```bash
cp .env.example .env              # GROQ_API_KEY, GROQ_GENERATOR_API_KEY, GEMINI_API_KEY, ADMIN_API_KEY
uv sync
docker compose up -d --wait db
make migrate ingest               # schema + chunk/embed/index the KB
make chat                         # talk to the bot in the terminal
make serve                        # API on :8000 (POST /v1/chat, /v1/feedback, admin endpoints)
make test                         # 80+ offline tests (scripted LLMs; DB tests skip without Postgres)
make eval-retrieval               # recall@5 / MRR per search mode
make eval                         # end-to-end eval on the dev split -> evals/runs/latest.json
```

On Windows without `make`, run the command from the matching target in the [`Makefile`](Makefile).

## Results

Measured numbers, experiments and judge validation are logged in
[`docs/results.md`](docs/results.md). Latest (golden set 0.5.0, 30 cases, hand labels):
dev **21/24**, holdout **4/5** (one holdout case not run yet); judge vs hand labels 22/24.
Weighted RRF lifted dev retrieval recall@5 from 0.76 to 0.93.

## Known limitations

- **Small golden set** (30 cases, 6 holdout): one case ≈ 4 points; run-to-run noise not yet measured.
- **Multi-rule totals** (e.g. package refund after partial treatment) are not computed; the bot
  gives the published amounts and refers to reception.
- **Judge validated on 24 replies** labelled by the project owner — enough to catch a lenient judge,
  not a precise reliability estimate.
- **Contact details typed in chat** are redacted from logs and saved to `leads` only with consent,
  but remain in the LangGraph checkpoint transcript.
- **Free-tier quotas** (Groq 200k tokens/day per model per key) limit how often a full uncached
  eval can run.
- Synthetic KB: a few internal inconsistencies were found and fixed (see `evals/golden/CHANGELOG.md`).
