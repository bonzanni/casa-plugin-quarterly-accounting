"""The reducer — spec §Match records, "The reducer, as one total function".
PURE. Given a lineage's folded state, its live row facts and its derived
expectation, return the ONE desired owned-tag set, a machinery status and
the residue reasons. The desired-set table in §"The projection" is derived
from this order; where they could differ, this order wins."""
from __future__ import annotations

import json
from dataclasses import dataclass

import expectation as ex
import fold as F

OWNED = ("acct::matched", "acct::proposed", "acct::portal",
         "acct::no-document-expected", "acct::open")

# Material facts of a row (spec §Match records: account, direction, currency,
# amount_minor, status, booking date, counterparty, remittance, and
# bank-feed's review flags).
FACT_KEYS = ("account_id", "direction", "currency", "amount_minor", "status",
             "booking_date", "counterparty", "remittance", "needs_review", "review_reason")


def facts_of(row: dict) -> dict:
    out = {k: row.get(k) for k in FACT_KEYS}
    out["amount_minor"] = int(out["amount_minor"]) if out["amount_minor"] is not None else None
    out["needs_review"] = int(out["needs_review"] or 0)
    for k in ("review_reason", "counterparty", "remittance", "booking_date", "status"):
        out[k] = out[k] if out[k] not in ("",) else None
    return out


def digest(facts: dict) -> str:
    """The full sha256 of a row's canonical facts: what a listing hands out in place
    of the facts themselves (issue #3), compared exactly by record_match/propose_match."""
    import hashlib
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def fingerprint(facts: dict, kind: str | None) -> str:
    return json.dumps({"facts": facts, "kind": kind}, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Inputs:
    ended: str | None
    eligible: bool
    fold: F.FoldState
    facts: dict | None
    expectation: ex.Expectation
    last_known_kind: str | None
    doc_kinds: dict
    portal: bool


@dataclass(frozen=True)
class Reduction:
    desired: frozenset
    status: str
    current: int | None
    reasons: tuple


def _fp(cand: F.Cand) -> dict:
    return json.loads(cand.fp) if cand.fp else {"facts": None, "kind": None}


def _row_ok(cand: F.Cand, inp: Inputs) -> bool:
    return _fp(cand)["facts"] == inp.facts


def _with_portal(tags: set, inp: Inputs) -> frozenset:
    if inp.portal and inp.eligible:
        tags = tags | {"acct::portal"}
    return frozenset(tags)


def reduce(inp: Inputs) -> Reduction:
    exp = inp.expectation
    reasons: list[str] = []
    if inp.fold.conflicted_ids():
        reasons.append("conflicted")
    # step 0 — ended
    if inp.ended:
        return Reduction(frozenset(), "ended", None, tuple(reasons))
    # step 1 — eligibility
    if not inp.eligible:
        return Reduction(frozenset(), "ineligible", None, tuple(reasons))
    # step 2 — operator precedence over the folded state
    if inp.fold.exemption is not None:
        return Reduction(_with_portal({"acct::no-document-expected"}, inp), "exempt", None,
                         tuple(reasons))
    op = inp.fold.operator_current()
    if op is not None:
        # step 3 — validity of the current operator pairing
        ok = _row_ok(op, inp)
        if not ok:
            reasons.append("facts-changed")
        tag = "acct::matched" if ok else "acct::proposed"
        return Reduction(_with_portal({tag}, inp), "matched" if ok else "proposed",
                         op.match_id, tuple(reasons))
    # step 4 — machine candidates, judged as a set; conflicted is sticky
    ms = inp.fold.machine_set()
    if len(ms) == 1 and ms[0].state in F.ACTIVE:
        m = ms[0]
        # step 5 — validity of the current machine pairing
        row_ok = _row_ok(m, inp)
        if not row_ok:
            reasons.append("facts-changed")
        ok = m.state == "matched" and row_ok
        tag = "acct::matched" if ok else "acct::proposed"
        return Reduction(_with_portal({tag}, inp), "matched" if ok else "proposed",
                         m.match_id, tuple(reasons))
    if len(ms) > 1:
        # D3: a set of machine candidates (fold._normalize made them all conflicted) is one
        # proposal awaiting the operator's pick — "to confirm", never "missing"
        return Reduction(_with_portal({"acct::proposed"}, inp), "proposed", None,
                         tuple(reasons))
    # step 6 — expectation, for a lineage with no current pairing
    if exp.kind == "none":
        return Reduction(_with_portal({"acct::no-document-expected"}, inp), "no-document",
                         None, tuple(reasons))
    if exp.tier == "optional":
        return Reduction(_with_portal(set(), inp), "optional", None, tuple(reasons))
    # step 7 — otherwise open (required or unknown)
    return Reduction(_with_portal({"acct::open"}, inp), "open", None, tuple(reasons))


def apply_fixed_point(actual: set, desired: frozenset) -> set:
    """actual := (actual − owned_tags) ∪ desired (spec §The sweep step 4)."""
    return (set(actual) - set(OWNED)) | set(desired)
