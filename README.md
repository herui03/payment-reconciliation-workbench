# Payment Reconciliation & Exception Management Workbench

**New here? Start with the plain-English [HR / hiring-manager overview](docs/HR_OVERVIEW.md)** — problem, a normal and a failure flow, 3-minute demo, screenshots, evidence and limitations.

A local, offline workbench for payment / finance-operations analysts: import ledger, PSP and bank CSVs, reconcile
**two chains independently**, work the exceptions with an audit trail, and publish an end-of-day report.

> **Portfolio prototype on synthetic data.** Portfolio project directed by Herui; implemented by Claude (AI coding assistant) and reviewed by Codex — see [attribution](docs/HR_OVERVIEW.md#how-it-was-built-attribution). All data is invented. It is not an
> employer system, contains no employer material, does not connect to any bank or PSP, needs no API key, and its
> results are not production accuracy. Single-user local demo: the "actor" is a demo label, not authentication;
> the audit log is append-only inside the app but is not tamper-proof or regulatory-grade; there is no formal
> maker/checker. Field choices are informed by Stripe's public
> [payout reconciliation](https://docs.stripe.com/reports/payout-reconciliation) docs; there is no Stripe
> integration and its report format is not supported.

![Dashboard](docs/evidence/screenshots/01_dashboard.png)

## What it does
| | |
|---|---|
| **Ingest** | Validates schema, amounts (`Decimal` → integer cents, no float, no rounding), dates, currencies (SGD/USD/EUR), sign rules, `net = gross − fee`. Bad files are rejected in a rolled-back transaction; bad rows are quarantined with a reason; `rows_read = loaded + duplicate + quarantined` always holds. Identical files (any name) import once; overlapping re-sends skip duplicate rows; same key with different content is quarantined and blocks matching. Every record keeps `file SHA-256 + row number`. |
| **Chain A** | Ledger **gross** ↔ PSP charge/refund **gross** by `(merchant, business_ref)`, exact amount, date tolerance ±2 days. |
| **Chain B** | PSP settlement batch **net** (incl. fees) ↔ bank lines: reference 1:1, split (1 batch → n lines), merged (n batches → 1 line), then unique amount+date; amount-only groups are *suggested*, never auto-matched. Ambiguity always goes to review. |
| **Exceptions** | Typed cases with amount, currency, age vs as-of, owner, status (Open / In review / Resolved), append-only notes, evidence rows, rule explanation. Resolving needs disposition + reason and never fabricates a match. Late data auto-clears cases on re-run, keeping notes. |
| **Report** | EOD HTML + CSV: input scope, per-chain per-currency counts and amounts, settlement bridge (gross − fee = net), open/overdue exceptions, data quality, run time, rules version. CSV cells are protected against formula injection. |

## Quick start (clean directory)
```bash
git clone <this repo> && cd payment-reconciliation-workbench
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m pytest -q                                    # full test suite (Playwright test auto-skips without Chromium)
python -m recon --db work/demo.db demo                 # import sample -> reconcile as-of 2026-07-10 -> EOD report in work/reports/
python -m recon --db work/demo.db serve                # UI at http://127.0.0.1:5000
```
In the UI: **Load sample data** → **Reconcile** (as-of pre-filled 2026-07-10) → **Exceptions**. To see late data
auto-clear cases: upload `data/sample/late/bank_late.csv` as *bank* and reconcile as-of `2026-07-11`.

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
`demo` refuses to reuse an existing database file (it never deletes history); pass a new `--db` path.

## Repository map
| Path | Content |
|---|---|
| `recon/money.py` | Decimal → minor-unit parsing |
| `recon/ingest.py` | CSV validation, quarantine, idempotency, conflicts |
| `recon/matching.py` | **Pure matching engine** (both chains, invariants) |
| `recon/reconcile.py` | Run orchestration, as-of cutoff, case sync across runs |
| `recon/cases.py` | Case workflow + audit |
| `recon/reports.py`, `templates/report.html` | EOD report, CSV formula guard |
| `recon/web.py`, `templates/`, `static/` | Flask UI |
| `recon/db.py` | SQLite schema, append-only triggers |
| `data/sample/` | Hand-written golden fixture (21 scenarios) + late / extra files |
| `tests/golden/expected.json` | Hand-written expected answers |
| `tests/evaluation/` | Synthetic generator + benchmark (truth stays here) |
| `benchmark/results/` | Actual benchmark output |
| `docs/` | Requirements, data dictionary, matching rules, process, UAT, defects, SOP, demo script, interview guide |
| `docs/evidence/` | Test output, demo run log, sample EOD report, UI screenshots |

## Results on synthetic data (see `benchmark/results/benchmark_results.md`)
Conservative rules produced **0 false matches** on all five synthetic datasets (dev seed 42, three holdouts,
30k-row scale run). On the adversarial holdout (one item per payout, few price points, 50 % of bank lines
without reference) chain B auto-matched only 47.3 % of v1-matchable payouts; the rest went to review as
ambiguous. That is the intended trade-off, measured — not a production accuracy figure.

## Limitations
- Synthetic data only; generator and rules were written by the same author, so benchmark numbers are optimistic
  about realism even where they are honest about the mechanics.
- Single user, no authentication, no CSRF protection, localhost only. Audit trail is app-level append-only, not
  tamper-evident storage.
- SGD/USD/EUR, 2-decimal currencies, no FX, no disputes/reserves/chargebacks, no time zones.
- Record IDs must be globally unique per source (see `docs/data-dictionary.md`).
- UI tested in Chromium only (Playwright); no accessibility audit; no external user acceptance yet.

---

## 中文说明（使用 & 面试）

**这是什么**：一个本地离线的「支付对账与异常管理工作台」求职作品。全部数据为独立编造的 synthetic 数据；不连接任何真实银行/支付账户；不是任何雇主的系统，模拟结果不代表雇主业绩或生产准确率。

**三分钟跑起来**
1. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
2. `python -m recon --db work/demo.db serve`，浏览器打开 http://127.0.0.1:5000
3. 点 **Load sample data** → **Reconcile**（as-of 自动填 2026-07-10）→ 看 Chain A / Chain B 分币种统计 → **Exceptions** 打开一个 `PARTIAL_RECEIPT` 案例，加 note、标 In review。
4. 回 Dashboard 上传 `data/sample/late/bank_late.csv`（source 选 bank），as-of 改 2026-07-11 再 Reconcile：部分到账案例被 `system` 自动关闭为 `AUTO_CLEARED`，note 和历史都还在。

**面试时怎么讲**（详见 `docs/interview-guide-zh.md` 和 `docs/demo-script.md`）
- 两条链分开核：A 链比 **gross**（账本 vs PSP 交易），B 链比 **net**（PSP 结算批次 vs 银行入账），绝不拿银行净额对账本毛额。
- 金额用整数分存储，不用 float、不设容差；币种、账户永不跨越，汇总永不跨币种相加。
- 只有「唯一且证据充分」才自动匹配；同金额歧义、仅靠金额凑出来的组合都进人工复核。benchmark 的对抗性 holdout 直接展示了这个取舍：0 误配，但 B 链覆盖率掉到 47%。
- 人工「已处理」只记录决策，不伪造匹配、不改金额；审计事件只能追加。
- 需要诚实说明的边界：单用户演示、actor 只是标签、无真实身份验证、审计不可篡改性只是 app 层面、没有外部用户验收。
