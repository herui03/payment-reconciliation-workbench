"""Matching-engine business-risk tests: overlap, order independence, boundaries, conservation, as-of cutoff."""
import ast
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import SAMPLE, settlement_view, status_map
from helpers import BANK_H, LEDGER_H, PSP_H, bank, csv_bytes, led, psp

from recon.ingest import import_bytes, import_file
from recon.matching import BankLine, EngineInput, PspItem, Rules, run_engine
from recon.reconcile import load_input, reconcile

D = date(2026, 7, 10)


def P(bid, net, sdate=D, acct="BK1", cur="SGD", pid=None):
    return PspItem(pid or f"P-{bid}", "charge", f"R-{bid}", "M1", cur, net, 0, net, sdate, sdate, bid, sdate, acct)


def K(kid, amt, vdate=D, ref="", acct="BK1", cur="SGD"):
    return BankLine(kid, acct, cur, amt, vdate, ref)


def run(psps, banks, as_of=D + timedelta(days=10), rules=Rules()):
    return run_engine(EngineInput(as_of=as_of, ledger=[], psp=psps, bank=banks, rules=rules))


def normalize(res):
    groups = sorted((g.chain, g.rule, tuple(sorted((m.side, m.record_type, m.record_id) for m in g.members)))
                    for g in res.groups)
    states = sorted((s.chain, s.record_type, s.record_id, s.status, s.rule, s.case_key) for s in res.states)
    exc = sorted((e.case_key, e.case_type, e.amount) for e in res.exceptions)
    return groups, states, exc


def sstat(res, bid):
    return next(s.status for s in res.states if s.record_type == "settlement" and s.record_id == bid)


# ---------------------------------------------------------------- ambiguity / overlap (acceptance point 5)
@pytest.mark.parametrize("order", [0, 1])
def test_two_settlements_one_bank_same_amount_never_auto_matched(order):
    psps, banks = [P("S1", 10000), P("S2", 10000)], [K("K1", 10000)]
    res = run(psps[::-1] if order else psps, banks)
    assert not res.groups
    assert {sstat(res, "S1"), sstat(res, "S2")} == {"AMBIGUOUS_CANDIDATES"}


def test_one_settlement_two_identical_bank_lines_ambiguous():
    res = run([P("S1", 10000)], [K("K1", 10000), K("K2", 10000)])
    assert not res.groups and sstat(res, "S1") == "AMBIGUOUS_CANDIDATES"


