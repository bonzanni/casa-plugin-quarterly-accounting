# server/fx.py
"""Issue #35: a foreign-currency document screened by the bank's own rate. bank-feed
0.22.0 (casa-specialist-finance#91) exports a payment's `exchange_rate` and its
`exchange_unit_currency` verbatim; the import keeps the pair only when both halves
validate (bank-feed's own rules), and nothing else here trusts them further."""
from __future__ import annotations

import decimal
import re

import amounts

RATE_RE = re.compile(r"^[0-9]{1,15}(?:\.[0-9]{1,20})?$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
TOLERANCE = decimal.Decimal("0.03")     # 3% of the expected amount (R3) ...
FLOOR_MINOR = 2                         # ... and never less than 0.02


def pair(rate, unit):
    """The (rate, unit) pair an export row carries, or None when either half is missing
    or invalid — a rate without its unit does not say which way it converts."""
    if not isinstance(rate, str) or not isinstance(unit, str):
        return None
    if not RATE_RE.match(rate) or not CURRENCY_RE.match(unit):
        return None
    if decimal.Decimal(rate) == 0:
        return None
    return rate, unit


def canonical(fx):
    """The pair compared as evidence (C1, Astra S1): the rate by its value, so `1.10` and
    `1.100` are the same rate; the stored rate stays verbatim."""
    if not fx:
        return None
    with decimal.localcontext() as ctx:
        ctx.prec = 80
        value = format(decimal.Decimal(fx["rate"]).normalize(), "f")
    return [value, fx["unit"]]


def expected(fx, pay_minor: int, pay_cur: str, doc_cur: str):
    """The document's amount, in its currency's minor units, that the bank's rate gives
    for the payment (R2), or None when there is no rate or it converts neither way."""
    if not fx or pay_minor is None or not pay_cur or not doc_cur or pay_cur == doc_cur:
        return None
    rate, unit = fx["rate"], fx["unit"]
    with decimal.localcontext() as ctx:
        ctx.prec = 80
        pay = decimal.Decimal(abs(int(pay_minor)))
        r = decimal.Decimal(rate)
        if unit == pay_cur:
            amount = pay * r
        elif unit == doc_cur:
            amount = pay / r
        else:
            return None
        return int(amount.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))


def screen(fx, pay_minor, pay_cur, doc_minor, doc_cur):
    """None when the document's amount can be the payment at the bank's rate (or there is
    nothing to screen by); else the sentence that says why it cannot (R3)."""
    want = expected(fx, pay_minor, pay_cur, doc_cur)
    if want is None or doc_minor is None:
        return None
    slack = max(FLOOR_MINOR, int((TOLERANCE * want).to_integral_value(decimal.ROUND_CEILING)))
    if abs(abs(int(doc_minor)) - want) <= slack:
        return None
    return (f"this {amounts.fmt(abs(int(doc_minor)), doc_cur)} document cannot be the "
            f"{amounts.fmt(abs(int(pay_minor)), pay_cur)} payment: at the bank's rate that "
            f"payment is {amounts.fmt(want, doc_cur)}")
