import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recon.db import connect  # noqa: E402

SAMPLE = ROOT / "data" / "sample"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def chain_a_status(conn, run_id):
    """{'merchant|ref': status} for chain A from persisted record_status (ledger side preferred)."""
    out = {}
    for src in ("psp", "ledger"):
        for r in conn.execute(
            f"SELECT r.merchant_account m, r.business_ref b, s.status FROM record_status s "
            f"JOIN {src}_records r ON r.record_id=s.record_id "
            f"WHERE s.run_id=? AND s.chain='A' AND s.record_type=?", (run_id, src)):
            if r["b"]:
                out[f"{r['m']}|{r['b']}"] = r["status"]
    return out


def settlement_view(conn, run_id):
    out = {}
    for s in conn.execute("SELECT * FROM record_status WHERE run_id=? AND chain='B' AND record_type='settlement'",
                          (run_id,)):
        lines = []
        if s["group_id"]:
            lines = sorted(r[0] for r in conn.execute(
                "SELECT record_id FROM match_members WHERE run_id=? AND group_id=? AND record_type='bank'",
                (run_id, s["group_id"])))
        out[s["record_id"]] = [s["status"], s["rule"] if s["status"] == "MATCHED" else "", lines]
    return out


def status_map(conn, run_id, chain, rtype):
    return {r["record_id"]: r["status"] for r in conn.execute(
        "SELECT * FROM record_status WHERE run_id=? AND chain=? AND record_type=?", (run_id, chain, rtype))}
