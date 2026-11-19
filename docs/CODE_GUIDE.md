# Code guide — BrightSmile bot

A lesson-by-lesson walk through the codebase: what each part does, how it works, why it was
built that way, and what an interviewer is likely to ask. Every claim points to a file and line
(`path:line`) or to a measured number in [`docs/results.md`](results.md).

| # | Lesson | Main files |
|---|---|---|
| 0 | [Big picture](#lesson-0--big-picture) | `configs/`, `Makefile` |
| 1 | [Config & LLM layer](#lesson-1--config--llm-layer) | `src/bot/config.py`, `src/bot/llm/` |
| 2 | [Knowledge-base ingestion](#lesson-2--knowledge-base-ingestion) | `src/bot/retrieval/loaders.py`, `chunking.py`, `ingest.py`, `src/bot/db/` |
| 3 | Hybrid retrieval | `src/bot/retrieval/hybrid.py` |
| 4 | Grounded answering + self-verification | `src/bot/rag/` |
| 5 | Conversation flow (LangGraph) | `src/bot/graph/`, `src/bot/leads/` |
| 6 | API & product | `src/bot/api/` |
| 7 | Evaluation | `evals/` |
| 8 | CI/CD & deployment | `.github/workflows/`, `Dockerfile` |
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
    groq_generator_api_key: str = ""   # optional 2nd key: the generator gets its own quota
    gemini_api_key: str = ""
    llm_cache_enabled: bool = False    # the eval runner turns this on
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
    if is_daily_quota(exc):        # "tokens per day (TPD)": won't clear for hours
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS
    name = type(exc).__name__.lower()
    return "connection" in name or "timeout" in name

@retry(retry=retry_if_exception(is_retryable), stop=stop_after_attempt(8),
       wait=wait_retry_after, reraise=True)
def _call(self, req): return self.provider.complete(req)
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
    repair = [*messages, Message("assistant", resp.text),
              Message("user", f"That JSON was invalid: {err}. Reply with corrected JSON only.")]
    ...  # second try; if it also fails → LLMError
```
The Pydantic class is both the instruction (its JSON Schema goes into the prompt) and the check.
`_extract_json` (`:43`) tolerates code fences and stray prose around the object. Groq sometimes
rejects its own JSON-mode output with a 400 `json_validate_failed`; `GroqProvider.complete`
retries that once without JSON mode (`providers.py:95-104`) and lets `chat_json` repair it.

**6. Embeddings** (`src/bot/llm/embeddings.py:56`)
```python
resp = self.client.models.embed_content(
    model=self.model, contents=texts,
    config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims))
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
    kind: BlockKind          # "heading" | "text" | "table" | "row"
    text: str = ""
    level: int = 0           # heading level
    rows: list[str] = ...    # table rows, already rendered
    key: str = ""            # CSV first column, used as the citation anchor
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
50-token overlap over the flattened text. Both chunkers are always indexed (`ingest.py:298`), in
the same table with a `chunker` column, so the chunking experiment switches with one config
value (`retrieval.yaml: chunking.strategy`) and no re-ingest.

**6. What gets embedded** (`ingest.py:269`)
```python
def embedding_text(c: Chunk, status: str) -> str:
    flag = "[ARCHIVED — superseded, not current]\n" if status == "archived" else ""
    return f"{flag}{c.title}\n{c.section}\n{c.content}".strip()
```
The title and section path are added before the content, so a chunk that only says "₹6,800"
still carries "2026 Service Tariff" in its vector. The archived 2025 price list is marked in its
embedding text too.

**7. `kb_version`: one hash for everything that shapes the index** (`ingest.py:275`)
```python
h.update(json.dumps(manifest, sort_keys=True).encode())
for name in sorted(files):
    h.update(name.encode()); h.update(files[name].read_bytes())
h.update(json.dumps(load_yaml("retrieval")["chunking"], sort_keys=True).encode())
h.update(json.dumps(load_yaml("models")["embeddings"], sort_keys=True).encode())
```
Every chunk row stores it, `/v1/health` reports it, and every eval run records it. A score
is always tied to an exact KB, chunking settings and embedding model.

**8. Replace, atomically** (`ingest.py:312`)
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

**10. Migrations** (`migrate.py:371`): a `schema_migrations` table records applied files;
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
