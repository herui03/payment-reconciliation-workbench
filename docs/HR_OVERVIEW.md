# Payment Reconciliation Workbench — overview for recruiters and hiring managers

**In one sentence:** a small local web tool that checks whether money a business *booked*, money its payment
provider *processed*, and money that actually *arrived in the bank* agree — and turns every disagreement into a
tracked, explainable work item.

> **Portfolio project on invented (synthetic) data.** It is not used by any company, is not connected to any
> real bank or payment account, and has not been tested by external users. No business impact is claimed.

## How it was built (attribution)
| Role | Who |
|---|---|
| Project direction and intended use: chose the job-portfolio goal and priorities, authorized the work | **Herui** (project owner) |
| Implementation: code, tests, sample data, documentation | **Claude** (Anthropic's AI coding assistant) |
| Scope translation into requirements, review criteria and cases, independent review and implementation-review decisions (found defects [D-08…D-10](defects.md)) | **Codex** (AI reviewer) |

The code was not hand-written by Herui. Herui's own understanding of the rules and trade-offs is something to be
demonstrated in conversation — for example by walking through the demo below or the
[matching rules](matching-rules.md).

## The problem, in plain English
When a customer pays an online shop, the same payment shows up in three places:
1. the shop's **own books** ("we sold an order for 1,000.00"),
2. the **payment provider's report** ("we processed 1,000.00, kept a 20.00 fee, and will pay you 980.00"),
3. the **bank statement** ("980.00 arrived").

A payments-operations analyst must prove every day that these agree. Common problems: money arrives in two parts
or combined with other payouts, two payouts have the same amount so it is unclear which is which, files are sent
twice, a row has a typo, or the bank line arrives a day late. Doing this by hand in spreadsheets is slow and
error-prone (e.g. counting a file twice, or mixing the 1,000 and the 980).

## What the tool does
- **Checks input files** and sets bad rows aside with a reason (nothing is silently dropped; re-sending the same
  file does not double-count).
- **Compares in two separate steps:** books vs. provider (before fees), then provider payout vs. bank (after fees).
- **Only auto-matches when the answer is unambiguous**; anything uncertain becomes an exception for a person.
- **Exception cases** with owner, status, notes and a full history of who changed what.
- **End-of-day report** (web page + spreadsheet files), with amounts kept separate per currency.

## A concrete normal flow
Order ORD-1001: the books say 1,000.00 SGD. The provider says 1,000.00 processed, 20.00 fee, 980.00 paid out in
batch STL-SG-0703. The bank shows 980.00 with reference "STL-SG-0703".
Result: both steps match automatically, and the screen explains why (same reference, exact amount, dates within
the allowed window).

## A concrete failure flow (and recovery)
Batch STL-EU-0705 should pay 2,000.00 EUR, but the bank shows only 1,200.00.
1. The tool opens a **"partial receipt"** exception showing 800.00 outstanding, with the exact file and row of
   each piece of evidence.
2. The analyst assigns it, adds a note ("asked provider for the remainder") and marks it *In review*.
3. Next day the late bank line (800.00) is imported. On the next run the tool sees 1,200 + 800 = 2,000, matches
   it, and **closes the case automatically**, recording why. The analyst's note and history are kept.

Other failures it catches in the sample data: a 50.00 unexplained shortfall, two same-amount payouts it refuses
to guess between, money sent to the wrong bank account, a wrong currency, conflicting versions of the same
record, and badly formatted amounts or dates.

## What can be shown in 3 minutes
1. Click **Load sample data** — bad rows are counted and set aside, not hidden.
2. Click **Reconcile** — two result panels (before fees / after fees), per currency.
3. Open the **Exceptions** list — e.g. the ambiguous same-amount payouts and the 50.00 shortfall.
4. Work the partial-receipt case, upload the late bank file, re-run — the case closes itself with an explanation.
5. Generate the end-of-day report.

Step-by-step script: [demo-script.md](demo-script.md).

## Screenshots (captured by the automated browser test)
| | |
|---|---|
| Dashboard | [01_dashboard.png](evidence/screenshots/01_dashboard.png) |
| Matched payouts (split / combined / exact) | [02_matches_chain_b.png](evidence/screenshots/02_matches_chain_b.png) |
| Exceptions list | [03_exceptions.png](evidence/screenshots/03_exceptions.png) |
| Partial-receipt case with evidence rows | [04_case_partial_receipt.png](evidence/screenshots/04_case_partial_receipt.png) |
| Same case auto-closed after late bank data | [05_case_auto_cleared_after_late_data.png](evidence/screenshots/05_case_auto_cleared_after_late_data.png) |
| Data-quality page (rejected / set-aside rows) | [06_data_quality.png](evidence/screenshots/06_data_quality.png) |

![Dashboard](evidence/screenshots/01_dashboard.png)

## Technology
Python 3.11, SQLite (local database file), Flask + server-rendered HTML (web interface), pytest (automated
tests), Playwright + Chromium (browser test), GitHub Actions (automated test run on each push). Runs offline;
no API keys, no AI at run time — all matching rules are fixed and explainable.

## Evidence (what was actually run)
| Evidence | Where |
|---|---|
| 127 automated developer tests passing, including a real-browser test | [pytest_output.txt](evidence/pytest_output.txt) |
| 21 hand-written test scenarios with expected answers written before the tool was run on them | [tests/golden](../tests/golden/README.md) |
| Command-line walkthrough: duplicate files, case handling, late data, repeat run | [cli_walkthrough.txt](evidence/cli_walkthrough.txt) |
| Sample end-of-day reports | [sample_reports](evidence/sample_reports/) |
| Benchmark on generated data (up to ~30,000 ledger rows): 0 wrong automatic matches in all 5 datasets; on the hardest dataset only 47.3 % of payouts were auto-matched and the rest went to human review | [benchmark_results.md](../benchmark/results/benchmark_results.md) |
| Defects found during the build and review, with fixes and tests | [defects.md](defects.md) |
| Current status and what is not verified | [STATUS.md](../STATUS.md) |

These are developer tests on invented data. They show that the rules behave as designed; they are **not**
real-world accuracy figures.

## Limitations (stated plainly)
- Invented data only; the data generator and the rules were produced in the same project, so benchmark
  results are optimistic about realism.
- **No external user acceptance testing yet** ([UAT.md](UAT.md) lists developer evidence only).
- Single user on one computer; no login, no approval workflow (maker/checker), not an audit-grade or
  regulatory system.
- Three currencies (SGD, USD, EUR), no currency conversion, no chargebacks/disputes, no real bank file formats.
- Field ideas were informed by Stripe's public
  [payout reconciliation docs](https://docs.stripe.com/reports/payout-reconciliation); there is no Stripe
  integration.

More detail: [README](../README.md) · [requirements](requirements.md) · [matching rules](matching-rules.md) ·
[process before/after](process-as-is-to-be.md) · [SOP](SOP.md)
