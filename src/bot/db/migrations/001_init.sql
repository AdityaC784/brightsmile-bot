-- 001_init: core schema for BrightSmile bot
-- Embedding dimension (768) must match configs/models.yaml -> embeddings.dimensions.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Knowledge base chunks
-- ---------------------------------------------------------------------------
CREATE TABLE chunks (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT        NOT NULL DEFAULT 'default',
    doc_id         TEXT        NOT NULL,              -- citation anchor, e.g. 'DENT-019#root-canal'
    source_doc_id  TEXT        NOT NULL,              -- manifest id, e.g. 'DENT-019'
    title          TEXT        NOT NULL,              -- document title
    section        TEXT        NOT NULL DEFAULT '',   -- heading path / CSV row key
    content        TEXT        NOT NULL,
    category       TEXT        NOT NULL,
    status         TEXT        NOT NULL CHECK (status IN ('active', 'archived')),
    chunker        TEXT        NOT NULL CHECK (chunker IN ('heading', 'fixed')),
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    content_hash   TEXT        NOT NULL,
    embedding      vector(768),
    kb_version     TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '') || ' ' || coalesce(section, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED,
    UNIQUE (tenant_id, chunker, doc_id)
);

CREATE INDEX chunks_fts_idx       ON chunks USING GIN (fts);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_filter_idx    ON chunks (tenant_id, chunker, status);

-- ---------------------------------------------------------------------------
-- Conversations & per-turn logs
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
    thread_id      TEXT PRIMARY KEY,
    tenant_id      TEXT        NOT NULL DEFAULT 'default',
    handoff_status TEXT        NOT NULL DEFAULT 'none'
                   CHECK (handoff_status IN ('none', 'pending', 'resolved')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id             BIGSERIAL PRIMARY KEY,
    thread_id      TEXT        NOT NULL REFERENCES conversations(thread_id) ON DELETE CASCADE,
    role           TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'human_agent')),
    content        TEXT        NOT NULL,
    intent         TEXT,
    retrieved_ids  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    citations      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    verifier       JSONB,
    abstained      BOOLEAN     NOT NULL DEFAULT false,
    latency_ms     INTEGER,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_usd       NUMERIC(10, 6),
    model          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX messages_thread_idx ON messages (thread_id, created_at);

-- ---------------------------------------------------------------------------
-- Leads (personal data: saved only after explicit consent; hard-deletable)
-- ---------------------------------------------------------------------------
CREATE TABLE leads (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id      TEXT        NOT NULL UNIQUE,      -- one lead per thread => idempotent save
    need           TEXT,
    budget         TEXT,
    timeline       TEXT,
    name           TEXT        NOT NULL,
    contact        TEXT        NOT NULL,
    consent_at     TIMESTAMPTZ NOT NULL,
    consent_text   TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Feedback & review candidates (feedback loop -> golden set)
-- ---------------------------------------------------------------------------
CREATE TABLE feedback (
    id             BIGSERIAL PRIMARY KEY,
    thread_id      TEXT        NOT NULL,
    message_id     BIGINT      REFERENCES messages(id) ON DELETE SET NULL,
    rating         SMALLINT    NOT NULL CHECK (rating IN (-1, 1)),
    comment        TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE candidates (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT        NOT NULL
                    CHECK (source IN ('thumbs_down', 'abstained', 'handoff', 'citation_failed')),
    thread_id       TEXT        NOT NULL,
    message_id      BIGINT      REFERENCES messages(id) ON DELETE SET NULL,
    question        TEXT        NOT NULL,
    bot_answer      TEXT,
    retrieved_ids   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'rejected', 'deferred')),
    reviewer_answer TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at     TIMESTAMPTZ
);
CREATE INDEX candidates_status_idx ON candidates (status, created_at);

-- ---------------------------------------------------------------------------
-- Eval history
-- ---------------------------------------------------------------------------
CREATE TABLE eval_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_name        TEXT        NOT NULL,
    dataset_version TEXT        NOT NULL,
    git_sha         TEXT,
    config          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    metrics         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE eval_results (
    id        BIGSERIAL PRIMARY KEY,
    run_id    BIGINT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_id   TEXT   NOT NULL,
    split     TEXT   NOT NULL CHECK (split IN ('dev', 'holdout')),
    case_type TEXT   NOT NULL,
    passed    BOOLEAN,
    scores    JSONB  NOT NULL DEFAULT '{}'::jsonb,
    output    JSONB  NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, case_id)
);
