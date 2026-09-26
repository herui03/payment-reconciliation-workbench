"""Money handling: Decimal parsing into integer minor units. Floats are never used."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# ISO 4217 minor-unit exponents for the currencies in scope.
CURRENCY_EXPONENT = {"SGD": 2, "USD": 2, "EUR": 2}
SUPPORTED_CURRENCIES = tuple(sorted(CURRENCY_EXPONENT))

_AMOUNT_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
# Sanity ceiling: 1 trillion major units. Keeps every sum far inside SQLite's signed 64-bit integer range.
MAX_ABS_MAJOR = 10 ** 12


class AmountError(ValueError):
    pass


def parse_amount(text: str, currency: str) -> int:
    """Parse a plain decimal string (e.g. '-1000.50') into integer minor units.

    Rejects thousands separators, currency symbols, exponents, blanks and any precision beyond the
    currency's minor unit (e.g. '10.005' for SGD) instead of rounding it away.
    """
    if currency not in CURRENCY_EXPONENT:
        raise AmountError(f"unsupported currency {currency!r}")
    s = (text or "").strip()
    if not _AMOUNT_RE.fullmatch(s):
        raise AmountError(f"not a plain decimal amount: {text!r}")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise AmountError(f"not a decimal: {text!r}") from exc
    if abs(d) >= MAX_ABS_MAJOR:
        raise AmountError(f"{text[:40]!r} exceeds the sanity limit of {MAX_ABS_MAJOR:,} major units")
    exp = CURRENCY_EXPONENT[currency]
    frac = s.split(".")[1] if "." in s else ""
    if len(frac.rstrip("0")) > exp:
        raise AmountError(f"{text!r} has more than {exp} decimal places for {currency}; not rounded")
    scaled = d.scaleb(exp)
    return int(scaled)


def fmt_minor(minor: int | None, currency: str | None = None, sign: bool = False) -> str:
    """Format integer minor units as a decimal string with thousands separators (display only)."""
    if minor is None:
        return ""
    exp = CURRENCY_EXPONENT.get(currency or "", 2)
    neg = minor < 0
    q, r = divmod(abs(minor), 10 ** exp)
    body = f"{q:,}.{r:0{exp}d}" if exp else f"{q:,}"
    prefix = "-" if neg else ("+" if sign and minor > 0 else "")
    return f"{prefix}{body}"


def to_plain(minor: int, currency: str) -> str:
    """Plain machine-readable decimal string (no separators), for CSV export."""
    exp = CURRENCY_EXPONENT.get(currency, 2)
    neg = minor < 0
    q, r = divmod(abs(minor), 10 ** exp)
    return f"{'-' if neg else ''}{q}.{r:0{exp}d}"