@pytest.mark.parametrize("perm", range(6))
def test_greedy_trap_overlapping_candidates(perm):
    """K1 could be S1 or S2; K2 could only be S2. A greedy pass would consume S2 for K2 and then give S1 to K1.
    Because S2 has two candidates, the engine must flag all of it for review instead."""
    import itertools
    psps = [P("S1", 10000), P("S2", 10000, sdate=D + timedelta(days=3))]
    banks = [K("K1", 10000, vdate=D + timedelta(days=2)), K("K2", 10000, vdate=D + timedelta(days=5))]
    orders = list(itertools.permutations(range(2)))
    p_ord, k_ord = orders[perm % 2], orders[(perm // 2) % 2]
    res = run([psps[i] for i in p_ord], [banks[i] for i in k_ord])
    assert not res.groups
    assert sstat(res, "S1") == "AMBIGUOUS_CANDIDATES" and sstat(res, "S2") == "AMBIGUOUS_CANDIDATES"


def test_reference_evidence_outranks_amount_only():
    res = run([P("S1", 10000), P("S2", 10000)], [K("K1", 10000, ref="S1"), K("K2", 10000)])
    got = {g.rule: sorted(m.record_id for m in g.members) for g in res.groups}
    assert got == {"B1_REF_1TO1": ["K1", "S1"], "B3_AMOUNT_DATE_UNIQUE": ["K2", "S2"]}


def test_amount_group_with_two_solutions_is_ambiguous_not_suggested():
    res = run([P("S1", 10000), P("S2", 20000), P("S3", 15000), P("S4", 15000)], [K("K1", 30000)])
    assert not res.groups
    assert {sstat(res, s) for s in ("S1", "S2", "S3", "S4")} == {"AMBIGUOUS_CANDIDATES"}


def test_unique_amount_group_is_only_a_suggestion():
    res = run([P("S1", 10000), P("S2", 20000)], [K("K1", 30000)])
    assert not res.groups
    assert sstat(res, "S1") == sstat(res, "S2") == "SUGGESTED_GROUP"


def test_candidate_cap_skips_search_and_reports():
    rules = Rules(max_candidates=5)
    psps = [P(f"S{i}", 100 + i) for i in range(6)]
    res = run(psps, [K("K1", 100 + 101)], rules=rules)
    k = next(e for e in res.exceptions if e.case_key == "B|BANK|K1")
    assert k.case_type == "UNEXPECTED_BANK_CREDIT" and "more than 5 candidates" in k.explanation
    assert not res.groups


def test_record_never_consumed_twice_and_amounts_conserved():
    # merged reference, split reference and duplicate reference credit in one input
    psps = [P("S1", 5000), P("S2", 7000), P("S3", 9000)]
    banks = [K("K1", 12000, ref="S1,S2"), K("K2", 4000, ref="S3"), K("K3", 5000, ref="S3"),
             K("K4", 9000, ref="S3 duplicate")]
    res = run(psps, banks)
    seen = set()
    for g in res.groups:
        assert g.total("L") == g.total("R")
        for m in g.members:
            assert (g.chain, m.record_id) not in seen
            seen.add((g.chain, m.record_id))
    by_rule = {g.rule: sorted(m.record_id for m in g.members) for g in res.groups}
    assert by_rule == {"B2M_REF_MERGED": ["K1", "S1", "S2"]}
    # S3: K4 (90.00) matches exactly, but K2 + K3 (40 + 50) is an equally conserving split -> review, not a pick
    assert sstat(res, "S3") == "AMBIGUOUS_CANDIDATES"


def test_duplicate_reference_credit_after_exact_match():
    res = run([P("S1", 5000)], [K("K1", 5000, ref="S1"), K("K2", 5000, ref="S1", vdate=D + timedelta(days=9))])
    assert [g.rule for g in res.groups] == ["B1_REF_1TO1"]
    assert any(e.case_type == "DUPLICATE_REFERENCE_CREDIT" and e.anchor_id == "K2" for e in res.exceptions)


# ---------------------------------------------------------------- boundaries
@pytest.mark.parametrize("offset,matched", [(-2, False), (-1, True), (0, True), (3, True), (4, False)])
def test_chain_b_window_boundaries(offset, matched):
    res = run([P("S1", 5000)], [K("K1", 5000, vdate=D + timedelta(days=offset), ref="S1")])
    assert bool(res.groups) is matched
    if not matched:
        assert sstat(res, "S1") == "DATE_OUT_OF_WINDOW"


def test_currency_and_account_never_crossed():
    res = run([P("S1", 5000)], [K("K1", 5000, ref="S1", cur="USD")])
    assert not res.groups and sstat(res, "S1") == "CURRENCY_MISMATCH"
    res = run([P("S1", 5000)], [K("K1", 5000, ref="S1", acct="BK2")])
    assert not res.groups and sstat(res, "S1") == "ACCOUNT_MISMATCH"
    res = run([P("S1", 5000)], [K("K1", 5000, acct="BK2")])   # no reference, other account: no B3 either
    assert not res.groups


def test_partial_receipt_then_remainder():
    res = run([P("S1", 10000)], [K("K1", 6000, ref="S1")])
    e = next(x for x in res.exceptions if x.anchor_id == "S1")
    assert (e.case_type, e.amount) == ("PARTIAL_RECEIPT", 4000)
    res = run([P("S1", 10000)], [K("K1", 6000, ref="S1"), K("K2", 4000, ref="S1", vdate=D + timedelta(days=1))])
    assert [g.rule for g in res.groups] == ["B2S_REF_SPLIT"]


def test_reference_is_whole_token_not_substring():
    res = run([P("S1", 5000), P("S10", 7000)], [K("K1", 7000, ref="S10")])
    g = res.groups[0]
    assert g.rule == "B1_REF_1TO1" and {m.record_id for m in g.members} == {"S10", "K1"}


# ---------------------------------------------------------------- order independence (acceptance point 5)
def test_engine_result_independent_of_input_order(conn):
    for src, n in (("ledger", "ledger.csv"), ("psp", "psp.csv"), ("bank", "bank.csv")):
        import_file(conn, src, SAMPLE / n)
    import_file(conn, "bank", SAMPLE / "late" / "bank_late.csv")
    base = load_input(conn, date(2026, 7, 11))
    ref = normalize(run_engine(base))
    rng = random.Random(1234)
    for _ in range(25):
        inp = replace(base, ledger=rng.sample(base.ledger, len(base.ledger)), psp=rng.sample(base.psp, len(base.psp)),
                      bank=rng.sample(base.bank, len(base.bank)))
        assert normalize(run_engine(inp)) == ref


def test_shuffled_csv_files_give_identical_persisted_results(tmp_path):
    from recon.db import connect

    def go(shuffle_seed):
        c = connect(tmp_path / f"s{shuffle_seed}.db")
        for src, n in (("ledger", "ledger.csv"), ("psp", "psp.csv"), ("bank", "bank.csv")):
            lines = (SAMPLE / n).read_text().splitlines()
            body = lines[1:]
            if shuffle_seed:
                random.Random(shuffle_seed).shuffle(body)
            import_bytes(c, src, ("\n".join([lines[0]] + body) + "\n").encode(), n)
        rid = reconcile(c, date(2026, 7, 10))["run_id"]
        rows = sorted(tuple(r) for r in c.execute(
            "SELECT chain, record_type, record_id, status, rule FROM record_status WHERE run_id=?", (rid,)))
        cases = sorted(tuple(r) for r in c.execute("SELECT case_key, case_type FROM cases"))
        c.close()
        return rows, cases

    base = go(0)
    for seed in (1, 2, 3):
        rows, cases = go(seed)
        # Conflicting rows: which of the two loads first depends on file order, but either way the key is blocked,
        # so statuses and cases are identical.
        assert rows == base[0] and cases == base[1]


# ---------------------------------------------------------------- as-of cutoff (acceptance point 2)
def test_as_of_in_the_past_excludes_future_records(conn):
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-05"),
                                                      led(2, "O2", "20.00", "2026-07-06")]), "l.csv")
    import_bytes(conn, "psp", csv_bytes(PSP_H, [
        psp(1, "O1", "10.00", "0.50", "2026-07-05", batch="B1", sdate="2026-07-05"),
        psp(2, "O2", "20.00", "1.00", "2026-07-06", batch="B2", sdate="2026-07-07", avail="2026-07-06")]), "p.csv")
    import_bytes(conn, "bank", csv_bytes(BANK_H, [bank(1, "9.50", "2026-07-05", ref="B1"),
                                                  bank(2, "19.00", "2026-07-07", ref="B2")]), "b.csv")
    # as-of 07-05: day-06 ledger/psp and day-07 bank/settlement are invisible
    r1 = reconcile(conn, date(2026, 7, 5))["run_id"]
    assert status_map(conn, r1, "A", "ledger") == {"L1": "MATCHED"}
    assert set(status_map(conn, r1, "B", "bank")) == {"K1"}
    assert settlement_view(conn, r1) == {"B1": ["MATCHED", "B1_REF_1TO1", ["K1"]]}
    s = conn.execute("SELECT summary_json FROM runs WHERE run_id=?", (r1,)).fetchone()[0]
    import json
    assert json.loads(s)["excluded_after_as_of"] == {"ledger": 1, "psp": 1, "bank": 1, "psp_settlement_after_as_of": 0}
    total = conn.execute("SELECT SUM(amount_minor) FROM record_status WHERE run_id=? AND chain='A' AND "
                         "record_type='ledger'", (r1,)).fetchone()[0]
    assert total == 1000
    # as-of 07-06: PSP item created but its batch settles 07-07 -> treated as unsettled, not matched
    r2 = reconcile(conn, date(2026, 7, 6))["run_id"]
    assert status_map(conn, r2, "B", "psp") == {"P2": "NOT_DUE"}
    assert "B2" not in settlement_view(conn, r2)
    # as-of 07-07 boundary: dated == as-of is included
    r3 = reconcile(conn, date(2026, 7, 7))["run_id"]
    assert settlement_view(conn, r3)["B2"] == ["MATCHED", "B1_REF_1TO1", ["K2"]]


