"""Case workflow, audit trail and re-run behaviour."""
import sqlite3
from datetime import date

import pytest
from conftest import SAMPLE, status_map
from helpers import BANK_H, LEDGER_H, PSP_H, bank, csv_bytes, led, psp

from recon.cases import WorkflowError, add_note, assign, set_status
from recon.ingest import import_bytes, import_file
from recon.reconcile import reconcile


@pytest.fixture
def sample(conn):
    for src, n in (("ledger", "ledger.csv"), ("psp", "psp.csv"), ("bank", "bank.csv")):
        import_file(conn, src, SAMPLE / n)
    run = reconcile(conn, date(2026, 7, 10))["run_id"]
    return conn, run


def cid(conn, key):
    return conn.execute("SELECT case_id FROM cases WHERE case_key=?", (key,)).fetchone()[0]


def case(conn, c):
    return dict(conn.execute("SELECT * FROM cases WHERE case_id=?", (c,)).fetchone())


def test_resolve_requires_disposition_and_reason(sample):
    conn, _ = sample
    c = cid(conn, "A|MER-US-01|ORD-1002")
    with pytest.raises(WorkflowError):
        set_status(conn, c, "Resolved", "a1")
    with pytest.raises(WorkflowError):
        set_status(conn, c, "Resolved", "a1", "NO_ACTION_REQUIRED", "short")
    with pytest.raises(WorkflowError):
        set_status(conn, c, "Resolved", "a1", "AUTO_CLEARED", "users cannot pick the system disposition")
    with pytest.raises(WorkflowError):
        set_status(conn, c, "Resolved", "system", "NO_ACTION_REQUIRED", "system actor is reserved")
    assert case(conn, c)["status"] == "Open"
    set_status(conn, c, "In review", "a1")
    set_status(conn, c, "Resolved", "a1", "PSP_QUERY_RAISED", "Asked PSP about the 50.00 difference")
    assert case(conn, c)["status"] == "Resolved"
    with pytest.raises(WorkflowError):
        set_status(conn, c, "Open", "a1", reason="")
    set_status(conn, c, "Open", "a2", reason="PSP says no adjustment; still open")
    x = case(conn, c)
    assert (x["status"], x["disposition"]) == ("Open", "")
    acts = [tuple(r) for r in conn.execute("SELECT actor, action FROM audit_events WHERE entity_type='case' AND "
                                           "entity_id=? ORDER BY event_id", (str(c),))]
    assert acts == [("system", "CASE_OPENED"), ("a1", "CASE_STATUS_CHANGED"), ("a1", "CASE_RESOLVED"),
                    ("a2", "CASE_REOPENED")]


def test_manual_resolution_never_creates_match_or_changes_amount(sample):
    conn, run = sample
    c = cid(conn, "B|STL|STL-US-0704")
    before_amt = case(conn, c)["amount_minor"]
    set_status(conn, c, "Resolved", "a1", "MANUAL_MATCH_CONFIRMED", "Bank confirmed by phone, statement pending")
    run2 = reconcile(conn, date(2026, 7, 10))["run_id"]
    x = case(conn, c)
    assert x["amount_minor"] == before_amt and x["engine_active"] == 1 and x["status"] == "Resolved"
    assert status_map(conn, run2, "B", "settlement")["STL-US-0704"] == "MISSING_IN_BANK"
    assert conn.execute("SELECT COUNT(*) FROM match_members WHERE record_id='STL-US-0704'").fetchone()[0] == 0


def test_rerun_same_data_is_idempotent(sample):
    conn, _ = sample
    c = cid(conn, "A|MER-SG-01|ORD-1021")
    add_note(conn, c, "checked", "a1")
    assign(conn, c, "ops.lead", "a1")
    n_cases = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    n_case_events = conn.execute("SELECT COUNT(*) FROM audit_events WHERE entity_type='case'").fetchone()[0]
    r = reconcile(conn, date(2026, 7, 10))
    assert r["summary"]["case_changes"] == {"opened": 0, "updated": 0, "auto_cleared": 0, "reopened": 0,
                                            "cleared_after_manual": 0, "reappeared_after_manual": 0}
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == n_cases
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE entity_type='case'").fetchone()[0] == n_case_events
    x = case(conn, c)
    assert x["owner"] == "ops.lead" and x["status"] == "Open"
    assert conn.execute("SELECT COUNT(*) FROM case_notes WHERE case_id=?", (c,)).fetchone()[0] == 1


def test_auto_cleared_case_reopens_when_issue_reappears(conn):
    import_bytes(conn, "psp", csv_bytes(PSP_H, [psp(1, "O1", "10.00", "0.50", "2026-07-01", batch="B1",
                                                    sdate="2026-07-02")]), "p.csv")
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-01")]), "l.csv")
    reconcile(conn, date(2026, 7, 8))                   # no bank line yet -> MISSING_IN_BANK
    c = cid(conn, "B|STL|B1")
    add_note(conn, c, "waiting for statement", "a1")
    import_bytes(conn, "bank", csv_bytes(BANK_H, [bank(1, "9.50", "2026-07-05", ref="B1")]), "b.csv")
    reconcile(conn, date(2026, 7, 8))                   # matched -> auto-cleared
    x = case(conn, c)
    assert (x["status"], x["disposition"], x["engine_active"]) == ("Resolved", "AUTO_CLEARED", 0)
    # re-run as of 07-04: the 07-05 bank line is not yet visible and (with a 1-day window) the settlement is overdue,
    # so the issue re-appears -> the system re-opens the auto-cleared case instead of creating a second one
    from recon.matching import Rules
    reconcile(conn, date(2026, 7, 4), rules=Rules(b_window_after=1))
    x = case(conn, c)
    assert (x["status"], x["engine_active"]) == ("Open", 1)
    acts = [r[0] for r in conn.execute("SELECT action FROM audit_events WHERE entity_type='case' AND entity_id=? "
                                       "ORDER BY event_id", (str(c),))]
    assert acts == ["CASE_OPENED", "NOTE_ADDED", "CASE_AUTO_CLEARED", "CASE_REOPENED"]
    assert conn.execute("SELECT COUNT(*) FROM case_notes WHERE case_id=?", (c,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1


def test_audit_and_notes_are_append_only(sample):
    conn, _ = sample
    c = cid(conn, "A|MER-SG-01|ORD-1021")
    add_note(conn, c, "original note", "a1")
    for sql in ("UPDATE audit_events SET actor='x'", "DELETE FROM audit_events",
                "UPDATE case_notes SET text='edited'", "DELETE FROM case_notes",
                "UPDATE match_groups SET rule='X'", "DELETE FROM record_status"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


def test_amount_is_not_user_editable_via_workflow_api(sample):
    import inspect

    from recon import cases as m
    for fn in (m.set_status, m.assign, m.add_note):
        assert "amount" not in inspect.signature(fn).parameters
