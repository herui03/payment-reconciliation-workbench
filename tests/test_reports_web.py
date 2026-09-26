"""EOD report content, CSV formula-injection guard, HTML escaping, and Flask API/UI smoke tests."""
import csv
import io
from datetime import date

import pytest
from conftest import SAMPLE
from helpers import BANK_H, LEDGER_H, bank, csv_bytes, led

from recon.cases import add_note
from recon.ingest import import_bytes, import_file
from recon.reconcile import reconcile
from recon.reports import csv_safe, dq_csv, eod_data, exceptions_csv, render_html, summary_csv, write_eod
from recon.web import create_app


@pytest.mark.parametrize("v,out", [("=1+1", "'=1+1"), ("+SUM(A1:A2)", "'+SUM(A1:A2)"), ("-2+3", "'-2+3"),
                                   ("@cmd", "'@cmd"), ("\tx", "'\tx"), ("-12.50", "-12.50"), ("100", "100"),
                                   ("normal", "normal"), ("", "")])
def test_csv_safe(v, out):
    assert csv_safe(v) == out


def _load(conn):
    for src, n in (("ledger", "ledger.csv"), ("psp", "psp.csv"), ("bank", "bank.csv")):
        import_file(conn, src, SAMPLE / n)
    return reconcile(conn, date(2026, 7, 10))["run_id"]


def test_injection_text_is_neutralised_in_csv_and_escaped_in_html(conn):
    import_bytes(conn, "bank", csv_bytes(BANK_H, [bank(1, "5.00", "2026-07-01", ref="=HYPERLINK(\"x\")") +
                                                  "<script>alert(1)</script>"]), "b.csv")
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "+SUM(A1)", "7.00", "2026-07-01")]), "l.csv")
    import_bytes(conn, "bank", b"bank_txn_id,bank_account,currency,amount,value_date,reference\n"
                               b"=cmd(),BK1,SGD,oops,2026-07-01,@x\n", "evil.csv")
    reconcile(conn, date(2026, 7, 10))
    d = eod_data(conn)
    seen_guarded = 0
    for text in (exceptions_csv(d), summary_csv(d), dq_csv(d)):
        for row in csv.reader(io.StringIO(text)):
            for cell in row:
                seen_guarded += cell.startswith("'=") or cell.startswith("'@")
                dangerous = cell[:1] in ("=", "+", "@", "\t", "\r") or (
                    cell[:1] == "-" and not cell.lstrip("-").replace(".", "", 1).isdigit())
                assert not dangerous, cell
    assert seen_guarded >= 1
    html = render_html(d)
    assert "=HYPERLINK" in html and "<script>" not in html


def test_eod_report_per_currency_and_chains_separate(conn, tmp_path):
    _load(conn)
    files = write_eod(conn, tmp_path)
    rows = list(csv.DictReader(io.StringIO(files["summary_csv"].read_text())))
    money_rows = [r for r in rows if r["amount"]]
    assert money_rows and all(r["currency"] in ("SGD", "USD", "EUR") for r in money_rows)  # never cross-currency
    chains = {r["chain"] for r in rows if r["section"] == "chain"}
    assert chains == {"A (gross)", "B (net)"}
    bridge = {(r["currency"], r["metric"]): r["amount"] for r in rows if r["section"] == "settlement_bridge"}
    assert bridge[("SGD", "gross")] == "7550.00" and bridge[("SGD", "fee")] == "178.00" and bridge[("SGD", "net")] == "7372.00"
    html = files["html"].read_text()
    for needle in ("Input data scope", "Chain A", "Chain B", "Unresolved exceptions", "Data quality", "2026.09-v1",
                   "PARTIAL", "overdue"):
        assert needle in html


def test_notes_are_escaped_in_case_page(tmp_path):
    app = create_app(tmp_path / "w.db", tmp_path / "rep")
    c = app.test_client()
    c.post("/load-sample")
    c.post("/reconcile", data={"as_of": "2026-07-10"})
    c.post("/cases/1/note", data={"text": "<img src=x onerror=alert(1)> =1+1"})
    page = c.get("/cases/1").get_data(as_text=True)
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "<img src=x" not in page


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "w.db", tmp_path / "rep")
    return app.test_client()


