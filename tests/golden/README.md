# Golden fixture (hand-written)

Files: `data/sample/{ledger,psp,bank}.csv` (step 1), `data/sample/extra/*` (step 2), `data/sample/late/bank_late.csv` (step 3).
Expected answers: `expected.json` — written by hand from the table below **before** the engine was run on this
fixture. The engine never reads this file; `tests/test_golden.py` compares engine output to it.

| # | Scenario | Records | Expected |
|---|----------|---------|----------|
| S01 | Normal: gross 1000 / fee 20 / net 980 | L-1001, PSP-1001, STL-SG-0703, B-001 | A1 match; B1 match |
| S02 | 50 missing, unexplained | L-1002 500.00 vs PSP-1002 450.00 USD | A: AMOUNT_MISMATCH +50.00; B still matches (PSP net paid in full) |
| S03 | Refund across days | L-1003 07-02 vs PSP-1003 07-03 (-200) in STL-SG-0704 (net 94.00) | A1 match (1d ≤ 2d); B1 match |
| S04 | Bank split | STL-SG-0705 net 5000 vs B-004 3000 + B-005 2000 | B2S split match |
| S05 | Bank merged | STL-US-0706A 970 + 0706B 485 vs B-006 1455 | B2M merged match |
| S06 | Same-amount ambiguity | STL-EU-0707A/B both 490, B-007 490 no reference | AMBIGUOUS_CANDIDATES x2, nothing auto-matched |
| S07 | Duplicate file / renamed duplicate / overlapping re-send | ledger.csv again, extra/ledger_renamed_copy.csv, extra/ledger_resend_overlap.csv | DUPLICATE_FILE x2; overlap: 1 duplicate row + 1 new row |
| S08 | Conflicting key | L-1010 twice with 750 / 705; B-018 twice with 20 / 21 | second row quarantined; A SOURCE_CONFLICT; B SOURCE_CONFLICT |
| S09 | Bad schema / amount / date | extra/bank_missing_amount_column.csv; L-1011 `64.0O`; L-1026 `10.005`; L-1012 `2026-13-01`; PSP-1099 net≠gross−fee | file REJECTED; rows quarantined with codes |
| S10 | Currency / account mismatch | ORD-1013 EUR vs USD; ORD-1032 under MER-SG-01 vs MER-SG-02; B-009 to BANK-SG-002 | CURRENCY_MISMATCH; ACCOUNT_MISMATCH x2 (A); ACCOUNT_MISMATCH (B) |
| S11 | Date boundaries | ORD-1015 (2d) vs ORD-1016 (3d); STL-SG-0706 bank +3d vs STL-US-0703 bank +4d | match / DATE_OUT_OF_TOLERANCE; match / DATE_OUT_OF_WINDOW |
| S12 | Partial receipt | STL-EU-0705 net 2000 vs B-014 1200 | PARTIAL_RECEIPT outstanding 800.00 |
| S13 | Not yet due / in transit | PSP-1017 available 07-12; STL-SG-0710 settles on as-of | NOT_DUE; IN_TRANSIT (no case) |
| S14 | Late bank data then re-run | bank_late.csv: B-020 (800 remainder), B-021 (97), B-022 (147); re-run as-of 07-11 | STL-EU-0705 split match; STL-US-0704 match; both cases AUTO_CLEARED; STL-SG-0710 match |
| S15 | One-sided | L-1021 no PSP; PSP-1022 no ledger; B-015 interest; PSP-1011 unsettled since 07-03 | MISSING_IN_PSP; MISSING_IN_LEDGER; UNEXPECTED_BANK_CREDIT; UNSETTLED_OVERDUE |
| S16 | No-reference unique amount | STL-EU-0704 147 vs B-010 147 "SEPA CREDIT" | B3 match |
| S17 | Amount-only group | STL-EU-0706A 98 + 0706B 196 vs B-011 294 no reference | SUGGESTED_GROUP (review, not matched) |
| S18 | Same business_ref, different merchants | ORD-2001 under MER-US-01 (USD) and MER-SG-01 (SGD) | two independent A1 matches, never merged |
| S19 | Grace period | L-1027 (1 day old, no PSP) | PENDING, no case |
| S20 | As-of cutoff | L-1031 dated 07-11; B-017 dated 07-12; STL-US-0711 settles 07-11 | excluded at as-of 07-10; batch treated as unsettled |
| S21 | PSP fee row | PSP-FEE-0708 -25.00 in STL-SG-0708 | chain A OUT_OF_SCOPE; included in batch net 710.00 |
