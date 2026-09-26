# Three-minute demo script

Setup (before the call): `python -m recon --db work/demo_call.db serve` → http://127.0.0.1:5000 (new DB file).

| Time | Show | Say |
|------|------|-----|
| 0:00–0:20 | Dashboard banner | "Independent portfolio tool on synthetic data. Local, single-user, no real accounts." |
| 0:20–0:45 | **Load sample data** → flashes show `PARTIAL` with row counts; Data quality page | "Bad amount `64.0O`, bad date, 3-decimal amount, net ≠ gross − fee, conflicting duplicates: all quarantined with row numbers — nothing silently dropped. Re-importing the same file, even renamed, loads nothing." |
| 0:45–1:15 | **Reconcile** (as-of 2026-07-10) → Chain A / Chain B cards | "Two chains. A: ledger gross vs PSP gross. B: payout net vs bank. The bridge shows gross − fees = net per currency; currencies are never added." |
| **Normal** 1:15–1:30 | Matches → Chain B: `B1_REF_1TO1`, `B2S_REF_SPLIT`, `B2M_REF_MERGED` | "1000 gross, 20 fee, 980 net matched to the bank line by reference. Split and merged payouts balance to the cent." |
| **Exception** 1:30–2:10 | Exceptions → `AMBIGUOUS_CANDIDATES` (two EUR 490 payouts, one line) → then `AMOUNT_MISMATCH` ORD-1002 | "The engine refuses to guess between equal amounts. And the 50 USD gap is flagged with no tolerance applied." |
| **Fix** 2:10–2:50 | Open `PARTIAL_RECEIPT` → add note, In review → Dashboard: upload `data/sample/late/bank_late.csv` as bank, as-of 2026-07-11 → Reconcile → back to case | "The remainder arrived late. The re-run matched the split and the system auto-cleared the case, citing the rule; my note and the history are kept. A manual 'resolved' never counts as matched." |
| 2:50–3:00 | Reports → Generate | "EOD report: input scope, per-chain per-currency figures, open and overdue exceptions, data quality, rules version." |

Backup answers: benchmark (`benchmark/results/benchmark_results.md`) — 0 false matches; adversarial holdout 47 %
chain B coverage because ambiguous items go to review. Limits: no auth, no FX, synthetic only.