# ---------------------------------------------------------------- chain A specifics
def _a(conn, ledger_rows, psp_rows, as_of="2026-07-10"):
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, ledger_rows), "l.csv")
    import_bytes(conn, "psp", csv_bytes(PSP_H, psp_rows), "p.csv")
    rid = reconcile(conn, date.fromisoformat(as_of))["run_id"]
    return status_map(conn, rid, "A", "ledger"), status_map(conn, rid, "A", "psp")


def test_same_ref_different_merchants_not_merged(conn):
    l, p = _a(conn, [led(1, "ORD-1", "10.00", "2026-07-01", mer="MA"), led(2, "ORD-1", "10.00", "2026-07-01", mer="MB")],
              [psp(1, "ORD-1", "10.00", "0.10", "2026-07-01", mer="MB")])
    assert l == {"L1": "ACCOUNT_MISMATCH", "L2": "MATCHED"} and p == {"P1": "MATCHED"}
    g = conn.execute("SELECT group_id FROM match_members WHERE record_id='P1'").fetchall()
    members = {r[0] for r in conn.execute("SELECT record_id FROM match_members WHERE group_id=? AND chain='A'", (g[0][0],))}
    assert members == {"L2", "P1"}


@pytest.mark.parametrize("psp_date,status", [("2026-07-03", "MATCHED"), ("2026-07-04", "DATE_OUT_OF_TOLERANCE"),
                                             ("2026-06-29", "MATCHED"), ("2026-06-28", "DATE_OUT_OF_TOLERANCE")])
