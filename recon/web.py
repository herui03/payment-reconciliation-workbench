"""Local single-user web UI (Flask + server-rendered Jinja, autoescaped). Binds to 127.0.0.1 via the CLI.

Not a multi-user system: the "actor" is a free-text demo label kept in the browser session, not authentication.
There is no CSRF protection because the app is only meant to run on localhost for demos.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)

from . import RULES_VERSION, cases
from .db import connect
from .ingest import SOURCES, import_bytes
from .money import fmt_minor
from .reconcile import latest_run, reconcile
from .reports import SLA_DAYS, age_days, eod_data, write_eod

DEFAULT_ACTOR = "demo.analyst"


def create_app(db_path: str | Path, reports_dir: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.urandom(24), MAX_CONTENT_LENGTH=25 * 1024 * 1024,
                      DB_PATH=str(db_path),
                      REPORTS_DIR=str(reports_dir or Path(db_path).resolve().parent / "reports"))
    app.jinja_env.filters["money"] = lambda v, cur=None: fmt_minor(v, cur)
    app.jinja_env.filters["fromjson"] = json.loads
    connect(db_path).close()  # ensure schema

    def db():
        if "db" not in g:
            g.db = connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    def actor() -> str:
        return session.get("actor", DEFAULT_ACTOR)

    @app.context_processor
    def _ctx():
        return {"actor": actor(), "rules_version": RULES_VERSION, "sla_days": SLA_DAYS}

    def default_as_of() -> str:
        run = latest_run(db())
        if run:
            return run["as_of"]
        if session.get("suggest_as_of"):
            return session["suggest_as_of"]
        r = db().execute("SELECT MAX(d) FROM (SELECT MAX(event_date) d FROM ledger_records UNION ALL "
                         "SELECT MAX(created_date) FROM psp_records UNION ALL SELECT MAX(value_date) "
                         "FROM bank_records WHERE value_date <= date('now'))").fetchone()[0]
        return r or date.today().isoformat()

    # ------------------------------------------------------------ pages
    @app.get("/")
    def dashboard():
        run = latest_run(db())
        d = eod_data(db(), run["run_id"]) if run else None
        imports = [dict(r) for r in db().execute("SELECT * FROM imports ORDER BY import_id DESC LIMIT 12")]
        counts = {s: db().execute(f"SELECT COUNT(*) FROM {s}_records").fetchone()[0] for s in SOURCES}
        return render_template("dashboard.html", run=run, d=d, imports=imports, counts=counts,
                               default_as_of=default_as_of(), sources=SOURCES)

    @app.post("/actor")
    def set_actor():
        a = (request.form.get("actor") or "").strip()[:60]
        if not a or a.lower() == "system":
            flash("Actor label must be non-empty and cannot be 'system'.", "error")
        else:
            session["actor"] = a
            flash(f"Demo actor label set to '{a}' (not authentication).", "info")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/load-sample")
    def load_sample_route():
        from .cli import SAMPLE_AS_OF, load_sample
        for r in load_sample(db(), actor()):
            _flash_import(r)
        session["suggest_as_of"] = SAMPLE_AS_OF
        flash(f"Sample scenarios are designed for as-of {SAMPLE_AS_OF} (pre-filled below).", "info")
        return redirect(url_for("dashboard"))

    @app.post("/upload")
    def upload():
        src = request.form.get("source")
        f = request.files.get("file")
        if src not in SOURCES or not f or not f.filename:
            flash("Choose a source type and a CSV file.", "error")
            return redirect(url_for("dashboard"))
        r = import_bytes(db(), src, f.read(), Path(f.filename).name, actor=actor(),
                         strict=bool(request.form.get("strict")))
        _flash_import(r.as_dict())
        return redirect(url_for("dashboard"))

    def _flash_import(r: dict):
        cat = {"LOADED": "ok", "PARTIAL": "warn", "DUPLICATE_FILE": "warn"}.get(r["status"], "error")
        flash(f"{r['source']} · {r['file_name']}: {r['status']} — {r['message']}", cat)

    @app.post("/reconcile")
    def run_reconcile():
        try:
            as_of = date.fromisoformat(request.form.get("as_of", ""))
        except ValueError:
            flash("As-of date must be YYYY-MM-DD.", "error")
            return redirect(url_for("dashboard"))
        r = reconcile(db(), as_of, actor=actor())
        s = r["summary"]
        ch = s["case_changes"]
        flash(f"Run #{r['run_id']} as-of {as_of}: {s['counts']['groups']} match groups, "
              f"{s['counts']['exceptions']} exceptions reported. Cases: {ch['opened']} opened, "
              f"{ch['auto_cleared']} auto-cleared, {ch['reopened']} re-opened, {ch['updated']} updated.", "ok")
        if not s["case_sync"]:
            flash(s["case_sync_note"] + " Re-run with the current as-of to return to the working view.", "warn")
        return redirect(url_for("dashboard"))

    @app.get("/exceptions")
    def exceptions():
        run = latest_run(db())
        as_of_s = request.args.get("as_of") or (run["as_of"] if run else date.today().isoformat())
        try:
            as_of = date.fromisoformat(as_of_s)
        except ValueError:
            as_of = date.today()
        view = request.args.get("status") or "unresolved"
        f = {k: request.args.get(k) or None for k in ("chain", "case_type", "currency")}
        rows = cases.list_cases(db(), status=view if view in cases.STATUSES else None, **f)
        if view == "unresolved":
            rows = [c for c in rows if c["status"] != "Resolved"]
        for c in rows:
            c["age_days"] = age_days(c["anchor_date"], as_of)
            c["overdue"] = c["status"] != "Resolved" and c["age_days"] > SLA_DAYS
        types = [r[0] for r in db().execute("SELECT DISTINCT case_type FROM cases ORDER BY 1")]
        curs = [r[0] for r in db().execute("SELECT DISTINCT currency FROM cases ORDER BY 1")]
        return render_template("exceptions.html", rows=rows, f=f, as_of=as_of.isoformat(), types=types,
                               curs=curs, view=view, statuses=cases.STATUSES)

    @app.get("/cases/<int:case_id>")
    def case_detail(case_id):
        c = db().execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not c:
            abort(404)
        c = dict(c)
        run = latest_run(db())
        as_of = date.fromisoformat(request.args.get("as_of") or (run["as_of"] if run else date.today().isoformat()))
        c["age_days"] = age_days(c["anchor_date"], as_of)
        ev = cases.evidence_rows(db(), json.loads(c["evidence_json"]))
        notes = [dict(r) for r in db().execute("SELECT * FROM case_notes WHERE case_id=? ORDER BY note_id",
                                               (case_id,))]
        events = [dict(r) for r in db().execute(
            "SELECT * FROM audit_events WHERE entity_type='case' AND entity_id=? ORDER BY event_id", (str(case_id),))]
        return render_template("case.html", c=c, ev=ev, notes=notes, events=events, as_of=as_of.isoformat(),
                               dispositions=cases.DISPOSITIONS, statuses=cases.STATUSES)

    @app.post("/cases/<int:case_id>/status")
    def case_status(case_id):
        try:
            cases.set_status(db(), case_id, request.form.get("status", ""), actor(),
                             request.form.get("disposition", ""), request.form.get("reason", ""))
            flash(f"Case {case_id} → {request.form.get('status')}", "ok")
        except cases.WorkflowError as e:
            flash(f"Not changed: {e}", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    @app.post("/cases/<int:case_id>/assign")
    def case_assign(case_id):
        try:
            cases.assign(db(), case_id, request.form.get("owner", ""), actor())
            flash("Owner updated.", "ok")
        except cases.WorkflowError as e:
            flash(f"Not changed: {e}", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    @app.post("/cases/<int:case_id>/note")
    def case_note(case_id):
        try:
            cases.add_note(db(), case_id, request.form.get("text", ""), actor())
            flash("Note added.", "ok")
        except cases.WorkflowError as e:
            flash(f"Not added: {e}", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    @app.get("/matches")
    def matches():
        run = latest_run(db())
        chain = request.args.get("chain") or "B"
        groups = []
        if run:
            groups = [dict(r) for r in db().execute(
                "SELECT * FROM match_groups WHERE run_id=? AND chain=? ORDER BY group_id", (run["run_id"], chain))]
            mem = {}
            for m in db().execute("SELECT * FROM match_members WHERE run_id=? AND chain=? ORDER BY side, record_id",
                                  (run["run_id"], chain)):
                mem.setdefault(m["group_id"], []).append(dict(m))
            for gr in groups:
                gr["members"] = mem.get(gr["group_id"], [])
        return render_template("matches.html", run=run, groups=groups, chain=chain)

    @app.get("/data")
    def data_quality():
        imports = [dict(r) for r in db().execute("SELECT * FROM imports ORDER BY import_id")]
        issues = [dict(r) for r in db().execute(
            "SELECT ri.*, i.file_name, i.sha256 FROM row_issues ri JOIN imports i USING(import_id) "
            "ORDER BY ri.issue_id")]
        return render_template("data.html", imports=imports, issues=issues)

    @app.get("/reports")
    def reports():
        rd = Path(app.config["REPORTS_DIR"])
        files = sorted((p.name for p in rd.glob("eod_*")), reverse=True) if rd.exists() else []
        return render_template("reports.html", files=files, run=latest_run(db()))

    @app.post("/reports/generate")
    def reports_generate():
        try:
            out = write_eod(db(), app.config["REPORTS_DIR"])
            flash("EOD report written: " + ", ".join(p.name for p in out.values()), "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("reports"))

    @app.get("/reports/file/<path:name>")
    def reports_file(name):
        if not name.startswith("eod_") or "/" in name or ".." in name:
            abort(404)
        return send_from_directory(app.config["REPORTS_DIR"], name, as_attachment=name.endswith(".csv"))

    @app.get("/audit")
    def audit_log():
        rows = [dict(r) for r in db().execute("SELECT * FROM audit_events ORDER BY event_id DESC LIMIT 500")]
        return render_template("audit.html", rows=rows)

    @app.get("/api/summary")
    def api_summary():
        run = latest_run(db())
        if not run:
            return jsonify({"run": None})
        d = eod_data(db(), run["run_id"])
        return jsonify({"run_id": run["run_id"], "as_of": run["as_of"], "rules_version": run["rules_version"],
                        "chain_a": d["chain_a"], "chain_b": d["chain_b"], "open_cases": len(d["open_cases"]),
                        "case_basis": d["case_basis"], "case_sync": d["case_sync"],
                        "workflow_open_cases": db().execute(
                            "SELECT COUNT(*) FROM cases WHERE status != 'Resolved'").fetchone()[0],
                        "overdue": len(d["overdue"]), "bridge": d["bridge"]})

    return app
