"""SQLite schema and connection helpers.

Append-only tables (source records, row issues, match results, case notes, audit events) are protected by
triggers that abort UPDATE/DELETE. This is an application-level guard for a local demo, not tamper-proofing:
anyone with the database file can still alter it outside the app.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    import_id       INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    imported_at     TEXT NOT NULL,
    actor           TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('LOADED','PARTIAL','REJECTED','DUPLICATE_FILE')),
    rows_read       INTEGER NOT NULL DEFAULT 0,
    rows_loaded     INTEGER NOT NULL DEFAULT 0,
    rows_duplicate  INTEGER NOT NULL DEFAULT 0,
    rows_quarantined INTEGER NOT NULL DEFAULT 0,
    loaded_totals_json TEXT NOT NULL DEFAULT '{}',  -- control totals of loaded rows per currency
    message         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ledger_records (
    record_id        TEXT PRIMARY KEY,           -- ledger_entry_id
    event_type       TEXT NOT NULL,              -- SALE | REFUND
    business_ref     TEXT NOT NULL,
    original_ref     TEXT NOT NULL DEFAULT '',
    merchant_account TEXT NOT NULL,
    currency         TEXT NOT NULL,
    gross_minor      INTEGER NOT NULL,
    event_date       TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL,
    import_id        INTEGER NOT NULL REFERENCES imports(import_id),
    source_row       INTEGER NOT NULL
);
-- business_ref is unique per merchant_account (not globally); record_id is unique per source.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_bref ON ledger_records(merchant_account, business_ref);

CREATE TABLE IF NOT EXISTS psp_records (
    record_id        TEXT PRIMARY KEY,           -- psp_txn_id
    type             TEXT NOT NULL,              -- charge | refund | fee
    business_ref     TEXT NOT NULL DEFAULT '',
    merchant_account TEXT NOT NULL,
    currency         TEXT NOT NULL,
    gross_minor      INTEGER NOT NULL,
    fee_minor        INTEGER NOT NULL,
    net_minor        INTEGER NOT NULL,
    created_date     TEXT NOT NULL,
    available_on     TEXT NOT NULL,
    settlement_batch_id TEXT NOT NULL DEFAULT '',
    settlement_date  TEXT NOT NULL DEFAULT '',
    bank_account     TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL,
    import_id        INTEGER NOT NULL REFERENCES imports(import_id),
    source_row       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_psp_bref ON psp_records(merchant_account, business_ref);
CREATE INDEX IF NOT EXISTS ix_psp_batch ON psp_records(settlement_batch_id);

CREATE TABLE IF NOT EXISTS bank_records (
    record_id        TEXT PRIMARY KEY,           -- bank_txn_id
    bank_account     TEXT NOT NULL,
    currency         TEXT NOT NULL,
    amount_minor     INTEGER NOT NULL,
    value_date       TEXT NOT NULL,
    reference        TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL,
    import_id        INTEGER NOT NULL REFERENCES imports(import_id),
    source_row       INTEGER NOT NULL
);

-- Rows that were read but not loaded: quarantined (error / conflict) or skipped duplicates.
CREATE TABLE IF NOT EXISTS row_issues (
    issue_id        INTEGER PRIMARY KEY,
    import_id       INTEGER NOT NULL REFERENCES imports(import_id),
    source          TEXT NOT NULL,
    source_row      INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('QUARANTINED','DUPLICATE')),
    reason_code     TEXT NOT NULL,
    detail          TEXT NOT NULL,
    record_id       TEXT NOT NULL DEFAULT '',
    business_key    TEXT NOT NULL DEFAULT '',
    conflicts_with  TEXT NOT NULL DEFAULT '',   -- record_id of the already-loaded record in conflict
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    as_of           TEXT NOT NULL,
    rules_version   TEXT NOT NULL,
    actor           TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL DEFAULT '',
    elapsed_ms      INTEGER,
    summary_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS match_groups (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    group_id    TEXT NOT NULL,
    chain       TEXT NOT NULL CHECK (chain IN ('A','B')),
    rule        TEXT NOT NULL,
    currency    TEXT NOT NULL,
    left_total  INTEGER NOT NULL,
    right_total INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    PRIMARY KEY (run_id, group_id),
    CHECK (left_total = right_total)            -- amount conservation, enforced by the database too
);

CREATE TABLE IF NOT EXISTS match_members (
    run_id      INTEGER NOT NULL,
    group_id    TEXT NOT NULL,
    chain       TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('L','R')),
    record_type TEXT NOT NULL,                  -- ledger | psp | settlement | bank
    record_id   TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    UNIQUE (run_id, chain, record_type, record_id)   -- a record can be consumed once per chain per run
);

CREATE TABLE IF NOT EXISTS record_status (
    run_id      INTEGER NOT NULL,
    chain       TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    rule        TEXT NOT NULL DEFAULT '',
    group_id    TEXT NOT NULL DEFAULT '',
    case_key    TEXT NOT NULL DEFAULT '',
    currency    TEXT NOT NULL DEFAULT '',
    amount_minor INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, chain, record_type, record_id)
);

CREATE TABLE IF NOT EXISTS cases (
    case_id         INTEGER PRIMARY KEY,
    case_key        TEXT NOT NULL UNIQUE,
    chain           TEXT NOT NULL,
    case_type       TEXT NOT NULL,
    rule            TEXT NOT NULL,
    anchor_type     TEXT NOT NULL,
    anchor_id       TEXT NOT NULL,
    currency        TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,      -- engine-computed exposure; never edited by users
    anchor_date     TEXT NOT NULL,
    explanation     TEXT NOT NULL,
    evidence_json   TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL CHECK (status IN ('Open','In review','Resolved')),
    disposition     TEXT NOT NULL DEFAULT '',
    resolution_reason TEXT NOT NULL DEFAULT '',
    engine_active   INTEGER NOT NULL DEFAULT 1,   -- 1 = engine still reports this issue in the latest run
    first_seen_run  INTEGER NOT NULL,
    last_seen_run   INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_notes (
    note_id     INTEGER PRIMARY KEY,
    case_id     INTEGER NOT NULL REFERENCES cases(case_id),
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    text        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id    INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    run_id      INTEGER
);
"""

APPEND_ONLY_TABLES = (
    "ledger_records", "psp_records", "bank_records", "row_issues",
    "match_groups", "match_members", "record_status", "case_notes", "audit_events",
)


def _triggers() -> str:
    out = []
    for t in APPEND_ONLY_TABLES:
        for op in ("UPDATE", "DELETE"):
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(out)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit BEGIN/COMMIT via tx()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.executescript(_triggers())
    return conn


class tx:
    """Explicit transaction context: BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any exception."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def audit(conn: sqlite3.Connection, actor: str, entity_type: str, entity_id: str, action: str,
          detail_json: str = "{}", run_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events(ts, actor, entity_type, entity_id, action, detail_json, run_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (utcnow(), actor, entity_type, str(entity_id), action, detail_json, run_id),
    )
