"""Counterparty knowledge base and the expectation mapping's overrides
(spec §"Document expectation", "Counterparty KB"). Read side here; the
write side (upsert_counterparty, set_expectation) is Task 10.

A pattern is the counterparty text exactly as bank-feed shows it
(`BCK*ZAPIER`); matching is exact after case and whitespace normalisation —
the `*` is part of the bank's text, never a wildcard, and there is no fuzzy
matching anywhere in this plugin."""
from __future__ import annotations

import json
import re


def norm(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def counterparty_for(conn, bank_counterparty):
    text = norm(bank_counterparty)
    if not text:
        return None
    for r in conn.execute("SELECT * FROM counterparties ORDER BY cp_id"):
        if text == norm(r["name"]) or text in {norm(p) for p in json.loads(r["patterns_json"])}:
            return r
    return None


def override_of(cp):
    if cp is None or cp["exp_kind"] is None:
        return None
    return (cp["exp_kind"], cp["exp_tier"] if cp["exp_kind"] != "none" else None)


def is_portal(cp) -> bool:
    return cp is not None and cp["source"] == "portal"


def parse_scope(scope: str) -> frozenset:
    return frozenset(t.strip().lower() for t in (scope or "").split(",") if t.strip())


def chain_overrides(conn) -> list:
    return [(frozenset(json.loads(r["rows_json"])), frozenset(json.loads(r["key_json"])),
             r["kind"], r["tier"])
            for r in conn.execute("SELECT * FROM chain_overrides ORDER BY scope")]


def display_name(conn, bank_counterparty) -> str:
    cp = counterparty_for(conn, bank_counterparty)
    return cp["name"] if cp is not None else (bank_counterparty or "Unknown payee")
