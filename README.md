# BrightSmile Dental Care — Support & Lead-Gen Chatbot with an Eval Gate

A website chatbot for a (fictional) dental clinic in Pune. It answers patient questions from a
36-document knowledge base **with citations**, checks every answer before showing it, says "I
don't know" when the KB doesn't cover something, routes emergencies to first aid and hospitals,
captures leads **only after consent**, and hands chats to staff. Every pull request runs an
**evaluation suite in CI that blocks merges that make answers worse**.

**Live:** [chat](https://brightsmile-bot.onrender.com/) · [staff page](https://brightsmile-bot.onrender.com/staff)
(admin key) · Demo video: *link coming* · Free hosting sleeps when idle: the first request can take
30–50 s.

| | |
|---|---|
| Answer correctness (judge, 53 cases) | **0.83** (44/53) |
| Faithfulness · citation accuracy | **1.00** · **1.00** |
| Safety cases · abstention recall | **1.00** · **1.00** |
| Retrieval recall@5 / MRR | 0.90 / 0.85 |

Every number comes from [`evals/baseline.md`](evals/baseline.md) and [`docs/results.md`](docs/results.md).
Full write-up: [`audit.md`](audit.md) · original plan: [`docs/PLAN.md`](docs/PLAN.md).

## How it works

```mermaid
flowchart LR
    Q[Patient message] --> C{classify}
    C -->|support| R[rewrite follow-up] --> S[hybrid retrieval<br/>full-text + vector, weighted RRF]
    S --> G[generate JSON reply<br/>with citations] --> V{verify<br/>citations + claims}
    V -->|ok| A[verified reply]
    V -->|fails| P[one repair] --> V2{verify} -->|fails| F[safe fallback]
    V2 -->|ok| A
    C -->|lead| L[collect need → consent text<br/>→ name + phone, email optional] --> DB[(leads)]
    C -->|handoff| H[staff page:<br/>live chat]
    C -->|greeting / out of scope| O[greet / refuse]
```

- **Verified before shown.** The reply streams only after the verifier passed, so a rejected
  answer never appears. ₹ amounts and service codes must be in the cited source; an LLM checks
  the other claims.
- **Emergencies** (spreading swelling, trouble breathing, bleeding over 45 min, knocked-out tooth)
  get first aid plus where to go when the clinic is closed. They never fall back to generic text,
  and the bot answers them even while staff hold the chat.
- **Leads** are saved only after the consent text was shown and the patient agreed. A phone
  number is required, email is optional.
- **Rate limits** protect the shared free LLM quota: 10 messages a minute and 60 a day per chat,
  100 a day per visitor IP ([`configs/generation.yaml`](configs/generation.yaml)). Over the limit,
  or when the LLM provider fails, the patient gets a fixed message with reception's number and the
  emergency route, with no LLM call.
- **Staff page** (`/staff`, one admin-key login): live chats with patient online/away/left,
  several replies, End chat, timeouts; a **Leads** view lists every lead in a table (phone and
  email in separate columns) with Delete.

## The eval gate

On every PR, [`eval-gate`](.github/workflows/eval-gate.yml) runs all golden cases (dev +
holdout), compares them with the committed baseline, comments on the PR, and **blocks the merge**
([`configs/gate.yaml`](configs/gate.yaml)) when:
- **hard fail:** any safety case fails, abstention recall < 85 %, or a case errors;
- **soft fail:** correctness, faithfulness, citation accuracy, recall@5 or MRR drops more than
  3 points (one flipped case passes, two fail).

LLM calls are cached by request, so a PR that doesn't change what the bot says replays from cache
at no cost. A deliberately bad PR (emergency replies swapped for a generic message) passed all
unit tests and was blocked by the gate:

![Eval gate blocking a bad PR](docs/img/bad-pr-gate-comment.png)

The judge (GPT-OSS 120B, checklist grading) was validated against the owner's hand labels after
the first, lenient judge missed 3 of 4 failures (Cohen's kappa 0.36 → 0.59).

## Stack

| Layer | Choice |
|---|---|
| API | FastAPI; verified replies streamed over SSE |
| Orchestration | LangGraph with a Postgres checkpointer |
| Storage | Postgres 16 + pgvector (chunks, conversations, leads, feedback, review candidates) |
| Retrieval | Postgres full-text + pgvector HNSW, weighted Reciprocal Rank Fusion, one-hop reference expansion |
| Generator | Qwen 3.8 27B on Groq |
| Utility (classify, rewrite, lead extraction, verifier) | GPT-OSS 20B on Groq |
| Judge (evals only) | GPT-OSS 120B on Groq, temperature 0 |
| Embeddings | Gemini `gemini-embedding-001`, 768-dim |
| Hosting | Render (Docker) + Neon Postgres |

Model IDs: [`configs/models.yaml`](configs/models.yaml). Clinic facts (names, numbers, links) come
from [`configs/generation.yaml`](configs/generation.yaml), never hard-coded in prompts.

## Knowledge base

`data/kb/`: 36 documents in mixed formats (11 PDF, 10 DOCX, 10 Markdown, 5 CSV). The KB has
deliberate retrieval traps: exact IDs (`SRV-005A`, `POL-CAN-01`), superseded 2025 prices next to
the 2026 tariff, facts spread over several documents, tables, and unanswerable questions.
Weighted RRF lifted dev recall@5 from 0.76 to 0.93; plain vector search scored 0.95 but missed
exact IDs ([results §1](docs/results.md)).

## Feedback loop: adding test cases

The API saves **review candidates** automatically: a thumbs-down, an abstained answer, a handoff
or a failed citation check. Then:

```bash
uv run python -m evals.review_cli --neon     # production candidates (omit --neon for local)
```

For each candidate you see the question, the bot's answer and the retrieved documents, and you
**accept** (you write the correct answer yourself, never an LLM), **reject** or **defer**.
Accepted cases are added to [`evals/golden/v1.jsonl`](evals/golden/v1.jsonl) with a new
`dataset_version` and an entry in [`CHANGELOG.md`](evals/golden/CHANGELOG.md). Open a PR, then
refresh the baseline in its own commit (`make eval-baseline`). Golden 0.7.0 added 3 cases from
real conversations this way.

## Run it

```bash
cp .env.example .env              # GROQ_API_KEY, GROQ_GENERATOR_API_KEY, GEMINI_API_KEY, ADMIN_API_KEY
uv sync
docker compose up -d --wait db    # Postgres 16 + pgvector on localhost:5433
make migrate ingest               # schema + chunk/embed/index the KB
make serve                        # http://localhost:8000 (chat page), /staff (staff page)
make chat                         # or talk to the bot in the terminal
make test                         # 106 tests (scripted LLMs; DB tests skip without Postgres)
make eval-retrieval               # recall@5 / MRR per search mode
make eval                         # end-to-end eval, dev split -> evals/runs/latest.json
```

On Windows without `make`, run the command from the matching target in the [`Makefile`](Makefile).

## Known limitations

- **Small golden set** (53 cases, 12 holdout): one case moves correctness by about 2 points.
  Run-to-run noise wasn't measured separately (temperature 0, and CI replays cached replies).
- **Groq isn't fully deterministic at temperature 0**: a borderline case can pass on one fresh
  run and fail on the next.
- **Open safety gap:** asked to "call 112", the bot sometimes doesn't say it can't place calls.
  The case is held back; the fix (one prompt line) waits for a full eval run.
- **Multi-rule totals** (e.g. a package refund after partial treatment) are not computed; the bot
  gives the published amounts and refers to reception (q-016).
- **Outside the gate:** follow-up rewriting (the gate uses hand-written standalone questions) and
  consent reading (no lead eval set, so the gate reports consent as "not measured"). Spot checks
  with the real model ([results §4e](docs/results.md)): the rewriter resolved 2 of 4 follow-ups;
  0 of 9 hesitant replies ("maybe later", "why do you need my number?") were read as consent.
- **Judge validated on 24 replies**: enough to catch a lenient judge, not a precise reliability
  figure.
- **Free-tier quotas** (Groq: 200k tokens/day per model): a full uncached eval needs about two
  days. Rate limits slow quota use but don't stop a determined script (it can fake its IP header
  and open new chats); when the quota runs out, patients get the "technical problem" message.
- **Contact details** typed in chat are redacted from logs and saved to `leads` only with
  consent, but remain in the LangGraph checkpoint transcript.
- Synthetic KB: a few internal inconsistencies were found and fixed (see the golden CHANGELOG).
