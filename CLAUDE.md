# CLAUDE.md — working agreement for this repository

## What this is
Herui's independent job-search portfolio project: **Payment Reconciliation & Exception Management Workbench**.
A local, offline, single-user demo tool for payment / finance-operations analysts.

## Hard constraints (scope, authenticity, execution)
1. **Synthetic data only.** Every CSV in this repo is independently generated or hand-written for this project.
   Never copy, paraphrase or reconstruct any internship/employer internal material, field names, volumes or results.
2. **No employer claims.** Benchmark or demo results are results *on synthetic data*. Never describe them as
   employer outcomes, production accuracy, or hours saved.
3. **No real accounts.** No connection to real banks, PSPs or payment accounts. No API keys. Runs fully offline.
4. **Repository stays private.** Do not publish a website, change account permissions, or widen CI/workflow scopes.
   If CI is blocked by permission scope, document the blocker in `STATUS.md` instead of escalating.
5. **No LLM decides amounts or matches.** Matching is deterministic, rule-based and explainable.
6. **Honest evidence.** Tests are automated developer tests, not user acceptance. `UAT.md` must not present automated
   tests as external user sign-off. `defects.md` only records defects actually found. Never invent numbers.
7. **Public reference only.** Stripe's public payout reconciliation docs
   (https://docs.stripe.com/reports/payout-reconciliation) are a field/scenario reference; the tool does NOT integrate
   with Stripe or support its full report format.
8. **Demo-grade controls.** Actor names are demo labels, not authentication. The audit log is append-only at the
   application/SQLite-trigger level, but it is NOT tamper-proof, NOT regulatory-compliant, and there is no formal
   maker/checker permission model.

## Engineering rules
- Python 3.11+, SQLite (transactions), Flask + Jinja (server-rendered UI), pytest, optional Playwright.
- Money is **integer minor units** end-to-end (parsed via `Decimal`, never `float`). No tolerance on amounts.
- Currencies in scope: SGD, USD, EUR (all 2 decimals). No FX conversion. Never add amounts across currencies.
- Matching engine (`recon/matching.py`) is pure: it receives records, never reads ground truth, scenario names,
  or files under `tests/evaluation/`. A test enforces this.
- Every source record is traceable to `(file sha256, source row number)`.
- Source records, audit events and case notes are append-only (SQLite triggers block UPDATE/DELETE).
- Work on the session branch; do not merge to main automatically.

## Commands
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q                         # all tests
python -m recon --db work/demo.db demo      # fresh demo: import sample, reconcile, report
python -m recon --db work/demo.db serve     # UI at http://127.0.0.1:5000
python tests/evaluation/benchmark.py        # synthetic benchmark (writes benchmark/results)
```
