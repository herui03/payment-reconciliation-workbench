# Data dictionary (v1, rules 2026.09-v1)

All three inputs are UTF-8 CSV with a header row. Column names are case-insensitive and trimmed. Unknown extra
columns are **ignored and reported** in the import message. A missing required column, a duplicated column name,
an empty file or a non-UTF-8 file **rejects the whole file** (nothing loaded, rejection logged).

Row numbers everywhere (evidence, data-quality pages, reports) are **file rows with the header = row 1**, so the
first data row is row 2 — the number you see in a spreadsheet.

## Common value rules
| Item | Rule | On violation |
|------|------|--------------|
| Amount | Plain decimal: optional sign, digits, optional `.` + digits (`1000`, `-200.5`, `+5`). Surrounding spaces are trimmed. | `BAD_AMOUNT` (row quarantined) |
| Rejected amount forms | `0.001` / `10.005` (more decimals than the currency allows — never rounded), `NaN`, `Infinity`, `1e3`, `1,000.00`, `$10`, `.5`, `12.`, blank | `BAD_AMOUNT` |
| Amount range | absolute value < 1,000,000,000,000 major units (sanity ceiling; keeps sums inside SQLite 64-bit integers) | `BAD_AMOUNT` |
| Storage | integer **minor units** (cents); parsed with `Decimal`, never `float`. No tolerance is applied anywhere. | — |
| Currency | `SGD`, `USD`, `EUR` (2 decimals each). No FX conversion. | `UNSUPPORTED_CURRENCY` |
| Date | ISO `YYYY-MM-DD`, must be a real calendar date | `BAD_DATE` |
| Required text | non-blank after trim | `MISSING_VALUE` |
| Row shape | same number of cells as the header | `BAD_ROW_SHAPE` |

## Uniqueness scope (important)
| Key | Scope in v1 | Duplicate with same content | Same key, different content |
|-----|-------------|-----------------------------|-----------------------------|
| `ledger_entry_id`, `psp_txn_id`, `bank_txn_id` (record IDs) | **Globally unique within each source**, across all merchants / bank accounts. A bank reusing a txn id on a different account is treated as a conflict, never merged. | skipped as `DUPLICATE_ROW` (idempotent re-send) | quarantined `CONFLICT_RECORD_ID`; the loaded record is **blocked** from auto-matching and a `SOURCE_CONFLICT` case is raised |
| Business key: `(merchant_account, business_ref)` for ledger rows and PSP charge/refund rows | Unique **per merchant account**. The same `business_ref` under two merchants is two different events and is matched independently (fixture S18). | as above | quarantined `CONFLICT_BUSINESS_KEY`; key blocked; `SOURCE_CONFLICT` case |
| Settlement batch id | Globally unique. Items of one batch must agree on merchant, currency, bank account and settlement date. | — | batch not matchable: `BATCH_INCONSISTENT` case |
| File | SHA-256 of the bytes. Same bytes under any file name → `DUPLICATE_FILE`, nothing loaded. | — | — |

Which of two conflicting rows is stored depends on file order ("first loaded wins" storage), but the outcome does
not: the key is blocked and both rows are shown as evidence, so match results are order-independent (tested).

## 1. Internal ledger (`source = ledger`)
**Grain:** one row per business event booked by the merchant (a sale, or a refund). Amounts are **gross**
(before PSP fees).

| Column | Req | Meaning |
|--------|-----|---------|
| `ledger_entry_id` | ✓ | Record ID (unique per source). |
| `event_type` | ✓ | `SALE` or `REFUND`. |
| `business_ref` | ✓ | Order / refund reference sent to the PSP (e.g. `ORD-1001`, `RFD-1003`). |
| `original_ref` | ✓ col, value required for REFUND | Order the refund belongs to. Informational in v1. |
| `merchant_account` | ✓ | Merchant / PSP account the event belongs to. |
| `currency` | ✓ | SGD / USD / EUR. |
| `gross_amount` | ✓ | **Signed**: SALE > 0, REFUND < 0 (`SIGN_RULE` otherwise). |
| `event_date` | ✓ | Business date of the event (used for as-of cutoff, tolerance, ageing). |
| `description` | optional | Free text (escaped in HTML, formula-guarded in CSV). |

