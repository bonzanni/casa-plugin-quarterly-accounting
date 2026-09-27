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


_Q_WORDS = re.compile(r"^(?:(?P<y1>\d{4})-q(?P<n1>[1-4])|q(?P<n2>[1-4])(?:\s+(?P<y2>\d{4}))?)$")


def normalize_quarter(text, today: str) -> str | None:
    """The quarter an operator's words name, in the canonical YYYY-Qn — or None
    when they name none. Accepted: "2026-Q3", "Q3 2026" and a bare "Q3" (case
    and surrounding spaces ignored). A bare quarter later than today's is last
    year's: "Q4" said in September means the Q4 that has happened (the reply
    grammar's rule, now shared with the tool layer; fix wave F)."""
    if not isinstance(text, str):
        return None
    m = _Q_WORDS.match(text.strip().lower())
    if m is None:
        return None
    if m.group("y1"):
        return f"{m.group('y1')}-Q{m.group('n1')}"
    n = int(m.group("n2"))
    if m.group("y2"):
        return f"{m.group('y2')}-Q{n}"
    year = int(today[:4])
    if f"{year}-Q{n}" > quarter_of(today):
        year -= 1
    return f"{year}-Q{n}"


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
