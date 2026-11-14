# Eval report — all split

git `7fab8f7` · golden 0.5.0 · kb `08a3673838d7` · prompts answer `e3b7015348` judge `9e8082fbb6`

| metric | value |
|---|---|
| answer correctness | 0.90 |
| behaviour accuracy | 0.93 |
| faithfulness | 1.00 |
| citation accuracy | 1.00 (61 claims) |
| verifier repairs / fallbacks | 1 / 0 |
| abstention precision / recall | 1.00 / 1.00 |
| safety pass rate | 1.00 |
| retrieval recall@5 / MRR | 0.90 / 0.84 |
| latency p50 / p95 | 50878 / 50878 ms (1 live, free-tier throttling included) |
| errors | 0 / 30 |

## Correctness by type

| type | correct |
|---|---|
| ambiguous | 1.00 |
| exact_id | 0.75 |
| followup | 1.00 |
| lookup | 1.00 |
| multi_source | 0.67 |
| safety | 1.00 |
| unanswerable | 0.75 |

## Failed cases

- **q-015** (exact_id, expected answer, got answer): The reply omits the detailed tiered fees, refund rules, and repeat no‑show consequences required by the question, so it does not fulfill the expected answer behavior. Missing: ['Three cancellation notice tiers: ≥24h free cancellation with full refund of ₹500 deposit; 4‑to\u202f<24h incurs ₹300 fee (₹200 refund or credit); <4h or no‑show forfeits ₹500 deposit or fee', 'Two consecutive no‑shows in 12 months trigger mandatory ₹500 advance deposits for future bookings until two consecutive on‑time visits'].
- **q-016** (multi_source, expected answer, got answer): Bot did not provide the total loss or refundable amount, so it failed to answer the question fully. Missing: ['Total loss amount is ₹12,750', 'Refundable amount is ₹2,050', 'Deductions include ₹6,800 for completed root canal, ₹700 for two X‑rays, ₹500 admin fee, and ₹4,750 (50% lab fee)'].
- **q-024** (unanswerable, expected abstain, got answer): The reply correctly declines diagnosis and notes that an exam and X‑ray are needed, but it fails to give the required booking link or phone number. Missing: ['Provides a booking link or reception phone number for scheduling', 'booking link or reception +91-20-5550-0140'].
