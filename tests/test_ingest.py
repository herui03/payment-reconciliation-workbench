"""Input validation, quarantine, rollback, idempotency and key-scope rules."""
import json

import pytest
from helpers import BANK_H, LEDGER_H, PSP_H, bank, csv_bytes, led, psp

from recon.ingest import import_bytes
from recon.money import AmountError, parse_amount


# ---------------------------------------------------------------- amounts (acceptance point 6)
@pytest.mark.parametrize("text,minor", [
    ("1000", 100000), ("1000.00", 100000), ("-200.5", -20050), ("+5", 500), ("0.10", 10), ("10.100", 1010),
    (" 12.50 ", 1250), ("999999999999.99", 99999999999999),
])
def test_parse_amount_ok(text, minor):
    assert parse_amount(text, "SGD") == minor
    assert isinstance(parse_amount(text, "SGD"), int)


@pytest.mark.parametrize("text", [
    "0.001", "10.005", "NaN", "nan", "Infinity", "-Infinity", "inf", "1e3", "1E-2", "1,000.00", "$10", "",
    " ", "12.", ".5", "--1", "1 000", "0x10", "1000000000000", "-1000000000000.00", "9" * 400, "1\n2",
])
def test_parse_amount_rejects(text):
    with pytest.raises(AmountError):
        parse_amount(text, "SGD")


def test_unsupported_currency():
    with pytest.raises(AmountError):
        parse_amount("1.00", "JPY")


# ---------------------------------------------------------------- file-level rejection
def _counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("ledger_records", "psp_records", "bank_records", "row_issues"))


def test_missing_column_rejects_whole_file(conn):
    r = import_bytes(conn, "bank", b"bank_txn_id,bank_account,currency,value_date,reference\nK1,BK1,SGD,2026-07-01,x\n", "b.csv")
    assert r.status == "REJECTED" and "amount" in r.message
    assert _counts(conn) == (0, 0, 0, 0)
    assert conn.execute("SELECT status FROM imports").fetchone()[0] == "REJECTED"


def test_duplicate_header_rejected(conn):
    r = import_bytes(conn, "bank", csv_bytes(BANK_H + ",amount", [bank(1, "1.00", "2026-07-01") + ",2.00"]), "b.csv")
    assert r.status == "REJECTED" and "duplicate column" in r.message
    assert _counts(conn) == (0, 0, 0, 0)


def test_unknown_column_is_ignored_and_reported(conn):
    r = import_bytes(conn, "bank", csv_bytes(BANK_H + ",memo_extra", [bank(1, "1.00", "2026-07-01") + ",hello"]), "b.csv")
    assert r.status == "LOADED" and "ignored unknown column(s): memo_extra" in r.message


def test_empty_and_non_utf8_rejected(conn):
    assert import_bytes(conn, "bank", b"", "e.csv").status == "REJECTED"
    assert import_bytes(conn, "bank", b"\xff\xfe\x00bad", "x.csv").status == "REJECTED"
    assert _counts(conn) == (0, 0, 0, 0)


def test_header_only_file_loads_zero_rows(conn):
    r = import_bytes(conn, "bank", csv_bytes(BANK_H, []), "b.csv")
    assert (r.status, r.rows_read, r.rows_loaded) == ("LOADED", 0, 0)


# ---------------------------------------------------------------- 3 rows, last invalid (acceptance point 4)
THREE = csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-01"), led(2, "O2", "20.00", "2026-07-01"),
                             led(3, "O3", "NaN", "2026-07-01")])


def test_three_rows_last_invalid_default_is_partial_and_explicit(conn):
    r = import_bytes(conn, "ledger", THREE, "l.csv")
    assert r.status == "PARTIAL"                       # never shown as a clean LOADED
    assert (r.rows_read, r.rows_loaded, r.rows_quarantined) == (3, 2, 1)
    assert r.rows_read == r.rows_loaded + r.rows_duplicate + r.rows_quarantined
    assert r.loaded_totals == {"SGD": {"rows": 2, "amount_minor": 3000}}
    row = conn.execute("SELECT * FROM imports").fetchone()
    assert row["status"] == "PARTIAL" and json.loads(row["loaded_totals_json"])["SGD"]["amount_minor"] == 3000
    q = conn.execute("SELECT source_row, reason_code FROM row_issues").fetchall()
    assert [tuple(x) for x in q] == [(4, "BAD_AMOUNT")]
    assert conn.execute("SELECT SUM(gross_minor) FROM ledger_records").fetchone()[0] == 3000


def test_three_rows_last_invalid_strict_rolls_back_everything(conn):
    r = import_bytes(conn, "ledger", THREE, "l.csv", strict=True)
    assert r.status == "REJECTED" and r.rows_loaded == 0 and "rolled back" in r.message
    assert _counts(conn) == (0, 0, 0, 0)
    # the same bytes can be re-tried (a rejected file is not treated as already imported)
    r2 = import_bytes(conn, "ledger", THREE, "l.csv")
    assert r2.status == "PARTIAL" and r2.rows_loaded == 2


