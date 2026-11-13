# Results

All numbers are measured, never estimated. Re-run with the command shown; raw output lands in
`evals/runs/` (gitignored). Holdout numbers are reported only at milestones, never used for tuning.

## 1. Retrieval: dense vs full-text vs hybrid (2026-11-13)

`make eval-retrieval` · golden set 0.2.0 · dev split, 21 scored cases (cases with gold docs)
· kb_version `6bacaec951d2` · `gemini-embedding-001` @768 · RRF k=60, 50 candidates/list,
archived_penalty 0.85 · follow-ups use the hand-written standalone question (oracle rewrite).

recall@5 / MRR@10, document-level matching:

| chunker | hybrid | lexical (FTS) | dense |
|---|---|---|---|
| heading | 0.76 / 0.78 | 0.76 / 0.68 | **0.88 / 0.80** |
| fixed | 0.74 / 0.72 | 0.69 / 0.61 | 0.79 / 0.71 |

recall@5 by type (heading chunker):

| type | n | hybrid | lexical | dense |
|---|---|---|---|---|
| lookup | 9 | 0.89 | 0.89 | 1.00 |
| exact_id | 3 | 1.00 | 1.00 | 1.00 |
| multi_source | 2 | 0.50 | 0.25 | 1.00 |
| followup | 2 | 1.00 | 1.00 | 1.00 |
| ambiguous | 1 | 0.50 | 0.50 | 1.00 |
| safety | 2 | 0.75 | 0.75 | 0.75 |
| unanswerable | 2 | 0.00 | 0.25 | 0.00 |

**Reading it honestly**
- On this first cut, **dense beats hybrid**. Hybrid is not a free win: lexical noise
  (common words matching many docs) pulls irrelevant chunks into the fused top 5.
- Heading-aware chunks beat fixed windows in every mode.
- exact_id cases score 1.00 everywhere because doc-level recall@5 is lenient: in manual testing
  dense ranked `EQP-005` first for "SRV-005A" but still had DENT-014 at rank 3.
  MRR and harder ID cases are needed to show the lexical advantage.
- n = 21: one case moves recall by ~0.05. Treat differences under ~0.1 as noise until the set
  reaches 50 cases.

**Main failure patterns (heading / hybrid)**
1. **The 2026 tariff (DENT-019) is rarely retrieved** for price questions (q-006, q-016,
   q-018, q-025). Its table is split into ~440-token chunks holding 6–9 services each, so no
   chunk is close to a single-service question; service pages (which repeat the price) win.
   Candidate fix: one chunk per tariff row, as already done for CSV rows.
2. **DENT-036 (assistant abstention rules) is not retrieved** for abstain/refuse cases
   (q-021, q-024, q-029). Open question: should those gold labels include DENT-036 at all, or
   should the abstention rules live in the system prompt instead of per-question retrieval?

**Next experiments**: row-level tariff chunks; revisit DENT-036 gold labels; lexical weight
or query-term filtering in RRF; archived-penalty sweep.

## 2. End-to-end answers (2026-11-13)

`make eval` · golden 0.2.0 · dev split, 24 cases · generator `qwen/qwen3.8-27b` (Groq) ·
verifier `openai/gpt-oss-20b` · judge `gemini-3.5-flash-lite` (temperature 0) · hybrid
retrieval, heading chunks, top-6 · citation verification on (min support 1.0).

| metric | value |
|---|---|
| answer correctness | 0.92 (22/24) |
| behaviour accuracy | 0.96 |
| faithfulness | 1.00 |
| citation accuracy | 0.96 (50 claims) |
| abstention precision / recall | 0.80 / 1.00 |
| safety pass rate | 1.00 (4/4) |

**Both failures are retrieval misses, not generation errors**
- q-016 (refund calculation): DENT-024 (REF-03) is never retrieved, so the bot can only
  compute the ₹4,750 lab fee, not the full ₹12,750.
- q-018 (insurance + root canal price): DENT-019 is not retrieved; the bot cites the wrong
  source for SRV-005A and the deterministic check correctly replaces the reply with the fallback.

**Caveats — read before quoting these numbers**
- The judge is **not yet validated** against human labels (`make judge-export` / `judge-score`).
  Until then, correctness is the judge's opinion.
- n = 24. One case = ~0.04. Noise across repeated uncached runs is not yet measured.
- Follow-ups use the oracle standalone question (no rewriter yet).
- Judge choice was forced by quota: `gemini-3.6-flash` free tier = 20 requests/day.

**Fixes found by running the eval** (each was a real bug, not tuning):
verifier dropped results counted as "unsupported" (3 false fallbacks); Groq JSON-mode 400s not
retried; Groq 429s needing the Retry-After wait; correct refusals counted against abstention
precision; the judge was shown the bot's own behaviour label and graded the label instead of the
reply; emergency replies that failed verification fell back to "call reception" — now a fixed,
KB-sourced emergency message.
