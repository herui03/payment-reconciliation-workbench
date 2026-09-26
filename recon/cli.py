"""Command-line interface (automation-friendly; JSON output where useful)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import RULES_VERSION, cases
from .db import connect
from .ingest import SOURCES, import_file
from .reconcile import reconcile
from .reports import write_eod

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "data" / "sample"
SAMPLE_FILES = (("ledger", "ledger.csv"), ("psp", "psp.csv"), ("bank", "bank.csv"))
SAMPLE_AS_OF = "2026-07-10"


def load_sample(conn, actor: str = "demo.analyst") -> list[dict]:
    return [import_file(conn, src, SAMPLE_DIR / name, actor=actor).as_dict() for src, name in SAMPLE_FILES]


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m recon", description=f"Payment reconciliation workbench ({RULES_VERSION}). "
                                 "Synthetic/demo data only.")
    ap.add_argument("--db", default="work/recon.db", help="SQLite database path (default work/recon.db)")
    ap.add_argument("--actor", default="demo.analyst", help="demo actor label (not authentication)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the database schema")
    p = sub.add_parser("import", help="import a CSV")
    p.add_argument("source", choices=SOURCES)
    p.add_argument("file")
    p.add_argument("--strict", action="store_true", help="roll back the whole file if any row fails validation")
    sub.add_parser("load-sample", help="import the bundled synthetic sample (ledger, psp, bank)")
    p = sub.add_parser("reconcile", help="run both reconciliation chains")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p = sub.add_parser("report", help="write EOD report (HTML + CSV)")
    p.add_argument("--out", default="work/reports")
    p.add_argument("--run", type=int)
    p = sub.add_parser("cases", help="list cases")
    p.add_argument("--status")
    p.add_argument("--chain")
    p = sub.add_parser("case-note", help="append a note to a case")
    p.add_argument("case_id", type=int)
    p.add_argument("text")
    p = sub.add_parser("case-status", help="change case status")
    p.add_argument("case_id", type=int)
    p.add_argument("status", choices=cases.STATUSES)
    p.add_argument("--disposition", default="")
    p.add_argument("--reason", default="")
    p = sub.add_parser("case-assign", help="set case owner")
    p.add_argument("case_id", type=int)
    p.add_argument("owner")
    p = sub.add_parser("demo", help="fresh end-to-end demo into a NEW database: sample -> reconcile -> report")
    p.add_argument("--out", default="work/reports")
    p = sub.add_parser("serve", help="start the local web UI (127.0.0.1 only)")
    p.add_argument("--port", type=int, default=5000)
    a = ap.parse_args(argv)

    if a.cmd == "demo" and Path(a.db).exists():
        print(f"refusing: {a.db} already exists. The demo never deletes history; pass a new --db path.",
              file=sys.stderr)
        return 2
    if a.cmd == "serve":
        from .web import create_app
        app = create_app(a.db)
        print(f"Workbench on http://127.0.0.1:{a.port}  (db={a.db}; synthetic/demo data only)")
        app.run(host="127.0.0.1", port=a.port, debug=False)
        return 0

    conn = connect(a.db)
    try:
        if a.cmd == "init-db":
            print(f"schema ready at {a.db}")
        elif a.cmd == "import":
            r = import_file(conn, a.source, a.file, actor=a.actor, strict=a.strict)
            _print(r.as_dict())
            return 0 if r.status in ("LOADED", "PARTIAL", "DUPLICATE_FILE") else 1
        elif a.cmd == "load-sample":
            _print(load_sample(conn, a.actor))
        elif a.cmd == "reconcile":
            _print(reconcile(conn, date.fromisoformat(a.as_of), actor=a.actor))
        elif a.cmd == "report":
            _print({k: str(v) for k, v in write_eod(conn, a.out, a.run).items()})
        elif a.cmd == "cases":
            for c in cases.list_cases(conn, status=a.status, chain=a.chain):
                print(f"{c['case_id']:>4} {c['status']:<10} {c['chain']} {c['case_type']:<24} {c['currency']} "
                      f"{c['amount_minor']:>12} {c['anchor_id']:<28} owner={c['owner'] or '-'} notes={c['note_count']}")
        elif a.cmd == "case-note":
            print(cases.add_note(conn, a.case_id, a.text, a.actor))
        elif a.cmd == "case-status":
            c = cases.set_status(conn, a.case_id, a.status, a.actor, a.disposition, a.reason)
            print(f"case {c['case_id']} -> {c['status']} {c['disposition']}")
        elif a.cmd == "case-assign":
            c = cases.assign(conn, a.case_id, a.owner, a.actor)
            print(f"case {c['case_id']} owner={c['owner']}")
        elif a.cmd == "demo":
            imps = load_sample(conn, a.actor)
            for i in imps:
                print(f"import {i['source']:<6} {i['file_name']:<12} {i['status']:<8} {i['message']}")
            run = reconcile(conn, date.fromisoformat(SAMPLE_AS_OF), actor=a.actor)
            s = run["summary"]
            print(f"run {run['run_id']} as-of {SAMPLE_AS_OF}: {s['counts']['groups']} match groups, "
                  f"{s['counts']['exceptions']} exceptions, {run['elapsed_ms']} ms")
            files = write_eod(conn, a.out)
            for k, v in files.items():
                print(f"{k}: {v}")
    except cases.WorkflowError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0
