# Eval report — all split

git `97d43cd` · golden 0.6.1 · kb `08a3673838d7` · prompts answer `e3b7015348` judge `9e8082fbb6`

| metric | value |
|---|---|
| answer correctness | 0.84 |
| behaviour accuracy | 0.96 |
| faithfulness | 1.00 |
| citation accuracy | 1.00 (108 claims) |
| verifier repairs / fallbacks | 3 / 0 |
| abstention precision / recall | 1.00 / 1.00 |
| safety pass rate | 1.00 |
| retrieval recall@5 / MRR | 0.92 / 0.85 |
| latency p50 / p95 | 38957 / 38957 ms (1 live, free-tier throttling included) |
| errors | 0 / 50 |

## Correctness by type

| type | correct |
|---|---|
| ambiguous | 1.00 |
| exact_id | 0.83 |
| followup | 1.00 |
| lookup | 0.84 |
| multi_source | 0.50 |
| safety | 1.00 |
| unanswerable | 0.83 |

## Failed cases

- **q-015** (exact_id, expected answer, got answer): The reply omits the detailed tiered fees, refund rules, and repeat no‑show consequences required by the question, so it does not fulfill the expected answer behavior. Missing: ['Three cancellation notice tiers: ≥24h free cancellation with full refund of ₹500 deposit; 4‑to\u202f<24h incurs ₹300 fee (₹200 refund or credit); <4h or no‑show forfeits ₹500 deposit or fee', 'Two consecutive no‑shows in 12 months trigger mandatory ₹500 advance deposits for future bookings until two consecutive on‑time visits'].
- **q-016** (multi_source, expected answer, got answer): Bot did not provide the total loss or refundable amount, so it failed to answer the question fully. Missing: ['Total loss amount is ₹12,750', 'Refundable amount is ₹2,050', 'Deductions include ₹6,800 for completed root canal, ₹700 for two X‑rays, ₹500 admin fee, and ₹4,750 (50% lab fee)'].
- **q-024** (unanswerable, expected abstain, got answer): The reply correctly declines diagnosis and notes that an exam and X‑ray are needed, but it fails to give the required booking link or phone number. Missing: ['Provides a booking link or reception phone number for scheduling', 'booking link or reception +91-20-5550-0140'].
- **q-035** (lookup, expected answer, got answer): Bot correctly gave filing channels and the 24‑hour acknowledgment SLA but omitted the resolution‑time SLAs required by the reference. Missing: ['Level\u202f1 complaints resolved within 5 working days; Level\u202f2 complaints resolved within 7 working days'].
- **q-037** (lookup, expected answer, got answer): The reply answers the question and includes the main no‑sale statement and limited sharing with partners, but omits the statutory/judicial disclosure clause from the reference. Missing: ['Data may be disclosed to statutory or judicial authorities when required by Indian law.'].
- **q-038** (lookup, expected answer, got answer): The reply answers the question and includes most key facts, but does not explicitly state the typical 5‑14 day resolution window. Missing: ['Sensitivity typically resolves within 5 to 14 days'].
- **q-041** (multi_source, expected answer, got answer): The reply gives the correct price per tooth and deposit but omits the pre‑op OPG cost and the emergency‑bleeding contact, and it leaves out several after‑care warnings; all stated facts are supported by sources. Missing: ['Surgical impacted wisdom tooth extraction costs ₹6,500 per tooth, plus a ₹1,000 pre‑op OPG X‑ray and a ₹500 slot‑hold deposit', 'If bright‑red bleeding continues beyond 45\u202fminutes, call +91-20-5550-0199 immediately (or seek emergency care)'].
- **q-043** (multi_source, expected answer, got answer): Bot answered but omitted the free‑cancellation (≥24 h) refund tier, a key fact needed to fully address the question. Missing: ['Cancelling ≥24 hours in advance gives 100% refund of the ₹500 deposit'].
