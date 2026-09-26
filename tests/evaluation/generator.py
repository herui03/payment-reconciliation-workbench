"""Deterministic synthetic data generator for the benchmark (independent, invented data; no real-world source).

Writes engine-visible CSVs (ledger/psp/bank) to one directory and ground truth to ANOTHER directory under
tests/evaluation/. IDs are opaque random hex (``ord_…``, ``po_…``) so nothing can be inferred from ID formats.

Profiles
  default  - wide amount range (few coincidental equal amounts), moderate anomaly rates      (development)
  collide  - amounts drawn from a few price points + more bank lines without reference        (holdout)
  messy    - higher anomaly rates, more split / merged payouts, more late/partial receipts    (holdout)
  adversarial - one PSP item per payout, few price points, half of bank lines without reference and
                noise credits drawn from the same net amounts: stresses amount-only (B3) matching  (holdout)
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from recon.money import to_plain

PROFILES = {
    "default": dict(price_points=None, p_refund=0.05, p_missing_psp=0.01, p_missing_ledger=0.01, p_amount_diff=0.005,
                    p_date_shift=0.01, p_noref=0.10, p_split=0.04, p_merge=0.04, p_missing_bank=0.01,
                    p_partial=0.005, p_late_bank=0.005, p_noise=0.01),
    "collide": dict(price_points=[990, 1990, 2500, 4900, 9900, 12000, 19900], p_refund=0.05, p_missing_psp=0.01,
                    p_missing_ledger=0.01, p_amount_diff=0.005, p_date_shift=0.01, p_noref=0.35, p_split=0.04,
                    p_merge=0.04, p_missing_bank=0.02, p_partial=0.01, p_late_bank=0.01, p_noise=0.02),
    "messy": dict(price_points=None, p_refund=0.08, p_missing_psp=0.03, p_missing_ledger=0.03, p_amount_diff=0.02,
                  p_date_shift=0.03, p_noref=0.15, p_split=0.10, p_merge=0.10, p_missing_bank=0.03,
                  p_partial=0.02, p_late_bank=0.02, p_noise=0.03),
    "adversarial": dict(price_points=[990, 1990, 2500, 4900], p_refund=0.0, p_missing_psp=0.01,
                        p_missing_ledger=0.01, p_amount_diff=0.005, p_date_shift=0.01, p_noref=0.50, p_split=0.02,
                        p_merge=0.02, p_missing_bank=0.03, p_partial=0.01, p_late_bank=0.03, p_noise=0.03,
                        per_item_payout=True, noise_from_nets=True),
}
MERCHANTS = [("MER-SG-A", "SGD", "BANK-SG-A"), ("MER-SG-B", "SGD", "BANK-SG-B"), ("MER-US-A", "USD", "BANK-US-A"),
             ("MER-US-B", "USD", "BANK-US-B"), ("MER-EU-A", "EUR", "BANK-EU-A"), ("MER-EU-B", "EUR", "BANK-EU-B")]


@dataclass
class GenResult:
    data_dir: Path
    truth_dir: Path
    as_of: date
    counts: dict


def generate(n_ledger: int, seed: int, profile: str, data_dir: Path, truth_dir: Path,
             start: date = date(2026, 1, 1), days: int = 90) -> GenResult:
    cfg = PROFILES[profile]
    rng = random.Random(seed)
    oid = lambda p: f"{p}_{rng.getrandbits(48):012x}"  # noqa: E731
    data_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    ledger, psp, bank = [], [], []
    truth_a = []   # {"ledger": id|None, "psp": id|None, "label": ...}
    sales = []
    n_sales = int(n_ledger / (1 + cfg["p_refund"]))
    for _ in range(n_sales):
        mer, cur, acct = rng.choice(MERCHANTS)
        d = start + timedelta(days=rng.randrange(days))
        amt = rng.choice(cfg["price_points"]) if cfg["price_points"] else rng.randint(500, 200000)
        sales.append((mer, cur, acct, d, amt))
    events = [("SALE", s, oid("ord"), "") for s in sales]
    for _, s, sale_ref, _ in rng.sample(events, int(n_sales * cfg["p_refund"])):
        mer, cur, acct, d, amt = s
        events.append(("REFUND", (mer, cur, acct, d + timedelta(days=rng.randint(1, 5)), -rng.randint(1, amt)),
                       oid("rfd"), sale_ref))
    rng.shuffle(events)

    for et, (mer, cur, acct, d, amt), ref, orig in events:
        lid, pid = oid("led"), oid("txn")
        r = rng.random()
        label = "clean"
        cuts = [("missing_psp", cfg["p_missing_psp"]), ("missing_ledger", cfg["p_missing_ledger"]),
                ("amount_diff", cfg["p_amount_diff"]), ("date_shift", cfg["p_date_shift"])]
        acc = 0.0
        for name, p in cuts:
            acc += p
            if r < acc:
                label = name
                break
        delay = rng.choices([0, 1, 2], weights=[70, 25, 5])[0]
        if label == "date_shift":
            delay = rng.randint(3, 6)
        pgross = amt
        if label == "amount_diff":
            pgross = amt - rng.randint(1, max(1, abs(amt) // 10)) * (1 if amt > 0 else -1)
            if pgross == 0 or (pgross > 0) != (amt > 0):
                pgross = amt - (1 if amt > 0 else -1)
        created = d + timedelta(days=delay)
        if label != "missing_ledger":
            ledger.append({"ledger_entry_id": lid, "event_type": et, "business_ref": ref,
                           "original_ref": orig, "merchant_account": mer,
                           "currency": cur, "gross_amount": to_plain(amt, cur), "event_date": d.isoformat(),
                           "description": ""})
        if label != "missing_psp":
            fee = (round(pgross * 0.029) + 30) if et == "SALE" else 0
            psp.append({"psp_txn_id": pid, "type": "charge" if et == "SALE" else "refund", "business_ref": ref,
                        "merchant_account": mer, "currency": cur, "gross": pgross, "fee": fee, "net": pgross - fee,
                        "created_date": created, "available_on": created + timedelta(days=rng.choice([1, 2])),
                        "bank_account": acct})
        truth_a.append({"ledger": lid if label != "missing_ledger" else None,
                        "psp": pid if label != "missing_psp" else None, "label": label})

    # settlement batches: one per merchant per available_on day, up to the last generated day
    # horizon chosen so every generated item is created and settled on/before as-of (clean denominators)
    as_of = start + timedelta(days=days + 12)
    last_settle = start + timedelta(days=days + 10)
    batches: dict[tuple, list] = {}
    for p in psp:
        if p["available_on"] <= last_settle:
            key = (p["merchant_account"], p["available_on"], p["psp_txn_id"] if cfg.get("per_item_payout") else "")
            batches.setdefault(key, []).append(p)
    batch_list = []
    for (mer, sdate, _), items in sorted(batches.items()):
        bid = oid("po")
        for p in items:
            p["batch"], p["sdate"] = bid, sdate
        batch_list.append({"id": bid, "merchant": mer, "cur": items[0]["currency"], "acct": items[0]["bank_account"],
                           "sdate": sdate, "net": sum(p["net"] for p in items)})

    truth_b = []   # {"batches": [...], "bank": [...], "label": ...}
    by_mer: dict[str, list] = {}
    for b in batch_list:
        by_mer.setdefault(b["merchant"], []).append(b)
    consumed = set()

    def line(acct, cur, amt, vdate, ref):
        kid = oid("bk")
        bank.append({"bank_txn_id": kid, "bank_account": acct, "currency": cur, "amount": to_plain(amt, cur),
                     "value_date": vdate.isoformat(), "reference": ref, "description": "PSP PAYOUT"})
        return kid

    for mer in sorted(by_mer):
        bl = by_mer[mer]
        for i, b in enumerate(bl):
            if b["id"] in consumed:
                continue
            consumed.add(b["id"])
            vd = b["sdate"] + timedelta(days=rng.choices([0, 1, 2], weights=[60, 30, 10])[0])
            r = rng.random()
            nxt = bl[i + 1] if i + 1 < len(bl) and bl[i + 1]["id"] not in consumed else None
            opts = [("missing_bank", cfg["p_missing_bank"]), ("partial", cfg["p_partial"]),
                    ("late_bank", cfg["p_late_bank"]), ("split", cfg["p_split"]), ("merge", cfg["p_merge"]),
                    ("clean_noref", cfg["p_noref"])]
            label, acc = "clean_ref", 0.0
            for name, p in opts:
                acc += p
                if r < acc:
                    label = name
                    break
            if b["net"] <= 0 and label in ("split", "merge", "partial"):
                label = "clean_ref"
            if label == "merge" and (nxt is None or nxt["net"] <= 0 or (nxt["sdate"] - b["sdate"]).days > 2):
                label = "clean_ref"
            if label == "missing_bank":
                truth_b.append({"batches": [b["id"]], "bank": [], "label": label})
            elif label == "partial":
                k = line(b["acct"], b["cur"], b["net"] - rng.randint(1, max(1, b["net"] // 2)), vd, b["id"])
                truth_b.append({"batches": [b["id"]], "bank": [k], "label": label})
            elif label == "late_bank":
                k = line(b["acct"], b["cur"], b["net"], b["sdate"] + timedelta(days=rng.randint(4, 8)), b["id"])
                truth_b.append({"batches": [b["id"]], "bank": [k], "label": label})
            elif label == "split":
                n = rng.randint(2, 3)
                cuts = sorted(rng.sample(range(1, b["net"]), n - 1)) if b["net"] > n else []
                if not cuts:
                    k = line(b["acct"], b["cur"], b["net"], vd, b["id"])
                    truth_b.append({"batches": [b["id"]], "bank": [k], "label": "clean_ref"})
                    continue
                parts = [y - x for x, y in zip([0] + cuts, cuts + [b["net"]])]
                ks = [line(b["acct"], b["cur"], a, b["sdate"] + timedelta(days=min(j, 2)), f"{b['id']} PART {j + 1}")
                      for j, a in enumerate(parts)]
                truth_b.append({"batches": [b["id"]], "bank": ks, "label": label})
            elif label == "merge":
                consumed.add(nxt["id"])
                k = line(b["acct"], b["cur"], b["net"] + nxt["net"], nxt["sdate"], f"{b['id']},{nxt['id']}")
                truth_b.append({"batches": [b["id"], nxt["id"]], "bank": [k], "label": label})
            elif label == "clean_noref":
                k = line(b["acct"], b["cur"], b["net"], vd, "PSP PAYOUT")
                truth_b.append({"batches": [b["id"]], "bank": [k], "label": label})
            else:
                k = line(b["acct"], b["cur"], b["net"], vd, f"PAYOUT {b['id']}")
                truth_b.append({"batches": [b["id"]], "bank": [k], "label": label})
    noise = []
    for _ in range(int(len(batch_list) * cfg["p_noise"])):
        mer, cur, acct = rng.choice(MERCHANTS)
        amt = rng.choice(batch_list)["net"] if cfg.get("noise_from_nets") else rng.randint(100, 5000)
        noise.append(line(acct, cur, amt, start + timedelta(days=rng.randrange(days)), "INTEREST"))

    rng.shuffle(ledger)
    rng.shuffle(psp)
    rng.shuffle(bank)
    _write(data_dir / "ledger.csv", ["ledger_entry_id", "event_type", "business_ref", "original_ref",
                                     "merchant_account", "currency", "gross_amount", "event_date", "description"], ledger)
    psp_rows = [{"psp_txn_id": p["psp_txn_id"], "type": p["type"], "business_ref": p["business_ref"],
                 "merchant_account": p["merchant_account"], "currency": p["currency"],
                 "gross_amount": to_plain(p["gross"], p["currency"]), "fee_amount": to_plain(p["fee"], p["currency"]),
                 "net_amount": to_plain(p["net"], p["currency"]), "created_date": p["created_date"].isoformat(),
                 "available_on": p["available_on"].isoformat(), "settlement_batch_id": p.get("batch", ""),
                 "settlement_date": p["sdate"].isoformat() if p.get("sdate") else "",
                 "bank_account": p["bank_account"] if p.get("batch") else ""} for p in psp]
    _write(data_dir / "psp.csv", list(psp_rows[0]), psp_rows)
    _write(data_dir / "bank.csv", ["bank_txn_id", "bank_account", "currency", "amount", "value_date", "reference",
                                   "description"], bank)
    meta = {"seed": seed, "profile": profile, "n_ledger_target": n_ledger, "start": start.isoformat(), "days": days,
            "as_of": as_of.isoformat()}
    (truth_dir / "truth_chain_a.json").write_text(json.dumps(truth_a))
    (truth_dir / "truth_chain_b.json").write_text(json.dumps({"groups": truth_b, "noise_bank": noise}))
    (truth_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    counts = {"ledger": len(ledger), "psp": len(psp), "bank": len(bank), "batches": len(batch_list)}
    return GenResult(data_dir, truth_dir, as_of, counts)


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
