# Payment Reconciliation & Exception Management Workbench

Every payment an online business takes shows up three times: in its own ledger, in the payment provider's
(PSP) settlement report, and on the bank statement. This workbench imports those three CSV files, checks that
they agree, and turns every mismatch into a tracked exception case with the evidence attached, then produces an
end-of-day report. It runs locally in a browser and uses synthetic sample data.

[Project overview (non-technical)](docs/HR_OVERVIEW.md) · [3-minute demo script](docs/demo-script.md) ·
[Screenshots](docs/evidence/screenshots/)

![Dashboard](docs/evidence/screenshots/01_dashboard.png)

## Capabilities
| | |
|---|---|
| **Import and validation** | Checks columns, amounts, dates, currencies (SGD/USD/EUR), sign rules and `net = gross − fee`. Amounts are parsed with `Decimal` into integer cents (no floats, no rounding). A file with structural errors is rejected in a rolled-back transaction; bad rows are quarantined with a reason, and `rows_read = loaded + duplicate + quarantined` always holds. The same file under any name is imported only once; overlapping re-sends skip duplicate rows; a record that reappears with different content is quarantined and blocked from matching. Every record keeps its file SHA-256 and row number. |
| **Chain A: ledger ↔ PSP (gross)** | Matches by `(merchant, business_ref)` on the exact amount, with a ±2-day date tolerance. |
| **Chain B: PSP payout ↔ bank (net)** | Matches settlement batch net (after fees) to bank lines: by reference 1:1, split (one batch → several lines), merged (several batches → one line), then by a unique amount + date. Amount-only groupings are shown as suggestions, not matched automatically; ambiguous cases go to review. |
| **Exception cases** | Type, amount, currency, age against an as-of date, owner, status (Open / In review / Resolved), append-only notes, evidence rows and the rule that fired. Resolving requires a disposition and a reason and does not mark the item as matched. When late data arrives, a re-run closes the affected cases and keeps their notes. A re-run with an earlier as-of date is a read-only snapshot that leaves current cases unchanged. |
| **End-of-day report** | HTML and CSV: input scope, per-chain and per-currency counts and amounts, a gross − fee = net bridge, open and overdue exceptions, data-quality issues, run time and rules version. CSV cells are protected against spreadsheet formula injection. |

## Example
Order ORD-1001 is booked at 1,000.00 SGD. The PSP reports 1,000.00 processed, a 20.00 fee and 980.00 paid out in
batch STL-SG-0703; the bank shows 980.00 with reference STL-SG-0703. Chain A matches 1,000.00 to 1,000.00 and
chain B matches 980.00 to 980.00.

Batch STL-EU-0705 should pay 2,000.00 EUR, but only 1,200.00 arrives, so a *partial receipt* case opens with
800.00 outstanding. The next day's statement adds the 800.00; on the re-run 1,200.00 + 800.00 = 2,000.00
matches as a split payout, and the case closes automatically, keeping its notes and history.

## Quick start
```bash
git clone https://github.com/herui03/payment-reconciliation-workbench && cd payment-reconciliation-workbench
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m pytest -q                                    # test suite (the Playwright test skips if Chromium is absent)
python -m recon --db work/demo.db demo                 # import sample -> reconcile as-of 2026-07-10 -> EOD report in work/reports/
python -m recon --db work/demo.db serve                # UI at http://127.0.0.1:5000
```
In the UI: **Load sample data** → **Reconcile** (as-of pre-filled 2026-07-10) → **Exceptions**. To see late data
close cases, upload `data/sample/late/bank_late.csv` as *bank* and reconcile as-of `2026-07-11`.

### CLI
```bash
python -m recon --db work/x.db import ledger data/sample/ledger.csv [--strict]
python -m recon --db work/x.db import psp data/sample/psp.csv
python -m recon --db work/x.db import bank data/sample/bank.csv
python -m recon --db work/x.db reconcile --as-of 2026-07-10
python -m recon --db work/x.db cases --status Open
python -m recon --db work/x.db --actor analyst.a case-note 3 "Chased PSP"
python -m recon --db work/x.db --actor analyst.a case-status 3 Resolved --disposition PSP_QUERY_RAISED --reason "Query ref 123 raised"
python -m recon --db work/x.db report --out work/reports
python tests/evaluation/benchmark.py [--rows 3000] [--full]   # synthetic benchmark -> benchmark/results/
```
`demo` will not reuse an existing database file (it never deletes history); pass a new `--db` path.

