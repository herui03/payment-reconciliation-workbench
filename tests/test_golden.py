"""Golden fixture: compare engine output with hand-written expected answers (tests/golden/expected.json)."""
import json
from datetime import date

from conftest import SAMPLE, chain_a_status, settlement_view, status_map

from recon.cases import add_note, set_status
from recon.ingest import import_file
from recon.reconcile import reconcile
from recon.reports import eod_data

EXP = json.loads((SAMPLE.parent.parent / "tests" / "golden" / "expected.json").read_text())


def _step1(conn):
    res = {n: import_file(conn, src, SAMPLE / n) for src, n in (("ledger", "ledger.csv"), ("psp", "psp.csv"),
                                                                ("bank", "bank.csv"))}
    run = reconcile(conn, date.fromisoformat(EXP["step1_as_of"]))
    return res, run["run_id"]


def _cases(conn):
    return {r["case_key"]: dict(r) for r in conn.execute("SELECT * FROM cases")}


def test_step1_imports(conn):
    res, _ = _step1(conn)
    for name, e in EXP["step1_imports"].items():
        r = res[name]
        assert (r.status, r.rows_read, r.rows_loaded, r.rows_duplicate, r.rows_quarantined) == \
               (e["status"], e["read"], e["loaded"], e["duplicate"], e["quarantined"]), name
        q = {str(i["row"]): i["code"] for i in r.issues if i["kind"] == "QUARANTINED"}
        assert q == e["quarantine_codes"], name
        assert [i["row"] for i in r.issues if i["kind"] == "DUPLICATE"] == e["duplicate_rows"], name


def test_step1_chain_a(conn):
    _, run = _step1(conn)
    got = chain_a_status(conn, run)
    for k, v in EXP["step1_chain_a"].items():
        assert got.get(k) == v, (k, got.get(k), v)
    for k in EXP["step1_chain_a_absent"]:
        assert k not in got, k
    cases = _cases(conn)
    for k, amt in EXP["step1_chain_a_amounts"].items():
        assert cases[f"A|{k}"]["amount_minor"] == amt, k


def test_step1_chain_b(conn):
    _, run = _step1(conn)
    got = settlement_view(conn, run)
    for bid, e in EXP["step1_settlements"].items():
        assert got.get(bid) == e, (bid, got.get(bid), e)
    for bid in EXP["step1_settlements_absent"]:
        assert bid not in got
    cases = _cases(conn)
    for bid, amt in EXP["step1_partial_outstanding"].items():
        assert cases[f"B|STL|{bid}"]["amount_minor"] == amt
    psp = status_map(conn, run, "B", "psp")
    assert {k: psp.get(k) for k in EXP["step1_unsettled_psp"]} == EXP["step1_unsettled_psp"]
    bank = status_map(conn, run, "B", "bank")
    assert {k: bank.get(k) for k in EXP["step1_bank"]} == EXP["step1_bank"]
    for k in EXP["step1_bank_absent"]:
        assert k not in bank


def test_step1_counts_and_totals(conn):
    _, run = _step1(conn)
    g = dict(conn.execute("SELECT chain, COUNT(*) FROM match_groups WHERE run_id=? GROUP BY chain", (run,)).fetchall())
    assert g == EXP["step1_group_counts"]
    e = dict(conn.execute("SELECT chain, COUNT(*) FROM cases GROUP BY chain").fetchall())
    assert e == EXP["step1_exception_counts"]
    d = eod_data(conn, run)
    led = {x["currency"]: x for x in d["chain_a"]["ledger"]}
    for cur, t in EXP["step1_ledger_totals"].items():
        assert (led[cur]["total_count"], led[cur]["total_amount"], led[cur]["matched_count"],
                led[cur]["matched_amount"]) == (t["count"], t["amount"], t["matched_count"], t["matched_amount"]), cur
    bank = {x["currency"]: x for x in d["chain_b"]["bank"]}
    for cur, t in EXP["step1_bank_totals"].items():
        assert (bank[cur]["total_count"], bank[cur]["matched_count"], bank[cur]["matched_amount"]) == \
               (t["count"], t["matched_count"], t["matched_amount"]), cur
    assert d["bridge"] == EXP["step1_bridge"]


def test_step2_duplicate_and_rejected_files(conn):
    _step1(conn)
    for name, e in EXP["step2_imports"].items():
        r = import_file(conn, e["source"], SAMPLE / name)
        assert r.status == e["status"], (name, r.status, r.message)
        assert r.rows_loaded == e["loaded"], name
        if "duplicate" in e:
            assert r.rows_duplicate == e["duplicate"]
    assert conn.execute("SELECT COUNT(*) FROM bank_records WHERE record_id='B-900'").fetchone()[0] == 0


def test_step3_late_data_rerun(conn):
    _, run1 = _step1(conn)
    for name, e in EXP["step2_imports"].items():
        import_file(conn, e["source"], SAMPLE / name)
    cases = _cases(conn)
    # analyst works a case before late data arrives
    cid = cases["B|STL|STL-US-0704"]["case_id"]
    add_note(conn, cid, "Chased PSP; payout trace requested", "analyst.a")
    set_status(conn, cid, "In review", "analyst.a")
    # manually resolve an unrelated case: must stay resolved and must NOT become a match
    bcid = cases["B|BANK|B-015"]["case_id"]
    set_status(conn, bcid, "Resolved", "analyst.a", "NO_ACTION_REQUIRED", "Bank interest, booked separately")

    import_file(conn, "bank", SAMPLE / EXP["step3_late_file"])
    run2 = reconcile(conn, date.fromisoformat(EXP["step3_as_of"]))["run_id"]
    got = settlement_view(conn, run2)
    for bid, e in EXP["step3_settlements"].items():
        assert got.get(bid) == e, (bid, got.get(bid), e)
    a = chain_a_status(conn, run2)
    for k, v in EXP["step3_chain_a"].items():
        assert a.get(k) == v, k
    g = dict(conn.execute("SELECT chain, COUNT(*) FROM match_groups WHERE run_id=? GROUP BY chain", (run2,)).fetchall())
    assert g == EXP["step3_group_counts"]
    active = dict(conn.execute("SELECT chain, COUNT(*) FROM cases WHERE engine_active=1 GROUP BY chain").fetchall())
    assert active == EXP["step3_exception_counts"]
    cases2 = _cases(conn)
    assert len(cases2) == EXP["step3_total_cases"]
    for k in EXP["step3_auto_cleared_case_keys"]:
        assert (cases2[k]["status"], cases2[k]["disposition"]) == ("Resolved", "AUTO_CLEARED"), k
    # notes survive, manual resolution survives and is still flagged as an engine exception
    assert conn.execute("SELECT COUNT(*) FROM case_notes WHERE case_id=?", (cid,)).fetchone()[0] == 1
    b = cases2["B|BANK|B-015"]
    assert (b["status"], b["disposition"], b["engine_active"]) == ("Resolved", "NO_ACTION_REQUIRED", 1)
    assert status_map(conn, run2, "B", "bank")["B-015"] == "UNEXPECTED_BANK_CREDIT"
    acts = [r[0] for r in conn.execute("SELECT action FROM audit_events WHERE entity_type='case' AND entity_id=? "
                                       "ORDER BY event_id", (str(cid),))]
    assert acts == ["CASE_OPENED", "NOTE_ADDED", "CASE_STATUS_CHANGED", "CASE_AUTO_CLEARED"]
