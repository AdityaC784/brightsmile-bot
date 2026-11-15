# BrightSmile Bot API image. Runs migrations, then serves FastAPI on $PORT (default 8000).
# Secrets (GROQ_*, GEMINI_API_KEY, ADMIN_API_KEY, DATABASE_URL) come from the environment at
# run time; nothing secret is baked into the image. Index the KB once per database with
#   docker run --rm --env-file .env brightsmile-bot python -m bot.retrieval.ingest
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY evals ./evals
COPY prompts ./prompts
COPY configs ./configs
COPY data/kb ./data/kb
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["sh", "-c", "python -m bot.db.migrate && exec uvicorn bot.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
