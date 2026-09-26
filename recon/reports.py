"""End-of-day report (CSV + HTML) built from a persisted run.

Financial figures are kept per chain and per currency. Chain A is expressed in *gross* (ledger vs PSP gross),
chain B in *net* (settlement net vs bank). A settlement bridge (gross - fee = net) links the two without
adding them together. No figure ever sums across currencies.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import RULES_VERSION
from .db import utcnow
from .money import fmt_minor, to_plain

SLA_DAYS = 3  # an open exception older than this (vs as-of) is reported as overdue

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html"]))
_env.filters["money"] = lambda v, cur=None: fmt_minor(v, cur)

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @, tab, CR) in user-controllable text cells.
    Plain numbers (including negative amounts) are left untouched."""
    s = "" if value is None else str(value)
    if s and s[0] in "=+-@\t\r" and not _NUMERIC.match(s):
        return "'" + s
    return s


def age_days(anchor_date: str, as_of: date) -> int:
    return (as_of - date.fromisoformat(anchor_date)).days


def eod_data(conn: sqlite3.Connection, run_id: int | None = None) -> dict:
    run = (conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone() if run_id else
           conn.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone())
    if run is None:
        raise ValueError("no completed reconciliation run; run reconcile first")
    run = dict(run)
    summ = json.loads(run["summary_json"])
    as_of = date.fromisoformat(run["as_of"])
    rid = run["run_id"]

    imports = [dict(r) for r in conn.execute("SELECT * FROM imports ORDER BY import_id")]
    ranges = {}
    for src, col in (("ledger", "event_date"), ("psp", "created_date"), ("bank", "value_date")):
        r = conn.execute(f"SELECT COUNT(*) n, MIN({col}) lo, MAX({col}) hi FROM {src}_records").fetchone()
        ranges[src] = dict(r)

    # per-chain / per-currency status tables from record_status of this run
    tables: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for r in conn.execute("SELECT chain, record_type, currency, status, COUNT(*) n, SUM(amount_minor) amt "
                          "FROM record_status WHERE run_id=? GROUP BY 1,2,3,4 ORDER BY 1,2,3,4", (rid,)):
        tables[f"{r['chain']}:{r['record_type']}"][r["currency"]][r["status"]] = {"count": r["n"], "amount": r["amt"]}
    currencies = sorted({c for t in tables.values() for c in t})

    def side(key):
        out = []
        for cur in currencies:
            st = tables.get(key, {}).get(cur, {})
            if not st:
                continue
            tot_n = sum(v["count"] for v in st.values())
            tot_a = sum(v["amount"] for v in st.values())
            m = st.get("MATCHED", {"count": 0, "amount": 0})
            out.append({"currency": cur, "total_count": tot_n, "total_amount": tot_a,
                        "matched_count": m["count"], "matched_amount": m["amount"],
                        "unmatched_count": tot_n - m["count"], "unmatched_amount": tot_a - m["amount"],
                        "by_status": dict(sorted(st.items()))})
        return out

    chain_a = {"ledger": side("A:ledger"), "psp": side("A:psp")}
    chain_b = {"settlement": side("B:settlement"), "bank": side("B:bank"), "unsettled_psp": side("B:psp")}
    groups = [dict(r) for r in conn.execute(
        "SELECT chain, rule, currency, COUNT(*) n, SUM(left_total) amt FROM match_groups WHERE run_id=? "
        "GROUP BY 1,2,3 ORDER BY 1,2,3", (rid,))]

    cases = [dict(r) for r in conn.execute("SELECT * FROM cases ORDER BY case_id")]
    for c in cases:
        c["age_days"] = age_days(c["anchor_date"], as_of)
        c["overdue"] = c["status"] != "Resolved" and c["age_days"] > SLA_DAYS
    open_cases = [c for c in cases if c["status"] != "Resolved"]
    resolved_still_active = [c for c in cases if c["status"] == "Resolved" and c["engine_active"]]
    # Aggregate ABSOLUTE exposure: signed exposures of opposite sides (e.g. +50 / -50 for the two halves of an
    # account mismatch) must not net to zero in a management summary.
    open_by = defaultdict(lambda: {"count": 0, "amount": 0})
    for c in open_cases:
        x = open_by[(c["chain"], c["case_type"], c["currency"])]
        x["count"] += 1
        x["amount"] += abs(c["amount_minor"])
    dq = [dict(r) for r in conn.execute(
        "SELECT ri.*, i.file_name, i.sha256 FROM row_issues ri JOIN imports i USING(import_id) "
        "WHERE ri.kind='QUARANTINED' ORDER BY ri.issue_id")]
    dups = conn.execute("SELECT COUNT(*) FROM row_issues WHERE kind='DUPLICATE'").fetchone()[0]
    rejected = [i for i in imports if i["status"] in ("REJECTED", "DUPLICATE_FILE")]
    return {
        "generated_at": utcnow(), "run": run, "as_of": run["as_of"], "rules_version": run["rules_version"],
        "code_rules_version": RULES_VERSION, "summary": summ, "imports": imports, "ranges": ranges,
        "currencies": currencies, "chain_a": chain_a, "chain_b": chain_b, "groups": groups,
        "bridge": summ.get("settlement_bridge", {}), "open_cases": open_cases,
        "overdue": [c for c in open_cases if c["overdue"]], "resolved_still_active": resolved_still_active,
        "open_by": [{"chain": k[0], "case_type": k[1], "currency": k[2], **v} for k, v in sorted(open_by.items())],
        "dq": dq, "duplicate_rows": dups, "rejected_imports": rejected, "sla_days": SLA_DAYS,
        "excluded_after_as_of": summ.get("excluded_after_as_of", {}),
        "all_cases_count": len(cases),
    }


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow([csv_safe(x) for x in r])
    return buf.getvalue()


