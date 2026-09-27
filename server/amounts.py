"""Money as bank-feed stores it: integer minor units, sign in `direction`.
Two decimal places (every currency this ledger has held is EUR; a
three-decimal currency would need an exponent table — not assumed)."""
from __future__ import annotations


def fmt(minor: int, currency: str) -> str:
    sign = "-" if minor < 0 else ""
    m = abs(int(minor))
    return f"{currency} {sign}{m // 100:,}.{m % 100:02d}"