def test_web_end_to_end_smoke(client):
    assert client.get("/api/summary").get_json() == {"run": None}
    r = client.post("/load-sample", follow_redirects=True).get_data(as_text=True)
    assert "PARTIAL" in r and "quarantined 4" in r
    r = client.post("/load-sample", follow_redirects=True).get_data(as_text=True)
    assert "DUPLICATE_FILE" in r
    r = client.post("/reconcile", data={"as_of": "2026-07-10"}, follow_redirects=True).get_data(as_text=True)
    assert "29 match groups, 20 exceptions" in r
    s = client.get("/api/summary").get_json()
    assert s["as_of"] == "2026-07-10" and s["open_cases"] == 20
    assert {x["currency"] for x in s["chain_a"]["ledger"]} == {"SGD", "USD", "EUR"}
    assert "PARTIAL_RECEIPT" in client.get("/exceptions").get_data(as_text=True)
    for path in ("/matches", "/matches?chain=A", "/data", "/audit", "/reports", "/exceptions?status=all&chain=B"):
        assert client.get(path).status_code == 200
    assert client.post("/reconcile", data={"as_of": "10/07/2026"}, follow_redirects=True).status_code == 200


def test_web_resolve_validation_and_no_edit_endpoints(client):
    client.post("/load-sample")
    client.post("/reconcile", data={"as_of": "2026-07-10"})
    r = client.post("/cases/1/status", data={"status": "Resolved", "reason": "x"}, follow_redirects=True)
    assert "Not changed" in r.get_data(as_text=True)
    r = client.post("/cases/1/status", data={"status": "Resolved", "disposition": "NO_ACTION_REQUIRED",
                                             "reason": "reviewed with PSP statement"}, follow_redirects=True)
    assert "Case 1 → Resolved" in r.get_data(as_text=True)
    # no route exists to edit or delete history
    rules = {str(r) for r in client.application.url_map.iter_rules()}
    assert not any(k in x for x in rules for k in ("delete", "edit", "audit/"))
    assert client.post("/audit").status_code == 405


def test_web_upload_partial_and_rejected_are_not_shown_as_success(client):
    from io import BytesIO
    data = csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-01"), led(2, "O2", "20.00", "2026-07-01"),
                                led(3, "O3", "0.001", "2026-07-01")])
    r = client.post("/upload", data={"source": "ledger", "file": (BytesIO(data), "three.csv")},
                    content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "three.csv: PARTIAL" in r and "loaded 2" in r and "quarantined 1" in r
    r = client.post("/upload", data={"source": "bank", "file": (BytesIO(b"a,b\n1,2\n"), "bad.csv")},
                    content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "bad.csv: REJECTED" in r and "missing required column" in r
    r = client.post("/upload", data={"source": "ledger", "strict": "1", "file": (BytesIO(data.replace(b"O1", b"Z1")), "s.csv")},
                    content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "s.csv: REJECTED" in r and "rolled back" in r


# ---------------------------------------------------------------- reviewer finding R3 (defect D-10)
def test_historical_report_is_run_consistent(conn):
    from helpers import PSP_H, psp

    from recon.cases import set_status
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "REF1", "1000.00", "2026-07-01")]), "l.csv")
    run1 = reconcile(conn, date(2026, 7, 10))["run_id"]
    d1_before = eod_data(conn, run1)
    assert d1_before["live_cases"] and [c["case_key"] for c in d1_before["open_cases"]] == ["A|M1|REF1"]
    import_bytes(conn, "psp", csv_bytes(PSP_H, [psp(1, "REF1", "1000.00", "20.00", "2026-07-01", batch="S1",
                                                    sdate="2026-07-03", acct="BANK1")]), "p.csv")
    run2 = reconcile(conn, date(2026, 7, 10))["run_id"]
    c_stl = conn.execute("SELECT case_id FROM cases WHERE case_key='B|STL|S1'").fetchone()[0]
    set_status(conn, c_stl, "In review", "a1")                         # later manual action
    d1 = eod_data(conn, run1)
    assert not d1["live_cases"] and "Snapshot of run #1" in d1["case_basis"]
    assert [(c["case_key"], c["status"]) for c in d1["open_cases"]] == [("A|M1|REF1", "Open")]
    assert [i["file_name"] for i in d1["imports"]] == ["l.csv"] and d1["later_imports"] == 1
    assert d1["ranges"]["psp"]["n"] == 0
    assert "Historical run report (snapshot)" in render_html(d1)
    d2 = eod_data(conn, run2)
    assert d2["live_cases"] and [(c["case_key"], c["status"]) for c in d2["open_cases"]] == [("B|STL|S1", "In review")]


def test_web_backward_run_warns_and_keeps_cases(client):
    client.post("/load-sample")
    client.post("/reconcile", data={"as_of": "2026-07-10"})
    r = client.post("/reconcile", data={"as_of": "2026-07-01"}, follow_redirects=True).get_data(as_text=True)
    assert "read-only snapshot, cases were not changed" in r and "snapshot-banner" in r
    j = client.get("/api/summary").get_json()
    assert j["case_sync"] is False and j["open_cases"] == 1          # what the 07-01 snapshot run reported
    assert j["workflow_open_cases"] == 20                             # live cases untouched