def summary_csv(d: dict) -> str:
    rows = [["section", "chain", "source", "currency", "metric", "count", "amount"]]
    rows.append(["run", "", "", "", f"run_id={d['run']['run_id']} as_of={d['as_of']} rules={d['rules_version']} "
                 f"elapsed_ms={d['run']['elapsed_ms']}", "", ""])
    for src, r in d["ranges"].items():
        rows.append(["input_range", "", src, "", f"{r['lo']}..{r['hi']}", r["n"], ""])
    for i in d["imports"]:
        rows.append(["import", "", i["source"], "", f"#{i['import_id']} {i['file_name']} sha256={i['sha256'][:12]} "
                     f"{i['status']}: {i['message']}", i["rows_loaded"], ""])
    for chain, block in (("A (gross)", d["chain_a"]), ("B (net)", d["chain_b"])):
        for src, lines in block.items():
            for ln in lines:
                cur = ln["currency"]
                rows.append(["chain", chain, src, cur, "total", ln["total_count"], to_plain(ln["total_amount"], cur)])
                rows.append(["chain", chain, src, cur, "matched", ln["matched_count"],
                             to_plain(ln["matched_amount"], cur)])
                for st, v in ln["by_status"].items():
                    if st != "MATCHED":
                        rows.append(["chain", chain, src, cur, f"status:{st}", v["count"], to_plain(v["amount"], cur)])
    for cur, b in sorted(d["bridge"].items()):
        rows.append(["settlement_bridge", "B", "psp", cur, "gross", b["batches"], to_plain(b["gross_minor"], cur)])
        rows.append(["settlement_bridge", "B", "psp", cur, "fee", b["batches"], to_plain(b["fee_minor"], cur)])
        rows.append(["settlement_bridge", "B", "psp", cur, "net", b["batches"], to_plain(b["net_minor"], cur)])
    for g in d["groups"]:
        rows.append(["match_groups", g["chain"], g["rule"], g["currency"], "groups", g["n"],
                     to_plain(g["amt"], g["currency"])])
    for o in d["open_by"]:
        rows.append(["open_exceptions", o["chain"], o["case_type"], o["currency"], "open (abs exposure)", o["count"],
                     to_plain(o["amount"], o["currency"])])
    rows.append(["overdue", "", "", "", f"open cases older than {d['sla_days']}d", len(d["overdue"]), ""])
    rows.append(["data_quality", "", "", "", "quarantined rows", len(d["dq"]), ""])
    rows.append(["data_quality", "", "", "", "duplicate rows skipped", d["duplicate_rows"], ""])
    rows.append(["data_quality", "", "", "", "rejected or duplicate files", len(d["rejected_imports"]), ""])
    for src, n in d["excluded_after_as_of"].items():
        rows.append(["as_of_cutoff", "", src, "", "excluded (dated after as-of)", n, ""])
    return _csv(rows)


def exceptions_csv(d: dict) -> str:
    rows = [["case_id", "case_key", "chain", "type", "status", "owner", "currency", "amount", "anchor_date",
             "age_days", "overdue", "disposition", "resolution_reason", "engine_active", "rule", "explanation"]]
    for c in d["open_cases"] + d["resolved_still_active"]:
        rows.append([c["case_id"], c["case_key"], c["chain"], c["case_type"], c["status"], c["owner"], c["currency"],
                     to_plain(c["amount_minor"], c["currency"]), c["anchor_date"], c["age_days"], c["overdue"],
                     c["disposition"], c["resolution_reason"], c["engine_active"], c["rule"], c["explanation"]])
    return _csv(rows)


def dq_csv(d: dict) -> str:
    rows = [["issue_id", "source", "file_name", "sha256", "source_row", "reason_code", "detail", "record_id",
             "conflicts_with", "raw"]]
    for q in d["dq"]:
        rows.append([q["issue_id"], q["source"], q["file_name"], q["sha256"], q["source_row"], q["reason_code"],
                     q["detail"], q["record_id"], q["conflicts_with"], q["raw_json"]])
    for i in d["rejected_imports"]:
        rows.append(["", i["source"], i["file_name"], i["sha256"], "", i["status"], i["message"], "", "", ""])
    return _csv(rows)


def render_html(d: dict) -> str:
    return _env.get_template("report.html").render(d=d)


def write_eod(conn: sqlite3.Connection, out_dir: str | Path, run_id: int | None = None) -> dict[str, Path]:
    d = eod_data(conn, run_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"eod_{d['as_of']}_run{d['run']['run_id']}"
    files = {
        "html": out / f"{stem}.html",
        "summary_csv": out / f"{stem}_summary.csv",
        "exceptions_csv": out / f"{stem}_exceptions.csv",
        "data_quality_csv": out / f"{stem}_data_quality.csv",
    }
    files["html"].write_text(render_html(d), encoding="utf-8")
    files["summary_csv"].write_text(summary_csv(d), encoding="utf-8")
    files["exceptions_csv"].write_text(exceptions_csv(d), encoding="utf-8")
    files["data_quality_csv"].write_text(dq_csv(d), encoding="utf-8")
    return files