def test_chain_a_date_tolerance_boundary(conn, psp_date, status):
    l, _ = _a(conn, [led(1, "O1", "10.00", "2026-07-01")], [psp(1, "O1", "10.00", "0.10", psp_date)])
    assert l == {"L1": status}


@pytest.mark.parametrize("ldate,status", [("2026-07-08", "PENDING"), ("2026-07-07", "MISSING_IN_PSP")])
def test_chain_a_grace_boundary(conn, ldate, status):
    l, _ = _a(conn, [led(1, "O1", "10.00", ldate)], [])
    assert l == {"L1": status}


def test_amount_difference_one_cent_is_not_matched(conn):
    l, _ = _a(conn, [led(1, "O1", "10.00", "2026-07-01")], [psp(1, "O1", "9.99", "0.10", "2026-07-01")])
    assert l == {"L1": "AMOUNT_MISMATCH"}


# ---------------------------------------------------------------- isolation from ground truth (REC-09)
def test_engine_code_cannot_reach_ground_truth():
    root = Path(__file__).resolve().parent.parent / "recon"
    for f in root.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any(m and ("evaluation" in m or m.startswith("tests")) for m in mods), f
        src = f.read_text().lower()
        assert "truth" not in src.replace("ground truth", ""), f"{f.name} mentions truth"
    matching = (root / "matching.py").read_text()
    assert "open(" not in matching and "sqlite3" not in matching and "import os" not in matching
