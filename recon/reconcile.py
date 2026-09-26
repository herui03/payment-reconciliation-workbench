"""Reconciliation run orchestration: load records from SQLite, run the pure engine, persist results, sync cases.

Case lifecycle across re-runs (EXC-06 / EXC-07)
- A case is identified by a stable ``case_key`` (``A|<business_ref>``, ``B|STL|<batch>``, ``B|BANK|<line>``,
  ``B|ITEM|<psp item>``), so a re-run never creates a second case for the same subject.
- Engine-owned fields (type, rule, amount, explanation, evidence) are refreshed; analyst-owned fields (owner,
  status, notes, disposition) are never touched by a re-run, except:
    * subject no longer an exception and case not Resolved  -> Resolved / AUTO_CLEARED by ``system``
    * subject no longer an exception and case already Resolved (manually) -> stays as is, audit note only
    * AUTO_CLEARED case whose issue re-appears -> re-opened by ``system``
- Every such change writes an audit event.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from datetime import date

from . import RULES_VERSION
from .db import audit, tx, utcnow
from .ingest import blocked_keys
from .matching import BankLine, EngineInput, EngineResult, Ledger, PspItem, Rules, run_engine

SYSTEM_ACTOR = "system"


def _d(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def load_input(conn: sqlite3.Connection, as_of: date, rules: Rules | None = None,
               max_import_id: int | None = None) -> EngineInput:
    """Load records as known on ``as_of``.

    As-of cutoff (point-in-time by business date): ledger rows with event_date > as_of, PSP items with
    created_date > as_of and bank lines with value_date > as_of are excluded from the run entirely (not matched,
    not in totals). A PSP item created on/before as_of whose settlement_date is after as_of is treated as not yet
    settled. The cutoff uses business dates only; the tool does not track when a row was physically received.
    """
    iso = as_of.isoformat()
    hw = max_import_id if max_import_id is not None else 1 << 62
    excluded = {"ledger": 0, "psp": 0, "bank": 0, "psp_settlement_after_as_of": 0}
    ledger = []
    for r in conn.execute("SELECT * FROM ledger_records WHERE import_id <= ?", (hw,)):
        if r["event_date"] > iso:
            excluded["ledger"] += 1
            continue
        ledger.append(Ledger(r["record_id"], r["event_type"], r["business_ref"], r["merchant_account"],
                             r["currency"], r["gross_minor"], _d(r["event_date"])))
    psp = []
    for r in conn.execute("SELECT * FROM psp_records WHERE import_id <= ?", (hw,)):
        if r["created_date"] > iso:
            excluded["psp"] += 1
            continue
        batch, sdate, acct = r["settlement_batch_id"], _d(r["settlement_date"]), r["bank_account"]
        if batch and r["settlement_date"] > iso:
            excluded["psp_settlement_after_as_of"] += 1
            batch, sdate = "", None
        psp.append(PspItem(r["record_id"], r["type"], r["business_ref"], r["merchant_account"], r["currency"],
                           r["gross_minor"], r["fee_minor"], r["net_minor"], _d(r["created_date"]),
                           _d(r["available_on"]), batch, sdate, acct))
    bank = []
    for r in conn.execute("SELECT * FROM bank_records WHERE import_id <= ?", (hw,)):
        if r["value_date"] > iso:
            excluded["bank"] += 1
            continue
        bank.append(BankLine(r["record_id"], r["bank_account"], r["currency"], r["amount_minor"],
                             _d(r["value_date"]), r["reference"]))
    blocked = {src: {rid: [row["issue_id"] for row in rows if row["import_id"] <= hw] for rid, rows in m.items()}
               for src, m in blocked_keys(conn).items()}
    blocked = {src: {rid: ids for rid, ids in m.items() if ids} for src, m in blocked.items()}
    hints: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in conn.execute("SELECT ri.*, i.sha256 FROM row_issues ri JOIN imports i USING(import_id) "
                          "WHERE ri.kind='QUARANTINED' AND ri.reason_code NOT LIKE 'CONFLICT%' AND ri.import_id <= ? "
                          "ORDER BY issue_id", (hw,)):
        raw = json.loads(r["raw_json"])
        bref = (raw.get("business_ref") or "").strip()
        mer = (raw.get("merchant_account") or "").strip()
        if bref:
            hints[(r["source"], f"{mer}|{bref}")].append(
                f"Note: a {r['source']} row for {bref} was quarantined ({r['reason_code']}, file "
                f"{r['sha256'][:12]} row {r['source_row']}).")
    return EngineInput(as_of=as_of, ledger=ledger, psp=psp, bank=bank, blocked=blocked, hints=dict(hints),
                       rules=rules or Rules(), excluded_after_as_of=excluded)


def input_fingerprint(conn: sqlite3.Connection) -> str:
    shas = sorted(r[0] for r in conn.execute("SELECT sha256 FROM imports WHERE status IN ('LOADED','PARTIAL')"))
    return hashlib.sha256("|".join(shas).encode()).hexdigest()


def reconcile(conn: sqlite3.Connection, as_of: date, actor: str = "demo.analyst",
              rules: Rules | None = None) -> dict:
    t0 = time.perf_counter()
    started = utcnow()
    max_import = conn.execute("SELECT COALESCE(MAX(import_id), 0) FROM imports").fetchone()[0]
    inp = load_input(conn, as_of, rules, max_import)
    res = run_engine(inp)
    engine_ms = int((time.perf_counter() - t0) * 1000)
    summary = build_summary(inp, res)
    with tx(conn):
        # Policy: only a run whose as-of is on/after the latest workflow-synced run may change cases. A run
        # "looking back" (earlier as-of) is persisted as a read-only historical snapshot: it never opens, clears,
        # re-opens or edits cases, so browsing the past cannot close today's operational items.
        prev = conn.execute("SELECT MAX(as_of) FROM runs WHERE case_sync=1 AND finished_at IS NOT NULL").fetchone()[0]
        case_sync = prev is None or as_of.isoformat() >= prev
        run_id = conn.execute(
            "INSERT INTO runs(started_at, as_of, rules_version, actor, input_fingerprint, max_import_id, case_sync) "
            "VALUES (?,?,?,?,?,?,?)",
            (started, as_of.isoformat(), RULES_VERSION, actor, input_fingerprint(conn), max_import, int(case_sync)),
        ).lastrowid
        conn.executemany(
            "INSERT INTO match_groups(run_id, group_id, chain, rule, currency, left_total, right_total, explanation) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, g.group_id, g.chain, g.rule, g.currency, g.total("L"), g.total("R"), g.explanation)
             for g in res.groups])
        conn.executemany(
            "INSERT INTO match_members(run_id, group_id, chain, side, record_type, record_id, amount_minor) "
            "VALUES (?,?,?,?,?,?,?)",
            [(run_id, g.group_id, g.chain, m.side, m.record_type, m.record_id, m.amount)
             for g in res.groups for m in g.members])
        conn.executemany(
            "INSERT INTO record_status(run_id, chain, record_type, record_id, status, rule, group_id, case_key, "
            "currency, amount_minor, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, s.chain, s.record_type, s.record_id, s.status, s.rule, s.group_id, s.case_key, s.currency,
              s.amount, s.detail) for s in res.states])
        if case_sync:
            changes = sync_cases(conn, res, run_id, inp)
        else:
            changes = {"opened": 0, "updated": 0, "auto_cleared": 0, "reopened": 0, "cleared_after_manual": 0,
                       "reappeared_after_manual": 0}
        summary["case_changes"] = changes
        summary["case_sync"] = case_sync
        summary["case_sync_note"] = ("" if case_sync else
                                     f"Historical run: as-of {as_of} is before the latest workflow run as-of {prev}; "
                                     f"read-only snapshot, cases were not changed.")
        _snapshot_exceptions(conn, res, run_id)
        summary["rules"] = inp.rules.as_dict()
        elapsed = int((time.perf_counter() - t0) * 1000)
        summary["engine_ms"] = engine_ms
        conn.execute("UPDATE runs SET finished_at=?, elapsed_ms=?, summary_json=? WHERE run_id=?",
                     (utcnow(), elapsed, json.dumps(summary, sort_keys=True), run_id))
        audit(conn, actor, "run", str(run_id), "RUN_COMPLETED",
              json.dumps({"as_of": as_of.isoformat(), "rules_version": RULES_VERSION, "case_sync": case_sync,
                          "groups": len(res.groups),
                          "exceptions": len(res.exceptions), "case_changes": changes}), run_id)
    return {"run_id": run_id, "summary": summary, "elapsed_ms": elapsed}


# ---------------------------------------------------------------- cases

def _snapshot_exceptions(conn: sqlite3.Connection, res: EngineResult, run_id: int) -> None:
    """Persist what this run reported plus the case workflow state at run completion (run-consistent history)."""
    cases = {r["case_key"]: r for r in conn.execute("SELECT case_key, case_id, status, owner, disposition FROM cases")}
    rows = []
    for e in res.exceptions:
        c = cases.get(e.case_key)
        rows.append((run_id, e.case_key, c["case_id"] if c else None, e.chain, e.case_type, e.rule, e.anchor_id,
                     e.currency, e.amount, e.anchor_date.isoformat(), e.explanation,
                     c["status"] if c else "Not tracked (historical run)", c["owner"] if c else "",
                     c["disposition"] if c else ""))
    conn.executemany("INSERT INTO run_exceptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _anchor_state(res: EngineResult, key: str, a_subjects: dict[str, list[tuple[str, str]]]) -> str:
    idx = {(s.chain, s.record_type, s.record_id): s for s in res.states}
    parts = key.split("|")
    if parts[0] == "A":
        cand = [idx.get(("A", t, rid)) for t, rid in a_subjects.get(key, [])]
    else:
        rtype = {"STL": "settlement", "BANK": "bank", "ITEM": "psp"}[parts[1]]
        cand = [idx.get(("B", rtype, parts[2]))]
    cand = [c for c in cand if c]
    if not cand:
        return "not present in this run (e.g. dated after the as-of date)"
    c = cand[0]
    return f"now {c.status}" + (f" by rule {c.rule} (group {c.group_id})" if c.group_id else f" ({c.rule})")


def sync_cases(conn: sqlite3.Connection, res: EngineResult, run_id: int, inp: EngineInput | None = None) -> dict:
    now = utcnow()
    a_subjects: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if inp is not None:
        for x in inp.ledger:
            a_subjects[f"A|{x.merchant_account}|{x.business_ref}"].append(("ledger", x.record_id))
        for p in inp.psp:
            if p.type != "fee":
                a_subjects[f"A|{p.merchant_account}|{p.business_ref}"].append(("psp", p.record_id))
    existing = {r["case_key"]: dict(r) for r in conn.execute("SELECT * FROM cases")}
    produced = {e.case_key: e for e in res.exceptions}
    ch = {"opened": 0, "updated": 0, "auto_cleared": 0, "reopened": 0, "cleared_after_manual": 0,
          "reappeared_after_manual": 0}
    for key, e in produced.items():
        fields = {"case_type": e.case_type, "rule": e.rule, "chain": e.chain, "anchor_type": e.anchor_type,
                  "anchor_id": e.anchor_id, "currency": e.currency, "amount_minor": e.amount,
                  "anchor_date": e.anchor_date.isoformat(), "explanation": e.explanation,
                  "evidence_json": json.dumps(e.evidence, sort_keys=True)}
        c = existing.get(key)
        if c is None:
            cid = conn.execute(
                "INSERT INTO cases(case_key, chain, case_type, rule, anchor_type, anchor_id, currency, amount_minor, "
                "anchor_date, explanation, evidence_json, status, engine_active, first_seen_run, last_seen_run, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'Open',1,?,?,?,?)",
                (key, e.chain, e.case_type, e.rule, e.anchor_type, e.anchor_id, e.currency, e.amount,
                 fields["anchor_date"], e.explanation, fields["evidence_json"], run_id, run_id, now, now),
            ).lastrowid
            audit(conn, SYSTEM_ACTOR, "case", str(cid), "CASE_OPENED",
                  json.dumps({"case_key": key, "type": e.case_type, "amount_minor": e.amount,
                              "currency": e.currency}), run_id)
            ch["opened"] += 1
            continue
        cid = c["case_id"]
        diff = {k: {"from": c[k], "to": v} for k, v in fields.items() if c[k] != v}
        sets = dict(fields)
        sets["last_seen_run"] = run_id
        if c["engine_active"] == 0:
            sets["engine_active"] = 1
            if c["status"] == "Resolved" and c["disposition"] == "AUTO_CLEARED":
                sets.update(status="Open", disposition="", resolution_reason="")
                audit(conn, SYSTEM_ACTOR, "case", str(cid), "CASE_REOPENED",
                      json.dumps({"from": "Resolved", "to": "Open",
                                  "reason": f"issue re-appeared in run {run_id} ({e.case_type})"}), run_id)
                ch["reopened"] += 1
            else:
                audit(conn, SYSTEM_ACTOR, "case", str(cid), "CASE_ISSUE_REAPPEARED",
                      json.dumps({"status_kept": c["status"], "type": e.case_type}), run_id)
                ch["reappeared_after_manual"] += 1
        if diff:
            sets["updated_at"] = now
            important = {k: v for k, v in diff.items() if k in ("case_type", "rule", "amount_minor", "currency",
                                                                   "evidence_json")}
            if important:
                audit(conn, SYSTEM_ACTOR, "case", str(cid), "CASE_ENGINE_FIELDS_UPDATED",
                      json.dumps(important, sort_keys=True), run_id)
                ch["updated"] += 1
        conn.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in sets)} WHERE case_id=?",
                     list(sets.values()) + [cid])
    for key, c in existing.items():
        if key in produced or c["engine_active"] == 0:
            continue
        cid = c["case_id"]
        now_state = _anchor_state(res, key, a_subjects)
        if c["status"] != "Resolved":
            reason = f"Run {run_id}: subject {now_state}; no longer an exception."
            conn.execute("UPDATE cases SET engine_active=0, status='Resolved', disposition='AUTO_CLEARED', "
                         "resolution_reason=?, updated_at=? WHERE case_id=?", (reason, now, cid))
            audit(conn, SYSTEM_ACTOR, "case", str(cid), "CASE_AUTO_CLEARED",
                  json.dumps({"from": c["status"], "to": "Resolved", "disposition": "AUTO_CLEARED",
                              "reason": reason}), run_id)
            ch["auto_cleared"] += 1
        else:
            conn.execute("UPDATE cases SET engine_active=0, updated_at=? WHERE case_id=?", (now, cid))
            audit(conn, SYSTEM_ACTOR, "case", str(cid), "CASE_ENGINE_CLEARED",
                  json.dumps({"status_kept": "Resolved", "disposition_kept": c["disposition"],
                              "note": f"Run {run_id}: subject {now_state}"}), run_id)
            ch["cleared_after_manual"] += 1
    return ch


# ---------------------------------------------------------------- summary

def build_summary(inp: EngineInput, res: EngineResult) -> dict:
    """Per-chain, per-currency counts and amounts. Never sums across currencies."""
    def bucket():
        return {"count": 0, "amount_minor": 0}

    out = {"as_of": inp.as_of.isoformat(), "rules_version": RULES_VERSION, "chains": {}}
    by = defaultdict(lambda: defaultdict(lambda: defaultdict(bucket)))  # chain/rtype -> currency -> status -> bucket
    for s in res.states:
        b = by[f"{s.chain}:{s.record_type}"][s.currency][s.status]
        b["count"] += 1
        b["amount_minor"] += s.amount
    out["status_breakdown"] = {k: {cur: dict(v2) for cur, v2 in v.items()} for k, v in by.items()}

    # Chain B bridge: settled PSP gross - fees = net, per currency (from settlements, not from ledger).
    bridge = defaultdict(lambda: {"batches": 0, "gross_minor": 0, "fee_minor": 0, "net_minor": 0})
    for s in res.settlements.values():
        if s.problem.startswith("BATCH_INCONSISTENT"):
            continue
        x = bridge[s.currency]
        x["batches"] += 1
        x["gross_minor"] += s.gross
        x["fee_minor"] += s.fee
        x["net_minor"] += s.net
    out["settlement_bridge"] = dict(bridge)
    groups = defaultdict(lambda: defaultdict(lambda: {"groups": 0, "amount_minor": 0}))
    for g in res.groups:
        x = groups[g.chain][f"{g.rule}|{g.currency}"]
        x["groups"] += 1
        x["amount_minor"] += g.total("L")
    out["groups_by_rule"] = {c: dict(v) for c, v in groups.items()}
    exc = defaultdict(lambda: {"count": 0, "amount_minor": 0})
    for e in res.exceptions:
        x = exc[f"{e.chain}|{e.case_type}|{e.currency}"]
        x["count"] += 1
        x["amount_minor"] += e.amount
    out["exceptions"] = dict(exc)
    out["excluded_after_as_of"] = inp.excluded_after_as_of
    out["counts"] = {"groups": len(res.groups), "exceptions": len(res.exceptions),
                     "ledger": len(inp.ledger), "psp": len(inp.psp), "bank": len(inp.bank),
                     "settlements": len(res.settlements)}
    return out


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()
