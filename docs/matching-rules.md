# Matching rules (rules version 2026.09-v1)

Code: `recon/matching.py` (pure function `run_engine`). Parameters: `Rules` dataclass (defaults below), printed
in every EOD report.

## Principles
1. **Two independent chains.** Chain A compares *gross* (ledger ↔ PSP transactions). Chain B compares *net*
   (PSP settlement batch ↔ bank). Bank net is never compared with ledger gross.
2. **Exact money.** Integer minor units; no amount tolerance anywhere.
3. **Never cross** merchant/bank account or currency.
4. **Auto-match only when unique and sufficiently evidenced.** Otherwise: review case with candidates listed.
5. **Deterministic and order-independent.** Records are sorted by ID; candidate maps are built completely
   *before* any decision (no greedy consumption). Tested with shuffled inputs.
6. **No hidden knowledge.** The engine does no I/O, never sees truth files or scenario labels, and never parses
   meaning from ID formats — references are compared as whole tokens against known batch IDs only.
7. **Invariants enforced three times**: in the engine (raise), in the DB (`CHECK`, `UNIQUE`), and in tests.

## Parameters
| Name | Default | Meaning |
|------|---------|---------|
| `a_date_tolerance_days` | 2 | |ledger event_date − PSP created_date| allowed for A1 |
| `a_grace_days` | 2 | one-sided chain A item younger than this is `PENDING`, not an exception |
| `b_window_before` / `b_window_after` | 1 / 3 | bank value_date ∈ [settlement_date − 1, settlement_date + 3] |
| `unsettled_grace_days` | 2 | unsettled PSP item overdue when as_of > available_on + 2 |
| `max_candidates` | 12 | cap for amount-only subset search (B4) and competing-split check |
| `max_group_size` | 4 | max members on the "many" side of a B4 suggestion |
| `max_ref_group` | 10 | max members on the "many" side of a reference group (B2S / B2M) |

## Chain A — ledger gross ↔ PSP transaction gross
Key: `(merchant_account, business_ref)`. PSP `fee` rows are `OUT_OF_SCOPE` for chain A (rule `A0_FEE_ROW`).

| Order | Condition | Result | Rule |
|-------|-----------|--------|------|
| 1 | either side blocked by a source conflict | `SOURCE_CONFLICT` case | `A-X1_CONFLICT` |
| 2 | both sides; currency differs | `CURRENCY_MISMATCH` | `A-X3_CURRENCY` |
| 3 | both sides; SALE↔charge / REFUND↔refund differs | `TYPE_MISMATCH` | `A-X4_TYPE` |
| 4 | both sides; gross differs (even by 0.01) | `AMOUNT_MISMATCH`, amount = ledger − PSP | `A-X5_AMOUNT` |
| 5 | both sides; |date diff| > 2 | `DATE_OUT_OF_TOLERANCE` | `A-X6_DATE` |
| 6 | both sides; all equal, |date diff| ≤ 2 | **MATCH** | `A1_EXACT_REF` |
| 7 | one side only, same `business_ref` exists on the other side under a *different* merchant | `ACCOUNT_MISMATCH` (one case per merchant key) | `A-X2_ACCOUNT` |
| 8 | one side only, age ≤ 2 days | `PENDING` (no case) | `A-T1_GRACE` |
| 9 | ledger only | `MISSING_IN_PSP` | `A-X7_NO_PSP` |
| 10 | PSP only | `MISSING_IN_LEDGER` | `A-X8_NO_LEDGER` |

Refunds are just negative events with their own reference; a refund booked one day before the PSP processes it
matches under the date tolerance (fixture S03).

## Chain B — PSP settlement net ↔ bank
Settlement = PSP items grouped by `settlement_batch_id`, net = Σ item net (includes fee rows).

**Pre-steps**
- Unsettled PSP item: `NOT_DUE` while as_of ≤ available_on + 2, else `UNSETTLED_OVERDUE` case (`B-X1`).
- Bank line with conflicting source rows → `SOURCE_CONFLICT` (`B-X2`).
- Batch inconsistent (items disagree on account/currency/date/merchant) or containing a conflicted item →
  `BATCH_INCONSISTENT` / `SOURCE_CONFLICT` (`B-X3`); bank lines referencing it are held with the case.

