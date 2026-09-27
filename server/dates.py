"""Quarters and dates. The quarter identifier is YYYY-Qn everywhere (spec
§Data model). A row's effective date is its booking_date, or its value_date
while it is pending and has none (spec §"The projection", admission)."""
from __future__ import annotations

import datetime as _dt
import re

_Q_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def today() -> str:
    return _today().isoformat()


def parse_day(s: str) -> _dt.date:
    return _dt.date.fromisoformat(str(s)[:10])


def quarter_of(day: str) -> str:
    d = parse_day(day)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def parse_quarter(q: str) -> tuple[int, int]:
    m = _Q_RE.match(q or "")
    if not m:
        raise ValueError(f"a quarter is written YYYY-Qn, not {q!r}")
    return int(m.group(1)), int(m.group(2))


def quarter_bounds(q: str) -> tuple[str, str]:
    year, n = parse_quarter(q)
    start = _dt.date(year, 3 * (n - 1) + 1, 1)
    end = _dt.date(year + 1, 1, 1) if n == 4 else _dt.date(year, 3 * n + 1, 1)
    return start.isoformat(), end.isoformat()


def quarter_start(day: str) -> str:
    return quarter_bounds(quarter_of(day))[0]


def effective_date(row: dict) -> str | None:
    return (row.get("booking_date") or row.get("value_date") or None)


def is_partial(q: str, today_iso: str) -> bool:
    return today_iso < quarter_bounds(q)[1]


def short_day(day: str) -> str:
    d = parse_day(day)
    return f"{d.day} {_MONTHS[d.month - 1]}"


def quarter_label(q: str) -> str:
    year, n = parse_quarter(q)
    return f"Q{n} {year}"
