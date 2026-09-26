"""Tiny CSV builders for focused tests (synthetic values only)."""
LEDGER_H = "ledger_entry_id,event_type,business_ref,original_ref,merchant_account,currency,gross_amount,event_date,description"
PSP_H = ("psp_txn_id,type,business_ref,merchant_account,currency,gross_amount,fee_amount,net_amount,created_date,"
         "available_on,settlement_batch_id,settlement_date,bank_account")
BANK_H = "bank_txn_id,bank_account,currency,amount,value_date,reference,description"


def csv_bytes(header: str, rows: list[str]) -> bytes:
    return ("\n".join([header] + rows) + "\n").encode()


def led(i, ref, amt, d, mer="M1", cur="SGD", typ="SALE", orig=""):
    return f"L{i},{typ},{ref},{orig},{mer},{cur},{amt},{d},"


def psp(i, ref, gross, fee, d, batch="", sdate="", acct="BK1", mer="M1", cur="SGD", typ="charge", avail=None):
    from decimal import Decimal
    net = Decimal(gross) - Decimal(fee)
    return f"P{i},{typ},{ref},{mer},{cur},{gross},{fee},{net:.2f},{d},{avail or d},{batch},{sdate},{acct if batch else ''}"


def bank(i, amt, d, ref="", acct="BK1", cur="SGD"):
    return f"K{i},{acct},{cur},{amt},{d},{ref},"
