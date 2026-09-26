"""Deterministic, rule-based matching engine for the two reconciliation chains.

Chain A  ledger (gross)            <->  PSP charge/refund items (gross)        by exact business reference
Chain B  PSP settlement batch (net) <->  bank statement lines                   by reference, then amount/date

This module is intentionally pure: it receives already-validated records and returns match groups, per-record
states and exceptions. It performs no I/O, never sees ground truth or scenario labels, and never parses meaning
out of ID formats (references are compared only as whole tokens against known batch IDs).

Auto-match policy: a match is created only when the candidate is unique AND the evidence is sufficient:
  A1  exact business_ref + same merchant + same currency + same type + exact gross + date within tolerance
  B1  bank reference names exactly this settlement batch, exact amount, value date in window (1:1)
  B2S bank lines referencing one batch sum exactly to its net, all in window (1 settlement : N bank)
  B2M one bank line references several batches whose nets sum exactly to it, all in window (N : 1)
  B3  no reference: exact amount + window + same account/currency, and unique in BOTH directions (1:1)
Amount-only many-to-one / one-to-many combinations (B4) are only *suggested* for human review: coincidental
subset sums grow combinatorially, so amount alone is not treated as sufficient evidence for a group.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from .money import fmt_minor


# ---------------------------------------------------------------- inputs

@dataclass(frozen=True)
class Rules:
    a_date_tolerance_days: int = 2      # |ledger event_date - PSP created_date| allowed for A1
    a_grace_days: int = 2               # one-sided item younger than this (vs as-of) is PENDING, not an exception
    b_window_before: int = 1            # bank value_date may be this many days before settlement_date
    b_window_after: int = 3             # ... or this many days after
    unsettled_grace_days: int = 2       # unsettled PSP item overdue when as_of > available_on + grace
    max_candidates: int = 12            # candidate cap for amount-only group search (B4)
    max_group_size: int = 4             # max members on the "many" side of a B4 suggestion
    max_ref_group: int = 10             # max members on the "many" side of a reference group (B2S/B2M)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Ledger:
    record_id: str
    event_type: str
    business_ref: str
    merchant_account: str
    currency: str
    gross: int
    event_date: date


@dataclass(frozen=True)
class PspItem:
    record_id: str
    type: str
    business_ref: str
    merchant_account: str
    currency: str
    gross: int
    fee: int
    net: int
    created_date: date
    available_on: date
    batch_id: str
    settlement_date: date | None
    bank_account: str


@dataclass(frozen=True)
class BankLine:
    record_id: str
    bank_account: str
    currency: str
    amount: int
    value_date: date
    reference: str


@dataclass
class Settlement:
    batch_id: str
    items: list[PspItem]
    bank_account: str = ""
    currency: str = ""
    merchant_account: str = ""
    settlement_date: date | None = None
    net: int = 0
    gross: int = 0
    fee: int = 0
    problem: str = ""   # non-empty => not matchable (inconsistent / blocked)


@dataclass
class EngineInput:
    as_of: date
    ledger: list[Ledger]
    psp: list[PspItem]
    bank: list[BankLine]
    blocked: dict[str, dict[str, list[int]]] = field(default_factory=dict)   # source -> record_id -> issue ids
    hints: dict[tuple[str, str], list[str]] = field(default_factory=dict)    # (source, business_ref) -> notes
    rules: Rules = field(default_factory=Rules)
    excluded_after_as_of: dict[str, int] = field(default_factory=dict)  # informational, set by the loader


# ---------------------------------------------------------------- outputs

@dataclass
class Member:
    side: str          # 'L' (ledger / settlement side) or 'R' (PSP / bank side)
    record_type: str   # ledger | psp | settlement | bank
    record_id: str
    amount: int


@dataclass
class MatchGroup:
    group_id: str
    chain: str
    rule: str
    currency: str
    members: list[Member]
    explanation: str

    def total(self, side: str) -> int:
        return sum(m.amount for m in self.members if m.side == side)


@dataclass
class RecordState:
    chain: str
    record_type: str
    record_id: str
    status: str
    currency: str
    amount: int
    rule: str = ""
    group_id: str = ""
    case_key: str = ""
    detail: str = ""


@dataclass
class ExceptionItem:
    case_key: str
    chain: str
    case_type: str
    rule: str
    anchor_type: str
    anchor_id: str
    currency: str
    amount: int          # exposure / difference in minor units (signed)
    anchor_date: date
    explanation: str
    evidence: list[dict]  # {"record_type","record_id","role"}


@dataclass
class EngineResult:
    groups: list[MatchGroup]
    states: list[RecordState]
    exceptions: list[ExceptionItem]
    settlements: dict[str, Settlement]


class EngineInvariantError(AssertionError):
    pass


_TOKEN_SPLIT = re.compile(r"[\s,;/|]+")


def reference_tokens(reference: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split(reference.upper()) if t}


def _m(x: int, cur: str) -> str:
    return f"{fmt_minor(x, cur)} {cur}"


# ---------------------------------------------------------------- engine

class _Builder:
    def __init__(self, inp: EngineInput):
        self.inp = inp
        self.r = inp.rules
        self.groups: list[MatchGroup] = []
        self.states: dict[tuple[str, str, str], RecordState] = {}
        self.exceptions: dict[str, ExceptionItem] = {}
        self._gseq = {"A": 0, "B": 0}

    def group(self, chain: str, rule: str, currency: str, members: list[Member], explanation: str) -> MatchGroup:
        self._gseq[chain] += 1
        g = MatchGroup(f"{chain}-{self._gseq[chain]:06d}", chain, rule, currency, members, explanation)
        if g.total("L") != g.total("R"):
            raise EngineInvariantError(f"amount not conserved in {g.group_id}: {g.total('L')} != {g.total('R')}")
        self.groups.append(g)
        for m in members:
            self.state(chain, m.record_type, m.record_id, "MATCHED", currency, m.amount, rule=rule,
                       group_id=g.group_id, detail=explanation)
        return g

    def state(self, chain, rtype, rid, status, currency, amount, rule="", group_id="", case_key="", detail=""):
        k = (chain, rtype, rid)
        if k in self.states:
            raise EngineInvariantError(f"record {k} assigned twice ({self.states[k].status} then {status})")
        self.states[k] = RecordState(chain, rtype, rid, status, currency, amount, rule, group_id, case_key, detail)

    def exc(self, e: ExceptionItem) -> None:
        if e.case_key in self.exceptions:
            raise EngineInvariantError(f"duplicate exception key {e.case_key}")
        self.exceptions[e.case_key] = e


def run_engine(inp: EngineInput) -> EngineResult:
    b = _Builder(inp)
    _chain_a(b)
    settlements = _chain_b(b)
    _check_invariants(b)
    groups = sorted(b.groups, key=lambda g: g.group_id)
    states = sorted(b.states.values(), key=lambda s: (s.chain, s.record_type, s.record_id))
    exceptions = sorted(b.exceptions.values(), key=lambda e: e.case_key)
    return EngineResult(groups, states, exceptions, settlements)


# ---------------------------------------------------------------- chain A

_TYPE_PAIR = {"SALE": "charge", "REFUND": "refund"}


def _chain_a(b: _Builder) -> None:
    inp, r = b.inp, b.r
    blocked_l = inp.blocked.get("ledger", {})
    blocked_p = inp.blocked.get("psp", {})
    # Business key scope is (merchant_account, business_ref): equal references under different merchants are
    # different events and are never paired (REC-03).
    led = {(x.merchant_account, x.business_ref): x for x in inp.ledger}
    psp = {}
    for p in inp.psp:
        if p.type == "fee":
            b.state("A", "psp", p.record_id, "OUT_OF_SCOPE", p.currency, p.gross, rule="A0_FEE_ROW",
                    detail="PSP fee row has no ledger counterpart by design; counted in chain B net only")
        else:
            psp[(p.merchant_account, p.business_ref)] = p
    led_merchants: dict[str, set[str]] = {}
    psp_merchants: dict[str, set[str]] = {}
    for mer, ref in led:
        led_merchants.setdefault(ref, set()).add(mer)
    for mer, ref in psp:
        psp_merchants.setdefault(ref, set()).add(mer)
    for mer, ref in sorted(set(led) | set(psp)):
        L, P = led.get((mer, ref)), psp.get((mer, ref))
        key = f"A|{mer}|{ref}"
        cur = (L or P).currency
        evid = []
        if L:
            evid.append({"record_type": "ledger", "record_id": L.record_id, "role": "ledger"})
        if P:
            evid.append({"record_type": "psp", "record_id": P.record_id, "role": "psp"})
        hint = " ".join(inp.hints.get(("ledger", f"{mer}|{ref}"), []) + inp.hints.get(("psp", f"{mer}|{ref}"), []))

        def fail(ctype, rule, amount, adate, expl):
            e = ExceptionItem(key, "A", ctype, rule, "business_ref", f"{mer}|{ref}", cur, amount, adate,
                              (expl + (" " + hint if hint else "")).strip(), evid)
            b.exc(e)
            if L:
                b.state("A", "ledger", L.record_id, ctype, L.currency, L.gross, rule=rule, case_key=key, detail=expl)
            if P:
                b.state("A", "psp", P.record_id, ctype, P.currency, P.gross, rule=rule, case_key=key, detail=expl)

        conflict_ids = []
        if L and L.record_id in blocked_l:
            conflict_ids += blocked_l[L.record_id]
        if P and P.record_id in blocked_p:
            conflict_ids += blocked_p[P.record_id]
        if conflict_ids:
            evid += [{"record_type": "row_issue", "record_id": str(i), "role": "conflicting_row"} for i in conflict_ids]
            fail("SOURCE_CONFLICT", "A-X1_CONFLICT", (L or P).gross, (L.event_date if L else P.created_date),
                 f"Business reference {ref} has conflicting source rows (same key, different content). "
                 f"Blocked from auto-matching until the source is corrected.")
            continue
        if L and P:
            dd = abs((L.event_date - P.created_date).days)
            if L.currency != P.currency:
                fail("CURRENCY_MISMATCH", "A-X3_CURRENCY", L.gross, L.event_date,
                     f"Same reference but currency differs: ledger {_m(L.gross, L.currency)} vs PSP "
                     f"{_m(P.gross, P.currency)}. No FX conversion in v1.")
            elif _TYPE_PAIR[L.event_type] != P.type:
                fail("TYPE_MISMATCH", "A-X4_TYPE", L.gross, L.event_date,
                     f"Ledger {L.event_type} vs PSP {P.type} for the same reference.")
            elif L.gross != P.gross:
                fail("AMOUNT_MISMATCH", "A-X5_AMOUNT", L.gross - P.gross, L.event_date,
                     f"Same reference; ledger gross {_m(L.gross, cur)} vs PSP gross {_m(P.gross, cur)}: "
                     f"difference {_m(L.gross - P.gross, cur)} (ledger minus PSP). Exact amount required; "
                     f"no tolerance applied.")
            elif dd > r.a_date_tolerance_days:
                fail("DATE_OUT_OF_TOLERANCE", "A-X6_DATE", L.gross, L.event_date,
                     f"Reference and amount agree but dates differ by {dd} days "
                     f"(ledger {L.event_date}, PSP {P.created_date}); tolerance is {r.a_date_tolerance_days} days.")
            else:
                b.group("A", "A1_EXACT_REF", cur,
                        [Member("L", "ledger", L.record_id, L.gross), Member("R", "psp", P.record_id, P.gross)],
                        f"A1: reference {ref}, merchant {L.merchant_account}, {_m(L.gross, cur)} gross, "
                        f"{L.event_type}/{P.type}, dates {L.event_date} vs {P.created_date} ({dd}d ≤ "
                        f"{r.a_date_tolerance_days}d).")
        elif L and (psp_merchants.get(ref, set()) - {mer}):
            others = sorted(psp_merchants[ref] - {mer})
            fail("ACCOUNT_MISMATCH", "A-X2_ACCOUNT", L.gross, L.event_date,
                 f"Ledger {ref} is booked under merchant {mer}, but the PSP reports this reference only under "
                 f"merchant(s) {', '.join(others)}. Accounts are never crossed, so nothing is matched.")
        elif P and (led_merchants.get(ref, set()) - {mer}):
            others = sorted(led_merchants[ref] - {mer})
            fail("ACCOUNT_MISMATCH", "A-X2_ACCOUNT", -P.gross, P.created_date,
                 f"PSP {ref} is reported under merchant {mer}, but the ledger books this reference only under "
                 f"merchant(s) {', '.join(others)}. Accounts are never crossed, so nothing is matched.")
        elif L:
            age = (inp.as_of - L.event_date).days
            if age <= r.a_grace_days:
                b.state("A", "ledger", L.record_id, "PENDING", L.currency, L.gross, rule="A-T1_GRACE",
                        detail=f"No PSP item yet; ledger entry is {age}d old (grace {r.a_grace_days}d).")
            else:
                fail("MISSING_IN_PSP", "A-X7_NO_PSP", L.gross, L.event_date,
                     f"Ledger {L.event_type} {ref} {_m(L.gross, cur)} on {L.event_date} has no PSP item "
                     f"after {age} days.")
        else:
            age = (inp.as_of - P.created_date).days
            if age <= r.a_grace_days:
                b.state("A", "psp", P.record_id, "PENDING", P.currency, P.gross, rule="A-T1_GRACE",
                        detail=f"No ledger entry yet; PSP item is {age}d old (grace {r.a_grace_days}d).")
            else:
                fail("MISSING_IN_LEDGER", "A-X8_NO_LEDGER", -P.gross, P.created_date,
                     f"PSP {P.type} {ref} {_m(P.gross, cur)} on {P.created_date} has no ledger entry "
                     f"after {age} days.")


# ---------------------------------------------------------------- chain B

def build_settlements(psp: list[PspItem], blocked_psp: dict[str, list[int]]) -> dict[str, Settlement]:
    by_batch: dict[str, list[PspItem]] = {}
    for p in psp:
        if p.batch_id:
            by_batch.setdefault(p.batch_id, []).append(p)
    out = {}
    for bid in sorted(by_batch):
        items = sorted(by_batch[bid], key=lambda p: p.record_id)
        s = Settlement(bid, items)
        accts = {p.bank_account for p in items}
        curs = {p.currency for p in items}
        dates = {p.settlement_date for p in items}
        mers = {p.merchant_account for p in items}
        if len(accts) > 1 or len(curs) > 1 or len(dates) > 1 or len(mers) > 1:
            s.problem = (f"BATCH_INCONSISTENT: items disagree on bank_account {sorted(accts)}, currency "
                         f"{sorted(curs)}, settlement_date {sorted(map(str, dates))} or merchant {sorted(mers)}")
        s.bank_account, s.currency, s.merchant_account = items[0].bank_account, items[0].currency, items[0].merchant_account
        s.settlement_date = items[0].settlement_date
        if len(curs) == 1:
            s.net = sum(p.net for p in items)
            s.gross = sum(p.gross for p in items)
            s.fee = sum(p.fee for p in items)
        blocked = [p.record_id for p in items if p.record_id in blocked_psp]
        if blocked and not s.problem:
            s.problem = f"SOURCE_CONFLICT: item(s) {', '.join(blocked)} have conflicting source rows"
        out[bid] = s
    return out


def _chain_b(b: _Builder) -> dict[str, Settlement]:
    inp, r = b.inp, b.r
    as_of = inp.as_of
    blocked_p = inp.blocked.get("psp", {})
    blocked_b = inp.blocked.get("bank", {})
    settlements = build_settlements(inp.psp, blocked_p)

    def skey(s: Settlement) -> str:
        return f"B|STL|{s.batch_id}"

    def in_window(s: Settlement, k: BankLine) -> bool:
        d = (k.value_date - s.settlement_date).days
        return -r.b_window_before <= d <= r.b_window_after

    def s_evid(s: Settlement, role="settlement") -> list[dict]:
        return [{"record_type": "settlement", "record_id": s.batch_id, "role": role}] + [
            {"record_type": "psp", "record_id": p.record_id, "role": "settlement_item"} for p in s.items]

    def k_evid(lines, role) -> list[dict]:
        return [{"record_type": "bank", "record_id": k.record_id, "role": role} for k in lines]

    def s_fail(s: Settlement, ctype, rule, amount, expl, lines=(), line_role="candidate", line_status=None):
        key = skey(s)
        b.exc(ExceptionItem(key, "B", ctype, rule, "settlement", s.batch_id, s.currency, amount,
                            s.settlement_date, expl, s_evid(s) + k_evid(lines, line_role)))
        b.state("B", "settlement", s.batch_id, ctype, s.currency, s.net, rule=rule, case_key=key, detail=expl)
        for k in lines:
            if ("B", "bank", k.record_id) not in b.states:
                b.state("B", "bank", k.record_id, line_status or "HELD_FOR_REVIEW", k.currency, k.amount,
                        rule=rule, case_key=key, detail=f"Linked to settlement {s.batch_id} case")

    # 1) unsettled PSP items (no batch yet)
    for p in sorted(inp.psp, key=lambda p: p.record_id):
        if p.batch_id:
            continue
        overdue_after = (as_of - p.available_on).days
        if overdue_after <= r.unsettled_grace_days:
            b.state("B", "psp", p.record_id, "NOT_DUE", p.currency, p.net, rule="B-T1_NOT_DUE",
                    detail=f"Unsettled; available_on {p.available_on}, grace {r.unsettled_grace_days}d after that.")
        else:
            key = f"B|ITEM|{p.record_id}"
            expl = (f"PSP item {p.record_id} ({p.type}, net {_m(p.net, p.currency)}) was available on "
                    f"{p.available_on} but is in no settlement batch {overdue_after}d later.")
            b.exc(ExceptionItem(key, "B", "UNSETTLED_OVERDUE", "B-X1_UNSETTLED", "psp", p.record_id, p.currency,
                                p.net, p.available_on, expl, [{"record_type": "psp", "record_id": p.record_id,
                                                              "role": "psp"}]))
            b.state("B", "psp", p.record_id, "UNSETTLED_OVERDUE", p.currency, p.net, rule="B-X1_UNSETTLED",
                    case_key=key, detail=expl)

    # 2) blocked bank lines
    bank_ok: list[BankLine] = []
    for k in sorted(inp.bank, key=lambda k: k.record_id):
        if k.record_id in blocked_b:
            key = f"B|BANK|{k.record_id}"
            expl = f"Bank line {k.record_id} has conflicting source rows (same id, different content); blocked."
            b.exc(ExceptionItem(key, "B", "SOURCE_CONFLICT", "B-X2_CONFLICT", "bank", k.record_id, k.currency,
                                k.amount, k.value_date, expl,
                                k_evid([k], "bank") + [{"record_type": "row_issue", "record_id": str(i),
                                                        "role": "conflicting_row"} for i in blocked_b[k.record_id]]))
            b.state("B", "bank", k.record_id, "SOURCE_CONFLICT", k.currency, k.amount, rule="B-X2_CONFLICT",
                    case_key=key, detail=expl)
        else:
            bank_ok.append(k)

    # 3) reference links (whole-token equality against known batch ids only)
    links: dict[str, list[str]] = {}
    for k in bank_ok:
        toks = reference_tokens(k.reference)
        hit = sorted(bid for bid in settlements if bid.upper() in toks)
        if hit:
            links[k.record_id] = hit
    bank_by_id = {k.record_id: k for k in bank_ok}

    # problem settlements (inconsistent / blocked) -> exception, referenced bank lines held
    for bid, s in settlements.items():
        if s.problem:
            ref_lines = [bank_by_id[kid] for kid, bids in sorted(links.items()) if bid in bids]
            ctype = "SOURCE_CONFLICT" if s.problem.startswith("SOURCE_CONFLICT") else "BATCH_INCONSISTENT"
            s_fail(s, ctype, "B-X3_BATCH_PROBLEM", s.net,
                   f"Settlement {bid} cannot be auto-matched: {s.problem}.", ref_lines, "referencing_line")
    good = {bid: s for bid, s in settlements.items() if not s.problem}

    # union-find components over (settlement, bank line) reference links
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for kid, bids in links.items():
        if ("B", "bank", kid) in b.states:
            continue
        for bid in bids:
            if bid in good:
                parent[find("K:" + kid)] = find("S:" + bid)
    comps: dict[str, tuple[list[str], list[str]]] = {}
    for node in list(parent):
        root = find(node)
        S, K = comps.setdefault(root, ([], []))
        (S if node.startswith("S:") else K).append(node[2:])

    for S_ids, K_ids in sorted((sorted(S), sorted(K)) for S, K in comps.values()):
        if not K_ids:
            continue
        S = [good[x] for x in S_ids]
        K = [bank_by_id[x] for x in K_ids]
        _ref_component(b, S, K, in_window, s_fail, skey, s_evid, k_evid)

    # 4) B3: amount + window, unique both ways (no reference evidence)
    rem_s = [s for bid, s in sorted(good.items()) if ("B", "settlement", bid) not in b.states]
    rem_k = [k for k in bank_ok if ("B", "bank", k.record_id) not in b.states]
    k_cands = {k.record_id: [s for s in rem_s if s.bank_account == k.bank_account and s.currency == k.currency
                             and s.net == k.amount and in_window(s, k)] for k in rem_k}
    s_cands: dict[str, list[BankLine]] = {s.batch_id: [] for s in rem_s}
    for k in rem_k:
        for s in k_cands[k.record_id]:
            s_cands[s.batch_id].append(k)
    ambiguous_s: set[str] = set()
    for k in rem_k:
        cs = k_cands[k.record_id]
        if len(cs) == 1 and len(s_cands[cs[0].batch_id]) == 1:
            s = cs[0]
            b.group("B", "B3_AMOUNT_DATE_UNIQUE", s.currency,
                    [Member("L", "settlement", s.batch_id, s.net), Member("R", "bank", k.record_id, k.amount)],
                    f"B3: no batch reference on bank line; unique exact amount {_m(k.amount, k.currency)} on "
                    f"{k.bank_account}, value {k.value_date} within window of settlement date {s.settlement_date}; "
                    f"no other candidate on either side.")
        elif cs:
            ambiguous_s.update(s.batch_id for s in cs)
    for s in rem_s:
        if len(s_cands[s.batch_id]) > 1:
            ambiguous_s.add(s.batch_id)
    for bid in sorted(ambiguous_s):
        s = good[bid]
        if ("B", "settlement", bid) in b.states:
            continue
        lines = s_cands[bid]
        others = sorted({o.batch_id for k in lines for o in k_cands[k.record_id]} - {bid})
        s_fail(s, "AMBIGUOUS_CANDIDATES", "B-X4_AMBIGUOUS", s.net,
               f"Settlement {bid} net {_m(s.net, s.currency)} has {len(lines)} same-amount bank line(s) in window "
               f"({', '.join(k.record_id for k in lines)}) competing with settlement(s) "
               f"{', '.join(others) or 'none'}; no reference to disambiguate, so nothing is auto-matched.",
               lines, "candidate", "AMBIGUOUS")

    # 5) B4 amount-only group suggestions (never auto-matched)
    rem_s = [s for bid, s in sorted(good.items()) if ("B", "settlement", bid) not in b.states]
    rem_k = [k for k in bank_ok if ("B", "bank", k.record_id) not in b.states]
    suggestions: list[tuple[list[Settlement], list[BankLine], str]] = []
    capped_k: set[str] = set()
    for k in rem_k:
        cands = [s for s in rem_s if s.bank_account == k.bank_account and s.currency == k.currency
                 and in_window(s, k) and (s.net > 0) == (k.amount > 0) and abs(s.net) < abs(k.amount)]
        if len(cands) > r.max_candidates:
            capped_k.add(k.record_id)
            continue
        for n in range(2, min(r.max_group_size, len(cands)) + 1):
            for combo in combinations(cands, n):
                if sum(s.net for s in combo) == k.amount:
                    suggestions.append((list(combo), [k], "N:1"))
    for s in rem_s:
        cands = [k for k in rem_k if k.bank_account == s.bank_account and k.currency == s.currency
                 and in_window(s, k) and (s.net > 0) == (k.amount > 0) and abs(k.amount) < abs(s.net)]
        if len(cands) > r.max_candidates:
            continue
        for n in range(2, min(r.max_group_size, len(cands)) + 1):
            for combo in combinations(cands, n):
                if sum(k.amount for k in combo) == s.net:
                    suggestions.append(([s], list(combo), "1:N"))
    usage: dict[str, int] = {}
    for S, K, _ in suggestions:
        for x in [f"S:{s.batch_id}" for s in S] + [f"K:{k.record_id}" for k in K]:
            usage[x] = usage.get(x, 0) + 1
    for S, K, shape in suggestions:
        contested = any(usage[f"S:{s.batch_id}"] > 1 for s in S) or any(usage[f"K:{k.record_id}"] > 1 for k in K)
        for s in S:
            if ("B", "settlement", s.batch_id) in b.states:
                continue
            n_alt = usage[f"S:{s.batch_id}"]
            ctype = "AMBIGUOUS_CANDIDATES" if contested else "SUGGESTED_GROUP"
            s_fail(s, ctype, "B4_AMOUNT_GROUP_SUGGESTION", s.net,
                   f"Amount-only {shape} combination found: settlements "
                   f"{' + '.join(f'{x.batch_id} ({fmt_minor(x.net, x.currency)})' for x in S)} = bank "
                   f"{' + '.join(f'{k.record_id} ({fmt_minor(k.amount, k.currency)})' for k in K)} "
                   f"{s.currency}. No reference evidence, so this is a suggestion for review, not a match"
                   f"{f' ({n_alt} alternative combinations involve this settlement)' if contested else ''}.",
                   K, "suggested_counterpart", "SUGGESTED")
        for k in K:
            if ("B", "bank", k.record_id) not in b.states:
                b.state("B", "bank", k.record_id, "SUGGESTED", k.currency, k.amount,
                        rule="B4_AMOUNT_GROUP_SUGGESTION", case_key=skey(S[0]), detail="Part of a suggested group")

    # 6) leftovers
    for bid, s in sorted(good.items()):
        if ("B", "settlement", bid) in b.states:
            continue
        due = (as_of - s.settlement_date).days
        if due <= r.b_window_after:
            b.state("B", "settlement", bid, "IN_TRANSIT", s.currency, s.net, rule="B-T2_IN_TRANSIT",
                    detail=f"No bank line yet; settlement date {s.settlement_date}, window ends "
                           f"+{r.b_window_after}d.")
        else:
            s_fail(s, "MISSING_IN_BANK", "B-X5_NO_BANK", s.net,
                   f"Settlement {bid} net {_m(s.net, s.currency)} to {s.bank_account} (settlement date "
                   f"{s.settlement_date}) has no bank line {due}d later (window +{r.b_window_after}d).")
    for k in bank_ok:
        if ("B", "bank", k.record_id) in b.states:
            continue
        key = f"B|BANK|{k.record_id}"
        refd = links.get(k.record_id, [])
        note = ""
        if refd:
            note = f" Its reference names settlement(s) {', '.join(refd)} which are already fully matched."
        if k.record_id in capped_k:
            note += f" Amount-group search skipped: more than {r.max_candidates} candidates."
        ctype = "UNEXPECTED_BANK_CREDIT" if k.amount > 0 else "UNEXPECTED_BANK_DEBIT"
        expl = (f"Bank line {k.record_id} {_m(k.amount, k.currency)} on {k.bank_account}, value {k.value_date}, "
                f"reference '{k.reference}' has no settlement counterpart.{note}")
        b.exc(ExceptionItem(key, "B", ctype, "B-X6_NO_SETTLEMENT", "bank", k.record_id, k.currency, k.amount,
                            k.value_date, expl, k_evid([k], "bank") + [
                                {"record_type": "settlement", "record_id": x, "role": "referenced"} for x in refd]))
        b.state("B", "bank", k.record_id, ctype, k.currency, k.amount, rule="B-X6_NO_SETTLEMENT",
                case_key=key, detail=expl)
    return settlements


def _ref_component(b: _Builder, S, K, in_window, s_fail, skey, s_evid, k_evid) -> None:
    r = b.r
    cross = [(s, k) for s in S for k in K if (s.bank_account, s.currency) != (k.bank_account, k.currency)]
    if cross:
        for s in S:
            bad = [k for k in K if (k.bank_account, k.currency) != (s.bank_account, s.currency)]
            ctype = "CURRENCY_MISMATCH" if any(k.currency != s.currency for k in bad) else "ACCOUNT_MISMATCH"
            detail = "; ".join(f"{k.record_id} is {k.bank_account}/{k.currency}" for k in bad) or "linked lines differ"
            s_fail(s, ctype, "B-X7_ACCOUNT_OR_CURRENCY", s.net,
                   f"Bank line reference names settlement {s.batch_id} (expected {s.bank_account}/{s.currency}) "
                   f"but {detail}. Accounts and currencies are never crossed.", K, "referencing_line")
        return
    cur = S[0].currency
    if len(S) == 1:
        s = S[0]
        exact = [k for k in K if k.amount == s.net and in_window(s, k)]
        if len(exact) == 1:
            k = exact[0]
            b.group("B", "B1_REF_1TO1", cur,
                    [Member("L", "settlement", s.batch_id, s.net), Member("R", "bank", k.record_id, k.amount)],
                    f"B1: bank reference '{k.reference}' names batch {s.batch_id}; amount {_m(k.amount, cur)} equals "
                    f"settlement net (gross {fmt_minor(s.gross, cur)} - fees {fmt_minor(s.fee, cur)}); value date "
                    f"{k.value_date} vs settlement {s.settlement_date} within window.")
            for extra in K:
                if extra is k:
                    continue
                key = f"B|BANK|{extra.record_id}"
                expl = (f"Bank line {extra.record_id} {_m(extra.amount, cur)} also references {s.batch_id}, which is "
                        f"already fully matched to {k.record_id}. Possible duplicate credit.")
                b.exc(ExceptionItem(key, "B", "DUPLICATE_REFERENCE_CREDIT", "B-X8_DUP_REF", "bank",
                                    extra.record_id, cur, extra.amount, extra.value_date, expl,
                                    k_evid([extra], "bank") + s_evid(s, "referenced")))
                b.state("B", "bank", extra.record_id, "DUPLICATE_REFERENCE_CREDIT", cur, extra.amount,
                        rule="B-X8_DUP_REF", case_key=key, detail=expl)
            return
        if len(exact) > 1:
            s_fail(s, "AMBIGUOUS_CANDIDATES", "B-X4_AMBIGUOUS", s.net,
                   f"{len(exact)} bank lines reference {s.batch_id} with the exact net {_m(s.net, cur)}; "
                   f"cannot tell which is the payout and which a duplicate.", K, "candidate", "AMBIGUOUS")
            return
        total = sum(k.amount for k in K)
        all_in = all(in_window(s, k) for k in K)
        if total == s.net and all_in and 1 < len(K) <= r.max_ref_group:
            b.group("B", "B2S_REF_SPLIT", cur,
                    [Member("L", "settlement", s.batch_id, s.net)] +
                    [Member("R", "bank", k.record_id, k.amount) for k in K],
                    f"B2S: {len(K)} bank lines all referencing {s.batch_id} sum exactly to settlement net "
                    f"{_m(s.net, cur)} ({' + '.join(fmt_minor(k.amount, cur) for k in K)}); all value dates in window.")
            return
        if total == s.net and not all_in:
            s_fail(s, "DATE_OUT_OF_WINDOW", "B-X9_DATE", s.net,
                   f"Referencing bank line(s) sum to the settlement net {_m(s.net, cur)} but value date(s) "
                   f"{', '.join(str(k.value_date) for k in K)} fall outside settlement date {s.settlement_date} "
                   f"-{r.b_window_before}/+{r.b_window_after}d.", K, "referencing_line")
            return
        same_sign = all((k.amount > 0) == (s.net > 0) for k in K)
        if same_sign and abs(total) < abs(s.net):
            s_fail(s, "PARTIAL_RECEIPT", "B-X10_PARTIAL", s.net - total,
                   f"Received {_m(total, cur)} across {len(K)} line(s) referencing {s.batch_id} against net "
                   f"{_m(s.net, cur)}; outstanding {_m(s.net - total, cur)}. Not matched until amounts conserve.",
                   K, "partial_receipt")
            return
        s_fail(s, "AMOUNT_MISMATCH", "B-X11_REF_AMOUNT", s.net - total,
               f"Bank line(s) referencing {s.batch_id} total {_m(total, cur)} but settlement net is {_m(s.net, cur)} "
               f"(difference {_m(s.net - total, cur)}).", K, "referencing_line")
        return
    if len(K) == 1:
        k = K[0]
        total = sum(s.net for s in S)
        all_in = all(in_window(s, k) for s in S)
        if total == k.amount and all_in and len(S) <= r.max_ref_group:
            b.group("B", "B2M_REF_MERGED", cur,
                    [Member("L", "settlement", s.batch_id, s.net) for s in S] +
                    [Member("R", "bank", k.record_id, k.amount)],
                    f"B2M: bank line {k.record_id} reference '{k.reference}' names {len(S)} batches whose nets sum "
                    f"exactly to {_m(k.amount, cur)} ({' + '.join(fmt_minor(s.net, cur) for s in S)}); all in window.")
            return
        for s in S:
            s_fail(s, "AMOUNT_MISMATCH" if total != k.amount else "DATE_OUT_OF_WINDOW", "B-X12_REF_GROUP",
                   total - k.amount,
                   f"Bank line {k.record_id} {_m(k.amount, cur)} references batches "
                   f"{', '.join(x.batch_id for x in S)} totalling {_m(total, cur)}"
                   f"{'' if all_in else ' (some outside date window)'}; not conserved/valid, so not matched.",
                   K, "referencing_line")
        return
    for s in S:
        s_fail(s, "COMPLEX_REFERENCE", "B-X13_COMPLEX", s.net,
               f"Many-to-many reference links between settlements {', '.join(x.batch_id for x in S)} and bank lines "
               f"{', '.join(k.record_id for k in K)}; requires manual review.", K, "referencing_line")


# ---------------------------------------------------------------- invariants

def _check_invariants(b: _Builder) -> None:
    seen: set[tuple[str, str, str]] = set()
    for g in b.groups:
        if g.total("L") != g.total("R"):
            raise EngineInvariantError(f"{g.group_id} not conserved")
        curs = set()
        for m in g.members:
            k = (g.chain, m.record_type, m.record_id)
            if k in seen:
                raise EngineInvariantError(f"record {k} consumed by more than one group")
            seen.add(k)
            curs.add(g.currency)
        if len(curs) != 1:
            raise EngineInvariantError(f"{g.group_id} mixes currencies")
