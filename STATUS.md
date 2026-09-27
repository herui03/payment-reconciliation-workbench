# STATUS — v1 (merged to `main` via PR #1)

Last updated 2026-09-26. Everything below was actually run in this environment (Linux, Python 3.11.15, SQLite, Chromium 1194 via Playwright 1.63).

## Done and verified
| Item | Evidence |
|---|---|
| Ingest, two-chain engine, case workflow, audit, EOD report, CLI, Flask UI | code in `recon/` |
| 127 automated tests pass, 0 skipped (incl. real-browser Playwright UI flow) | `docs/evidence/pytest_output.txt` |
| Golden fixture: 21 hand-written scenarios, expected answers written before the engine ran on them | `tests/golden/`, `data/sample/` |
| CLI walkthrough incl. duplicate/rejected files, case work, late data, idempotent re-run, append-only audit | `docs/evidence/cli_walkthrough.sh` + `.txt` |
| Sample EOD reports (HTML + 3 CSV) | `docs/evidence/sample_reports/` |
| UI screenshots from the Playwright run | `docs/evidence/screenshots/` |
| Benchmark: dev + 3 holdouts (3,000 target rows) + 30k-row scale run | `benchmark/results/benchmark_results.{md,json}` |
| Clean clone (from GitHub) + fresh venv: `pip install -r requirements.txt`, `pytest` (127 passed), `demo` | verified at 652277d and at 80babd1 |
| 3 independent-reviewer failures (backward as-of, key delimiter collision, historical report mixing) fixed with regression tests | `docs/defects.md` D-08..D-10 |

## Benchmark (synthetic; not production accuracy)
| Dataset | A auto / false | A coverage (clean pairs) | B auto / false | B coverage (v1-matchable) | import + reconcile |
|---|---|---|---|---|---|
| dev seed 42 default | 2899 / 0 | 100.0 % | 512 / 0 | 100.0 % | 0.2 s + 0.2 s |
| holdout seed 7 collide | 2887 / 0 | 100.0 % | 503 / 0 | 100.0 % | 0.2 s + 0.2 s |
| holdout seed 2027 messy | 2652 / 0 | 100.0 % | 468 / 0 | 100.0 % | 0.2 s + 0.3 s |
| holdout seed 99 adversarial | 2901 / 0 | 100.0 % | 1285 / 0 | 47.3 % | 0.3 s + 1.0 s |
| scale seed 42 (≈30k ledger rows) | 28947 / 0 | 100.0 % | 533 / 0 | 100.0 % | 2.1 s + 2.0 s |
Chain A is near-trivial by construction (references are never corrupted by the generator). Rules and generator share an author, so these numbers show mechanics and trade-offs, not real-world accuracy.

## Not done / not verified
- **No external user acceptance.** `docs/UAT.md` lists developer evidence only; the external UAT script is written but not run.
- **CI**: GitHub Actions workflow `tests` passed on 652277d (run 36218140617) and 80babd1 (run 36218439365). CI installs only flask + pytest, so the Playwright UI test is *skipped in CI*; it runs locally (see evidence). No permission scope issue was hit.
- Not tested: other browsers, mobile layout, accessibility, >30k ledger rows, concurrent users.
- Not built (out of scope v1): authentication, CSRF protection, maker/checker, FX, disputes/reserves, time zones, real bank formats (MT940/CAMT), Stripe report compatibility, reference typo tolerance.
- Databases created by earlier WIP commits lack the new `runs` columns / `run_exceptions` table: start with a new `--db` file.