def test_row_shape_errors_quarantined(conn):
    r = import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "1.00", "2026-07-01") + ",EXTRA",
                                                          "L2,SALE,O2"]), "l.csv")
    assert r.rows_loaded == 0 and {i["code"] for i in r.issues} == {"BAD_ROW_SHAPE"}


@pytest.mark.parametrize("row,code", [
    (led(1, "O1", "1.00", "2026-02-30"), "BAD_DATE"), (led(1, "O1", "1.00", "07/01/2026"), "BAD_DATE"),
    (led(1, "O1", "1.00", "2026-7-1"), "BAD_DATE"), (led(1, "O1", "1.00", "2026-07-01", cur="JPY"), "UNSUPPORTED_CURRENCY"),
    (led(1, "O1", "-1.00", "2026-07-01"), "SIGN_RULE"), (led(1, "O1", "1.00", "2026-07-01", typ="REFUND", orig="O0"), "SIGN_RULE"),
    (led(1, "O1", "-1.00", "2026-07-01", typ="REFUND"), "MISSING_VALUE"), (led(1, "", "1.00", "2026-07-01"), "MISSING_VALUE"),
    (led(1, "O1", "1.00", "2026-07-01", typ="CHARGEBACK"), "BAD_VALUE"),
])
def test_ledger_row_rules(conn, row, code):
    r = import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [row]), "l.csv")
    assert [i["code"] for i in r.issues] == [code]


def test_psp_net_must_equal_gross_minus_fee(conn):
    bad = "P1,charge,O1,M1,SGD,100.00,2.00,97.00,2026-07-01,2026-07-02,,,"
    r = import_bytes(conn, "psp", csv_bytes(PSP_H, [bad]), "p.csv")
    assert [i["code"] for i in r.issues] == ["NET_MISMATCH"]


# ---------------------------------------------------------------- idempotency
def test_same_bytes_any_name_is_duplicate_file(conn):
    data = csv_bytes(BANK_H, [bank(1, "5.00", "2026-07-01")])
    assert import_bytes(conn, "bank", data, "a.csv").status == "LOADED"
    r = import_bytes(conn, "bank", data, "renamed.csv")
    assert r.status == "DUPLICATE_FILE" and r.rows_loaded == 0 and "a.csv" in r.message
    assert conn.execute("SELECT COUNT(*) FROM bank_records").fetchone()[0] == 1


def test_overlapping_resend_with_different_formatting_is_row_duplicate(conn):
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-01")]), "a.csv")
    r = import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "10", "2026-07-01"),
                                                          led(2, "O2", "3.00", "2026-07-01")]), "b.csv")
    assert (r.status, r.rows_loaded, r.rows_duplicate) == ("LOADED", 1, 1)


def test_conflicting_record_id_quarantined_not_overwritten(conn):
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-01")]), "a.csv")
    r = import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "11.00", "2026-07-01")]), "b.csv")
    assert r.status == "PARTIAL" and r.issues[0]["code"] == "CONFLICT_RECORD_ID"
    assert conn.execute("SELECT gross_minor FROM ledger_records").fetchone()[0] == 1000


# ---------------------------------------------------------------- key scope (acceptance point 1)
def test_same_business_ref_under_two_merchants_is_two_records(conn):
    r = import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "ORD-1", "10.00", "2026-07-01", mer="MA"),
                                                          led(2, "ORD-1", "10.00", "2026-07-01", mer="MB")]), "l.csv")
    assert r.rows_loaded == 2 and not r.issues


def test_same_business_ref_same_merchant_different_id_is_conflict(conn):
    r = import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "ORD-1", "10.00", "2026-07-01"),
                                                          led(2, "ORD-1", "10.00", "2026-07-01")]), "l.csv")
    assert r.rows_loaded == 1 and r.issues[0]["code"] == "CONFLICT_BUSINESS_KEY"


def test_record_ids_are_globally_unique_per_source_not_per_account(conn):
    """v1 requires bank_txn_id / psp_txn_id / ledger_entry_id to be unique across ALL accounts of that source.
    A reused id on another account is quarantined as a conflict, never silently merged."""
    r = import_bytes(conn, "bank", csv_bytes(BANK_H, [bank(1, "5.00", "2026-07-01", acct="BK1"),
                                                      bank(1, "5.00", "2026-07-01", acct="BK2")]), "b.csv")
    assert r.rows_loaded == 1 and r.issues[0]["code"] == "CONFLICT_RECORD_ID"
    r = import_bytes(conn, "psp", csv_bytes(PSP_H, [psp(1, "O1", "10.00", "0.10", "2026-07-01", mer="MA"),
                                                    psp(1, "O1", "10.00", "0.10", "2026-07-01", mer="MB")]), "p.csv")
    assert r.rows_loaded == 1 and r.issues[0]["code"] == "CONFLICT_RECORD_ID"


def test_source_records_are_append_only(conn):
    import_bytes(conn, "ledger", csv_bytes(LEDGER_H, [led(1, "O1", "10.00", "2026-07-01")]), "a.csv")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ledger_records SET gross_minor=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM ledger_records")
