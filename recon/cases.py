"""Analyst case workflow: assign, review, note, resolve, reopen. Every change writes an audit event.

Manual actions only change case workflow fields (owner/status/disposition/reason) and append notes. They never
create match groups, never change record states, and never change the engine-computed amount.
"""
from __future__ import annotations

import json
import sqlite3

from .db import audit, tx, utcnow

STATUSES = ("Open", "In review", "Resolved")
DISPOSITIONS = {
    "TIMING_DIFFERENCE": "Timing difference - expected to clear",
    "PSP_ADJUSTMENT_CONFIRMED": "PSP fee / adjustment confirmed",
    "BANK_QUERY_RAISED": "Query raised with bank",
    "PSP_QUERY_RAISED": "Query raised with PSP",
    "LEDGER_CORRECTION_REQUESTED": "Ledger correction requested",
    "SOURCE_DATA_CORRECTED": "Source data to be re-sent / corrected",
    "MANUAL_MATCH_CONFIRMED": "Counterpart confirmed manually (engine result unchanged)",
    "NO_ACTION_REQUIRED": "No action required",
}
SYSTEM_ONLY_DISPOSITIONS = ("AUTO_CLEARED",)
MIN_REASON_LEN = 10


class WorkflowError(ValueError):
    pass


def _get(conn, case_id: int) -> dict:
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not row:
        raise WorkflowError(f"case {case_id} not found")
    return dict(row)


def _actor(actor: str) -> str:
    a = (actor or "").strip()
    if not a or a.lower() == "system":
        raise WorkflowError("a demo actor label is required (and 'system' is reserved)")
    return a[:60]


def set_status(conn: sqlite3.Connection, case_id: int, new_status: str, actor: str,
               disposition: str = "", reason: str = "") -> dict:
    actor = _actor(actor)
    if new_status not in STATUSES:
        raise WorkflowError(f"unknown status {new_status!r}")
    with tx(conn):
        c = _get(conn, case_id)
        old = c["status"]
        if old == new_status:
            raise WorkflowError(f"case already {old}")
        reason = (reason or "").strip()
        sets = {"status": new_status, "updated_at": utcnow()}
        if new_status == "Resolved":
            if disposition not in DISPOSITIONS:
                raise WorkflowError("resolving requires a disposition from the list")
            if len(reason) < MIN_REASON_LEN:
                raise WorkflowError(f"resolving requires a reason of at least {MIN_REASON_LEN} characters")
            sets.update(disposition=disposition, resolution_reason=reason)
            action = "CASE_RESOLVED"
        elif old == "Resolved":
            if len(reason) < MIN_REASON_LEN:
                raise WorkflowError(f"reopening requires a reason of at least {MIN_REASON_LEN} characters")
            sets.update(disposition="", resolution_reason="")
            action = "CASE_REOPENED"
        else:
            action = "CASE_STATUS_CHANGED"
        conn.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in sets)} WHERE case_id=?",
                     list(sets.values()) + [case_id])
        audit(conn, actor, "case", str(case_id), action, json.dumps(
            {"from": old, "to": new_status, "disposition": disposition or None, "reason": reason or None,
             "previous_disposition": c["disposition"] or None}))
        return _get(conn, case_id)


def assign(conn: sqlite3.Connection, case_id: int, owner: str, actor: str) -> dict:
    actor = _actor(actor)
    owner = (owner or "").strip()[:60]
    with tx(conn):
        c = _get(conn, case_id)
        if c["owner"] == owner:
            return c
        conn.execute("UPDATE cases SET owner=?, updated_at=? WHERE case_id=?", (owner, utcnow(), case_id))
        audit(conn, actor, "case", str(case_id), "CASE_ASSIGNED", json.dumps({"from": c["owner"], "to": owner}))
        return _get(conn, case_id)


def add_note(conn: sqlite3.Connection, case_id: int, text: str, actor: str) -> int:
    actor = _actor(actor)
    text = (text or "").strip()
    if not text:
        raise WorkflowError("note text is empty")
    if len(text) > 4000:
        raise WorkflowError("note longer than 4000 characters")
    with tx(conn):
        _get(conn, case_id)
        nid = conn.execute("INSERT INTO case_notes(case_id, created_at, actor, text) VALUES (?,?,?,?)",
                           (case_id, utcnow(), actor, text)).lastrowid
        audit(conn, actor, "case", str(case_id), "NOTE_ADDED", json.dumps({"note_id": nid, "length": len(text)}))
        return nid


def list_cases(conn: sqlite3.Connection, status: str | None = None, chain: str | None = None,
               case_type: str | None = None, currency: str | None = None) -> list[dict]:
    q, args = "SELECT c.*, (SELECT COUNT(*) FROM case_notes n WHERE n.case_id=c.case_id) AS note_count FROM cases c", []
    conds = []
    for col, val in (("status", status), ("chain", chain), ("case_type", case_type), ("currency", currency)):
        if val:
            conds.append(f"c.{col}=?")
            args.append(val)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY c.status='Resolved', c.anchor_date, c.case_id"
    return [dict(r) for r in conn.execute(q, args)]


def evidence_rows(conn: sqlite3.Connection, evidence: list[dict]) -> list[dict]:
    """Resolve evidence references to source rows with file hash + row number."""
    out = []
    for ev in evidence:
        t, rid = ev["record_type"], ev["record_id"]
        row = None
        if t in ("ledger", "psp", "bank"):
            row = conn.execute(
                f"SELECT r.*, i.file_name, i.sha256 FROM {t}_records r JOIN imports i USING(import_id) "
                f"WHERE r.record_id=?", (rid,)).fetchone()
        elif t == "row_issue":
            row = conn.execute("SELECT ri.*, i.file_name, i.sha256 FROM row_issues ri JOIN imports i "
                               "USING(import_id) WHERE issue_id=?", (int(rid),)).fetchone()
        item = {"record_type": t, "record_id": rid, "role": ev.get("role", "")}
        if row:
            d = dict(row)
            item.update(file_name=d["file_name"], sha256=d["sha256"], source_row=d["source_row"], fields=d)
        out.append(item)
    return out
