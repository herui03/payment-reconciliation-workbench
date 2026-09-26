"""Baseline benchmark on synthetic data. Ground truth is read ONLY here, after the engine has run.

Pipeline per dataset: generate -> import CSVs through the normal ingest path -> reconcile -> read persisted match
groups from SQLite -> compare with truth. The engine never receives truth or labels.

Denominators
  Chain A  pairs:  truth pairs (ledger_id, psp_id) that exist on both sides.
           "clean" pairs = same amount, same currency/merchant, PSP date within 0-2 days (labelled by generator).
           coverage = auto-matched clean pairs / clean pairs.  false match = engine pair not in truth.
  Chain B  groups: truth payout groups (batches <-> bank lines).
           "v1-matchable" groups = labels clean_ref, clean_noref, split, merge (amount conserved, in window).
           coverage = engine groups exactly equal to a v1-matchable truth group / v1-matchable truth groups.
           false match = engine group not exactly equal to ANY truth group (partial overlap counts as false).
Numbers describe this synthetic generator only. They are not production accuracy.

Usage: python tests/evaluation/benchmark.py [--rows 3000] [--full]   (--full adds a ~30,000-row run)
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from recon import RULES_VERSION  # noqa: E402
from recon.db import connect  # noqa: E402
from recon.ingest import import_file  # noqa: E402
from recon.reconcile import reconcile  # noqa: E402
from tests.evaluation.generator import generate  # noqa: E402

DATA = ROOT / "benchmark" / "data"
TRUTH = ROOT / "tests" / "evaluation" / "out"
RESULTS = ROOT / "benchmark" / "results"
MATCHABLE_B = {"clean_ref", "clean_noref", "split", "merge"}


def run_one(name: str, seed: int, profile: str, rows: int) -> dict:
    t0 = time.perf_counter()
    g = generate(rows, seed, profile, DATA / name, TRUTH / name)
    t_gen = time.perf_counter() - t0
    db = DATA / name / "bench.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    conn = connect(db)
    t1 = time.perf_counter()
    imps = {src: import_file(conn, src, g.data_dir / f"{src}.csv") for src in ("ledger", "psp", "bank")}
    t_imp = time.perf_counter() - t1
    t2 = time.perf_counter()
    run = reconcile(conn, g.as_of, actor="benchmark")
    t_rec = time.perf_counter() - t2
    rid = run["run_id"]

    # ---- engine output (from the database only)
    members: dict[str, dict[str, set]] = {}
    rule_of = {}
    for r in conn.execute("SELECT group_id, chain, record_type, record_id FROM match_members WHERE run_id=?", (rid,)):
        members.setdefault(r["group_id"], {"chain": r["chain"], "L": set(), "R": set()})
        side = "L" if r["record_type"] in ("ledger", "settlement") else "R"
        members[r["group_id"]][side].add(r["record_id"])
    for r in conn.execute("SELECT group_id, rule FROM match_groups WHERE run_id=?", (rid,)):
        rule_of[r["group_id"]] = r["rule"]
    exc = Counter()
    for r in conn.execute("SELECT chain, case_type FROM cases WHERE engine_active=1"):
        exc[f"{r['chain']}:{r['case_type']}"] += 1
    status_b = Counter(r[0] for r in conn.execute(
        "SELECT status FROM record_status WHERE run_id=? AND chain='B' AND record_type='settlement'", (rid,)))
    conn.close()

    # ---- ground truth (read only now)
    ta = json.loads((g.truth_dir / "truth_chain_a.json").read_text())
    tb = json.loads((g.truth_dir / "truth_chain_b.json").read_text())

    eng_a = {(next(iter(m["L"])), next(iter(m["R"]))) for m in members.values() if m["chain"] == "A"}
    truth_pairs = {(t["ledger"], t["psp"]): t["label"] for t in ta if t["ledger"] and t["psp"]}
    clean = {k for k, v in truth_pairs.items() if v == "clean"}
    a_false = eng_a - set(truth_pairs)
    a_anom_matched = Counter(truth_pairs[p] for p in eng_a if p in truth_pairs and truth_pairs[p] != "clean")
    a = {
        "auto_matched_groups": len(eng_a),
        "false_matches": len(a_false), "false_match_denominator": len(eng_a),
        "false_match_rate": _r(len(a_false), len(eng_a)),
        "clean_pairs": len(clean), "clean_pairs_auto_matched": len(eng_a & clean),
        "coverage_clean": _r(len(eng_a & clean), len(clean)),
        "truth_pairs_total": len(truth_pairs), "coverage_all_truth_pairs": _r(len(eng_a & set(truth_pairs)), len(truth_pairs)),
        "anomalous_pairs_auto_matched": dict(a_anom_matched),
        "truth_labels": dict(Counter(t["label"] for t in ta)),
    }

    eng_b = {}
    for gid, m in members.items():
        if m["chain"] == "B":
            eng_b[(frozenset(m["L"]), frozenset(m["R"]))] = rule_of[gid]
    truth_groups = {(frozenset(t["batches"]), frozenset(t["bank"])): t["label"] for t in tb["groups"]}
    matchable = {k for k, v in truth_groups.items() if v in MATCHABLE_B}
    b_false = [k for k in eng_b if k not in truth_groups]
    per_label = {}
    for lab in sorted(set(truth_groups.values())):
        keys = {k for k, v in truth_groups.items() if v == lab}
        per_label[lab] = {"truth_groups": len(keys), "engine_exact": len(keys & set(eng_b))}
    b = {
        "auto_matched_groups": len(eng_b),
        "false_matches": len(b_false), "false_match_denominator": len(eng_b),
        "false_match_rate": _r(len(b_false), len(eng_b)),
        "false_match_rules": dict(Counter(eng_b[k] for k in b_false)),
        "v1_matchable_truth_groups": len(matchable), "v1_matchable_matched_exactly": len(matchable & set(eng_b)),
        "coverage_v1_matchable": _r(len(matchable & set(eng_b)), len(matchable)),
        "by_truth_label": per_label,
        "groups_by_rule": dict(Counter(eng_b.values())),
        "settlement_status": dict(status_b),
        "noise_bank_lines": len(tb["noise_bank"]),
    }
    return {
        "dataset": name, "seed": seed, "profile": profile, "rows_target": rows, "as_of": g.as_of.isoformat(),
        "generated": g.counts,
        "imports": {k: {"status": v.status, "read": v.rows_read, "loaded": v.rows_loaded,
                        "quarantined": v.rows_quarantined} for k, v in imps.items()},
        "chain_a": a, "chain_b": b,
        "unresolved_by_type": dict(sorted(exc.items())),
        "elapsed_s": {"generate": round(t_gen, 2), "import": round(t_imp, 2), "reconcile_total": round(t_rec, 2),
                      "engine_only": round(run["summary"]["engine_ms"] / 1000, 2)},
    }


def _r(n, d):
    return None if d == 0 else round(n / d, 4)


def to_markdown(results: list[dict], meta: dict) -> str:
    L = [f"# Benchmark results (synthetic data) — rules {RULES_VERSION}", "",
         f"Generated {meta['run_date']} on {meta['platform']} / Python {meta['python']}. "
         "Synthetic generator only; **not** production accuracy and **not** employer data.", "",
         "Denominators: see `tests/evaluation/benchmark.py` docstring and `docs/matching-rules.md` §Benchmark.", ""]
    L += ["| dataset | profile | seed | ledger / psp / bank rows | A auto-matched | A false (÷ auto) | A coverage clean | "
          "B auto groups | B false (÷ auto) | B coverage v1-matchable | import s | reconcile s |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        a, b, g = r["chain_a"], r["chain_b"], r["generated"]
        L.append(f"| {r['dataset']} | {r['profile']} | {r['seed']} | {g['ledger']} / {g['psp']} / {g['bank']} | "
                 f"{a['auto_matched_groups']} | {a['false_matches']} / {a['false_match_denominator']} | "
                 f"{a['clean_pairs_auto_matched']} / {a['clean_pairs']} ({_pct(a['coverage_clean'])}) | "
                 f"{b['auto_matched_groups']} | {b['false_matches']} / {b['false_match_denominator']} | "
                 f"{b['v1_matchable_matched_exactly']} / {b['v1_matchable_truth_groups']} ({_pct(b['coverage_v1_matchable'])}) | "
                 f"{r['elapsed_s']['import']} | {r['elapsed_s']['reconcile_total']} |")
    for r in results:
        L += ["", f"## {r['dataset']}", "", f"- Chain B by truth label (engine exact / truth groups): " +
              ", ".join(f"{k} {v['engine_exact']}/{v['truth_groups']}" for k, v in r["chain_b"]["by_truth_label"].items()),
              f"- Chain B groups by rule: {r['chain_b']['groups_by_rule']}",
              f"- Chain B false matches by rule: {r['chain_b']['false_match_rules'] or 'none'}",
              f"- Chain A anomalous truth pairs auto-matched: {r['chain_a']['anomalous_pairs_auto_matched'] or 'none'}",
              f"- Unresolved by type: {r['unresolved_by_type']}"]
    return "\n".join(L) + "\n"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=3000)
    ap.add_argument("--full", action="store_true", help="also run the ~30,000-row development dataset")
    a = ap.parse_args()
    plan = [("dev_seed42_default", 42, "default", a.rows),
            ("holdout_seed7_collide", 7, "collide", a.rows),
            ("holdout_seed2027_messy", 2027, "messy", a.rows),
            ("holdout_seed99_adversarial", 99, "adversarial", a.rows)]
    if a.full:
        plan.append(("scale_seed42_default_30k", 42, "default", 30000))
    results = []
    for name, seed, prof, rows in plan:
        r = run_one(name, seed, prof, rows)
        results.append(r)
        print(f"{name}: A auto={r['chain_a']['auto_matched_groups']} false={r['chain_a']['false_matches']}/"
              f"{r['chain_a']['false_match_denominator']} cov_clean={_pct(r['chain_a']['coverage_clean'])} | "
              f"B auto={r['chain_b']['auto_matched_groups']} false={r['chain_b']['false_matches']}/"
              f"{r['chain_b']['false_match_denominator']} cov={_pct(r['chain_b']['coverage_v1_matchable'])} | "
              f"import {r['elapsed_s']['import']}s reconcile {r['elapsed_s']['reconcile_total']}s")
    RESULTS.mkdir(parents=True, exist_ok=True)
    meta = {"run_date": date.today().isoformat(), "platform": platform.platform(), "python": platform.python_version(),
            "rules_version": RULES_VERSION}
    (RESULTS / "benchmark_results.json").write_text(json.dumps({"meta": meta, "results": results}, indent=2))
    (RESULTS / "benchmark_results.md").write_text(to_markdown(results, meta))
    print(f"written: {RESULTS / 'benchmark_results.json'} and .md")


if __name__ == "__main__":
    main()
