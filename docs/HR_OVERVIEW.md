# Payment Reconciliation Workbench — project overview

A local web tool for payments-operations analysts. It checks that three records of the same payments agree and
turns each disagreement into a tracked case with the source rows attached. It is a portfolio project that runs
on synthetic sample data.

![Dashboard](evidence/screenshots/01_dashboard.png)

## The business problem
Each customer payment appears three times: in the business's own ledger, in the payment provider's settlement
report, and on the bank statement. The ledger records the **gross** amount the customer paid. The provider keeps
a fee and pays out the **net** amount: a 1,000.00 sale with a 20.00 fee arrives at the bank as 980.00.

An analyst has to confirm every day that these records agree. Payouts can arrive split in two or combined with
other payouts, two payouts can have the same amount, files can be sent twice, rows can contain typos, and bank
lines can arrive late. Comparing gross with net, or counting a file twice, produces false differences.

## What the tool does
- **Imports and validates** the three CSV files. Bad rows are set aside with a reason, and re-sending a file does
  not count it twice.
- **Reconciles in two steps:** ledger against provider on gross amounts, then provider payouts against the bank
  on net amounts.
- **Matches automatically only when there is exactly one valid answer.** Ambiguous items become exceptions for
  review.
- **Tracks exception cases** with owner, status, notes and a history of every change.
- **Produces an end-of-day report** (web page plus CSV files), keeping each currency separate.

## Example: a short payment and a late bank line
Payout batch STL-EU-0705 should pay 2,000.00 EUR, but the bank statement shows only 1,200.00.
1. A **partial receipt** case opens with 800.00 outstanding, pointing to the exact file and row of each record.
2. The analyst assigns the case, adds a note ("asked provider for the remainder") and marks it *In review*.
3. The next day's statement contains the missing 800.00. On the re-run, 1,200.00 + 800.00 = 2,000.00 matches,
   and the case closes automatically with the reason recorded. The note and history remain.

## Demo and screenshots
A 3-minute walkthrough: load the sample data, reconcile, open the exceptions, work the partial-receipt case, then
upload the late bank file and re-run. Script: [demo-script.md](demo-script.md).

Screenshots: [dashboard](evidence/screenshots/01_dashboard.png) ·
[matched payouts](evidence/screenshots/02_matches_chain_b.png) ·
[exceptions](evidence/screenshots/03_exceptions.png) ·
[partial-receipt case](evidence/screenshots/04_case_partial_receipt.png) ·
[case closed after late data](evidence/screenshots/05_case_auto_cleared_after_late_data.png) ·
[data quality](evidence/screenshots/06_data_quality.png)

## Technology
Python 3.11, SQLite, Flask (server-rendered pages), pytest, Playwright, GitHub Actions. Runs offline with no API
keys; matching is deterministic and rule-based. Setup (full steps in the [README](../README.md#quick-start)):

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m recon --db work/demo.db serve     # then open http://127.0.0.1:5000
```

## Validation
The project has automated tests, hand-written scenarios with expected answers, and a benchmark on generated data.
Details and results: [README](../README.md#validation) · [defects found and fixed](defects.md).

## Scope and limitations
- Synthetic data only; no connection to any bank or payment provider. Field choices follow Stripe's public
  [payout reconciliation docs](https://docs.stripe.com/reports/payout-reconciliation); there is no Stripe
  integration.
- Single user on one computer, with no login or approval workflow. The audit history is append-only within the
  app, not a regulatory record.
- SGD, USD and EUR only; no currency conversion, chargebacks or real bank file formats.
- Validated with developer tests only; no external user testing yet ([UAT.md](UAT.md)).
