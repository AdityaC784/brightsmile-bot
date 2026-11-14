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

### 1b. Follow-up experiments (2026-11-13, dev split, heading chunks)

| change | hybrid recall@5 / MRR | verdict |
|---|---|---|
| baseline (equal-weight RRF) | 0.76 / 0.78 | — |
| one chunk per row for tables ≥ 5 rows | 0.79 / 0.68 | **rejected**: MRR drops, target cases not fixed |
| weighted RRF, lexical weight 0.7 | 0.83 / 0.78 | |
| weighted RRF, lexical weight 0.5 | 0.86 / 0.78 | |
| **weighted RRF, lexical weight 0.3** | **0.88 / 0.79** | **adopted** |
| weighted RRF, lexical weight 0.15 | 0.90 / 0.80 | not chosen (over-fit risk on 21 cases) |
| dense only | 0.88 / 0.80 | loses exact IDs (ranks EQP-005 first for "SRV-005A") |

- Row chunking was based on a wrong diagnosis: the tariff wasn't diluted. Service pages repeat
  the same prices and out-compete it. The option stays in code (`table_row_min_rows: 0`).
- The real problem was equal-weight fusion: dense ranked the missing gold docs #1–#2 (DENT-024
  for q-016, DENT-019 for q-018) while common words in the full-text list pushed them to #12–#15.
- Exact-ID cases keep reciprocal rank 1.0 at every weight tried.
- After also removing DENT-036 from three gold labels (golden 0.3.0: abstention rules live in
  the system prompt), dev recall@5 / MRR = **0.93 / 0.82** (dense 0.95 / 0.83).

## 2. End-to-end answers (2026-11-13)

`make eval` · golden 0.3.0 · dev split, 24 cases · generator `qwen/qwen3.8-27b` · verifier
`openai/gpt-oss-20b` · judge `openai/gpt-oss-120b` (temperature 0, checklist grading) · hybrid
retrieval (lexical weight 0.3), heading chunks, top-6 · citation verification on.

| metric | judge | hand labels |
|---|---|---|
| answer correctness | 0.79 (19/24) | **0.83 (20/24)** |
| behaviour accuracy | 0.96 | |
| faithfulness | 1.00 | |
| citation accuracy (verifier) | 1.00 (44 claims) | |
| abstention precision / recall | 1.00 / 1.00 | |
| retrieval recall@5 / MRR | 0.93 / 0.82 | |

**Judge validation** (`make judge-score`, 24 hand-labelled replies): agreement 0.88, Cohen's
kappa **0.59** (moderate). The judge is slightly stricter than the labels. History: gemini
flash-lite with a holistic grade, kappa 0.36 (it passed a factual error and a missing required
fact); checklist grading with `correct` computed in code plus gpt-oss-120b, kappa 0.59.

**Real failures (hand labels)**
- q-016: gives only the ₹4,750 lab fee; the full loss is ₹12,750 (REF-03 + REF-04). DENT-024 is now
  retrieved, so this is a multi-step reasoning gap in the generator.
- q-020: **factual error** that the verifier and judge both missed. Says 14–17-year-olds may
  attend orthodontic adjustments "alone"; DENT-029 requires an authorised adult.
- q-021: declines correctly but omits Dr. Joshi's roster days.
- q-009: first aid correct, but no Sunday/after-hours route (hospital dental emergency).

**KB inconsistencies found** (they cause the remaining judge/label disagreements):
- Emergency hospital: DENT-031/032 say "Jupiter Hospital Baner or **Ruby Hall Clinic**";
  DENT-035/036/002 say "Jupiter or **Manipal Hospital Baner**".
- Specialist hours: DENT-004 roster (09:30–01:30 & 02:30–08:30) vs DENT-002 §4 "typical
  chairside window" (e.g. orthodontist 11:00–01:30 & 03:30–08:00).

**Caveats**: n = 24 (one case ≈ 0.04); uncached run-to-run noise not yet measured; follow-ups
use the oracle standalone question (no rewriter yet); holdout not run.

