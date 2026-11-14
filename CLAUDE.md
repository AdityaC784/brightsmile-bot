# CLAUDE.md — BrightSmile Bot

Dental-clinic support & lead-capture chatbot: hybrid RAG with citations and self-verification,
LangGraph flow, FastAPI, and an eval-gated CI. Full write-up: `audit.md`. Plan: `docs/PLAN.md`.
Every measured number: `docs/results.md`.

## Commands (Windows: run the `uv run …` line from the Makefile target if `make` is missing)

```bash
docker compose up -d --wait db        # Postgres 16 + pgvector on localhost:5433
make migrate ingest                   # schema + index data/kb (embeddings cached in .cache/llm)
make test                             # pytest; DB tests skip without Postgres
make lint                             # ruff check + format --check
make chat | make serve                # terminal chat | API on :8000
make eval-retrieval | make eval       # retrieval metrics | end-to-end eval (dev split)
uv run python -m evals.runner --split holdout --out evals/runs/holdout.json
uv run python -m bot.rag.ask "question" --debug
```

## Layout
- `src/bot/retrieval` loaders → chunking → ingest; `hybrid.py` search (weighted RRF + reference expansion)
- `src/bot/rag` `answer.py` pipeline (generate → verify → one repair → fallback), `verify.py`
- `src/bot/graph` LangGraph nodes (classify, rewrite, support, lead, handoff/human_wait, greet, refuse)
- `src/bot/api` FastAPI app, per-turn logging with contact redaction
- `src/bot/llm` providers/roles/cache/retries; models in `configs/models.yaml`
- `prompts/*.md` — clinic facts come from `configs/generation.yaml` via `bot.prompts.clinic_prompt`
- `evals/` golden set, runner, judge, judge_agreement, compare (CI gate); `.github/workflows/`

## Rules for working in this repo
- **Answer tuning is frozen** (since 2026-11-13). Don't add prompt rules or edit the golden set
  because one dev case fails. Never tune on holdout results. q-016 (multi-rule totals) is a known
  limitation.
- **Golden answers are hand-written** by the owner; never generate them with an LLM. Log every
  golden-set change in `evals/golden/CHANGELOG.md` and bump `dataset_version`.
- **No test-case wording in prompts** (a test enforces this) and no clinic facts hard-coded in
  prompt files.
- **Secrets**: `.env` holds three Groq keys and a Gemini key. Never print, paste or commit them;
  read them with `grep … | cut` into env vars when a run needs a specific key.
- **Quotas**: Groq free tier = 200k tokens/day per model per key (rolling 24 h); a full uncached
  eval of the 50 cases uses ~230k generator tokens. LLM calls are cached by request, so re-runs
  of unchanged cases are free. Don't run two evals at once (shared limits; they overwrite each
  other's output).
- **Record results**: new measurements go in `docs/results.md` and the status in `audit.md`;
  judge validation labels live in `evals/judge_labels/`.
- **CI gate** (GitHub: `AdityaC784/brightsmile-bot`, private): `max_drop_points` in
  `configs/gate.yaml` = one flipped case passes, two fail — re-set it whenever the golden set size
  changes. The gate's LLM cache is filled by **Actions → eval-gate → Run workflow** on `main`.
  Changing the baseline is its own commit (`make eval-baseline`).
- **Models**: don't switch a role's model/provider (`configs/models.yaml`) as a quick fix — it
  invalidates the baseline and CI cache. Only as a recorded experiment with a new baseline; a new
  judge must be re-validated (`make judge-score`).
- Safety invariants (tests guard them): verified-before-shown replies; emergencies never fall
  back to generic text; leads saved only after the consent text was shown and the patient agreed.
- Commit messages end with the co-author line used in history; check staged files for keys first.
