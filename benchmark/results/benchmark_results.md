# Benchmark results (synthetic data) — rules 2026.09-v1

Generated 2026-09-26 on Linux-6.18.44-fc-v37-x86_64-with-glibc2.39 / Python 3.11.15. Synthetic generator only; **not** production accuracy and **not** employer data.

Denominators: see `tests/evaluation/benchmark.py` docstring and `docs/matching-rules.md` §Benchmark.

| dataset | profile | seed | ledger / psp / bank rows | A auto-matched | A false (÷ auto) | A coverage clean | B auto groups | B false (÷ auto) | B coverage v1-matchable | import s | reconcile s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| dev_seed42_default | default | 42 | 2973 / 2973 / 566 | 2899 | 0 / 2899 | 2899 / 2899 (100.0%) | 512 | 0 / 512 | 512 / 512 (100.0%) | 0.19 | 0.19 |
| holdout_seed7_collide | collide | 7 | 2964 / 2967 / 552 | 2887 | 0 / 2887 | 2887 / 2887 (100.0%) | 503 | 0 / 503 | 503 / 503 (100.0%) | 0.18 | 0.19 |
| holdout_seed2027_messy | messy | 2027 | 2905 / 2899 / 560 | 2652 | 0 / 2652 | 2652 / 2652 (100.0%) | 468 | 0 / 468 | 468 / 468 (100.0%) | 0.19 | 0.19 |
| holdout_seed99_adversarial | adversarial | 99 | 2975 / 2967 / 2970 | 2901 | 0 / 2901 | 2901 / 2901 (100.0%) | 1285 | 0 / 1285 | 1285 / 2714 (47.3%) | 0.24 | 1.02 |
| scale_seed42_default_30k | default | 42 | 29699 / 29692 / 572 | 28947 | 0 / 28947 | 28947 / 28947 (100.0%) | 533 | 0 / 533 | 533 / 533 (100.0%) | 1.94 | 2.06 |

## dev_seed42_default

- Chain B by truth label (engine exact / truth groups): clean_noref 66/66, clean_ref 406/406, late_bank 0/7, merge 15/15, missing_bank 0/11, partial 0/5, split 25/25
- Chain B groups by rule: {'B1_REF_1TO1': 406, 'B2M_REF_MERGED': 15, 'B2S_REF_SPLIT': 25, 'B3_AMOUNT_DATE_UNIQUE': 66}
- Chain B false matches by rule: none
- Chain A anomalous truth pairs auto-matched: none
- Unresolved by type: {'A:AMOUNT_MISMATCH': 18, 'A:DATE_OUT_OF_TOLERANCE': 30, 'A:MISSING_IN_LEDGER': 26, 'A:MISSING_IN_PSP': 26, 'B:DATE_OUT_OF_WINDOW': 7, 'B:MISSING_IN_BANK': 11, 'B:PARTIAL_RECEIPT': 5, 'B:UNEXPECTED_BANK_CREDIT': 5}

## holdout_seed7_collide

- Chain B by truth label (engine exact / truth groups): clean_noref 187/187, clean_ref 272/272, late_bank 0/4, merge 23/23, missing_bank 0/16, partial 0/3, split 21/21
- Chain B groups by rule: {'B3_AMOUNT_DATE_UNIQUE': 187, 'B1_REF_1TO1': 272, 'B2S_REF_SPLIT': 21, 'B2M_REF_MERGED': 23}
- Chain B false matches by rule: none
- Chain A anomalous truth pairs auto-matched: none
- Unresolved by type: {'A:AMOUNT_MISMATCH': 17, 'A:DATE_OUT_OF_TOLERANCE': 28, 'A:MISSING_IN_LEDGER': 35, 'A:MISSING_IN_PSP': 32, 'B:DATE_OUT_OF_WINDOW': 4, 'B:MISSING_IN_BANK': 16, 'B:PARTIAL_RECEIPT': 3, 'B:UNEXPECTED_BANK_CREDIT': 10}

## holdout_seed2027_messy

- Chain B by truth label (engine exact / truth groups): clean_noref 83/83, clean_ref 294/294, late_bank 0/8, merge 52/52, missing_bank 0/12, partial 0/11, split 39/39
- Chain B groups by rule: {'B1_REF_1TO1': 294, 'B2S_REF_SPLIT': 39, 'B3_AMOUNT_DATE_UNIQUE': 83, 'B2M_REF_MERGED': 52}
- Chain B false matches by rule: none
- Chain A anomalous truth pairs auto-matched: none
- Unresolved by type: {'A:AMOUNT_MISMATCH': 67, 'A:DATE_OUT_OF_TOLERANCE': 86, 'A:MISSING_IN_LEDGER': 94, 'A:MISSING_IN_PSP': 100, 'B:DATE_OUT_OF_WINDOW': 8, 'B:MISSING_IN_BANK': 12, 'B:PARTIAL_RECEIPT': 11, 'B:UNEXPECTED_BANK_CREDIT': 16}

## holdout_seed99_adversarial

- Chain B by truth label (engine exact / truth groups): clean_noref 32/1461, clean_ref 1146/1146, late_bank 0/67, merge 60/60, missing_bank 0/96, partial 0/30, split 47/47
- Chain B groups by rule: {'B1_REF_1TO1': 1146, 'B2S_REF_SPLIT': 47, 'B2M_REF_MERGED': 60, 'B3_AMOUNT_DATE_UNIQUE': 32}
- Chain B false matches by rule: none
- Chain A anomalous truth pairs auto-matched: none
- Unresolved by type: {'A:AMOUNT_MISMATCH': 15, 'A:DATE_OUT_OF_TOLERANCE': 26, 'A:MISSING_IN_LEDGER': 25, 'A:MISSING_IN_PSP': 33, 'B:AMBIGUOUS_CANDIDATES': 1521, 'B:DATE_OUT_OF_WINDOW': 67, 'B:MISSING_IN_BANK': 4, 'B:PARTIAL_RECEIPT': 30, 'B:UNEXPECTED_BANK_CREDIT': 4}

## scale_seed42_default_30k

- Chain B by truth label (engine exact / truth groups): clean_noref 50/50, clean_ref 440/440, late_bank 0/1, merge 24/24, missing_bank 0/7, partial 0/1, split 19/19
- Chain B groups by rule: {'B1_REF_1TO1': 440, 'B3_AMOUNT_DATE_UNIQUE': 50, 'B2S_REF_SPLIT': 19, 'B2M_REF_MERGED': 24}
- Chain B false matches by rule: none
- Chain A anomalous truth pairs auto-matched: none
- Unresolved by type: {'A:AMOUNT_MISMATCH': 150, 'A:DATE_OUT_OF_TOLERANCE': 295, 'A:MISSING_IN_LEDGER': 300, 'A:MISSING_IN_PSP': 307, 'B:DATE_OUT_OF_WINDOW': 1, 'B:MISSING_IN_BANK': 7, 'B:PARTIAL_RECEIPT': 1, 'B:UNEXPECTED_BANK_CREDIT': 5}