## 2. PSP balance / settlement items (`source = psp`)
**Grain:** one row per PSP balance transaction. Loosely modelled on the public Stripe payout reconciliation
report concepts (balance transaction, gross/fee/net, `available_on`, payout ID) — see
<https://docs.stripe.com/reports/payout-reconciliation>. This is **not** the Stripe file format.

| Column | Req | Meaning |
|--------|-----|---------|
| `psp_txn_id` | ✓ | Record ID. |
| `type` | ✓ | `charge` (gross > 0), `refund` (gross < 0), `fee` (gross = 0, standalone fee, negative net). |
| `business_ref` | ✓ col; value required for charge/refund | Same reference as the ledger. |
| `merchant_account` | ✓ | Merchant account. |
| `currency` | ✓ | SGD / USD / EUR. |
| `gross_amount` | ✓ | Signed gross. |
| `fee_amount` | ✓ | PSP fee **deducted** (positive = cost). Refunds normally 0. |
| `net_amount` | ✓ | Must equal `gross_amount − fee_amount` exactly, else `NET_MISMATCH`. |
| `created_date` | ✓ | PSP transaction date (chain A date comparison, as-of cutoff). |
| `available_on` | ✓ | Date funds become available for payout (not-due / overdue logic). |
| `settlement_batch_id` | ✓ col | Payout / settlement batch; blank = not yet settled. |
| `settlement_date` | ✓ col; required if batch set | Expected payout value date of the batch. |
| `bank_account` | ✓ col; required if batch set | Destination bank account of the payout. |

**Derived settlement (chain B left side):** all items with the same `settlement_batch_id`;
`net = Σ item net` (charges − fees − refunds − fee rows). Gross and fee totals are kept for the bridge.

## 3. Bank statement lines (`source = bank`)
**Grain:** one row per statement line on a bank account.

| Column | Req | Meaning |
|--------|-----|---------|
| `bank_txn_id` | ✓ | Record ID (unique across all accounts in v1). |
| `bank_account` | ✓ | Receiving account. |
| `currency` | ✓ | SGD / USD / EUR. |
| `amount` | ✓ | Signed: credit > 0, debit < 0; zero rejected. |
| `value_date` | ✓ | Value date (window checks, as-of cutoff). |
| `reference` | ✓ col | Free-text reference; may contain one or more batch IDs. Tokens are split on space , ; / \| and compared **whole-token** with known batch IDs only. |
| `description` | optional | Free text. |

## Amount direction and sign conventions (summary)
| Where | Positive means | Negative means |
|-------|----------------|----------------|
| Ledger gross | sale | refund |
| PSP gross | charge | refund |
| PSP fee | fee charged (deducted) | fee rebate |
| PSP / settlement net | money owed to merchant | merchant owes PSP (e.g. refunds > sales) |
| Bank amount | credit | debit |
| Case amount, chain A | ledger gross − PSP gross (missing PSP ⇒ ledger gross; missing ledger ⇒ −PSP gross) | |
| Case amount, chain B settlement | outstanding = net − received (partial / mismatch) or full net (missing / ambiguous) | |
| Case amount, chain B bank line | bank line amount | |
| "Absolute exposure" in summaries | Σ |case amount| per chain / type / currency (so opposite-sign halves do not cancel) | |

## As-of cutoff
Records dated **after** the as-of date are excluded from a run (ledger `event_date`, PSP `created_date`, bank
`value_date`). A PSP item created on/before as-of whose `settlement_date` is after as-of is treated as unsettled.
The cutoff uses business dates only: the tool does not record when a row physically arrived.

## Database tables (SQLite)
`imports`, `ledger_records`, `psp_records`, `bank_records`, `row_issues` (quarantined + duplicate rows),
`runs`, `match_groups`, `match_members`, `record_status`, `cases`, `case_notes`, `audit_events`.
Source records, row issues, match results, notes and audit events are append-only (triggers abort UPDATE/DELETE).
`match_groups` has `CHECK (left_total = right_total)`; `match_members` has `UNIQUE(run_id, chain, record_type,
record_id)` — the database itself refuses double consumption and non-conserving groups.
