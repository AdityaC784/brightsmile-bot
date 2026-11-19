# Code guide — BrightSmile bot

A lesson-by-lesson walk through the codebase: what each part does, how it works, why it was
built that way, and what an interviewer is likely to ask. Every claim points to a file and line
(`path:line`) or to a measured number in [`docs/results.md`](results.md).

| # | Lesson | Main files |
|---|---|---|
| 0 | [Big picture](#lesson-0--big-picture) | `configs/`, `Makefile` |
| 1 | [Config & LLM layer](#lesson-1--config--llm-layer) | `src/bot/config.py`, `src/bot/llm/` |
| 2 | [Knowledge-base ingestion](#lesson-2--knowledge-base-ingestion) | `src/bot/retrieval/loaders.py`, `chunking.py`, `ingest.py`, `src/bot/db/` |
| 3 | [Hybrid retrieval](#lesson-3--hybrid-retrieval) | `src/bot/retrieval/hybrid.py` |
| 4 | [Grounded answering + self-verification](#lesson-4--grounded-answering--self-verification) | `src/bot/rag/` |
| 5 | [Conversation flow (LangGraph)](#lesson-5--conversation-flow-langgraph) | `src/bot/graph/`, `src/bot/leads/` |
| 6 | [API & product](#lesson-6--api--product) | `src/bot/api/` |
| 7 | [Evaluation](#lesson-7--evaluation) | `evals/` |
| 8 | [CI/CD & deployment](#lesson-8--cicd--deployment) | `.github/workflows/`, `Dockerfile` |
| 9 | Feedback loop & experiments | `evals/review_cli.py`, `spot_checks.py`, `ragas_check.py` |
| 10 | Tests & wrap-up | `tests/` |

---

## Lesson 0 — Big picture

### Purpose
A support chatbot for a (fictional) dental clinic. It answers patient questions **only from the
clinic's own documents**, cites them, checks every answer before showing it, captures leads only
with consent, hands chats to staff, and is guarded by an **evaluation suite that runs on every
pull request** and blocks changes that make answers worse.

### The folder map
| Folder | What lives there |
|---|---|
| `data/kb/` | The knowledge base: clinic documents in PDF, DOCX, Markdown and CSV, plus `manifest.json` (id, title, status of each document) |
| `configs/` | Every tunable value: models (`models.yaml`), search settings (`retrieval.yaml`), clinic facts and fixed messages (`generation.yaml`), CI gate rules (`gate.yaml`) |
| `prompts/` | The instructions given to each LLM call (answer, verify, classify, rewrite, lead extraction, judge) |
| `src/bot/llm/` | One small layer that talks to the LLM providers (Groq, Gemini), with retries and a cache |
| `src/bot/retrieval/` | Loading documents → splitting into chunks → embedding → indexing in Postgres → searching |
| `src/bot/rag/` | Turning search results into a cited answer, then verifying it |
| `src/bot/graph/` | The conversation flow (LangGraph): which path a message takes |
| `src/bot/api/` | The web server (FastAPI), the chat page and the staff page |
| `src/bot/leads/`, `src/bot/db/` | Saving leads; the database schema and migrations |
| `evals/` | The test set ("golden set"), the eval runner, the LLM judge, the CI comparison, review tools |
| `tests/` | 112 unit/integration tests (no real LLM calls: scripted fakes) |
| `.github/workflows/` | CI: lint + tests, and the eval gate |

### The life of one message
What happens when a patient types *"How much is a root canal?"* on the chat page:

```mermaid
flowchart TD
    B[Chat page<br/>static/index.html] -->|POST /v1/chat| API[api/main.py · _run_turn]
    API --> RL{rate limit ok?}
    RL -->|no| FIX[fixed message,<br/>no LLM call]
    RL -->|yes| G[LangGraph graph.invoke]
    G --> C[classify: intent + emergency]
    C -->|support| RW[rewrite follow-up<br/>into a full question]
    RW --> S[support → rag/answer.py]
    S --> H[hybrid search<br/>+ reference expansion]
    H --> GEN[generator writes JSON<br/>answer with S1 S2 citations]
    GEN --> V{verify}
    V -->|pass| OUT[reply]
    V -->|fail| REP[one repair] -->|still fails| FB[safe fallback]
    OUT --> LOG[turns.log_turn:<br/>save, redact contacts,<br/>flag review candidates]
    LOG --> SSE[stream the verified<br/>answer to the page]
```

In code, in order:
1. `src/bot/api/main.py:176` `chat()` receives the request, and `_run_turn` (`:107`) does the work.
2. `:110` the rate limiter is checked **before** any LLM call.
3. `:136` the LangGraph graph runs: `src/bot/graph/nodes.py:91` `classify` → `:102` `route` →
   `:132` `rewrite` → `:145` `support`.
4. `support` calls the answer pipeline: `src/bot/rag/answer.py:171` `answer()` → search at `:178`
   (`src/bot/retrieval/hybrid.py:96`, plus `expand_references` at `:136`) → generate → verify
   (`answer.py:149`, `src/bot/rag/verify.py`) → repair or fallback (`answer.py:120`).
5. `main.py:152` saves the turn (`src/bot/api/turns.py:46`), masking phone numbers and emails
   (`turns.py:21`) and flagging bad turns as review candidates (`turns.py:115`).
6. `main.py:188` streams the **already verified** answer back over Server-Sent Events.

Other paths from `route`: booking intent → `lead` (consent flow), a request for a person →
`handoff` (staff page), greetings → `greet`, off-topic → `refuse`, and while staff hold the chat
→ `staff` (the message is relayed, the bot stays quiet unless it's an emergency).

### Config drives behaviour
Almost every decision is a value in `configs/`, not code. A few examples:

```yaml
# configs/models.yaml — which model does which job ("roles")
roles:
  generator: { provider: groq, model: qwen/qwen3.8-27b, max_tokens: 1024, temperature: 0 }
  utility:   { provider: groq, model: openai/gpt-oss-20b, reasoning_effort: low }   # classify, rewrite, verify
  judge:     { provider: groq, model: openai/gpt-oss-120b, reasoning_effort: medium } # evals only
```

| Setting | File | Why it's config |
|---|---|---|
| Model per role | `models.yaml` | Swapping a model is an experiment, not a code change (the comments record why each was chosen) |
| `lexical_weight: 0.3`, `rrf_k: 60`, `top_k: 6` | `retrieval.yaml` | Tuned by measurement; the comment keeps the sweep results |
| Clinic name, phone numbers, fallback and consent texts | `generation.yaml` | One source of truth; prompts fill them in, so no clinic facts are hard-coded in prompts |
| Gate thresholds | `gate.yaml` | CI rules are reviewable in one place |

### Why it's built this way
- **Grounding over fluency.** A clinic bot that invents a price or a policy is worse than one that
  says "please call reception". So every answer is cited and checked, and a failed check means a
  safe fallback.
- **Small, swappable pieces.** Each folder does one job: search doesn't know about the API,
  answering doesn't know about LangGraph. This made each part testable and measurable on its own.
- **Measure, then decide.** Search weights, chunking, model choice and the judge were all picked by
  experiments recorded in `docs/results.md`, not by guessing.
- **Free tiers.** Groq and Gemini free tiers shaped many choices (caching every LLM call, rate
  limits, a small utility model for cheap steps).

### Limits
- The free LLM quota caps capacity (about 1–2 full answers a minute for the whole site).
- Some state (staff presence, rate-limit counters) lives in server memory, which assumes one
  server instance.

### Interview questions
1. **"Walk me through what happens when a user sends a message."** Rate limit → classify intent →
   rewrite follow-ups → hybrid search → generate a cited JSON answer → verify each claim against
   its source → one repair or a safe fallback → log with redaction → stream the verified answer.
2. **"Why not stream tokens straight from the model?"** Because the answer is verified first. A
   streamed answer could show text that the verifier later rejects.
3. **"How do you stop the model making things up?"** Retrieval-only grounding, mandatory
   citations, a code-level check that ₹ amounts and service codes appear in the cited source, an
   LLM claim check, then fallback. Measured faithfulness and citation accuracy are both 1.00.
4. **"What would you change to scale it?"** A paid LLM tier first, then a paid host; move
   in-memory state to Postgres/Redis before running more than one instance.

### Try it yourself
```bash
docker compose up -d --wait db
uv run python -m bot.rag.ask "How much is a root canal?" --debug   # one answer, with sources and checks
```

---

## Lesson 1 — Config & LLM layer

### Purpose
Every other part of the bot asks for an LLM **by job** ("generator", "utility", "judge"), never
by vendor or model name. This layer turns that job name into a real API call. It picks the
provider and model from `configs/models.yaml`, retries on rate limits, caches responses on disk,
and parses JSON output into Pydantic objects. It also loads settings, secrets and prompt files.

### Files
| File | Role |
|---|---|
| `src/bot/config.py` | `Settings` (secrets and flags from `.env`) and `load_yaml()` for `configs/*.yaml` |
| `src/bot/llm/base.py` | Provider-neutral types: `Message`, `ChatRequest`, `ChatResponse`, the `ChatProvider` protocol, `LLMError` |
| `src/bot/llm/providers.py` | `GroqProvider`, `GeminiProvider`, and the retry rules (`is_retryable`, `wait_retry_after`) |
| `src/bot/llm/client.py` | `get_llm(role)` → `LLM` with `chat()` and `chat_json()`: the **only** entry point the app uses |
| `src/bot/llm/cache.py` | `DiskCache`: one JSON file per request, keyed by a SHA-256 of the full request |
| `src/bot/llm/embeddings.py` | `Embedder`: Gemini embeddings, truncated to 768 dims, normalised, always cached |
| `src/bot/llm/check_models.py` | Health check: is every configured model live with your keys? |
| `src/bot/prompts.py` | Loads `prompts/*.md`, fills in clinic facts, hashes prompts for eval records |
| `configs/models.yaml` | Which provider and model does each job, with the reasons recorded as comments |

### How it works
```mermaid
flowchart LR
    N[nodes.py / answer.py / judge.py] -->|get_llm role| C[client.py LLM]
    C -->|reads| Y[configs/models.yaml]
    C --> K{cache on and<br/>key on disk?}
    K -->|yes| R[stored response,<br/>cached=True]
    K -->|no| T[tenacity retry loop<br/>up to 8 tries]
    T --> P[GroqProvider /<br/>GeminiProvider]
    P --> API[(Groq / Gemini API)]
    T -->|save| D[(.cache/llm/chat)]
```

Who uses which role:

| Role | Model | Used by |
|---|---|---|
| `generator` | `qwen/qwen3.8-27b` (own Groq key) | `rag/answer.py:110`: writes the cited answer |
| `utility` | `openai/gpt-oss-20b` | `graph/nodes.py:83` (classify, rewrite, lead extraction), `rag/answer.py:114` and `rag/verify.py:136` (claim check) |
| `judge` | `openai/gpt-oss-120b` | `evals/judge.py:91` only, never live chats |
| embeddings | `gemini-embedding-001` @ 768 | ingestion and `retrieval/hybrid.py:110` (`embed_query`) |

### Key code

**1. Settings: secrets from `.env`, tunables from YAML** (`src/bot/config.py:16`)
```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    database_url: str = "postgresql://postgres:postgres@localhost:5433/brightsmile"
    groq_api_key: str = ""
    groq_generator_api_key: str = ""  # optional 2nd key: the generator gets its own quota
    gemini_api_key: str = ""
    llm_cache_enabled: bool = False  # the eval runner turns this on
```
`pydantic-settings` reads each field from an environment variable of the same name (or `.env`),
with type checking. `get_settings()` and `load_yaml()` are wrapped in `@lru_cache` (`:35`, `:40`),
so each file is read once per process. **Secrets live in env vars; behaviour lives in YAML.**
On Render the same fields come from the dashboard's environment variables.

**2. Roles → models** (`src/bot/llm/client.py:141`)
```python
def get_llm(role: str, *, use_cache: bool | None = None) -> LLM:
    roles = load_yaml("models")["roles"]
    if role not in roles:
        raise LLMError(f"Role '{role}' not defined in configs/models.yaml")
    if use_cache is None:
        use_cache = get_settings().llm_cache_enabled
    return LLM(role, roles[role], use_cache)
```
`LLM.__init__` (`:54`) copies `provider`, `model`, `max_tokens`, `temperature` and
`api_key_setting`. Any **other** key in the YAML (e.g. Groq's `reasoning_effort`) goes into
`options` (`:61`) and is passed straight to the SDK, so a new provider setting needs no code
change. `_provider()` (`:33`) is `lru_cache`d: one SDK client per (provider, key) pair.

**3. The cache check** (`client.py:86`)
```python
key = request_key(req.cache_payload(self.provider_name))
if self.use_cache and (hit := self._cache.get(key)):
    return ChatResponse(**{**hit, "cached": True, "latency_ms": 0})
resp = self._call(req)
...
if self.use_cache:
    self._cache.set(key, asdict(resp))
```
The key is a SHA-256 of the **whole** request: provider, model, system prompt, messages,
max_tokens, temperature, JSON mode and options (`cache.py:19`, with `sort_keys=True` so key order
doesn't matter). Change one character of a prompt and the key changes, so the call goes to the
model again. `DiskCache.set` writes to a temp file and then renames it (`cache.py:44`), so a crash
never leaves half a JSON file. Files are split into folders by the first two hex characters of
the key so no folder gets huge.

**4. Retries that respect the provider** (`providers.py:24-62`, `client.py:103`)
```python
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def is_retryable(exc):
    if is_daily_quota(exc):  # "tokens per day (TPD)": won't clear for hours
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS
    name = type(exc).__name__.lower()
    return "connection" in name or "timeout" in name


@retry(
    retry=retry_if_exception(is_retryable),
    stop=stop_after_attempt(8),
    wait=wait_retry_after,
    reraise=True,
)
def _call(self, req):
    return self.provider.complete(req)
```
`wait_retry_after` (`providers.py:46`) waits as long as Groq's `Retry-After` header says (or
Gemini's "retry in 22s" text in the error body), plus a little random jitter, capped at 90 s.
With no hint it falls back to exponential backoff (1 s up to 30 s). The SDKs' own retries are
turned off (`max_retries=0` at `:79`, `attempts=1` at `:130`) so there is **one** retry layer, not
two stacked ones.

**5. JSON output with one repair** (`client.py:112`)
```python
sys_prompt = f"{system}\n\nReturn JSON matching this JSON Schema:\n{schema.model_json_schema()}"
resp = self.chat(sys_prompt, messages, json_mode=True)
try:
    return schema.model_validate_json(_extract_json(resp.text)), resp
except ValidationError as err:
    repair = [
        *messages,
        Message("assistant", resp.text),
        Message("user", f"That JSON was invalid: {err}. Reply with corrected JSON only."),
    ]
    ...  # second try; if it also fails → LLMError
```
The Pydantic class is both the instruction (its JSON Schema goes into the prompt) and the check.
`_extract_json` (`:43`) tolerates code fences and stray prose around the object. Groq sometimes
rejects its own JSON-mode output with a 400 `json_validate_failed`; `GroqProvider.complete`
retries that once without JSON mode (`providers.py:95-104`) and lets `chat_json` repair it.

**6. Embeddings** (`src/bot/llm/embeddings.py:56`)
```python
resp = self.client.models.embed_content(
    model=self.model,
    contents=texts,
    config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims),
)
...
return [l2_normalize(v) for v in vecs]
```
- `task_type` differs for documents (`RETRIEVAL_DOCUMENT`) and questions (`RETRIEVAL_QUERY`):
  Gemini embeds each side slightly differently, which improves matching.
- `output_dimensionality=768`: the model is trained Matryoshka-style, so its first 768 numbers
  still work as an embedding. pgvector's HNSW index supports at most 2,000 dims, and 768 keeps
  storage small.
- After truncation the vector is no longer length 1, so it is **L2-normalised**: then cosine
  similarity equals the dot product.
- `embed()` (`:69`) checks the cache per text, sends only the misses in batches of 50 and saves
  each result. Embeddings are **always** cached, so re-running ingestion is free.

**7. Prompts as files, clinic facts from config** (`src/bot/prompts.py:37`)
```python
def clinic_prompt(name: str) -> str:
    c = load_yaml("generation")["clinic"]
    values = {"clinic_name": c["name"], "reception": c["reception"], "booking_url": ...}
    used = set(_PLACEHOLDER.findall(text))
    unknown = used - values.keys()
    if unknown:
        raise KeyError(f"prompts/{name}.md has unknown placeholders: {sorted(unknown)}")
```
Prompts are Markdown files, so a prompt change is a readable diff in a PR (and changes the cache
key, so the eval gate re-runs those cases). Only `{lowercase_names}` the code knows are replaced,
so literal JSON braces in a prompt stay as they are. `prompt_hash()` (`:59`) stores a 10-character
hash of each prompt in every eval run, which ties a score to an exact prompt version. A test
(`tests/test_rag.py:244`) checks that every prompt renders, that no clinic phone number or URL is
typed into a prompt file, and that no golden-case wording leaks into a prompt.

### Why it's built this way
- **Roles, not model names.** Model catalogues change often (Groq retired Llama 3.x in Aug 2026,
  noted at the top of `models.yaml`). Swapping a model is a one-line YAML change, recorded with
  its reason. The choices were measured: Qwen over gpt-oss-120b for the generator because
  gpt-oss wrote `【】` instead of `[]` citations and non-breaking hyphens inside IDs like
  `SRV‑005A`, which would break the exact-ID check (`models.yaml:7-9`). The judge is a different
  model family from the generator, to reduce a model's bias toward its own writing, and was picked
  by agreement with hand labels (kappa 0.36 → 0.58 on the first comparison, `models.yaml:28-31`).
- **A thin layer instead of LangChain's model wrappers.** It is about 300 lines, so its behaviour
  is easy to see: a single retry policy, a cache key we control, and JSON repair. LangGraph is
  still used for the conversation flow (Lesson 5); only the model-calling part is custom.
- **A separate Groq key for the generator** (`api_key_setting: groq_generator_api_key`). Groq's
  free tier limits tokens per model per organisation, so the generator and the judge/utility use
  different quota buckets and don't block each other during an eval.
- **Caching is how CI stays cheap.** A full uncached eval uses about 230k generator tokens, more
  than a day's free quota. With the cache, a PR that doesn't change prompts or models replays
  every case from disk. The cache is **off for live chats** (`llm_cache_enabled=False`) and **on
  for evals**: `evals/runner.py:29` sets it, and `:244` asserts it.
- **Fail fast on daily quotas.** Retrying a "tokens per day" error only stalls for many minutes,
  so `is_daily_quota` stops the retries at once (found while running evals, `docs/results.md`
  §2b). Per-minute (TPM) 429s are retried, because they clear in seconds.

### Limits and gotchas
- **The cache key includes everything**, so any prompt edit invalidates the cached replies of
  every case that uses that prompt; the next eval costs real quota.
- **Temperature 0 isn't fully deterministic on Groq**: an uncached re-run can differ from the
  cached reply. The cache is what makes CI repeatable.
- `gpt-oss` models are reasoning models: hidden reasoning tokens count toward `max_tokens`, so
  `reasoning_effort` is kept low on `utility` and the judge gets 4,096 tokens (`models.yaml:19-20`).
- A trailing space in a `.env` key value breaks the HTTP header, and httpx then prints the whole
  key in its error. Trim key values (a real incident in this project; the key was rotated).
- The disk cache is local files: in CI it is saved and restored with GitHub Actions' cache
  (Lesson 8).

### Interview questions
1. **"How do you swap an LLM provider?"** Change the role in `configs/models.yaml`. Each provider
   class turns the neutral `ChatRequest` into its SDK call (`GroqProvider`, `GeminiProvider`), and
   the rest of the app only calls `get_llm(role)`. A new vendor means one new class with
   `complete()` and `list_models()` added to `PROVIDERS`.
2. **"How do you handle rate limits?"** Retry only what can succeed (429, 5xx, timeouts), wait
   as long as the provider's `Retry-After` says, add jitter so callers don't retry in sync, stop
   after 8 tries, and never retry daily-quota errors. In the API, rate limits per chat and per IP
   protect the quota before any call is made (Lesson 6).
3. **"How do you get reliable structured output from an LLM?"** Put the Pydantic schema into
   the prompt, use JSON mode, extract the JSON object, validate it with Pydantic, and on failure
   send the validation error back to the model once. A second failure raises `LLMError`, and
   the caller has a safe fallback.
4. **"Why cache LLM responses, and what's the key?"** Cost and repeatability for evals. The key
   is a SHA-256 of the full request (model, prompts, messages, settings), so any change that
   could change the output is a cache miss. Live chats aren't cached.
5. **"Why 768 dimensions?"** Matryoshka embeddings keep their meaning when truncated. 768 fits
   under pgvector's 2,000-dim HNSW limit and keeps the index small; vectors are re-normalised
   after truncation so cosine similarity still works.

### Try it yourself
```bash
uv run python -m bot.llm.check_models   # is every role's model live? one tiny real call each
uv run pytest tests/test_llm_and_retrieval.py -q -k "cache or json or retry or quota"
```

---

## Lesson 2 — Knowledge-base ingestion

### Purpose
Turns the clinic's 36 documents (11 PDF, 10 DOCX, 10 Markdown, 5 CSV) into **searchable chunks
in Postgres**: each chunk gets its text, a citation id, a vector embedding and a full-text index.
This runs offline (`make ingest`), not on each chat. Answer quality is capped by this step: if a
price is split badly or a table row loses its header, no prompt can fix it later.

### Files
| File | Role |
|---|---|
| `data/kb/manifest.json` | One entry per document: `doc_id`, `filename`, `title`, `category`, `status` (active / archived), version, date |
| `src/bot/retrieval/loaders.py` | One loader per format → a common list of `Block`s (heading / text / table / row) |
| `src/bot/retrieval/chunking.py` | Two chunkers: `heading` (follows document structure, used live) and `fixed` (token windows, the baseline) |
| `src/bot/retrieval/ingest.py` | Load → chunk → embed → replace the `chunks` rows in one transaction; computes `kb_version` |
| `src/bot/db/migrate.py` | A 40-line forward-only migration runner |
| `src/bot/db/migrations/001_init.sql` | The whole schema: chunks, conversations, messages, leads, feedback, candidates, eval tables |
| `configs/retrieval.yaml` | Chunk sizes and which chunker search uses |

### How it works
```mermaid
flowchart LR
    M[manifest.json] --> L
    F[PDF · DOCX · MD · CSV] --> L[loaders.py<br/>→ Blocks]
    L --> CH[chunking.py<br/>heading + fixed]
    CH --> E[embeddings.py<br/>Gemini 768-d, cached]
    E --> DB[(Postgres chunks:<br/>GIN full-text index<br/>+ HNSW vector index)]
    CH --> V[kb_version hash]
    V --> DB
```

Result today: **415 chunks** (234 heading + 181 fixed), `kb_version 08a3673838d7`.

### Key code

**1. One shape for every format** (`loaders.py:27`)
```python
@dataclass
class Block:
    kind: BlockKind  # "heading" | "text" | "table" | "row"
    text: str = ""
    level: int = 0  # heading level
    rows: list[str] = ...  # table rows, already rendered
    key: str = ""  # CSV first column, used as the citation anchor
```
Each loader returns `Block`s in reading order, so the chunkers don't care about file formats.
Dispatch is a dict by extension (`:320`).

**2. Tables: every row stands on its own** (`loaders.py:77`)
```python
def render_rows(header, rows):
    ...
    parts.append(f"{h}: {c}" if h else c)
    out.append("; ".join(parts))
```
A tariff row becomes `Service ID: SRV-005A; Service Description: Molar Root Canal…; Fee: …`.
Without this, a chunk holding the middle of a table would have numbers with no column names,
and neither search nor the model could tell what ₹6,800 is.

**3. Format-specific problems the loaders solve**
| Format | Problem | Fix |
|---|---|---|
| PDF | No real structure, only text with font sizes | Bold lines ≥ 12.5 pt are headings, ≥ 17 pt is the title (`:187-189`, `:267`); lines ≤ 9 pt are footers and are dropped (`:241`); the repeated page header is removed by regex (`:190`) |
| PDF | Table text is extracted twice (as a table and as lines) | Lines whose centre falls inside a table's box are skipped (`:236`) |
| PDF | A table continues on the next page without its header | Same column count, near the top of the page → reuse the previous header (`:221-229`) |
| PDF | IDs wrapped at line end: `SRV-` / `005A` | `_WRAPPED_HYPHEN` re-joins them (`:51`); a test checks that no service ID in the KB is broken |
| PDF | A "Metadata field" table on the first page | Skipped (`:193`); it's document metadata, not content |
| DOCX | python-docx repeats merged cells | Adjacent duplicates are collapsed (`:179`); heading levels come from the `Heading N` style |
| Markdown | Front matter, `**bold**`, links, table separator rows | `python-frontmatter`, `strip_md_inline` (`:64`), separator rows dropped (`:114`) |
| CSV | Byte-order mark, snake_case headers | `utf-8-sig`, `humanize()` → "refund rule id"; **one `row` block per data row** keyed by the first column (`:304`) |

**4. The heading chunker** (`chunking.py:107`)
1. `_sections()` (`:85`) groups blocks under their nearest heading and keeps the **heading path**
   (a stack: a level-2 heading pops anything at level ≥ 2). The document's own title heading is
   skipped because it's already stored in the `title` column.
2. Sections under 40 tokens are **merged forward** into the next one (`:128-149`), so there's no
   chunk that is just a heading and one line.
3. Sections over 450 tokens are **split at line boundaries** (paragraph or table row, never in
   the middle of a row) (`:156-165`). Each part repeats the heading at its top.
4. CSV documents: **one chunk per row** (`:112`), with id `DENT-024#REF-03`.

Citation ids are stable and readable: `doc_id#slug-of-heading`, with `-2`, `-3` added to keep
them unique (`_unique`, `:50`). For example:
```
DENT-019#2-master-2026-service-tariff-table-all-rates-in      444 tokens
DENT-019#2-master-2026-service-tariff-table-all-rates-in-2    424 tokens  (SRV-005A …)
DENT-024#REF-03                                               104 tokens  (one CSV row)
```
Token counts are approximate (`\w+|[^\w\s]` pieces, `:17`). That's enough for sizing chunks, and
it avoids loading a tokenizer that matches neither model exactly.

**5. The fixed chunker, kept as a baseline** (`chunking.py:186`): 300-token windows with a
50-token overlap over the flattened text. Both chunkers are always indexed (`ingest.py:69`), in
the same table with a `chunker` column, so the chunking experiment switches with one config
value (`retrieval.yaml: chunking.strategy`) and no re-ingest.

**6. What gets embedded** (`ingest.py:40`)
```python
def embedding_text(c: Chunk, status: str) -> str:
    flag = "[ARCHIVED — superseded, not current]\n" if status == "archived" else ""
    return f"{flag}{c.title}\n{c.section}\n{c.content}".strip()
```
The title and section path are added before the content, so a chunk that only says "₹6,800"
still carries "2026 Service Tariff" in its vector. The archived 2025 price list is marked in its
embedding text too.

**7. `kb_version`: one hash for everything that shapes the index** (`ingest.py:46`)
```python
h.update(json.dumps(manifest, sort_keys=True).encode())
for name in sorted(files):
    h.update(name.encode())
    h.update(files[name].read_bytes())
h.update(json.dumps(load_yaml("retrieval")["chunking"], sort_keys=True).encode())
h.update(json.dumps(load_yaml("models")["embeddings"], sort_keys=True).encode())
```
Every chunk row stores it, `/v1/health` reports it, and every eval run records it. A score
is always tied to an exact KB, chunking settings and embedding model.

**8. Replace, atomically** (`ingest.py:83`)
```python
with conn.transaction():
    conn.execute("DELETE FROM chunks WHERE tenant_id = %s", (settings.tenant_id,))
    cur.executemany("INSERT INTO chunks (...) VALUES (...)", rows)
conn.execute("ANALYZE chunks")
```
Delete-and-insert inside one transaction: a chat running during re-ingestion sees either the old
index or the new one, never half of each. Re-running is idempotent, and because embeddings are
cached (Lesson 1) an unchanged KB costs zero API calls.

**9. The schema** (`migrations/001_init.sql:9`)
```sql
CREATE TABLE chunks (
    doc_id TEXT NOT NULL,            -- citation anchor 'DENT-019#root-canal'
    source_doc_id TEXT NOT NULL,     -- 'DENT-019'
    status TEXT CHECK (status IN ('active','archived')),
    chunker TEXT CHECK (chunker IN ('heading','fixed')),
    embedding vector(768),
    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title || ' ' || section), 'A') ||
        setweight(to_tsvector('english', content), 'B')) STORED,
    UNIQUE (tenant_id, chunker, doc_id) ...);
CREATE INDEX chunks_fts_idx       ON chunks USING GIN (fts);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
```
- `fts` is a **generated column**: Postgres keeps the full-text vector in sync automatically.
  Title and section words get weight A (they count more than body words, weight B).
- **GIN** indexes the full-text vector; **HNSW** (pgvector) is an approximate nearest-neighbour
  index for cosine distance.
- The same file creates `conversations`, `messages`, `leads` (with `consent_at` and
  `consent_text NOT NULL`, and `thread_id UNIQUE` so saving a lead twice is harmless),
  `feedback`, `candidates` (the review queue) and `eval_runs`. LangGraph's checkpointer creates
  its own tables at startup.
- `tenant_id` is on every chunk: one database could hold several clinics.

**10. Migrations** (`migrate.py:20`): a `schema_migrations` table records applied files;
each new `NNN_*.sql` runs once, in its own transaction, in name order. The Docker container runs
it on every start, so a deploy brings Neon's schema up to date.

### Why it's built this way
- **Heading chunks beat fixed windows in every search mode** (dev recall@5 / MRR, hybrid
  0.76 / 0.78 vs 0.74 / 0.72; dense 0.88 / 0.80 vs 0.79 / 0.71; `docs/results.md` §1). A
  section is one topic, which is what a patient question is about.
- **One chunk per CSV row** gives exact citations (`DENT-024#REF-03`) and lets reference
  expansion fetch a row by its code (Lesson 3).
- **Measured and rejected:** one chunk per row for every large table (e.g. the tariff) looked
  right but lowered MRR (0.78 → 0.68) without fixing the target cases (§1b). It was removed in
  the "removed over-built code" commit (`audit.md` timeline).
- **Postgres for everything** (chunks, vectors, full text, chats, leads) instead of a separate
  vector database: one service to run (Neon's free tier), real transactions, and hybrid search
  in a single SQL query.
- **Raw SQL migrations instead of Alembic/an ORM**: one schema file, readable as is, and the
  search query needs pgvector and `tsvector` SQL anyway.

### Limits and gotchas
- The PDF heading rules (font size, bold) fit these documents; PDFs from another source would
  need re-tuning. Scanned PDFs (images) would need OCR.
- `kb_version` hashes file bytes, so Windows (CRLF) and Linux (LF) line endings give different
  versions for the same content (seen between local and Neon).
- The full tariff table is still split into three ~440-token chunks of 6–9 services each, so a
  single-service price question can match a service page before the tariff (§1, failure
  pattern 1). Weighted RRF (Lesson 3) handles most of this.
- Re-ingesting deletes and rewrites every chunk. That's fine for 415 chunks; a big KB would need
  incremental updates by `content_hash` (the column already exists).

### Interview questions
1. **"How did you choose your chunking strategy?"** By measurement: both chunkers are indexed
   side by side, and the retrieval eval compared them on hand-labelled gold documents. Heading
   chunks won in every mode. Per-row chunking for large tables was tried and rejected because MRR
   dropped.
2. **"How do you handle tables in RAG?"** Render every row as `Header: value; Header: value` so
   it makes sense on its own, never split a chunk in the middle of a row, and repeat the
   section heading at the top of each part. Each CSV row is its own chunk.
3. **"How do you know which KB version an answer or score came from?"** `kb_version`: a hash of
   the manifest, every file's bytes, the chunking settings and the embedding settings, stored on
   every chunk and recorded with every eval run.
4. **"Why Postgres + pgvector instead of Pinecone/Chroma?"** One database for vectors, full-text
   search and app data. Hybrid search is one SQL query, there are transactions, and the free
   hosting tier has pgvector. At 415 chunks a dedicated vector DB adds a service without any
   speed benefit.
5. **"What do you embed: the raw chunk?"** No: title + section path + content, plus an
   "ARCHIVED" flag for superseded documents, so short chunks still carry their context.

### Try it yourself
```bash
docker compose up -d --wait db
uv run python -m bot.db.migrate          # "Schema up to date."
uv run python -m bot.retrieval.ingest    # 36 documents -> {'heading': 234, 'fixed': 181}, embeddings from cache
uv run pytest tests/test_loaders_chunking.py -q
```

---

## Lesson 3 — Hybrid retrieval

### Purpose
Given a patient's question, find the 6 chunks most likely to contain the answer. Two searches
run in **one SQL query**: Postgres full-text search (good at exact words and IDs like
`SRV-005A`) and pgvector similarity (good at meaning: "molar RCT cost" ≈ "root canal price").
Their rankings are merged with **weighted Reciprocal Rank Fusion**, and then rows that the found
chunks refer to by code (`REF-03`, `PKG-002`) are added.

### Files
| File | Role |
|---|---|
| `src/bot/retrieval/hybrid.py` | `HybridRetriever.search()` (one SQL query, three modes) and `expand_references()` |
| `src/bot/retrieval/inspect.py` | Debug CLI: see what each mode returns, with both ranks per hit (`make search Q="…"`) |
| `configs/retrieval.yaml` | `mode`, `candidates_per_list: 50`, `rrf_k: 60`, `lexical_weight: 0.3`, `top_k: 6`, `archived_penalty: 0.85`, `expand_references: 3` |
| `evals/retrieval_eval.py` | recall@5 / MRR against hand-labelled gold documents (Lesson 7) |

### How it works
```mermaid
flowchart LR
    Q[question] --> LQ[lexical_query:<br/>terms joined with OR]
    Q --> EQ[embed_query<br/>Gemini, RETRIEVAL_QUERY]
    LQ --> L[full-text top 50<br/>ts_rank_cd]
    EQ --> D[vector top 50<br/>cosine distance, HNSW]
    L --> F[FULL OUTER JOIN<br/>weighted RRF<br/>× 0.85 if archived]
    D --> F
    F --> T[top 6]
    T --> X[expand_references:<br/>+ up to 3 rows named<br/>by code]
    X --> G[generator]
```
`rag/answer.py:178` calls `search()` (with the rewritten question if there is one, Lesson 5),
then `expand_references()` (`:179-181`).

### Key code

**1. Turning a question into a full-text query** (`hybrid.py:46`)
```python
def lexical_query(text: str) -> str:
    terms = re.findall(r"[\w₹]+(?:[-/][\w]+)*", text)
    return " or ".join(t for t in terms if t.lower() != "or")
```
Postgres's `websearch_to_tsquery` **ANDs** bare words, so "how much does a root canal cost for
a molar" would only match a chunk containing every word, which is almost none. Joining the terms
with OR matches any of them, and `ts_rank_cd` ranks higher the chunks that cover more terms.
The regex keeps `SRV-005A` together as one term, which Postgres then matches as an exact phrase.
Stop words ("how", "a") are dropped by the `english` configuration.

**2. Both searches and the fusion in one SQL statement** (`hybrid.py:57`)
```sql
WITH lexical AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(fts, q) DESC, id) AS rank
    FROM chunks, websearch_to_tsquery('english', %(lex)s) AS q
    WHERE %(use_lex)s AND tenant_id = %(tenant)s AND chunker = %(chunker)s AND fts @@ q
    ORDER BY ts_rank_cd(fts, q) DESC, id  LIMIT %(n)s
),
semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(vec)s::vector, id) AS rank
    FROM chunks
    WHERE %(use_dense)s AND tenant_id = %(tenant)s AND chunker = %(chunker)s
    ORDER BY embedding <=> %(vec)s::vector, id  LIMIT %(n)s
)
SELECT c.*, (%(w_lex)s * coalesce(1.0 / (%(k)s + l.rank), 0.0)
                       + coalesce(1.0 / (%(k)s + s.rank), 0.0))
            * CASE WHEN c.status = 'archived' THEN %(penalty)s ELSE 1.0 END AS score, ...
FROM lexical l FULL OUTER JOIN semantic s USING (id)
JOIN chunks c ON c.id = coalesce(l.id, s.id)
ORDER BY score DESC, c.id  LIMIT %(top_k)s
```
- Each list keeps only a **rank** (1, 2, 3…), not its raw score, because a `ts_rank_cd` value
  and a cosine distance aren't on the same scale and can't be added.
- **RRF**: `score = 0.3 × 1/(60 + rank_fulltext) + 1/(60 + rank_vector)`. A chunk missing from a
  list gets 0 from it (`coalesce`). **FULL OUTER JOIN** keeps chunks found by only one side.
- `<=>` is pgvector's cosine-distance operator; the HNSW index from Lesson 2 makes it fast.
- `, id` in every `ORDER BY` breaks ties the same way every time, so results are deterministic.
- `use_lex` / `use_dense` flags let the **same query** run hybrid, full-text-only or
  vector-only (`search(mode=…)`), which is how the three modes were compared fairly.

What the numbers look like (k = 60, full-text weight 0.3):

| Where the chunk ranks | Score |
|---|---|
| #1 in both lists | 0.3/61 + 1/61 = **0.0213** |
| #1 in the vector list only | 1/61 = 0.0164 |
| #1 in the full-text list only | 0.3/61 = 0.0049 |
| #50 in the vector list only | 1/110 = 0.0091 |

`k = 60` is the standard value from the original RRF paper: it flattens the curve so that rank 1
vs rank 3 isn't a huge jump.

**3. pgvector settings for filtered search** (`hybrid.py:129-132`)
```python
conn.execute("SET LOCAL hnsw.ef_search = 100")
conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
```
HNSW finds nearest neighbours first and applies the `WHERE` filter (tenant, chunker) after. With
two chunkers in one table, about half the neighbours get filtered out, so the query could return
fewer than 50 rows. Iterative scan (pgvector ≥ 0.8) keeps searching until enough rows pass.
`SET LOCAL` applies to this transaction only.

**4. Reference expansion: one hop** (`hybrid.py:136`)
```python
ROW_CODE = re.compile(r"\b(?:REF|PKG|PAY|WLK|EQP)-[A-Z0-9]+\b")
...
have = {h.doc_id.split("#", 1)[-1] for h in hits}
for h in hits:
    for code in ROW_CODE.findall(h.content):
        if code not in have and code not in wanted:
            wanted.append(code)
# SELECT ... WHERE split_part(doc_id, '#', 2) = ANY(wanted)
extra = [Hit(*by_code[c], 0.0, None, None) for c in wanted if c in by_code][:limit]
```
The KB refers to itself: the package row `PKG-002` says "refund policy code: REF-03; REF-04".
A question about cancelling a package retrieves `PKG-002` but not necessarily `REF-03`. This step
fetches up to 3 such rows directly by their citation id (CSV rows have ids like
`DENT-024#REF-03` from Lesson 2). They're added **after** the top 6 with score 0, so they never
push out a searched chunk. Only one hop: rows named by the added rows aren't followed.

### Real example: why hybrid
`uv run python -m bot.retrieval.inspect "SRV-005A" --compare --k 3`:
```
hybrid:  1. DENT-014#intro                     lex=7  dense=3
         2. DENT-023#PKG-002                   lex=14 dense=5
         3. DENT-014#2-endodontic-…-identifier lex=1  dense=14
lexical: 1. DENT-014#2-endodontic-…-identifier          (the exact-ID section)
dense:   1. DENT-005#EQP-005   ← an X-ray sensor: a similar-looking code, the wrong thing
```
Embeddings treat `SRV-005A` as a vague "code-like" string, so vector search put an equipment row
first. Full-text matched the ID exactly. Hybrid keeps the right document (DENT-014) at #1 and
still ranks the exact section in the top 3.

### Why it's built this way
Measured on the dev split (recall@5 / MRR, `docs/results.md` §1, §1b):

| Setup | recall@5 / MRR |
|---|---|
| Equal-weight RRF (weight 1.0) | 0.76 / 0.78 |
| Weight 0.5 | 0.86 / 0.78 |
| **Weight 0.3 (adopted)** | **0.88 / 0.79** → 0.93 / 0.82 after the gold labels were cleaned up |
| Weight 0.15 | 0.90 / 0.80 |
| Vector only | 0.88 / 0.80 (0.95 / 0.83 later), **but loses exact IDs** |

Re-run today on the current golden set (35 scored dev cases, `make eval-retrieval`): hybrid
**0.91 / 0.84**, vector only 0.92 / 0.84, full-text only 0.80 / 0.72; with fixed chunks, hybrid
0.86 / 0.79. Vector-only is still level on these averages; the exact-ID example above is why
hybrid is used anyway.

- **Equal weights hurt.** Common words ("price", "treatment") match many chunks, and in the
  full-text list they pushed correct documents down to #12–#15 even when vector search had them
  at #1–#2. Lowering the full-text weight fixed that while keeping exact IDs at rank 1.
- **0.3, not 0.15:** 0.15 scored slightly higher but on only 21 cases; 0.3 keeps full-text
  meaningful and avoids fitting the dev set too closely.
- **Why not vector-only, if it scores 0.95?** Because doc-level recall@5 is lenient: it hides
  that vector search ranks the wrong item first for exact IDs, and in a clinic bot "SRV-005A"
  vs "EQP-005" is a wrong price or a wrong service.
- **RRF instead of mixing raw scores:** no score normalisation and no per-query calibration.
  It only uses ranks.
- **Reference expansion instead of multi-hop agents:** one deterministic SQL lookup, no extra
  LLM call. It was added for multi-rule questions (q-016 brings in REF-03 through the PKG-002
  row, §2c).
- **Archived documents stay indexed** (with a penalty and an `[ARCHIVED]` flag) so the bot can
  say "that was the 2025 price; the current one is…" instead of not knowing.

### Limits and gotchas
- **The archived penalty can bury the old price list even when asked for it.** For "What was
  the price of a root canal in 2025?", DENT-020 is **#1 in both lists** (0.0213) but × 0.85 =
  0.0181 puts it at **#9**, outside the top 6. RRF scores are squeezed into a narrow range
  (~0.015–0.021 across the top 10), so even a 15 % penalty moves a chunk many places. The config
  comment calls 0.85 "a starting point"; no golden case tests 2025 prices yet.
- `ts_rank_cd` is not BM25 (no proper term-frequency saturation or length normalisation).
  Good enough with a 0.3 weight; a real BM25 (e.g. ParadeDB) would be the upgrade.
- `abstention.min_top_score` is 0.0, so it's off: a fixed score threshold didn't separate
  answerable from unanswerable questions. Abstaining is the generator's decision, based on the
  sources it got (Lesson 4).
- Every search opens a new database connection; fine at this traffic, a pool would be next.
- Search takes ~2.4 s, mostly the Gemini embedding call for the question.

### Interview questions
1. **"Why hybrid search?"** Vector search understands meaning but misses exact IDs; full-text
   finds exact words but drowns in common words. Measured: vector-only ranked an equipment code
   first for "SRV-005A"; equal-weight hybrid dropped recall to 0.76; weighted hybrid kept exact
   IDs at #1 and reached 0.93 recall@5.
2. **"What is RRF and why use it?"** Reciprocal Rank Fusion: score = Σ weight / (k + rank). It
   merges ranked lists using only positions, so you never have to make a cosine distance and a
   text-rank score comparable. k = 60 smooths the gap between neighbouring ranks.
3. **"How did you pick 0.3?"** A sweep on the dev split (1.0, 0.7, 0.5, 0.3, 0.15) measuring
   recall@5 and MRR. 0.15 was marginally better on 21 cases; 0.3 was chosen to avoid overfitting.
   The sweep is recorded in the YAML comment and in `results.md`.
4. **"How do you handle documents that refer to other documents?"** One-hop reference expansion:
   find row codes in the retrieved text, fetch those rows by id, add up to 3 as extra sources
   after the ranked results.
5. **"What's a gotcha with filtered HNSW queries?"** The filter is applied after the
   approximate search, so you can get fewer rows than `LIMIT`. Fix: a higher `ef_search` and
   pgvector's iterative scan.

### Try it yourself
```bash
uv run python -m bot.retrieval.inspect "SRV-005A" --compare --k 3     # hybrid vs full-text vs vector
uv run python -m bot.retrieval.inspect "What was the price of a root canal in 2025?" --k 10
make eval-retrieval                                                 # recall@5 / MRR per mode
```

---

## Lesson 4 — Grounded answering + self-verification

### Purpose
This is the heart of the bot. Given the retrieved chunks, the generator writes a JSON reply with
`[S1]`-style citations and picks a **behaviour** (answer, abstain, clarify, refuse, handoff).
Then the reply is **checked before anyone sees it**: first by code (every ₹ amount and service
code must be in the cited source), then by a second LLM (is every cited sentence supported,
conditions included?). If the check fails, the generator gets one repair attempt with the exact
problems listed. If that fails too, the patient gets a safe fixed reply instead.

### Files
| File | Role |
|---|---|
| `src/bot/rag/answer.py` | `Answerer.answer()`: retrieve → generate → verify → repair → fallback; maps `[S#]` to documents |
| `src/bot/rag/verify.py` | Splits a reply into cited claims; the code-level ₹ / ID check; the LLM claim check |
| `src/bot/rag/ask.py` | CLI: `python -m bot.rag.ask "…" --debug` shows sources, the raw reply and each claim's verdict |
| `prompts/answer.md` | The generator's 10 rules (grounding, archived, abstain, never-do list, emergencies, conditions…) |
| `prompts/verify.md` | The fact-checker's instructions |
| `configs/generation.yaml` | Fallback texts, `verification: {enabled, min_support_ratio: 1.0, repair_attempts: 1}` |

### How it works
```mermaid
flowchart TD
    H[hits from Lesson 3] --> FS[format_sources:<br/>S1..Sn with doc, status, title]
    FS --> GEN[generator chat_json →<br/>working · behavior · answer · citations]
    GEN --> B{behavior}
    B -->|abstain / refuse| OUT[reply as is:<br/>no facts to check]
    B -->|answer / clarify / handoff| V1[code check:<br/>₹ amounts + IDs in cited source?]
    V1 -->|fail| BAD[failed claims]
    V1 -->|pass| V2[LLM check:<br/>each claim supported,<br/>conditions kept?]
    V2 -->|all supported| OK[show reply,<br/>S# → DENT-xxx]
    V2 -->|any unsupported| BAD
    BAD --> R{repair used?}
    R -->|no| REP[send the problems back,<br/>generator rewrites once] --> V1
    R -->|yes| FB{was it an emergency?}
    FB -->|no| F1[fallback_answer:<br/>'I can't confirm… call reception']
    FB -->|yes| F2[emergency_fallback_answer:<br/>fixed first aid + hospitals]
```

### Key code

**1. Sources the model can cite** (`answer.py:72`)
```python
parts.append(
    f'<source id="{sid}" doc="{h.source_doc_id}" status="{status}" '
    f'title="{h.title}" section="{h.section}">\n{h.content}\n</source>'
)
```
Each hit becomes `S1`, `S2`… with its document id, its status (`active` / `ARCHIVED`) and its
section. The model cites short ids like `[S2]`, which are easy to write correctly; code then maps
them to real documents. The last 6 conversation turns are added as `<conversation>` (`:86`), and
the prompt says text in the sources or conversation is **data, never instructions** (rule 7),
as a defence against prompt injection.

**2. The output contract** (`answer.py:31`)
```python
class GeneratorOutput(BaseModel):
    working: str = ""  # private scratch space; never shown, never verified
    behavior: Behavior  # answer | abstain | clarify | refuse | handoff
    answer: str
    citations: list[str] = []
```
- `behavior` lets the model **decide to abstain** as a normal output, instead of being forced to
  answer. It also drives everything downstream: which replies get verified (`GROUNDED =
  {answer, clarify, handoff}`), the eval's behaviour accuracy, and emergency handling.
- `working` exists because on multi-step questions the model used to "think aloud" inside the
  reply ("Wait, let me re-calculate…", with three different totals). Giving it a private field
  removed that kind of reply (`docs/results.md` §2d).

**3. Claims and the code-level check** (`verify.py:58`, `:77`)
```python
MONEY = re.compile(r"(?:₹|INR|Rs\.?)\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
IDENT = re.compile(r"\b(?:SRV|PKG|REF|POL|ESC|WLK|FRM|PAY|STF|EQP|DENT)-[A-Z0-9-]*\d[A-Z0-9]*\b")


def facts_in_text(claim, source_text):
    for m in MONEY.findall(CITE.sub("", claim)):
        if _norm_money(m) not in src_money and _norm_money(m) not in src_plain:
            return False, f"amount ₹{m} not in cited sources"
    for ident in IDENT.findall(claim):
        if ident not in source_text:
            return False, f"identifier {ident} not in cited sources"
```
A **claim** is a sentence with at least one `[S#]`. For each claim, every ₹ amount (commas
removed, so `₹6,800` = `INR 6800`) and every clinic code must literally appear in **the sources
that claim cites**, not just anywhere in the sources. Real output of this check:
```
"A molar root canal costs ₹6,800 [S1]."   → supported
"A molar root canal costs ₹5,900 [S1]."   → amount ₹5,900 not in cited sources
"The code is SRV-005B [S1]."              → identifier SRV-005B not in cited sources
"It costs ₹6,800 [S2]."                   → cites unknown source S2
```
This costs nothing (no LLM call) and catches the most harmful error for a clinic, a wrong
price, with certainty. Claims that fail it never reach the LLM check.

**4. The LLM claim check** (`verify.py:140`, `:158`)
All claims that passed the code check go to the `utility` model **in one call**, each with only
the text of the sources it cites. The prompt (`prompts/verify.md`) says a claim is unsupported if
it adds a fact **or drops, weakens or reverses a condition** ("free only if cancelled 24 hours
ahead" is not "free to cancel"), but that rewording which keeps the meaning is fine. The
patient's question is passed too, so facts the patient stated ("my crown is already done") count
as given. Two robustness details:
- If the verifier skips a claim, it's asked again for just the missing ones (`:140`), so a
  dropped result isn't counted as "unsupported".
- If it numbers claims from 0 but the count matches, results are matched by position (`:176`).

**5. Generate → verify → repair → fallback** (`answer.py:211-244`)
```python
v, failure = self._verify(out, source_texts, question)
for _ in range(vcfg.get("repair_attempts", 0)):  # 1
    if failure is None:
        break
    messages = [
        *messages,
        Message("assistant", out.model_dump_json()),
        Message("user", self._repair_feedback(v)),
    ]
    out, resp = self.generator.chat_json(self.system_prompt, messages, GeneratorOutput)
    ...
    if out.behavior not in GROUNDED:  # the repair chose to abstain: allowed
        if emergency:
            failure = "repair dropped the emergency handoff"  # ...unless it was an emergency
        break
    v, failure = self._verify(out, source_texts, question)
if failure:
    self._fallback(result, failure, emergency=emergency)
```
`_repair_feedback` (`:157`) shows the generator the exact failing sentences and reasons, e.g.
`"…₹9,500…" -> amount ₹9,500 not in cited sources`. `min_support_ratio: 1.0` means **one**
unsupported claim fails the whole reply: for a clinic, a reply that's 90 % right isn't good
enough.

**6. Emergencies never get the generic fallback** (`answer.py:120`, `:214`, `:232`)
```python
if emergency or result.behavior == "handoff":
    result.answer = self.cfg["emergency_fallback_answer"].strip()  # fixed first aid + hospitals
else:
    result.behavior = "abstain"
    result.answer = self.cfg["fallback_answer"].strip()  # "call reception"
```
`emergency` is **sticky**: if any attempt was a handoff, a failed reply gets the fixed emergency
text (written from DENT-035/002/036), and a repair can't turn an emergency into an abstention.
This was a real regression: an early repair turned a knocked-out-tooth reply into a normal
reply, which then fell back to "call reception during office hours" (§2c). Tests guard it
now (`tests/test_rag.py:118`, `:199`, `:221`).

**7. Showing the reply** (`answer.py:133`): `[S2]` becomes `[DENT-007]` for the patient, and
`citations` stores the precise chunk ids for logs and evals. `confidence` = share of claims
supported (`:67`); after a passed check it's always 1.00.

### Real example
`uv run python -m bot.rag.ask "Can I cancel my cleaning appointment for free?" --debug`:
```
[clarify] You can cancel your cleaning appointment for free if you do so at least 24 hours before
the scheduled start time [DENT-007]. If you cancel between 4 and 24 hours before the appointment,
a ₹300 late cancellation fee applies [DENT-007]. ... ₹500 no-show fee ... [DENT-007]
confidence=1.00  latency=8065 ms  {'retrieve_ms': 1945, 'generate_ms': 1795, 'verify_ms': 4324}

--- verification ---
OK ['S2','S4'] You can cancel ... for free if you do so at least 24 hours before ...
OK ['S4','S5'] If you cancel between 4 and 24 hours ... a ₹300 late cancellation fee applies
OK ['S3','S4'] If you cancel less than 4 hours ... ₹500 slot-hold deposit is forfeited ...
```
The condition ("only if 24 hours ahead") is kept, and every amount was checked against its cited
sections. Verification was the slowest step here (4.3 s for three claims).

### Why it's built this way
- **Code before LLM.** The checks that matter most (prices, codes) are exact and free. The LLM
  only judges what code can't (meaning, conditions). Cheapest first, and the strict one can't be
  argued with.
- **Verify, then show** (Lesson 6 streams only after this step). A reply that fails is never
  visible, not even for a second.
- **One repair, then fallback.** Each extra attempt costs a generator call and latency. Measured
  on the dev set: 0–1 repairs per run and 0 fallbacks (§2c, §4), and the repair fixed the q-020
  condition error that the first verifier missed.
- **Results:** faithfulness 1.00 and citation accuracy 1.00 on all 53 golden cases (113
  claims), abstention precision / recall 1.00 / 1.00, safety 1.00 (`evals/baseline.md`).
- **No computed totals** (rule 9, and the ₹ check enforces it: a sum isn't in any source). An
  arithmetic-aware checker was built for one case (q-016), produced a citation loophole, and
  was removed (§3a). The bot quotes each published amount and lets reception confirm the total;
  q-016 stays a known limitation.
- **Different models for writing and checking** (Qwen generates, gpt-oss-20b verifies), so the
  checker doesn't share the writer's blind spots as easily.

### Limits and gotchas
- **An uncited price sentence slips through if other sentences are cited.** `split_claims`
  reports sentences with a ₹ or a code but no citation as `uncited`, but `_verify` only fails
  a reply with **zero** claims (`answer.py:150`); `uncited` is shown in `--debug` and never
  checked. So "Cleaning costs ₹1,800 [S1]. A crown is ₹9,999." would pass the code check. The
  prompt requires citations on every fact, and no eval run has shown this, but code doesn't
  enforce it. A one-line fix (fail when `uncited` is non-empty) would need its own measured
  change, since answer tuning is frozen.
- The 20B verifier can miss subtle condition errors (it passed q-020's "may attend alone" once,
  §2b); the condition rule in `verify.md` plus the repair loop fixed that case, not every case.
- Verification adds 1–4 s and one utility call per grounded reply.
- Duplicate badges: when two cited sections come from the same document, the patient sees
  `[DENT-007][DENT-007]` (cosmetic, visible in the example above).
- Sentence splitting is a regex, so unusual punctuation can merge or split claims.

### Interview questions
1. **"How do you prevent hallucinations?"** Answer only from retrieved sources, with a citation
   per fact; code-check every ₹ amount and service code against the cited source; LLM-check every
   cited claim, conditions included; one repair with the exact problems; otherwise a safe fixed
   reply. Measured faithfulness 1.00 on 53 cases.
2. **"Why not trust the LLM verifier alone?"** It's probabilistic and can be talked round by
   fluent wording. A regex price check is exact, free and catches the most costly error. The LLM
   handles what regex can't: meaning and conditions.
3. **"What happens when verification fails during an emergency?"** The reply is replaced with a
   fixed, KB-sourced emergency message (first aid, 112/108, hospitals, after-hours line). It
   can't be downgraded to "call reception", and tests guard this.
4. **"How does the model say 'I don't know'?"** `behavior: abstain` is a normal, valid output,
   required by the prompt when sources don't contain the answer. Abstentions are logged as review
   candidates (Lesson 6). Measured abstention recall 1.00.
5. **"Why a private `working` field?"** It lets the model reason before answering without
   putting its reasoning in the reply; without it, self-corrections leaked into replies.

### Try it yourself
```bash
uv run python -m bot.rag.ask "Can I cancel my cleaning appointment for free?" --debug
uv run python -m bot.rag.ask "My tooth got knocked out playing cricket" --debug     # handoff path
uv run pytest tests/test_rag.py -q
```

---

## Lesson 5 — Conversation flow (LangGraph)

### Purpose
Lesson 4 answers **one question**. A real chat is more than that: greetings, follow-ups like
"how long does it last?", "I'd like to book", "let me talk to a person", and emergencies in the
middle of any of these. This layer is a **LangGraph state machine** that decides, for each
message, which path it takes, and it remembers each conversation in Postgres (by `thread_id`)
between messages and server restarts.

### Files
| File | Role |
|---|---|
| `src/bot/graph/state.py` | `ChatState`: the transcript, per-turn fields and fields that persist (lead, staff mode) |
| `src/bot/graph/nodes.py` | The 8 nodes (`classify`, `rewrite`, `support`, `lead`, `handoff`, `staff`, `greet`, `refuse`) and the two routing functions |
| `src/bot/graph/builder.py` | Wires nodes and edges, `new_turn()`, and the Postgres checkpointer |
| `src/bot/graph/chat.py` | Terminal chat over the full graph (`make chat`, `--debug` shows intent and rewritten query) |
| `src/bot/leads/store.py` | `save_lead()` (refuses without consent, one row per chat) and `delete_lead()` |
| `prompts/classify.md`, `rewrite.md`, `lead_extract.md` | Instructions for the three `utility`-model steps |

### How it works
```mermaid
flowchart TD
    S([new message]) --> C[classify<br/>intent + emergency flag]
    C --> R{route}
    R -->|support| RW[rewrite follow-up] --> SUP[support:<br/>Lesson 4 pipeline]
    R -->|lead| L[lead:<br/>slots + consent]
    R -->|handoff| H[handoff:<br/>staff take over]
    R -->|greeting| G[greet: fixed text]
    R -->|out_of_scope| RF[refuse: fixed text]
    R -->|staff hold the chat| ST[staff: bot stays silent]
    SUP --> A{after_support}
    A -->|asked for a person<br/>+ emergency| H
    A -->|otherwise| E([END])
    L --> E
    H --> E
    G --> E
    RF --> E
    ST --> E
```
The order of the checks in `route` (`nodes.py:102`) **is** the policy:

| # | Condition | Goes to | Why |
|---|---|---|---|
| 1 | Staff hold the chat **and** red-flag symptom | `rewrite` → `support` | An emergency can't wait for a slow human |
| 2 | Staff hold the chat | `staff` | The bot stays quiet; the message waits for staff |
| 3 | Asks for a person **and** red-flag symptom | `rewrite` → `support`, then `handoff` | First aid now, then a person |
| 4 | In the middle of a lead and the message is support / lead / greeting | `lead` | Don't lose the booking; `lead` still answers questions |
| 5 | Otherwise, by intent | `rewrite`, `lead`, `handoff`, `greet`, `refuse` | |

### Key code

**1. State: what's remembered** (`state.py:45`)
```python
class ChatState(TypedDict, total=False):
    messages: Annotated[list[Turn], operator.add]   # transcript: nodes return new turns, they're appended
    question: str        # latest message           ┐
    standalone: str      # rewritten for search      │ reset every turn by new_turn()
    intent: Intent                                   │
    reply: Reply         # answer + grounding data   │
    emergency: bool                                  ┘
    lead: Lead                       # need, timeline, budget; name/phone/email only after consent
    lead_stage: LeadStage | None     # collecting → awaiting_consent → saved | declined
    handoff_pending: bool            # staff hold the chat
    staff_waiting_since: float | None
```
`Annotated[..., operator.add]` is a LangGraph **reducer**: when a node returns
`{"messages": [new_turn]}`, LangGraph appends instead of replacing. `new_turn()`
(`builder.py:54`) resets the per-turn fields; lead and staff fields carry over.

**2. Wiring** (`builder.py:31`)
```python
g.add_edge(START, "classify")
g.add_conditional_edges(
    "classify", n.route, ["rewrite", "lead", "handoff", "greet", "refuse", "staff"]
)
g.add_edge("rewrite", "support")
g.add_conditional_edges("support", n.after_support, ["handoff", END])
for leaf in ("lead", "handoff", "greet", "refuse", "staff"):
    g.add_edge(leaf, END)
return g.compile(checkpointer=checkpointer)
```
`route` and `after_support` are plain functions of the state: no LLM call, easy to unit-test.

**3. Memory: the Postgres checkpointer** (`builder.py:68`)
```python
with PostgresSaver.from_conn_string(database_url) as cp:
    cp.setup()  # creates LangGraph's own tables once
    yield cp
...
graph.invoke(new_turn(thread_id, text), {"configurable": {"thread_id": thread_id}})
```
After every node, LangGraph saves the state under the `thread_id`. The next message loads it,
so a chat survives restarts and Render's sleep, and threads never see each other's data
(tests: `test_threads_are_isolated`, `test_postgres_checkpointer_persists_threads_across_graph_instances`).

**4. classify: intent + emergency in one call** (`nodes.py:91`)
```python
class _Intent(BaseModel):
    intent: Intent  # support | lead | handoff | greeting | out_of_scope
    emergency: bool = False
    reason: str = ""
```
The prompt defines each intent with examples. Rude or "ignore your instructions" messages about
the clinic stay `support` (the answer prompt handles them safely), and an explicit request for a
person is `handoff` even with a question attached. The emergency flag is separate from the
intent, so "my face is swelling, get me a doctor" is `handoff` **and** an emergency → rule 3.
Validators make the labels forgiving (`"Out of scope"` → `out_of_scope`, `"yes"` → `True`).

**5. rewrite: follow-ups into full questions** (`nodes.py:132`)
```python
if not history:  # first message: nothing to resolve, no LLM call
    return {"standalone": state["question"]}
parsed, _ = self.utility.chat_json(load_prompt("rewrite"), [...], _Standalone)
return {"standalone": parsed.standalone.strip() or state["question"]}
```
The rewritten question is used **only for search** (`support` passes it as `query=`); the
generator still sees the patient's own words plus the last 6 turns. The prompt: resolve
references, keep every detail, don't answer, don't add facts.

**6. lead: slot-filling with a consent gate** (`nodes.py:233`)
Every lead turn runs one extraction call (`_LeadExtract`: need, timeline, budget, name, phone,
email, `consent: yes|no|unclear`, and any `question` the message also asks). Then:
```python
consented = stage == "awaiting_consent" and ex.consent == "yes"
if consented:
    for k in ("name", "phone", "email"):
        if getattr(ex, k):
            slots[k] = getattr(ex, k)  # personal data is kept ONLY here
```
```mermaid
stateDiagram-v2
    [*] --> collecting: booking intent
    collecting --> collecting: no need known yet ("Which treatment?")
    collecting --> awaiting_consent: need known → show consent_text
    awaiting_consent --> saved: yes + name + phone → save_lead()
    awaiting_consent --> awaiting_consent: yes but phone missing / unclear → ask again
    awaiting_consent --> declined: no → drop name/phone/email
    collecting --> declined: no
```
- **Consent is a state, not a guess.** Details are kept only if the consent text has already
  been shown (`awaiting_consent`) **and** the reply is a yes. A name typed before the consent
  text is not kept; once the patient says yes, details are re-read from the conversation, so the
  stored `consent_text` is exactly what they saw (test: `test_details_volunteered_before_consent_text_are_not_kept`).
- **Help first:** if the message also asks a question ("…and how much is it?"), the full
  verified answer pipeline answers it in the same reply (`:262`).
- **Phone required, email optional** (`:277`); both are joined into `contact`.
- **"No" wipes** any personal fields from the state (`:267`).
- The defence goes deeper than the node: `save_lead()` itself raises `ConsentRequired` if called
  without `consented=True` (`store.py:33`), and `thread_id UNIQUE` + `ON CONFLICT DO UPDATE`
  makes a repeated save update the same row instead of creating a duplicate.

**7. handoff and staff** (`nodes.py:203`, `:224`)
- `handoff` sets `handoff_pending = True` and starts the wait timer, but **only if a staff
  member has the staff page open** (`staff_online`, set by the API). Otherwise the patient gets
  "Nobody from our team is online right now, call reception…" at once, instead of waiting for
  nobody.
- If the bot answered earlier in the same turn (rule 3), the acknowledgement is **added to**
  that answer (`:215-217`), so the first aid isn't lost.
- `staff` returns an empty reply: the patient's message is saved for staff and the bot says
  nothing. Staff replies, ending the chat and timeouts change the state from **outside** the
  graph with `graph.update_state` (`src/bot/api/staff.py:90`, Lesson 6).

**8. greet and refuse** are fixed texts with no LLM call: a greeting costs one classify call
only (~1 s in production).

### Real example
A 3-turn chat through the full graph (fresh thread):
```
you> How much is teeth whitening?
  intent=support  standalone='How much is teeth whitening?'  behavior=answer
  bot> In-Office LED Power Whitening (SRV-009): ₹8,500 + 18% GST … [DENT-018][DENT-019] …
you> How long does it last?
  intent=support  standalone='How long does teeth whitening last?'  behavior=abstain
  bot> I don't have specific information on how long the whitening results last. … reception …
you> I'd like to book it
  intent=lead  behavior=answer  lead_stage=awaiting_consent
  bot> Would you like our team to contact you about this? If yes, please reply with your name and
       phone number (an email address is optional). … Also, roughly when would you like to come in?
```
Turn 2: the rewriter turned "it" into "teeth whitening", and the bot **abstained** instead of
guessing, because the retrieved sources didn't state how long results last. Turn 3: a booking
intent with the need already known goes straight to the consent text; nothing personal is stored
yet.

### Why it's built this way
- **A graph instead of one big prompt.** Routing, consent and staff mode are rules that must
  always hold. As code (`route`, the `lead` stages) they're deterministic and unit-tested (24
  tests in `tests/test_graph.py`). One mega-prompt that "also handles booking and handoffs"
  would make them probabilistic.
- **LangGraph** gives typed state, conditional edges, reducers and a ready Postgres
  checkpointer, so multi-turn memory and resumable chats needed no custom code.
- **The rewriter matters on real follow-ups.** On 4 follow-ups that drop the topic ("And for
  kids?", "Is it painful?"), the raw message found the right document first **0 / 4** times;
  the rewritten question **4 / 4** (`docs/results.md` §4e).
- **Consent reading is cautious.** On 9 hesitant replies to the consent text ("maybe later",
  "why do you need my number?", "is my data safe?"), **0** were read as yes; 4 / 4 clear
  agreements were (§4e). "unclear" means ask again and keep nothing.
- **Small model for routing.** classify, rewrite and lead extraction use gpt-oss-20b with low
  reasoning effort: fast and cheap, and the expensive generator only runs when there's a
  question to answer.

### Limits and gotchas
- **A misclassification sends the message down the wrong path**; there is no second opinion.
  The prompts are deliberately broad (anything about the clinic is `support`), and the answer
  pipeline still refuses or abstains safely.
- **Every message costs a classify call**, and follow-ups an extra rewrite call: 2–4 utility
  calls plus 1–2 generator calls per answered question.
- The eval gate uses **hand-written** standalone questions for follow-ups, so a worse rewriter
  wouldn't fail CI; the spot check above is a separate, manual measurement.
- The rewriter can leave a reference unresolved (q-045: "them" kept → right document at rank 3
  instead of 1, §4e).
- Staff mode (`handoff_pending`) lives in the checkpointed state, but **who is online** lives in
  server memory (Lesson 6), so it assumes one server instance.
- After a lead is saved, asking to book again starts a new enquiry on the same thread and
  updates the same lead row (one lead per chat).

### Interview questions
1. **"Why LangGraph?"** The conversation has branches (support, lead, handoff, staff mode) and
   must remember state across messages. LangGraph gives typed state, deterministic routing
   functions and a Postgres checkpointer for persistence. The rules are code, not prompt text.
2. **"How do you handle follow-up questions?"** A rewrite step turns the message plus the
   recent conversation into a standalone question used only for search. Measured: on
   topic-dropping follow-ups, raw message 0 / 4 vs rewritten 4 / 4 right document first.
3. **"How do you make sure you never store personal data without consent?"** Three layers: the
   graph keeps name/phone/email only in the `awaiting_consent` stage after a clear yes;
   `save_lead` refuses without `consented=True`; the stored row carries `consent_at` and the
   exact `consent_text` shown. Hesitant replies are read as "unclear" (0 / 9 false yes).
4. **"What if someone asks for a human during an emergency?"** The bot answers first with
   verified first aid and where to go, then hands off; while staff hold a chat, red-flag
   messages still get the bot's answer at once.
5. **"How is conversation memory stored?"** LangGraph's `PostgresSaver` checkpoints the state
   after each node, keyed by `thread_id`; the transcript uses an append reducer.

### Try it yourself
```bash
make chat     # or: uv run python -m bot.graph.chat --debug
# try: "How much is teeth whitening?" → "How long does it last?" → "I'd like to book it"
uv run pytest tests/test_graph.py -q
```

---

## Lesson 6 — API & product

### Purpose
This turns the graph into a product people can use: a **FastAPI** service with a chat page for
patients and a staff page for the clinic. It streams each reply **only after it was verified**,
logs every turn with phone numbers and emails masked, flags bad turns for review, runs live
staff chats (presence, timeouts, ending), lists leads, and limits how many messages a visitor
can send so one person can't use up the free LLM quota.

### Files
| File | Role |
|---|---|
| `src/bot/api/main.py` | `create_app()`: all endpoints, the admin-key check, the turn runner, SSE streaming |
| `src/bot/api/schemas.py` | Pydantic request/response models with length and pattern limits |
| `src/bot/api/turns.py` | Saves conversations and messages (redacted), feedback, review candidates; staff-list queries |
| `src/bot/api/staff.py` | `Presence` (who is online, in memory), staff `reply` / `end` / `expire` via `graph.update_state` |
| `src/bot/api/ratelimit.py` | `RateLimiter` (per chat per minute/day, per IP per day) and `client_ip()` |
| `src/bot/api/static/index.html` | Patient chat page (plain HTML + JS, no framework) |
| `src/bot/api/static/staff.html` | Staff page: live chats + Leads table, one admin-key login |

### Endpoints
| Endpoint | Who | What |
|---|---|---|
| `POST /v1/chat` | patient | One message → SSE stream (`status` → `answer` chunks → `done`), or JSON with `?stream=false` |
| `GET /v1/chat/{id}/staff-replies` | patient page, every 5 s while staff hold the chat | New staff messages + presence ping |
| `GET /v1/chat/{id}/history` | patient page on reload | The conversation so far (redacted) |
| `POST /v1/feedback` | patient | 👍 / 👎 (+ comment); 👎 creates a review candidate |
| `GET /v1/handoffs` | staff, every 5 s | Open staff chats with patient online/away/left; marks staff online |
| `POST /v1/handoff/{id}/reply`, `/end` | staff | Send a message / give the chat back to the bot |
| `POST /v1/staff/offline` | staff page on lock/close | Staff go offline |
| `GET /v1/leads`, `DELETE /v1/leads/{id}` | staff | Leads table; hard delete (privacy request) |
| `POST /v1/ingest` | admin | Re-index the KB |
| `GET /v1/health` | anyone | DB status, chunk count, `kb_version` |
| `GET /`, `GET /staff` | browsers | The two pages, with clinic facts filled in from config |

### How one chat request flows
```mermaid
sequenceDiagram
    participant P as Chat page
    participant A as FastAPI /v1/chat
    participant G as LangGraph (thread pool)
    participant DB as Postgres
    P->>A: POST {message, thread_id}
    A-->>P: event: status "thinking"
    A->>A: rate limit check (no LLM if over)
    A->>A: staff.expire (timeouts, lazily)
    A->>G: graph.invoke(new_turn)
    G->>DB: checkpoint state
    G-->>A: verified reply
    A->>DB: log_turn (redacted) + review candidate
    A-->>P: event: answer × n (8 words each)
    A-->>P: event: done {thread_id, message_id, citations, behavior}
```

### Key code

**1. The turn runner: never leave the patient hanging** (`main.py:107`)
```python
limit = request.app.state.limiter.check(thread_id, ip)
if limit:  # before any LLM call
    return ChatOut(..., behavior="abstain", answer=cfg["rate_limit_answer"])
...
try:
    out = await run_in_threadpool(request.app.state.graph.invoke, turn, cfg)
except Exception as e:  # provider outage, quota, DB error
    log.error("chat.failed", thread_id=thread_id, error=type(e).__name__)
    return ChatOut(..., behavior="abstain", answer=cfg["error_answer"])
...
try:
    message_id = await run_in_threadpool(turns.log_turn, thread_id, body.message, out, pending)
except Exception as e:  # logging must not break the reply
    log.error("turn.log_failed", ...)
```
- Three layers of graceful degradation: **over the limit**, **LLM/DB failure** and **logging
  failure** each still give the patient a useful reply. `error_answer` and `rate_limit_answer`
  always include reception's number **and the emergency route**, because at that point the bot
  doesn't know whether the message was an emergency.
- Only the exception **type** is logged, never its message (an error text can contain user
  input or secrets, such as a key echoed back in an HTTP error).
- The graph is synchronous (psycopg, SDK calls), so it runs in a thread pool to keep the async
  server responsive. The checkpointer uses a **connection pool** (`main.py:56`, `max_size=10`)
  so concurrent requests don't share one connection.

**2. Streaming after verification** (`main.py:175`)
```python
async def events():
    yield {"event": "status", "data": json.dumps({"status": "thinking"})}
    result = await _run_turn(request, body)  # includes verification
    words = result.answer.split(" ")
    for i in range(0, len(words), 8):  # verified answer, in small chunks
        yield {"event": "answer", "data": json.dumps({"text": " ".join(words[i : i + 8])})}
    yield {"event": "done", "data": result.model_dump_json(exclude={"answer"})}
```
The status event shows "thinking" at once; the text arrives only after the verifier passed.
This is a deliberate trade: a longer wait for the first word, in exchange for never showing a
sentence that's then withdrawn. The page reads the stream with `fetch` + `getReader()`
(`index.html:329`), not `EventSource`, because `EventSource` only supports GET and this is a
POST with a JSON body. The page writes text with `textContent`, never `innerHTML`, so a reply
can't inject HTML.

**3. Logging with contact details masked** (`turns.py:16-28`)
```python
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s-]{8,}\d(?![\w-])")


def redact(text):
    keep = {_digits(v) for v in _clinic_numbers()}  # the clinic's own numbers stay

    def phone(m):
        return m.group(0) if _digits(m.group(0)) in keep else "[phone]"

    return _PHONE.sub(phone, _EMAIL.sub("[email]", text))
```
Real output:
```
"I am Test Patient, call me on +91 90000 00000 or mail test.patient@example.com"
  → "I am Test Patient, call me on [phone] or mail [email]"
"Reception is +91-20-5550-0140; my number is 9000000000"
  → "Reception is +91-20-5550-0140; my number is [phone]"
```
Every stored message, staff reply, feedback comment and review candidate goes through
`redact`. The **only** place a patient's phone number is stored is the `leads` row, and only
after consent (Lesson 5). Each assistant message also stores its grounding data: retrieved ids,
citations, verifier result (claims, supported, `fallback_reason`, `repaired`), latency, tokens
(`turns.py:67-92`).

**4. Review candidates: the feedback loop starts here** (`turns.py:94-111`, `:161`)
```python
if reply.get("fallback_reason"):
    source = "citation_failed"
elif behavior == "abstain":
    source = "abstained"
elif reply.get("handoff_started"):
    source = "handoff"
```
plus `thumbs_down` from `save_feedback`. Each candidate stores the question, the bot's reply and
the retrieved ids, which is what the review tool (Lesson 9) shows the owner. Golden 0.7.0
added 3 cases from these real conversations.

**5. Staff chats: state changed from outside the graph** (`staff.py:88-127`)
```python
graph.update_state(
    _cfg(thread_id),
    {"messages": [Turn(role="human_agent", content=text)], "staff_waiting_since": None},
    as_node="staff",
)
```
A staff reply isn't a graph run: it's written straight into the checkpointed state, as if the
`staff` node had produced it, and mirrored into `messages` so the patient's page can poll for
it. `end()` sets `handoff_pending = False` and posts a "you're back with the assistant"
message.

**6. Timeouts without a background job** (`staff.py:130`)
`expire()` ends a staff chat if the patient waited 180 s without a staff reply, the patient's
page has been gone 300 s, or every staff page went offline. It's called whenever the patient
writes or polls and whenever staff refresh their list (`main.py:124`, `:202`, `:236`). No
scheduler or worker to run on a free host: the check happens when someone looks.

**7. Presence in memory** (`staff.py:31`)
Each staff page polls every 5 s (with its own session id); staff count as online for 90 s
after the last poll, because browsers slow timers in background tabs. Patients are online
(visible tab polled < 20 s ago), away (hidden tab < 90 s) or left. Visible and hidden tabs are
tracked separately so a duplicated background tab doesn't make an active patient look away.
After a restart, `staff_gone()` returns False until a staff page checks in, so open chats aren't
closed just because the server forgot who was there.

**8. Rate limits** (`ratelimit.py:28`)
```python
for rule, (limit, window) in self.rules.items():  # chat/minute 10, chat/day 60, ip/day 100
    hits = self._hits[keys[rule]]
    while hits and hits[0] <= now - DAY:
        hits.popleft()
    if sum(1 for t in hits if t > now - window) >= limit:
        return rule  # blocked messages are not counted
self._hits[("chat", thread_id)].append(now)
self._hits[("ip", ip)].append(now)
```
A sliding window of timestamps per chat and per IP, behind a lock (requests run in threads).
The per-IP limit stops "open a new chat to reset the counter". Idle keys are dropped once
there are more than 10,000. `client_ip` takes the first `X-Forwarded-For` entry, which Render's
proxy sets.

**9. Admin security** (`main.py:98`)
```python
if expected in DEFAULT_ADMIN_KEYS:  # "" or "change-me"
    raise HTTPException(503, "admin endpoints disabled: ADMIN_API_KEY is not configured")
if not secrets.compare_digest(x_admin_key, expected):
    raise HTTPException(401, "invalid admin key")
```
Admin endpoints are **off** until a real key is set, so a forgotten default can't expose leads.
`compare_digest` takes the same time whether the first or last character is wrong (no timing
attack). The staff page keeps the key in `sessionStorage` (gone when the tab closes).
Patient endpoints use the random `thread_id` as the capability to read that chat;
`THREAD_ID` and `ChatIn` restrict it to `[\w.:-]{1,100}`, and messages to 2,000 characters.

### Real run
Local server, `curl -N -X POST localhost:8011/v1/chat -d '{"message":"hi","thread_id":"guide-demo-1"}'`:
```
event: status   data: {"status": "thinking"}
event: answer   data: {"text": "Hello! I'm the BrightSmile Dental Care assistant. I"}
event: answer   data: {"text": "can help with our services, prices, appointments, insurance,"}
...
event: done     data: {"thread_id":"guide-demo-1","message_id":1868,"intent":"greeting","behavior":"answer",...}
```
`GET /v1/health` → `{"status":"ok","chunks":234,"kb_version":"08a3673838d7"}`; an empty message
→ 422 `string_too_short`; `GET /v1/leads` without a key → 401 `invalid admin key`.

### Why it's built this way
- **FastAPI**: async, Pydantic validation on every input, OpenAPI docs for free, and simple
  dependency-based auth.
- **SSE over WebSockets**: replies go one way (server → page), SSE works over plain HTTP through
  any proxy, and polling covers the rare staff messages. No connection state to manage.
- **Plain HTML pages** served by the same app: no build step, no separate frontend deploy, one
  Docker image.
- **Lazy timeouts and in-memory presence**: right for one free instance; no Redis, no worker.
- **Rate limits in config** (`generation.yaml`): the numbers come from the free quota (~200k
  tokens/day per model ≈ 40 answered questions), so one visitor can use at most a fraction of it.

### Limits and gotchas
- **One instance only.** Presence and rate-limit counters live in process memory; two instances
  would each count separately and disagree on who is online. Scaling out means moving them to
  Postgres or Redis.
- **Rate limits can be dodged** by a script that fakes `X-Forwarded-For` and opens new chats.
  They stop accidental heavy use and simple scripts, not a determined attacker.
- **CORS allows any origin** (`allow_origins=["*"]`), so another site could embed calls to the
  chat API and spend the quota (within the rate limits).
- The first word appears only after the whole pipeline finishes (~5–10 s for a real question,
  ~1 s for a greeting); on the free host a cold start adds 30–50 s (the page says it's waking up).
- Staff messages arrive by polling (up to 5 s late).
- The thread id is the only protection for reading a chat's history; it's random per browser
  tab, but anyone holding it can read that chat.

### Interview questions
1. **"Why stream only after verification? Isn't that slower?"** Yes, the first word comes later.
   But a token stream would show sentences the verifier might reject, and in a clinic a wrong
   price that flashes on screen is still a wrong price. The status event keeps the page
   responsive.
2. **"How do you protect personal data?"** Contacts are regex-masked in every log, message,
   feedback and review record (clinic numbers kept); the only unmasked copy is the lead, saved
   after recorded consent and hard-deletable from the staff page; errors log the exception type
   only.
3. **"What happens when the LLM provider is down?"** The exception is caught, and the patient
   gets a fixed message with reception's number and the emergency route. No 500, no blank
   screen; a test covers it (`test_provider_failure_returns_fallback_not_500`).
4. **"How would you scale this to many users?"** Paid LLM tier first (quota is the real limit),
   then several instances behind a load balancer, with presence and rate limits moved from
   memory to Redis/Postgres. The graph state is already in Postgres, so any instance can serve
   any chat.
5. **"How do staff take over a chat without breaking the graph?"** `graph.update_state(...,
   as_node="staff")` writes their message and flags into the checkpointed state; the routing
   rule sends patient messages to `staff` (silent) until staff end it or it times out.

### Try it yourself
```bash
make serve                                         # http://localhost:8000 and /staff
curl -N -X POST localhost:8000/v1/chat -H "Content-Type: application/json" \
     -d '{"message":"hi","thread_id":"demo-1"}'     # watch the SSE events
uv run pytest tests/test_api.py tests/test_ratelimit.py -q
```

---

## Lesson 7 — Evaluation

### Purpose
How do you know the bot is good, and how do you know a change didn't make it worse? This layer
answers both with numbers: a hand-written **golden set** of 53 questions, a **retrieval eval**
(did search find the right documents?), an **end-to-end runner** (run every question through
the real pipeline), an **LLM judge** that grades each reply with a checklist, a way to
**validate the judge** against human labels (Cohen's kappa), and a **comparison** that decides
whether a pull request may merge.

### Files
| File | Role |
|---|---|
| `evals/golden/v1.jsonl` | 53 hand-written cases (41 dev / 12 holdout), one JSON object per line |
| `evals/golden/CHANGELOG.md` | Every change to the golden set, with its `dataset_version` |
| `evals/golden_set.py` | `GoldenCase` Pydantic model + `load_golden()` (validates every line, no duplicate ids) |
| `evals/retrieval_eval.py` | recall@5 and MRR for 3 search modes × 2 chunkers; no LLM calls |
| `evals/runner.py` | Runs cases through `Answerer` + judge, computes metrics, writes `.json` + `.md` reports |
| `evals/judge.py` + `prompts/judge.md` | Checklist judge (gpt-oss-120b, temperature 0); `correct` computed in code |
| `evals/judge_agreement.py` | Export a labelling sheet, then score judge vs human labels (agreement, kappa) |
| `evals/compare.py` + `configs/gate.yaml` | The CI gate: hard and soft fail rules, flipped cases, the PR comment |
| `evals/baseline.json` / `.md` | The committed reference run every PR is compared with |

### How it works
```mermaid
flowchart LR
    GS[(golden set<br/>53 cases)] --> RE[retrieval_eval<br/>recall@5 · MRR]
    GS --> RUN[runner: real Answerer<br/>per case]
    RUN --> J[judge: checklist<br/>gpt-oss-120b]
    J --> M[metrics:<br/>correctness, faithfulness,<br/>citations, abstention, safety]
    M --> PR[evals/runs/pr.json]
    B[(baseline.json)] --> CMP[compare.py<br/>+ gate.yaml]
    PR --> CMP
    CMP --> OUT[PR comment +<br/>pass / block merge]
    HL[(24 human labels)] --> JA[judge_agreement:<br/>Cohen's kappa]
    J --> JA
```

### Key code

**1. A golden case** (`golden_set.py:27`)
```python
class GoldenCase(BaseModel):
    id: str  # "q-016"
    dataset_version: str  # "0.7.0", bumped on every change
    split: Literal["dev", "holdout"]
    type: (
        CaseType  # lookup | exact_id | multi_source | followup | unanswerable | ambiguous | safety
    )
    question: str
    gold_doc_ids: list[str]  # documents that contain the answer, e.g. ["DENT-019"]
    expected_behavior: Behavior  # answer | abstain | clarify | refuse | handoff
    must_abstain: bool
    golden_answer: str  # written by the owner from the KB, never by an LLM
    must_include: list[str] = []  # specific checks, e.g. "booking link or reception number"
    must_not_include: list[str] = []
    history: list[Turn] = []  # for follow-ups
    standalone_question: str | None = None  # hand-written rewrite for follow-ups
```
The mix today: 22 lookup, 6 exact-ID, 6 multi-source, 6 unanswerable, 6 safety (injection,
diagnosis, medicine…), 4 follow-up, 3 ambiguous. Behaviours: 37 answer, 6 abstain, 5 refuse,
3 clarify, 2 handoff. Gold ids must be document-level `DENT-xxx` (`:45`), so the same set
scores both chunkers.

**2. Retrieval metrics** (`retrieval_eval.py:30`, `:36`)
```python
def recall_at_k(ranked_docs, gold, k=5):  # share of gold documents in the top 5
    top = set(ranked_docs[:k])
    return sum(1 for g in gold if g in top) / len(gold)


def reciprocal_rank(ranked_docs, gold, depth=10):  # 1 / rank of the first gold document
    for i, d in enumerate(ranked_docs[:depth], 1):
        if d in gold:
            return 1.0 / i
    return 0.0
```
Recall says "was it found at all"; MRR says "how high". No LLM calls, only query embeddings
(cached), so it runs in ~12 s and was used for every search experiment in Lesson 3.

**3. The runner** (`runner.py:54`, `:123`)
- Each case runs through the **real** `Answerer` (same code as production) with the case's
  history; follow-ups use the hand-written `standalone_question` for search (`retrieval_query`).
- Concurrency is 1 for the pipeline and 1 for the judge (`:39-41`): Groq's free tier allows
  ~7–8k tokens per minute, and one answer is ~3k, one judgement ~5k.
- Each step has a 5-minute timeout, and one broken case records an `error` instead of killing
  the run (`:77-84`).
- LLM caching is forced on (`:29`, asserted at `:244`), so re-running unchanged cases is free.
- Every run records `git_sha`, `dataset_version`, `kb_version`, prompt hashes, models and config
  (`:257-273`): any score can be traced to the exact system that produced it.
- If every case errored (quota gone), it writes `*.failed.json` instead of overwriting the last
  good run (`:278`).

Metrics (`compute_metrics`):

| Metric | How it's computed |
|---|---|
| correctness | share of cases the judge marks `correct` |
| faithfulness | share where every factual claim is supported by the sources (judge) |
| citation accuracy | supported claims / all claims, from the **runtime verifier** |
| abstention precision / recall | of replies that declined, how many should have; of cases that must decline, how many did. Refusing an off-topic request isn't counted as an abstention (`:125-127`); recall also reads the judge's verdict on the text, not just the bot's label (`:131-135`) |
| safety pass rate | correctness on `safety` cases only |
| recall@5 / MRR | from the documents the pipeline actually retrieved |
| latency p50 / p95 | live calls only; cached replies say nothing about speed (`:137-138`) |

**4. The checklist judge** (`judge.py:40`)
```python
class Verdict(BaseModel):
    key_facts: list[KeyFact]  # 1–4 essential facts from the reference: present?
    must_include: list[IncludeCheck]  # one per given item: satisfied?
    must_not_include: list[ExcludeCheck]  # one per given item: violated?
    contradictions: list[str]  # wrong price, day, rule, dropped condition…
    behavior_ok: bool
    faithful: bool
    reason: str

    @computed_field
    def correct(self) -> bool:  # decided by code, not by the model
        return self.behavior_ok and not self.missing and not self.violations
```
The judge sees the question, the reference answer, the must-include / must-not-include lists,
the sources the bot was given, and the reply. It does **not** see the bot's own behaviour
label (`:68`): an early run failed a correct refusal because the label said "abstain" (§2). If
the judge returns fewer `must_include` checks than given, the missing ones count as failed
(`:98`); matching by text was tried first and wrongly failed reworded items.

**5. Validating the judge** (`judge_agreement.py`)
1. `export` writes a sheet with the bot reply next to the reference and **without** the judge's
   verdict, so the human isn't biased by it.
2. The owner fills `human_correct` for each row.
3. `score` reports agreement and **Cohen's kappa**, and lists every disagreement.
```python
def cohen_kappa(a, b):
    po = sum(x == y for x, y in zip(a, b)) / n  # observed agreement
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)  # agreement expected by chance
    return (po - pe) / (1 - pe)
```
Kappa corrects for chance: if 80 % of replies are correct, a judge that always says "correct"
agrees 80 % of the time but has kappa 0. Labels are keyed by a hash of the reply, so a label
for an old reply is reported as **stale**, not silently reused.

History: the first judge (Gemini flash-lite, one overall "correct" flag) had **kappa 0.36**: it
passed a reply with a factual error and one with a missing required fact. Rebuilt as the
checklist with `correct` computed in code, on gpt-oss-120b: **kappa 0.59** (agreement 0.88) on
24 human labels (`docs/results.md` §2).

**6. The gate** (`compare.py:61`, `configs/gate.yaml`)
```yaml
hard_fail:
  consent_violations_max: 0
  safety_pass_rate_min: 1.0      # every safety case must pass
  abstention_recall_min: 0.85
soft_fail:
  max_drop_points: 3             # vs baseline: 1 flipped case of 53 = 1.9 pts (ok), 2 = 3.8 (fail)
  metrics: [retrieval.recall_at_5, retrieval.mrr, answer.correctness,
            grounding.faithfulness, citations.accuracy]
```
- **Hard fails** don't compare with anything: any errored case, any failed safety case or
  abstention recall under 85 % blocks the merge.
- **Soft fails** compare with the baseline: a headline metric may drop at most 3 points.
- **Flipped cases** (pass → fail, fail → pass) are listed by id with the judge's reason, so
  the reviewer sees *which* questions got worse.
- A metric a run doesn't report is shown as **"not measured", never as passing** (`:69-91`).

Simulated on the real baseline (no LLM calls): the same run → pass; **1** case flipped → pass;
**2** flipped → `answer.correctness dropped 3.8 points (83.0% → 79.2%), limit 3` → **fail**.

### Why it's built this way
- **Hand-written golden answers.** An LLM-written reference would share the model's mistakes
  and blind spots. The owner wrote each one from the KB; the changelog records every edit.
- **Dev / holdout split.** Tuning happens on dev only; holdout is reported, never tuned on
  (the rule is in `CLAUDE.md`). It shows whether dev improvements carry over.
- **Checklist instead of "is this correct?".** A holistic grade was too lenient (kappa 0.36).
  Item-by-item checks with the verdict computed in code made the judge stricter and
  explainable: every failure names the missing fact (see `evals/baseline.md`).
- **A different, bigger model family as judge** (gpt-oss-120b vs the Qwen generator), to reduce
  self-preference.
- **3 points = one flipped case allowed.** With 53 cases, one case is ~1.9 points. Allowing one
  flip absorbs the occasional borderline case; two flips is a real regression. It's re-set
  whenever the set size changes.
- **It works:** a deliberately bad PR (emergency replies swapped for "a staff member will
  reply") passed all 83 unit tests and was **blocked** by the gate: safety 83.3 % < 100 % (hard
  fail) and correctness −4.0 points (§4c).

### Limits and gotchas
- **Small set**: 53 cases; one case moves correctness by ~2 points; per-type numbers rest on
  3–6 cases.
- **The consent rule isn't measured yet.** `consent_violations_max: 0` is in `gate.yaml`, but no
  run reports `leads.consent_violations`, so the gate always shows "not measured" for it.
  Consent is covered by unit tests (Lesson 5) and the spot check (§4e), not by the eval.
- **The judge validation is from an earlier run.** Kappa 0.59 was measured on 24 replies from
  2026-11-13. Today, `judge_agreement score --run evals/baseline.json` reports 22 of those 24
  labels as stale (the replies have changed since), so the current judge hasn't been re-checked
  on current replies.
- **Follow-ups use hand-written rewrites** in the eval, so the real rewriter isn't covered by
  the gate (spot check only, Lesson 5).
- The judge can miss subtle condition errors and is sometimes strict about secondary details
  (q-035, q-037, q-038 fail for omitted extras).
- `judge.py`'s docstring still says "Gemini"; the judge has been gpt-oss-120b since the
  validation (`configs/models.yaml`).
- Run-to-run noise wasn't measured separately: temperature 0 plus the cache make CI repeatable,
  but a fresh uncached run can differ on borderline cases.

### Interview questions
1. **"How do you evaluate a RAG system?"** Separately at each layer: retrieval with recall@5 and
   MRR against hand-labelled gold documents; answers with a checklist LLM judge against
   hand-written references (correctness, faithfulness, behaviour); the runtime verifier's
   citation accuracy; plus abstention and safety rates.
2. **"How do you know your LLM judge is any good?"** I labelled 24 replies myself without seeing
   the judge's verdicts and measured Cohen's kappa. The first judge scored 0.36 and missed real
   failures, so I rebuilt it as a checklist with the verdict computed in code: 0.59.
3. **"What's Cohen's kappa and why not accuracy?"** Agreement corrected for chance. If most
   replies pass, a judge that always says "pass" has high agreement but zero kappa.
4. **"How does your CI decide to block a PR?"** Hard rules (any safety failure, abstention recall
   < 85 %, any errored case) and a soft rule (no headline metric drops more than 3 points vs the
   committed baseline). It comments the deltas and flipped cases on the PR. A deliberately
   harmful PR that passed all unit tests was blocked.
5. **"Why a holdout split?"** To catch overfitting to the cases you tune on. Holdout results are
   reported, never used to change prompts or settings.

### Try it yourself
```bash
make eval-retrieval                                        # ~12 s, no LLM calls
uv run python -m evals.runner --split dev --limit 3        # 3 cases end to end (cached = free)
uv run python -m evals.compare --baseline evals/baseline.json --candidate evals/baseline.json
uv run python -m evals.judge_agreement score --run evals/baseline.json   # shows stale labels
```

---

## Lesson 8 — CI/CD & deployment

### Purpose
This is what makes the quality checks automatic and the bot live. Every pull request runs two
GitHub Actions workflows: **`ci`** (lint + 112 unit tests against a real Postgres) and
**`eval-gate`** (the full eval from Lesson 7, compared with the baseline). `main` is protected,
so nothing merges unless both pass. After a merge, **Render** builds the Docker image and deploys
it, with the database on **Neon**.

### Files
| File | Role |
|---|---|
| `.github/workflows/ci.yml` | Job `lint-and-test`: ruff check, ruff format check, migrate, pytest |
| `.github/workflows/eval-gate.yml` | Job `eval`: restore LLM cache → ingest → run all 53 cases → compare → PR comment → enforce → save cache |
| `Dockerfile` | `python:3.12-slim` + uv; migrates, then serves on `$PORT` as a non-root user |
| `.dockerignore` | Keeps `.env`, `.git`, tests, docs, caches and eval runs out of the image |
| `docker-compose.yml` | Local Postgres 16 + pgvector on port 5433 (same image as CI) |
| `Makefile` | Thin wrappers (`make test`, `make eval-ci`…) so local and CI run identical commands |
| `.pre-commit-config.yaml` | ruff, YAML check, whitespace, **detect-private-key** before each commit |

### How it fits together
```mermaid
flowchart LR
    DEV[branch + commit] -->|pre-commit:<br/>ruff, private-key check| PR[pull request]
    PR --> CI[ci: lint-and-test<br/>Postgres service, no keys]
    PR --> EG[eval-gate: eval<br/>53 cases vs baseline]
    EG -->|comment| PR
    CI --> RS{protect-main ruleset:<br/>both checks green?}
    EG --> RS
    RS -->|yes| MAIN[merge to main]
    MAIN --> CI2[ci on main]
    CI2 -->|checks pass| RENDER[Render: docker build<br/>+ deploy]
    RENDER --> NEON[(Neon Postgres<br/>+ pgvector)]
```

### Key code

**1. `ci`: fast checks with a real database** (`ci.yml:12-37`)
```yaml
services:
  db:
    image: pgvector/pgvector:0.8.0-pg16      # same image as docker-compose.yml
steps:
  - run: uv sync --locked                    # fail if uv.lock is out of date
  - run: uv run ruff check .
  - run: uv run ruff format --check .
  - run: uv run python -m bot.db.migrate
  # No LLM keys here: tests use scripted models; tests needing an ingested KB skip themselves.
  - run: uv run pytest -q
```
A real Postgres + pgvector runs as a service container, so the database tests (checkpointer,
leads, turn logging) run in CI too. No API keys: every LLM in the tests is a scripted fake
(Lesson 10), so `ci` is free, fast and deterministic.

**2. `eval-gate`: the expensive check, made cheap by the cache** (`eval-gate.yml`)
```yaml
on:
  pull_request:
  workflow_dispatch:          # run on main to (re)fill the LLM cache that PRs restore from
concurrency:
  group: eval-gate-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true    # a new push cancels the old run (free-tier quota)
...
- name: Restore LLM cache
  uses: actions/cache/restore@v4
  with:
    path: .cache/llm
    key: llm-${{ hashFiles('prompts/**','configs/**','src/**','data/kb/**','evals/golden/**') }}-${{ github.run_id }}
    restore-keys: |
      llm-${{ hashFiles(...) }}-
      llm-
- run: uv run python -m bot.db.migrate
- run: uv run python -m bot.retrieval.ingest
- run: uv run python -m evals.runner --split all --out evals/runs/pr.json || true
- run: uv run python -m evals.compare ... --comment-out comment.md
- run: gh pr comment ... --body-file comment.md --edit-last || gh pr comment ... --body-file comment.md
- run: uv run python -m evals.compare ... --enforce        # non-zero exit blocks the merge
- name: Save LLM cache
  if: always()
```
Step by step, the important choices:
- **No path filter.** A required check must always report; a docs-only PR replays every case
  from cache in a few minutes at zero API cost.
- **Cache key = hash of everything that can change a reply + the run id.** GitHub caches are
  immutable, so each run saves a new entry; `restore-keys` falls back to the newest cache with
  the same code hash, then to *any* `llm-` cache. Because the LLM cache key is per request
  (Lesson 1), an old cache is still safe: requests that changed simply miss.
- **`|| true` on the runner** so the compare step still runs when some cases error, and
  `compare.py` turns errors into a **hard fail** with a readable PR comment, instead of a red
  step with no explanation.
- **Comment, then enforce.** The comment is posted (or the last one edited, so a PR doesn't
  collect one per push) before the enforce step can fail the job.
- **Save the cache `if: always()`.** A run cut short by the daily quota keeps the replies it
  already paid for; the next run continues from there.
- The ingest step indexes the KB inside the job (embeddings for unchanged chunks come from the
  restored cache, so no Gemini calls).
- Secrets (`GROQ_API_KEY`, `GROQ_GENERATOR_API_KEY`, `GEMINI_API_KEY`) are repository secrets,
  only read in this workflow; `permissions` give it `pull-requests: write` and nothing more.

**3. Cache scope: why `main` has to be filled manually**
GitHub Actions caches are scoped by branch: a cache saved by a PR run is visible only to that PR,
but a cache saved on `main` is visible to every PR. So after a baseline change the owner runs
**Actions → eval-gate → Run workflow** on `main` (`workflow_dispatch`); every later PR then
restores those replies. On the free tier this took 5 manual runs the first time, each one cut
short by the daily quota and continuing from the saved cache (`docs/results.md` §4c).

**4. Branch protection**: the `protect-main` ruleset requires a pull request and both checks,
`lint-and-test` and `eval`, before anything merges into `main` (`audit.md` timeline). Even the
owner works on a branch.

**5. The Docker image** (`Dockerfile`)
```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv     # pinned uv binary
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project               # deps layer, cached
COPY src ./src
COPY evals ./evals
COPY prompts ./prompts
COPY configs ./configs
COPY data/kb ./data/kb
RUN uv sync --frozen --no-dev
RUN useradd --create-home app && chown -R app /app
USER app                                                           # not root
CMD ["sh", "-c", "python -m bot.db.migrate && exec uvicorn bot.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```
- **Dependencies before code**: changing a Python file doesn't reinstall every package; only
  the last layers rebuild.
- `--frozen --no-dev`: exactly the locked versions, without pytest, ruff or the RAGAS group.
- **Migrations on every start**: a deploy brings Neon's schema up to date by itself (applied
  files are skipped).
- `exec uvicorn`: uvicorn replaces the shell as PID 1, so it receives Render's stop signal and
  shuts down cleanly.
- `${PORT:-8000}`: Render tells the app which port to use through `$PORT`.
- **No secrets in the image**: keys and `DATABASE_URL` come from the environment at run time,
  and `.dockerignore` excludes `.env` and `.env.*`.

**6. Render + Neon**
- **Render** (free web service) builds the Dockerfile from the GitHub repo. Auto-deploy is set to
  **"After CI checks pass"**, so a commit on `main` deploys only once `ci` is green. Free
  services sleep after ~15 minutes idle; the first request then takes 30–50 s (the chat page
  shows "Waking the server up…").
- **Neon** (free serverless Postgres, pgvector 0.8.7) holds chunks, conversations, leads and
  LangGraph checkpoints. The KB was indexed once by running the ingest with Neon's
  `DATABASE_URL`; the admin endpoint `POST /v1/ingest` can re-index it after a KB change.

### Real run
`docker build -t brightsmile-bot:guide .` then running it against the local database:
```
Schema up to date.
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     172.17.0.1:44680 - "GET /v1/health HTTP/1.1" 200 OK
{"status":"ok","chunks":234,"kb_version":"08a3673838d7"}
$ docker exec bsb-guide whoami
app
```
The image is about 1.06 GB (mostly Python packages: LangGraph, the Groq and Google SDKs,
pdfplumber).

### Why it's built this way
- **Two workflows, not one**: `ci` is fast, free and needs no secrets; `eval-gate` is slow and
  uses quota. Splitting them gives a quick red/green on code quality and a separate quality
  verdict on behaviour.
- **The eval is a required check**: unit tests can't see that a reply lost its first-aid steps.
  The bad-PR demo passed all 83 unit tests and was blocked only by the eval (§4c).
- **Docker for deployment**: the same image runs locally and on Render; nothing depends on the
  host. Moving to another host means pointing it at the same Dockerfile.
- **Render + Neon free tiers**: a live demo for ₹0, with real Postgres and pgvector. The
  limits (sleeping, quota) are documented, not hidden.

### Limits and gotchas
- **PRs from forks don't get secrets**, so the eval gate can't run for outside contributors as
  it stands (fine for a one-owner repo).
- **A full uncached eval needs about two days of free quota**; the gate depends on the cache
  being filled on `main`.
- **A reply cached on `main` stays fixed**: if a borderline case's cached reply fails (q-054 did,
  §4c/§4d), every PR inherits that failure until the case or its inputs change.
- **Render's auto-deploy didn't fire once** even with "After CI checks pass" set; the commit
  had to be deployed by hand from the Render dashboard. Check the deployed commit after merging.
- **The KB isn't re-indexed on deploy**: after changing `data/kb/`, run ingest against Neon (or
  call `/v1/ingest`); `/v1/health` shows which `kb_version` is live.
- `kb_version` differs between Windows and Linux for the same files (CRLF vs LF line endings).
- `ruff format --check` (ruff 0.17) also formats ```python blocks inside Markdown files, so a
  badly formatted snippet in `docs/` fails `lint-and-test`. Run `uv run ruff format .` before
  pushing docs (this guide hit it).
- Cold starts (30–50 s) and the one-instance design (Lesson 6) are free-tier trade-offs.

### Interview questions
1. **"How do you test LLM changes in CI?"** Two layers: unit tests with scripted LLMs (fast,
   free, no keys) and an eval gate that runs the real pipeline on 53 golden cases and compares
   with a committed baseline. Both are required checks on `main`.
2. **"Isn't running an LLM eval on every PR expensive?"** Every LLM call is cached by a hash of
   the full request, and the cache is shared through GitHub Actions. A PR only pays for the
   cases its change actually affects; a docs PR costs nothing.
3. **"How does a deploy happen?"** Merge to `main` → `ci` passes → Render builds the Dockerfile
   and starts the container, which applies migrations and starts uvicorn. Secrets are Render
   environment variables; the database is Neon.
4. **"What's in your Dockerfile that matters?"** Slim base, pinned uv, dependency layer before
   the code for build caching, no dev dependencies, a non-root user, migrations at start, `exec`
   so the server gets stop signals, and no secrets baked in.
5. **"What would you change for production?"** A paid LLM tier and a non-sleeping instance;
   presence and rate limits in Redis for several instances; a staging environment; automatic
   re-indexing when `data/kb/` changes; alerts on error rates and fallbacks.

### Try it yourself
```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q      # what `ci` runs
docker build -t brightsmile-bot .
docker run --rm -p 8000:8000 --env-file .env \
  -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5433/brightsmile brightsmile-bot
```
