# UAT traceability (v1)

**Important:** the "Evidence" column below is **developer verification** (automated tests, the author's own UI
run, logs). It is **not** user acceptance. The "External UAT" column is empty because no external user has tested
the tool yet. Test IDs refer to `docs/evidence/pytest_output.txt` (127 passed, 0 skipped, run on 2026-09-26).

| Req | Use case / acceptance check | Developer evidence (actual result) | External UAT |
|-----|-----------------------------|------------------------------------|--------------|
| ING-01/02 | Import 3 sources; each record shows file + SHA + row | `test_golden::test_step1_imports` pass; case page evidence table (screenshot 04) | — |
| ING-03 | `0.001`, `NaN`, `Infinity`, `1e3`, `1,000.00`, huge amounts rejected; no rounding | `test_ingest::test_parse_amount_rejects[*]` 22 cases pass | — |
| ING-04 | Missing column / duplicate header / empty / non-UTF-8 → whole file rejected, nothing loaded | `test_missing_column_rejects_whole_file`, `test_duplicate_header_rejected`, `test_empty_and_non_utf8_rejected` pass; walkthrough step 2 `REJECTED` | — |
| ING-05 | 3 rows, 3rd invalid: default PARTIAL (2 loaded, 1 quarantined, totals 30.00); strict: rolled back | `test_three_rows_last_invalid_*` pass; UI flash "PARTIAL" (`test_web_upload_partial_and_rejected_are_not_shown_as_success`) | — |
| ING-06/07 | Same file / renamed copy imported once; overlapping re-send skips duplicate rows | `test_step2_duplicate_and_rejected_files`; walkthrough step 2 | — |
| ING-08 | Conflicting key quarantined, blocks matching, `SOURCE_CONFLICT` case | golden ORD-1010, B-018 | — |
| ING-10 | Sample button + upload with synthetic-only warning | Playwright test; screenshot 01 | — |
| REC-01/02 | Chains A and B separate; gross vs net never mixed | golden chain A/B tests; report sections 2/3 + bridge | — |
| REC-03 | Accounts/merchants/currencies never crossed; same ref under 2 merchants stays separate | `test_currency_and_account_never_crossed`, `test_same_ref_different_merchants_not_merged` | — |
| REC-04 | 1:1, split, merged, partial, late data | golden S01, S04, S05, S12, S14 | — |
| REC-05 | Same-amount and greedy-trap ambiguity not auto-matched | `test_two_settlements_one_bank_*`, `test_greedy_trap_*` (6 orders), D-04 test | — |
| REC-06 | No double consumption; amounts conserved | engine invariant + DB `UNIQUE`/`CHECK`; `test_record_never_consumed_twice_*` | — |
| REC-07 | Row order irrelevant | `test_engine_result_independent_of_input_order` (25 shuffles), `test_shuffled_csv_files_*` | — |
| REC-08 | Grace / not-due / in-transit are not exceptions; as-of cutoff | `test_chain_a_grace_boundary`, `test_as_of_in_the_past_excludes_future_records` | — |
| REC-09 | Engine cannot read truth | `test_engine_code_cannot_reach_ground_truth` | — |
| EXC-01..03 | Case page fields; resolve needs disposition + reason; reopen needs reason | `test_resolve_requires_disposition_and_reason`; Playwright resolve flow | — |
| EXC-04 | Manual resolution creates no match, changes no amount | `test_manual_resolution_never_creates_match_or_changes_amount`; "still unmatched" badge | — |
| EXC-05 | Audit/notes append-only; no edit routes | `test_audit_and_notes_are_append_only`, `test_web_resolve_validation_and_no_edit_endpoints`; walkthrough step 7 | — |
| EXC-06/07 | Re-run keeps notes; no duplicate cases; late data auto-clears; backward as-of is read-only | `test_rerun_same_data_is_idempotent`, `test_step3_late_data_rerun`, `test_backward_as_of_run_is_read_only_for_cases` | — |
| RPT-01/02 | EOD CSV + HTML, per currency, bridge, DQ, overdue, rules version; historical report run-consistent | `test_eod_report_per_currency_and_chains_separate`, `test_historical_report_is_run_consistent`; `docs/evidence/sample_reports/` | — |
| RPT-03 | Runs from a clean clone | Clean clone + fresh venv: 121 tests passed and `demo` succeeded (before reviewer fixes; re-verified in STATUS.md) | — |
| EVAL-01..03 | Golden ≥15 scenarios; generator; benchmark with denominators | 21 scenarios; `benchmark/results/` | — |

## External UAT script (to be run with a real user — not yet done)
1. Load sample, reconcile as-of 2026-07-10. Can you explain from the screen why ORD-1002 is not matched?
2. Find the ambiguous EUR payouts. Would you accept the tool's refusal to auto-match? What evidence would you use?
3. Work the partial receipt: assign, note, In review. Upload the late bank file and re-run as-of 2026-07-11.
4. Resolve the bank-interest credit. Is "still unmatched" clear enough?
5. Open the EOD report. Is anything missing for your team lead?
Record: tester role, date, pass/fail per step, comments → `defects.md`.