**Step 1 — reference evidence.** Build links "bank line reference contains batch ID as a whole token", take
connected components (settlements ⟷ bank lines):
| Component shape | Condition | Result | Rule |
|---|---|---|---|
| any | a linked line is in another bank account / currency | `ACCOUNT_MISMATCH` / `CURRENCY_MISMATCH`, nothing matched | `B-X7` |
| 1 settlement : 1..n lines | exactly one line = net and in window, and **no other combination of the linked lines also sums to net** | **MATCH** 1:1; any extra linked lines → `DUPLICATE_REFERENCE_CREDIT` | `B1_REF_1TO1` / `B-X8` |
| 1 : n | exact line exists but another subset also conserves the net | `AMBIGUOUS_CANDIDATES` | `B-X4` |
| 1 : n | two or more exact lines | `AMBIGUOUS_CANDIDATES` | `B-X4` |
| 1 : n (n ≥ 2, ≤ 10) | Σ lines = net, all in window | **MATCH split** | `B2S_REF_SPLIT` |
| 1 : n | Σ = net but a date outside window | `DATE_OUT_OF_WINDOW` | `B-X9` |
| 1 : n | same sign and Σ < net | `PARTIAL_RECEIPT`, amount = outstanding | `B-X10` |
| 1 : n | otherwise | `AMOUNT_MISMATCH` | `B-X11` |
| m : 1 (m ≤ 10) | Σ nets = line amount, all in window | **MATCH merged** | `B2M_REF_MERGED` |
| m : 1 | otherwise | `AMOUNT_MISMATCH` / `DATE_OUT_OF_WINDOW` | `B-X12` |
| m : n | — | `COMPLEX_REFERENCE` review | `B-X13` |

Settlements and lines in a failed reference component are **not** passed to amount-only steps: the reference
already says they belong together, and an amount guess must not contradict it.

**Step 2 — B3, amount + date, no reference.** For remaining lines/settlements in the same (bank account,
currency): candidate = exact amount and value date in window. Auto-match only if the line has exactly one
candidate settlement **and** that settlement has exactly one candidate line (`B3_AMOUNT_DATE_UNIQUE`). If any
side has ≥ 2 candidates, every involved settlement gets `AMBIGUOUS_CANDIDATES` (`B-X4`) — decided from complete
candidate maps, so the order of rows cannot change the outcome (tested with a greedy-trap case).

**Step 3 — B4, amount-only groups (suggest only).** For each remaining line, search 2..4 remaining settlements
(same account/currency/window/sign, each smaller than the line) that sum exactly to it; symmetric search for one
settlement ↔ 2..4 lines. Skip and note if > 12 candidates. Exactly one combination → `SUGGESTED_GROUP`; several
→ `AMBIGUOUS_CANDIDATES`. **Never auto-matched**: coincidental subset sums grow combinatorially with volume, so
amount alone is not sufficient evidence for a group (rule `B4_AMOUNT_GROUP_SUGGESTION`).

**Step 4 — leftovers.** Settlement with no candidate: `IN_TRANSIT` while as_of ≤ settlement_date + 3, else
`MISSING_IN_BANK` (`B-X5`). Bank line with no counterpart: `UNEXPECTED_BANK_CREDIT` / `…_DEBIT` (`B-X6`).

## Timing states (not exceptions)
`PENDING` (chain A grace), `NOT_DUE` (unsettled PSP item), `IN_TRANSIT` (settlement awaiting bank). These appear
in totals as "unmatched" but do not open cases until their grace period ends. `PARTIAL_RECEIPT` is raised
immediately (a design choice: a short payment is worth a look even inside the window); if the remainder arrives
the case is auto-cleared on the next run.

## Case lifecycle across runs
| Situation on re-run | Effect | Actor |
|---|---|---|
| New exception subject | case created `Open` | system |
| Same subject still an exception | engine fields refreshed (type/rule/amount/evidence changes are audited); owner, status, notes untouched | system |
| Subject no longer an exception, case not Resolved | `Resolved` + disposition `AUTO_CLEARED`, reason cites new status/rule/group | system |
| Subject no longer an exception, case Resolved manually | stays as is; `CASE_ENGINE_CLEARED` audit note | system |
| Issue re-appears on an AUTO_CLEARED case | re-opened | system |
| Issue still present on a manually Resolved case | stays Resolved, shown as "still unmatched" in UI and report | — |

Case identity: `A|<merchant>|<business_ref>`, `B|STL|<batch>`, `B|BANK|<line>`, `B|ITEM|<psp item>`.

## Benchmark definitions
See `tests/evaluation/benchmark.py`. Chain A denominator = truth pairs present on both sides; "clean" pairs are
those the generator created with equal amount/currency/merchant and date gap 0–2 days. Chain B denominator =
truth payout groups; "v1-matchable" = labels `clean_ref`, `clean_noref`, `split`, `merge`. A false match is any
engine group not exactly equal to a truth group (partial overlaps count as false). Results in
`benchmark/results/`.

## Known limitations of the rules
- B3 trusts amount + window + account when unique both ways. A missing true bank line plus an unrelated credit
  of the same amount in the window *can* produce a false B3 match; no such case occurred in the benchmark runs,
  but that is a property of the synthetic data, not a guarantee.
- No fuzzy reference matching (typos in references fall back to amount rules).
- No FX, no disputes / reserves / chargebacks lifecycle.
