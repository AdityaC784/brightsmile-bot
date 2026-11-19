# Code guide — BrightSmile bot

A lesson-by-lesson walk through the codebase: what each part does, how it works, why it was
built that way, and what an interviewer is likely to ask. Every claim points to a file and line
(`path:line`) or to a measured number in [`docs/results.md`](results.md).

| # | Lesson | Main files |
|---|---|---|
| 0 | [Big picture](#lesson-0--big-picture) | `configs/`, `Makefile` |
| 1 | [Config & LLM layer](#lesson-1--config--llm-layer) | `src/bot/config.py`, `src/bot/llm/` |
| 2 | Knowledge-base ingestion | `src/bot/retrieval/loaders.py`, `chunking.py`, `ingest.py`, `src/bot/db/` |
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