## Stack
Python 3.11 · SQLite · Flask with server-rendered Jinja templates · pytest · Playwright (Chromium) · GitHub
Actions. Runs offline with no API keys; matching is deterministic and rule-based.

## Repository map
| Path | Content |
|---|---|
| `recon/money.py` | Decimal → minor-unit parsing |
| `recon/ingest.py` | CSV validation, quarantine, idempotency, conflicts |
| `recon/matching.py` | Matching engine (pure functions, both chains, invariants) |
| `recon/reconcile.py` | Run orchestration, as-of cutoff, case sync across runs |
| `recon/cases.py` | Case workflow and audit events |
| `recon/reports.py`, `recon/templates/report.html` | EOD report, CSV formula guard |
| `recon/web.py`, `recon/templates/`, `recon/static/` | Flask UI |
| `recon/db.py` | SQLite schema, append-only triggers |
| `data/sample/` | Hand-written sample data (21 scenarios) plus late and extra files |
| `tests/golden/expected.json` | Hand-written expected answers for the sample |
| `tests/evaluation/` | Synthetic data generator and benchmark (ground truth stays here) |
| `benchmark/results/` | Benchmark output |
| `docs/` | Requirements, data dictionary, matching rules, process, UAT, defects, SOP, demo script |
| `docs/evidence/` | Test output, CLI walkthrough, sample EOD reports, UI screenshots |

## Validation
- 127 automated tests, including a Playwright browser run:
  [pytest_output.txt](docs/evidence/pytest_output.txt).
- 21 hand-written scenarios, with expected answers written before the engine was run on them:
  [tests/golden](tests/golden/README.md).
- Benchmark on generated data, from about 3,000 up to about 30,000 ledger rows: 0 false automatic matches across
  five datasets. On the adversarial holdout (one item per payout, few price points, half the bank lines without
  a reference), chain B auto-matched 47.3 % of matchable payouts and sent the rest to review:
  [benchmark_results.md](benchmark/results/benchmark_results.md).
- Defects found during development and review, each with its fix and regression test: [defects.md](docs/defects.md).

## Scope and limitations
- **Data:** all sample and benchmark data is synthetic; the generator and the matching rules come from the same
  project. Field choices follow Stripe's public
  [payout reconciliation](https://docs.stripe.com/reports/payout-reconciliation) documentation; there is no
  Stripe integration and its report format is not supported. No bank or PSP connections.
- **Scope:** single user on localhost. The actor name is a label, not a login; there is no CSRF protection and no
  maker/checker approval. The audit log is append-only within the app (SQLite triggers), not tamper-evident
  storage. SGD, USD and EUR (2 decimals) only; no FX, disputes, reserves, chargebacks or time zones. Record IDs
  must be unique per source file type ([data dictionary](docs/data-dictionary.md)).
- **Validation:** automated developer tests only; no external user acceptance testing ([UAT.md](docs/UAT.md)).
  Browser-tested in Chromium only; no accessibility audit.

---

## 中文简介

一个本地运行的支付对账与异常管理工作台，用 synthetic 示例数据演示：导入账本、PSP 结算明细和银行流水三份 CSV，分两条链核对（A 链：账本毛额 vs PSP 交易毛额；B 链：PSP 结算净额 vs 银行入账），把对不上的条目变成带证据的异常案例，并生成日终报告。

**运行**
1. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
2. `python -m recon --db work/demo.db serve`，浏览器打开 http://127.0.0.1:5000
3. 点 **Load sample data** → **Reconcile**（as-of 自动填 2026-07-10）→ **Exceptions**，打开一个 `PARTIAL_RECEIPT` 案例，加 note、标 In review。
4. 回 Dashboard 上传 `data/sample/late/bank_late.csv`（source 选 bank），as-of 改 2026-07-11 再 Reconcile：部分到账案例自动关闭为 `AUTO_CLEARED`，note 和历史保留。

演示脚本见 `docs/demo-script.md`，规则说明见 `docs/matching-rules.md`。
