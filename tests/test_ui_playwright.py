"""Real-browser UI test (Playwright + Chromium). Skipped - and reported as skipped - when unavailable.

Drives the actual UI: load sample -> reconcile -> open an exception -> note -> resolve -> upload late bank file
-> re-run -> verify auto-clear. Screenshots go to docs/evidence/screenshots/ when UI_SCREENSHOTS=1.
"""
import os
import threading
from pathlib import Path

import pytest
from conftest import ROOT, SAMPLE

pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CANDIDATES = [os.environ.get("CHROMIUM_PATH", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"]
SHOTS = ROOT / "docs" / "evidence" / "screenshots"


@pytest.fixture
def server(tmp_path):
    from werkzeug.serving import make_server

    from recon.web import create_app
    app = create_app(tmp_path / "ui.db", tmp_path / "reports")
    srv = make_server("127.0.0.1", 0, app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _launch(p):
    for exe in CANDIDATES:
        if exe and Path(exe).exists():
            return p.chromium.launch(executable_path=exe)
    try:
        return p.chromium.launch()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no Chromium available: {e}")


def test_ui_happy_and_exception_path(server):
    shots = os.environ.get("UI_SCREENSHOTS") == "1"
    if shots:
        SHOTS.mkdir(parents=True, exist_ok=True)
    with pw.sync_playwright() as p:
        b = _launch(p)
        pg = b.new_page(viewport={"width": 1360, "height": 900})
        pg.goto(server + "/")
        pg.click("#btn-load-sample")
        assert "PARTIAL" in pg.inner_text("main")
        assert pg.input_value("#as_of") == "2026-07-10"
        pg.click("#btn-reconcile")
        assert "29 match groups, 20 exceptions" in pg.inner_text(".flash.ok")
        assert "Chain A" in pg.inner_text("#chain-a") and "Chain B" in pg.inner_text("#chain-b")
        if shots:
            pg.screenshot(path=str(SHOTS / "01_dashboard.png"), full_page=True)

        pg.goto(server + "/matches?chain=B")
        assert "B2S_REF_SPLIT" in pg.inner_text("main") and "B2M_REF_MERGED" in pg.inner_text("main")
        if shots:
            pg.screenshot(path=str(SHOTS / "02_matches_chain_b.png"), full_page=True)

        pg.goto(server + "/exceptions")
        assert pg.locator("#exceptions-table tr").count() == 21  # header + 20
        if shots:
            pg.screenshot(path=str(SHOTS / "03_exceptions.png"), full_page=True)
        pg.select_option("select[name=case_type]", "PARTIAL_RECEIPT")
        pg.click("text=Apply")
        pg.locator("#exceptions-table a").first.click()
        assert "PARTIAL_RECEIPT" in pg.inner_text("h1")
        assert "outstanding 800.00 EUR" in pg.inner_text("#explanation")
        ev = pg.inner_text("#evidence")
        assert "bank.csv" in ev and "B-014" in ev and "PSP-1019" in ev
        pg.fill("#note-text", "Asked PSP for remainder <b>trace</b>")
        pg.click("#btn-note")
        assert "Asked PSP for remainder <b>trace</b>" in pg.inner_text("#notes")   # shown as text, not HTML
        pg.click("#btn-in-review")
        assert "In review" in pg.inner_text("h1")
        case_url = pg.url
        if shots:
            pg.screenshot(path=str(SHOTS / "04_case_partial_receipt.png"), full_page=True)

        # resolve another case manually (browser-side validation needs disposition + reason)
        pg.goto(server + "/exceptions?case_type=UNEXPECTED_BANK_CREDIT")
        pg.locator("#exceptions-table a").first.click()
        pg.select_option("#disposition", "NO_ACTION_REQUIRED")
        pg.fill("#reason", "Bank interest credit, booked by treasury")
        pg.click("#btn-resolve")
        assert "Resolved" in pg.inner_text("h1")

        # late bank statement arrives: upload through the real form, then re-run as of the next day
        pg.goto(server + "/")
        pg.select_option("#source", "bank")
        pg.set_input_files("#file", str(SAMPLE / "late" / "bank_late.csv"))
        pg.click("text=Upload CSV")
        assert "bank_late.csv: LOADED" in pg.inner_text("main")
        pg.fill("#as_of", "2026-07-11")
        pg.click("#btn-reconcile")
        assert "3 auto-cleared" not in pg.inner_text(".flash.ok")
        assert "2 auto-cleared" in pg.inner_text(".flash.ok")
        pg.goto(case_url)
        h = pg.inner_text("h1")
        assert "Resolved" in h
        assert "AUTO_CLEARED" in pg.inner_text("main") and "CASE_AUTO_CLEARED" in pg.inner_text("#history")
        assert "Asked PSP for remainder" in pg.inner_text("#notes")
        if shots:
            pg.screenshot(path=str(SHOTS / "05_case_auto_cleared_after_late_data.png"), full_page=True)

        pg.goto(server + "/exceptions?status=Resolved")
        assert "still unmatched" in pg.inner_text("main")   # manual resolution is not a match
        pg.goto(server + "/reports")
        pg.click("#btn-generate")
        assert "eod_2026-07-11_run2.html" in pg.inner_text("#report-files")
        pg.goto(server + "/data")
        if shots:
            pg.screenshot(path=str(SHOTS / "06_data_quality.png"), full_page=True)
        pg.goto(server + "/audit")
        assert "CASE_RESOLVED" in pg.inner_text("main") and "RUN_COMPLETED" in pg.inner_text("main")
        b.close()
