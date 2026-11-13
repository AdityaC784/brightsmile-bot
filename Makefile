# Thin wrappers around `uv run python -m ...` so CI (Linux) and local dev run identical commands.
# On Windows without make, run the command after each target directly (see README).

PY := uv run python -m

.PHONY: setup db-up db-down migrate ingest search eval-retrieval eval eval-ci eval-baseline judge-export judge-score review serve test lint fmt

setup:          ## install deps + git hooks
	uv sync
	uv run pre-commit install || true

db-up:          ## start local Postgres + pgvector
	docker compose up -d --wait db

db-down:
	docker compose down

migrate:        ## apply SQL migrations
	$(PY) bot.db.migrate

ingest: migrate ## load, chunk, embed and index the KB
	$(PY) bot.retrieval.ingest

search:         ## interactive retrieval inspector: make search Q="root canal price"
	$(PY) bot.retrieval.inspect "$(Q)"

eval-retrieval: ## recall@5 / MRR for each search mode x chunker (dev split)
	$(PY) evals.retrieval_eval --show-misses

eval:           ## full eval on dev split -> evals/runs/latest.json
	$(PY) evals.runner --split dev --out evals/runs/latest.json

eval-ci:        ## eval used by the CI gate
	$(PY) evals.runner --split all --out evals/runs/pr.json

eval-baseline:  ## deliberately refresh the baseline (commit it in its own PR)
	$(PY) evals.runner --split all --out evals/baseline.json

judge-export:   ## write a labelling sheet from the latest eval run (validate the judge)
	$(PY) evals.judge_agreement export

judge-score:    ## judge vs your labels: agreement + Cohen's kappa
	$(PY) evals.judge_agreement score

review:         ## review feedback candidates -> golden set
	$(PY) evals.review_cli

serve:          ## run the API locally
	uv run uvicorn bot.api.main:app --reload --port 8000

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .
