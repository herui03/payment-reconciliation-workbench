"""CSV ingestion with validation, quarantine, idempotency and conflict detection.

Guarantees
- File-level: identical bytes (any file name) are imported once; later attempts are logged as DUPLICATE_FILE.
- Structural errors (missing columns, empty/undecodable file) reject the whole file; nothing is loaded.
- Row-level errors are quarantined with a reason code; with ``strict=True`` any row error rolls back the file.
- Rows whose record id already exists with identical content are skipped as DUPLICATE (overlapping re-sends).
- Rows that collide on record id or business key with *different* content are quarantined as conflicts and the
  already-loaded record is blocked from auto-matching (see ``blocked_keys``).
- Invariant: rows_read == rows_loaded + rows_duplicate + rows_quarantined.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .db import audit, tx, utcnow
from .money import SUPPORTED_CURRENCIES, AmountError, parse_amount

SOURCES = ("ledger", "psp", "bank")

REQUIRED_COLUMNS = {
    "ledger": ["ledger_entry_id", "event_type", "business_ref", "original_ref", "merchant_account",
               "currency", "gross_amount", "event_date"],
    "psp": ["psp_txn_id", "type", "business_ref", "merchant_account", "currency", "gross_amount",
            "fee_amount", "net_amount", "created_date", "available_on", "settlement_batch_id",
            "settlement_date", "bank_account"],
    "bank": ["bank_txn_id", "bank_account", "currency", "amount", "value_date", "reference"],
}
OPTIONAL_COLUMNS = {"ledger": ["description"], "psp": [], "bank": ["description"]}
TABLE = {"ledger": "ledger_records", "psp": "psp_records", "bank": "bank_records"}


# Identifier syntax (record ids, business refs, merchant / bank accounts, batch ids): 1-64 chars, letters, digits
# and . _ : # - only. Reserved characters such as | , ; / and whitespace are rejected so that identifiers can
# never collide inside composite keys (e.g. case keys "A|merchant|ref") or be split by bank-reference tokenising.
IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#-]{0,63}")


class RowError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass
class ImportResult:
    import_id: int
    source: str
    file_name: str
    sha256: str
    status: str
    rows_read: int = 0
    rows_loaded: int = 0
    rows_duplicate: int = 0
    rows_quarantined: int = 0
    message: str = ""
    issues: list[dict] = field(default_factory=list)
    loaded_totals: dict = field(default_factory=dict)   # currency -> {rows, amount_minor} (psp: net)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["issues"] = self.issues[:50]
        return d


# ---------------------------------------------------------------- row parsing

def _req(row: dict, col: str) -> str:
    v = (row.get(col) or "").strip()
    if not v:
        raise RowError("MISSING_VALUE", f"required value '{col}' is blank")
    return v


def _ident(row: dict, col: str, required: bool = True) -> str:
    v = (row.get(col) or "").strip()
    if not v:
        if required:
            raise RowError("MISSING_VALUE", f"required value '{col}' is blank")
        return ""
    if not IDENT_RE.fullmatch(v):
        raise RowError("BAD_IDENTIFIER", f"'{col}'={v[:40]!r} must be 1-64 chars of letters, digits and . _ : # - "
                                         f"(no spaces or | , ; /)")
    return v


def _date(row: dict, col: str, required: bool = True) -> str:
    v = (row.get(col) or "").strip()
    if not v:
        if required:
            raise RowError("MISSING_VALUE", f"required date '{col}' is blank")
        return ""
    try:
        if len(v) != 10:
            raise ValueError
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise RowError("BAD_DATE", f"'{col}'={v!r} is not a valid YYYY-MM-DD date") from None


def _currency(row: dict) -> str:
    c = _req(row, "currency").upper()
    if c not in SUPPORTED_CURRENCIES:
        raise RowError("UNSUPPORTED_CURRENCY", f"currency {c!r} not in {', '.join(SUPPORTED_CURRENCIES)}")
    return c


def _amount(row: dict, col: str, currency: str) -> int:
    try:
        return parse_amount(row.get(col) or "", currency)
    except AmountError as exc:
        raise RowError("BAD_AMOUNT", f"'{col}': {exc}") from None


def parse_ledger(row: dict) -> dict:
    cur = _currency(row)
    et = _req(row, "event_type").upper()
    if et not in ("SALE", "REFUND"):
        raise RowError("BAD_VALUE", f"event_type {et!r} must be SALE or REFUND")
    gross = _amount(row, "gross_amount", cur)
    if et == "SALE" and gross <= 0:
        raise RowError("SIGN_RULE", "SALE gross_amount must be > 0")
    if et == "REFUND" and gross >= 0:
        raise RowError("SIGN_RULE", "REFUND gross_amount must be < 0")
    original = _ident(row, "original_ref", required=False)
    if et == "REFUND" and not original:
        raise RowError("MISSING_VALUE", "REFUND requires original_ref")
    return {
        "record_id": _ident(row, "ledger_entry_id"), "event_type": et, "business_ref": _ident(row, "business_ref"),
        "original_ref": original, "merchant_account": _ident(row, "merchant_account"), "currency": cur,
        "gross_minor": gross, "event_date": _date(row, "event_date"),
        "description": (row.get("description") or "").strip(),
    }


def parse_psp(row: dict) -> dict:
    cur = _currency(row)
    typ = _req(row, "type").lower()
    if typ not in ("charge", "refund", "fee"):
        raise RowError("BAD_VALUE", f"type {typ!r} must be charge, refund or fee")
    gross = _amount(row, "gross_amount", cur)
    fee = _amount(row, "fee_amount", cur)
    net = _amount(row, "net_amount", cur)
    if net != gross - fee:
        raise RowError("NET_MISMATCH", f"net_amount {net} != gross_amount {gross} - fee_amount {fee} (minor units)")
    if typ == "charge" and gross <= 0:
        raise RowError("SIGN_RULE", "charge gross_amount must be > 0")
    if typ == "refund" and gross >= 0:
        raise RowError("SIGN_RULE", "refund gross_amount must be < 0")
    if typ == "fee" and gross != 0:
        raise RowError("SIGN_RULE", "fee rows must have gross_amount 0")
    bref = _ident(row, "business_ref", required=False)
    if typ in ("charge", "refund") and not bref:
        raise RowError("MISSING_VALUE", f"{typ} requires business_ref")
    batch = _ident(row, "settlement_batch_id", required=False)
    sdate = _date(row, "settlement_date", required=False)
    bank_acct = _ident(row, "bank_account", required=False)
    if batch and (not sdate or not bank_acct):
        raise RowError("MISSING_VALUE", "settled item requires settlement_date and bank_account")
    if not batch and sdate:
        raise RowError("BAD_VALUE", "settlement_date given without settlement_batch_id")
    return {
        "record_id": _ident(row, "psp_txn_id"), "type": typ, "business_ref": bref,
        "merchant_account": _ident(row, "merchant_account"), "currency": cur,
        "gross_minor": gross, "fee_minor": fee, "net_minor": net,
        "created_date": _date(row, "created_date"), "available_on": _date(row, "available_on"),
        "settlement_batch_id": batch, "settlement_date": sdate, "bank_account": bank_acct,
    }


def parse_bank(row: dict) -> dict:
    cur = _currency(row)
    amt = _amount(row, "amount", cur)
    if amt == 0:
        raise RowError("BAD_AMOUNT", "bank amount must be non-zero")
    return {
        "record_id": _ident(row, "bank_txn_id"), "bank_account": _ident(row, "bank_account"), "currency": cur,
        "amount_minor": amt, "value_date": _date(row, "value_date"),
        "reference": (row.get("reference") or "").strip(), "description": (row.get("description") or "").strip(),
    }


PARSERS = {"ledger": parse_ledger, "psp": parse_psp, "bank": parse_bank}


def business_key(source: str, rec: dict) -> str:
    """Business key scope: (merchant_account, business_ref). The same business_ref under two different merchants
    is two different business events and is never merged. Bank lines have no business key (record id only)."""
    if source == "ledger" or (source == "psp" and rec["type"] in ("charge", "refund")):
        # JSON tuple encoding: collision-safe even if identifier rules were ever relaxed.
        return json.dumps([rec["merchant_account"], rec["business_ref"]], separators=(",", ":"))
    return ""


def content_hash(rec: dict) -> str:
    """Hash of the *parsed* record, so '1000' and '1000.00' are the same content."""
    payload = json.dumps({k: rec[k] for k in sorted(rec)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------- import

def _existing_index(conn: sqlite3.Connection, source: str) -> tuple[dict, dict]:
    by_id, by_bkey = {}, {}
    for r in conn.execute(f"SELECT * FROM {TABLE[source]}"):
        rec = dict(r)
        by_id[rec["record_id"]] = rec["content_hash"]
        bk = business_key(source, rec)
        if bk:
            by_bkey[bk] = rec["record_id"]
    return by_id, by_bkey


def _log_import(conn, res: ImportResult, actor: str) -> int:
    cur = conn.execute(
        "INSERT INTO imports(source, file_name, sha256, imported_at, actor, status, rows_read, rows_loaded, "
        "rows_duplicate, rows_quarantined, message) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (res.source, res.file_name, res.sha256, utcnow(), actor, res.status, res.rows_read, res.rows_loaded,
         res.rows_duplicate, res.rows_quarantined, res.message),
    )
    return cur.lastrowid


def import_bytes(conn: sqlite3.Connection, source: str, data: bytes, file_name: str,
                 actor: str = "demo.analyst", strict: bool = False) -> ImportResult:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {SOURCES}")
    sha = hashlib.sha256(data).hexdigest()
    res = ImportResult(import_id=0, source=source, file_name=file_name, sha256=sha, status="LOADED")

    prior = conn.execute(
        "SELECT import_id, file_name, source FROM imports WHERE sha256=? AND status IN ('LOADED','PARTIAL') "
        "ORDER BY import_id LIMIT 1",
        (sha,),
    ).fetchone()
    if prior:
        res.status = "DUPLICATE_FILE"
        res.message = (f"identical content already imported as import #{prior['import_id']} "
                       f"({prior['source']}: {prior['file_name']}); nothing loaded")
        return _finish_rejected(conn, res, actor)

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        res.status, res.message = "REJECTED", "file is not valid UTF-8 text"
        return _finish_rejected(conn, res, actor)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        res.status, res.message = "REJECTED", "empty file (no header row)"
        return _finish_rejected(conn, res, actor)
    header = [(h or "").strip().lower() for h in reader.fieldnames]
    if len(set(header)) != len(header):
        res.status, res.message = "REJECTED", f"duplicate column names in header: {header}"
        return _finish_rejected(conn, res, actor)
    missing = [c for c in REQUIRED_COLUMNS[source] if c not in header]
    if missing:
        res.status = "REJECTED"
        res.message = f"missing required column(s) for {source}: {', '.join(missing)}; whole file rejected"
        return _finish_rejected(conn, res, actor)
    reader.fieldnames = header
    known = set(REQUIRED_COLUMNS[source]) | set(OPTIONAL_COLUMNS[source])
    unknown = [h for h in header if h not in known]

    parser = PARSERS[source]
    table = TABLE[source]
    try:
        with tx(conn):
            by_id, by_bkey = _existing_index(conn, source)
            totals: dict[str, dict] = {}
            import_id = _log_import(conn, res, actor)  # placeholder counts, fixed below
            res.import_id = import_id
            for i, raw in enumerate(reader, start=2):  # header is row 1
                res.rows_read += 1
                raw_clean = {k: (v if v is not None else "") for k, v in raw.items() if k is not None}
                if None in raw:  # more cells than header columns
                    _issue(conn, res, i, "QUARANTINED", "BAD_ROW_SHAPE",
                           f"row has {len(raw[None])} extra cell(s) beyond the header", raw_clean)
                    continue
                if any(v is None for v in raw.values()):
                    _issue(conn, res, i, "QUARANTINED", "BAD_ROW_SHAPE", "row has fewer cells than header", raw_clean)
                    continue
                try:
                    rec = parser(raw_clean)
                except RowError as e:
                    _issue(conn, res, i, "QUARANTINED", e.code, e.detail, raw_clean,
                           record_id=(raw_clean.get(REQUIRED_COLUMNS[source][0]) or "").strip())
                    continue
                h = content_hash(rec)
                rid, bk = rec["record_id"], business_key(source, rec)
                if rid in by_id:
                    if by_id[rid] == h:
                        _issue(conn, res, i, "DUPLICATE", "DUPLICATE_ROW",
                               f"record {rid} already loaded with identical content; skipped", raw_clean,
                               record_id=rid, bkey=bk)
                    else:
                        _issue(conn, res, i, "QUARANTINED", "CONFLICT_RECORD_ID",
                               f"record {rid} already loaded with different content", raw_clean,
                               record_id=rid, bkey=bk, conflicts_with=rid)
                    continue
                if bk and bk in by_bkey:
                    _issue(conn, res, i, "QUARANTINED", "CONFLICT_BUSINESS_KEY",
                           f"(merchant, business_ref) {bk} already used by record {by_bkey[bk]}", raw_clean,
                           record_id=rid, bkey=bk, conflicts_with=by_bkey[bk])
                    continue
                cols = list(rec) + ["content_hash", "import_id", "source_row"]
                conn.execute(
                    f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [rec[c] for c in rec] + [h, import_id, i],
                )
                by_id[rid] = h
                if bk:
                    by_bkey[bk] = rid
                res.rows_loaded += 1
                amt = rec.get("gross_minor", rec.get("amount_minor", 0))
                t = totals.setdefault(rec["currency"], {"rows": 0, "amount_minor": 0})
                t["rows"] += 1
                t["amount_minor"] += amt if source != "psp" else rec["net_minor"]
            if strict and res.rows_quarantined:
                raise _StrictAbort()
            assert res.rows_read == res.rows_loaded + res.rows_duplicate + res.rows_quarantined
            if res.rows_quarantined:
                res.status = "PARTIAL"
            res.message = _summary_msg(res) + (f"; ignored unknown column(s): {', '.join(unknown)}" if unknown else "")
            res.loaded_totals = totals
            conn.execute(
                "UPDATE imports SET status=?, rows_read=?, rows_loaded=?, rows_duplicate=?, rows_quarantined=?, "
                "message=?, loaded_totals_json=? WHERE import_id=?",
                (res.status, res.rows_read, res.rows_loaded, res.rows_duplicate, res.rows_quarantined,
                 res.message, json.dumps(totals, sort_keys=True), import_id),
            )
            audit(conn, actor, "import", str(import_id), f"IMPORT_{res.status}", json.dumps(
                {"source": source, "file": file_name, "sha256": sha, "read": res.rows_read,
                 "loaded": res.rows_loaded, "duplicate": res.rows_duplicate, "quarantined": res.rows_quarantined}))
    except _StrictAbort:
        issues = res.issues
        res = ImportResult(import_id=0, source=source, file_name=file_name, sha256=sha, status="REJECTED",
                           rows_read=res.rows_read, issues=issues,
                           message=f"strict mode: {sum(1 for x in issues if x['kind'] == 'QUARANTINED')} row error(s); "
                                   f"transaction rolled back, nothing loaded. First errors: "
                                   + "; ".join(f"row {x['row']} {x['code']}" for x in issues
                                               if x['kind'] == 'QUARANTINED')[:500])
        return _finish_rejected(conn, res, actor)
    return res


class _StrictAbort(Exception):
    pass


def _summary_msg(res: ImportResult) -> str:
    return (f"{'PARTIAL: ' if res.rows_quarantined else ''}read {res.rows_read}, loaded {res.rows_loaded}, duplicate {res.rows_duplicate}, "
            f"quarantined {res.rows_quarantined}")


def _issue(conn, res: ImportResult, row_no: int, kind: str, code: str, detail: str, raw: dict,
           record_id: str = "", bkey: str = "", conflicts_with: str = "") -> None:
    conn.execute(
        "INSERT INTO row_issues(import_id, source, source_row, kind, reason_code, detail, record_id, business_key, "
        "conflicts_with, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (res.import_id, res.source, row_no, kind, code, detail, record_id, bkey, conflicts_with,
         json.dumps(raw, sort_keys=True)),
    )
    if kind == "DUPLICATE":
        res.rows_duplicate += 1
    else:
        res.rows_quarantined += 1
    res.issues.append({"row": row_no, "kind": kind, "code": code, "detail": detail, "record_id": record_id})


def _finish_rejected(conn, res: ImportResult, actor: str) -> ImportResult:
    with tx(conn):
        res.import_id = _log_import(conn, res, actor)
        audit(conn, actor, "import", str(res.import_id), f"IMPORT_{res.status}",
              json.dumps({"source": res.source, "file": res.file_name, "sha256": res.sha256, "message": res.message}))
    return res


def import_file(conn: sqlite3.Connection, source: str, path: str | Path, actor: str = "demo.analyst",
                strict: bool = False) -> ImportResult:
    p = Path(path)
    return import_bytes(conn, source, p.read_bytes(), p.name, actor=actor, strict=strict)


def blocked_keys(conn: sqlite3.Connection) -> dict[str, dict[str, list[dict]]]:
    """Record ids / business keys involved in conflicts, per source, with the quarantined evidence rows."""
    out: dict[str, dict[str, list[dict]]] = {s: {} for s in SOURCES}
    for r in conn.execute(
        "SELECT ri.*, i.sha256, i.file_name FROM row_issues ri JOIN imports i USING(import_id) "
        "WHERE ri.reason_code IN ('CONFLICT_RECORD_ID','CONFLICT_BUSINESS_KEY') ORDER BY ri.issue_id"
    ):
        out[r["source"]].setdefault(r["conflicts_with"], []).append(dict(r))
    return out
