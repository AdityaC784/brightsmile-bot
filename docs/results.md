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