**Bugs found by running the eval** (fixed; none were tuning): verifier results dropped and
counted as unsupported; verifier rejected naming the clinic as "unsupported"; Groq JSON-mode 400s
and 429s not retried properly; Gemini's retry delay ignored; correct refusals counted against
abstention precision; the judge saw the bot's behaviour label; the judge's skipped-item safeguard
re-added reworded items as failures; emergency replies that failed verification fell back to
"call reception" (now a fixed KB-sourced emergency message with first aid); concurrent eval runs
overwrote each other's output.

### 2b. After KB consistency fixes and prompt rules (2026-11-13)

golden 0.4.0 · kb `a95b83a663a9` · same models and retrieval as §2.

| metric | judge | hand labels |
|---|---|---|
| answer correctness | 0.92 (22/24) | **0.92 (22/24)** |
| faithfulness | 1.00 | |
| citation accuracy (verifier) | 0.95 (44 claims) | |
| abstention precision / recall | 0.80 / 1.00 | |
| safety pass rate | 1.00 | |
| retrieval recall@5 / MRR | 0.93 / 0.82 | |

Judge vs hand labels: 24/24 agreement (kappa 1.00). Caveat: on q-020 the judge failed the reply
for asking the child's age, not for its factual error, so the verdict matched for the wrong
reason. With n = 24, treat kappa 1.00 as "no disagreements on this run", not as a perfect judge.

**What changed and what it fixed**
- KB: Ruby Hall → Manipal in DENT-031/032 (q-029 fixed); DENT-004 hours aligned with DENT-002 §4
  (q-004 consistent); DENT-035 §3 now states the closed-clinic route for a knocked-out tooth.
- Prompt rules: emergency routing for closed hours (q-009 fixed); give the clinician's days when a
  slot can't be confirmed (q-021 fixed); keep conditions exact; show multi-rule calculations.

**Still failing**
- q-020: says 14–17-year-olds may attend orthodontic adjustments "alone"; DENT-029 requires an
  authorised adult. The prompt rule did not fix it and the verifier (gpt-oss-20b) passes the claim.
- q-016: the model now attempts the REF-03 + REF-04 calculation but cites the ₹9,500 crown price
  from a source that lacks it; the deterministic price check correctly replaces the reply with
  the fallback (safe, but unhelpful).

**Free-tier daily limits hit during this round** (Groq, per key, per model): 200k tokens/day,
about 55 generator answers or about 45 judge calls. Daily-quota errors now fail fast instead of
retrying, and each case has a 5-minute timeout.

### 2c. Repair loop, reference expansion, KB clarity fix (2026-11-13)

golden 0.5.0 · kb `0bb7a44de68d` · verifier `gpt-oss-20b` (condition-aware prompt) · one repair
attempt · one-hop reference expansion (≤ 3 rows).

| metric | judge | hand labels |
|---|---|---|
| answer correctness | 0.92 (22/24) | **0.96 (23/24)** |
| faithfulness | 1.00 | |
| citation accuracy | 1.00 (49 claims) | |
| verifier repairs / fallbacks | 1 / 0 | |
| abstention precision / recall | 0.80 / 1.00 | |
| safety pass rate | 1.00 | |

Judge vs labels: 23/24 agreement, kappa 0.65; the one disagreement (q-018) is the judge being
stricter (it wanted the explicit "not guaranteed" wording).

**What fixed what**
- q-020 ("may attend alone"): the condition rule in the verifier catches it (tested offline: 20b
  caught it with 0 false rejections on 7 good replies, same as 120b, so 20b kept); the repair
  loop lets the generator correct it; and the DENT-029 row now says "never alone" explicitly.
- q-016: reference expansion brings in REF-03 (named by the PKG-002 row); the question now
  states what is completed. The bot applies both rules but still lacks the 2026 prices of the
  completed services, so it declines to give a total. **Remaining failure.** Next fix: extend
  expansion to service codes (pull the tariff chunk containing SRV-005A / SRV-002).

**Regression caught and fixed in this round:** the first version of the condition rule was too
literal (rejected "must reach a dentist within 30–60 minutes"), and the repair turned the
knocked-out-tooth handoff into a non-emergency reply, which then fell back to "call reception".
Now: paraphrases that keep the meaning are accepted, and an emergency can never fall back to the
generic text (regression tests added).

### 2d. First holdout run + calculation support (2026-11-13)

Changes measured: tariff expansion (service codes from the question / package rows pull the
2026 price row), arithmetic-aware price check (multiples, sums, differences of source amounts),
patient's question passed to the verifier, private `working` field so the model never "thinks
aloud" in the reply.

| split | hand labels | judge | judge vs labels |
|---|---|---|---|
| **dev** (24) | **0.88 (21/24)** | 0.88 | 22/24, kappa 0.62 |
| **holdout** (5 of 6 run) | **0.80 (4/5)** | 0.80 | 5/5 |

Holdout q-023 did not run (third key's daily Qwen quota exhausted). **Holdout is reported, not
tuned on** — its one failure (q-015, an incomplete summary of POL-CAN-01) is left as is.

Dev failures (hand labels):
- q-016: correct totals were produced, but the total sat in a different sentence from its
  breakdown, so the arithmetic check rejected it → fallback. **Fixed in code after this run**
  (calculated amounts may use amounts anywhere in the reply; quoted prices must still be in the
  cited source).
- q-018: "generally covered up to the sub-limit" with no "not guaranteed" caveat (judge passed it).
- q-029: declined medication correctly but dropped the red-flag emergency route.
- q-018/q-029 addressed with two prompt rules from KB policy (DENT-022 §3, DENT-036 §3);
  **not yet re-measured** (needs a full regeneration once Groq daily quotas reset).

Noted on the way: the model's raw reply on q-016 contained self-corrections ("Wait, let me
re-calculate…") with three different totals; the verifier passed it because each figure was
arithmetically valid. The `working` field removes this class of reply.

## 3. Tuning freeze (2026-11-13)

Review of the day's work: the architecture and eval tooling are on plan, but the answer pipeline
was tuned too hard against 24 dev cases (6 prompt rules added right after specific dev failures;
several mechanisms added for q-016 alone; golden set edited after seeing results; noise never
measured). Decision:

- **Frozen:** no more prompt rules or golden-set edits driven by individual dev failures.
- **q-016 is a known limitation** (multi-rule refund maths), not a target.
- Next, in order: one re-measure on fresh quota (dev + holdout, no holdout-driven changes);
  noise measurement (two uncached dev runs, `--no-cache`); golden set to 50 with *new* questions;
  keep the arithmetic check / tariff expansion only if the re-measure shows they don't hurt
  other cases; then the CI gate.

### 3a. Simplification (2026-11-13)

Removed before the re-measure, so it measures the system we intend to keep (−246 / +31 lines):
- **Table row-chunking** — rejected by measurement (§1b); was dead code behind a 0 flag.
- **Tariff expansion** — built for q-016 only; added ~900 tokens to 8/20 dev prompts.
- **Arithmetic solver in the price check** — built for q-016 only; already produced one citation
  loophole. Policy now: every ₹ amount must appear in its cited source; computed totals fall
  back safely. The answer prompt asks for the published amounts and rules, with reception
  confirming exact totals.

Kept (general, evidence beyond one case): repair loop, emergency stickiness, private `working`
field, question-aware verification, reference expansion, weighted RRF. Retrieval unchanged
after removal (dev hybrid/heading 0.93 / 0.82). q-016 remains a known limitation.

### 3b. Prompt cleanup (2026-11-13)

- Removed dev-case wording used as prompt examples (q-020 "authorised adult… alone", q-024
  "may indicate pulpitis") from the answer, verifier and judge prompts; replaced with generic
  examples. A test now fails if golden-set phrases appear in any prompt.
- Answer prompt: rule 4 now pairs each "never" with its safe alternative (slot → clinician days;
  symptom/medicine → exam + red-flag route; insurance → not guaranteed); the separate slot rule
  was merged in (11 → 10 rules, 626 → 589 words).
- Clinic name, phone numbers, links and emergency contacts are rendered into every prompt from
  `configs/generation.yaml` (`bot.prompts.clinic_prompt`); none are hard-coded in prompt files.

This is the prompt set tomorrow's frozen re-measure evaluates.

## 4. Frozen re-measure (2026-11-14)

First full run of the frozen system (git `446b892`, golden 0.5.0, kb `08a3673838d7`, prompts
answer `e3b7015348` / judge `9e8082fbb6`). Fresh quota; no changes made in response to results.

| metric | dev (24) | holdout (6) |
|---|---|---|
| answer correctness | 0.96 (22/23 scored) | 0.83 (5/6) |
| behaviour accuracy | 1.00 | 0.83 |
| faithfulness | 1.00 | 1.00 |
| citation accuracy | 1.00 (45 claims) | 1.00 (13 claims) |
| verifier repairs / fallbacks | 0 / 0 | 1 / 0 |
| abstention precision / recall | 1.00 / 1.00 | 1.00 / 1.00 |
| safety pass rate | 1.00 | - |
| retrieval recall@5 / MRR | 0.92 / 0.85 | 0.80 / 0.90 |
| latency p50 / p95 (free tier) | 30.8 / 38.8 s | 32.5 / 36.8 s |
| errors | 1 | 0 |

Failures (recorded, not tuned):
- **q-016** (dev) — error: Groq per-minute input-token limit (429) after retries; not scored.
  Known limitation case anyway.
- **q-024** (dev) — declined diagnosis correctly but omitted the booking link / reception number.
- **q-015** (holdout, exact_id) — answered with a summary of the cancellation policy but left out
  the fee tiers and the repeat no-show rule.

Small n: one case = 4 points on dev, 17 on holdout. Noise not yet measured (next step).

### 4a. CI gate baseline (2026-11-14)

No separate noise run: all models run at temperature 0 and CI replays cached cases, so the
soft-fail threshold is set by case size instead — `max_drop_points: 7` tolerates one flipped
case on the 30-case set (~3.3 pts each) and fails on two. Baseline committed as
`evals/baseline.json` (`--split all`, git `7fab8f7`): correctness **0.90 (27/30)**, behaviour
0.93, faithfulness 1.00, citations 1.00 (61 claims), abstention P/R 1.00 / 1.00, safety 1.00,
recall@5 / MRR 0.90 / 0.84, 0 errors. Failing in baseline: q-015, q-016 (scored this time;
known limitation), q-024.

### 4b. Golden set 0.6.0 — 50-case baseline (2026-11-14)

20 new cases (q-031 – q-050; see `evals/golden/CHANGELOG.md`). Baseline re-committed on all 50:
correctness **0.84 (41/49 scored)**, behaviour 0.96, faithfulness 1.00, citations 1.00
(105 claims), abstention P/R 1.00 / 1.00, safety 1.00, recall@5 / MRR 0.92 / 0.85.

Failing: q-015, q-016, q-024 (as before); q-035, q-037, q-038, q-041 (new — omitted secondary
details; no unsupported facts). Noted, not fixed: q-043 errored on Groq's per-minute input limit
(not scored); q-047 abstained correctly but failed a checklist item whose wording ("… / care is
at the Baner Road clinic") the judge read as requiring both halves.

**Update (golden 0.6.1):** q-047 checklist reworded (see CHANGELOG) and the baseline re-scored.
q-047 now passes; q-043 ran this time (0 errors) and failed — it omitted the ≥24 h full-refund
tier. Baseline: correctness **0.84 (42/50)**; failing q-015, q-016, q-024, q-035, q-037, q-038,
q-041, q-043.

**Gate threshold (golden 0.6.1):** `max_drop_points` 7 → 3. The 7 was set for 30 cases and
actually tolerated two flips; with 50 cases (2 pts each) 3 keeps the rule "one flip passes,
two fail".
