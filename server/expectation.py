"""Document expectation — spec §"Document expectation". PURE: no store, no
I/O. One decision procedure, direction-aware, first rule that applies; the
spec's table (rows 1–13) is the oracle and tests/test_expectation.py encodes
it row by row. This plugin classifies nothing: it reads the classifier's
tags and derives what document, if any, the transaction needs."""
from __future__ import annotations

from dataclasses import dataclass

KINDS = ("invoice", "sales-invoice", "credit-note", "payslip", "statement", "receipt")
DOC_KINDS = KINDS + ("other",)
TIERS = ("required", "optional")

# bank-feed rules.py: WORKFLOW_TAGS and NAMESPACE_SEP (parity-tested).
WORKFLOW_TAGS = ("awaiting-operator", "unclassifiable")
NAMESPACE_SEP = "::"

# Flow corrections that decide a row on their own (rows 6–8). Two of them on
# one row is a classification conflict (row 5). `fees` is a chain marker
# here (row 10), as the spec's rows 5 and 10 use it (plan §D7).
FLOW = {"internal-transfer": 6, "cash-withdrawal": 6, "refund": 7, "reimbursement": 8}
DBIT_PAYROLL = frozenset({"salary", "payroll"})              # row 9
DBIT_STATEMENT = frozenset({"fees", "interest", "tax"})      # row 10
CRDT_NO_DOCUMENT = frozenset({"interest", "dividend"})       # row 12

# The shipped mapping (rows 6–13). Defaults err toward required.
DEFAULTS = {
    6: ("none", None),
    7: ("credit-note", "required"),
    8: ("receipt", "optional"),
    9: ("payslip", "optional"),
    10: ("statement", "optional"),
    11: ("invoice", "required"),
    12: ("none", None),
    13: ("sales-invoice", "required"),
}


@dataclass(frozen=True)
class Expectation:
    kind: str | None          # a KINDS member, "none", or None when unknown
    tier: str | None          # "required" | "optional"; None for "none"; "required" when unknown
    row: int                  # the decision-table row that produced it
    conflict: bool = False    # row 5

    @property
    def unknown(self) -> bool:
        return self.kind is None

    @property
    def seeks_document(self) -> bool:
        return self.kind in KINDS


def is_classification_tag(tag: str) -> bool:
    return NAMESPACE_SEP not in tag and tag not in WORKFLOW_TAGS


def classification_state(tags) -> str:
    """bank-feed rules.classification_state: terminal > parked > classified >
    workable. Parity-tested against the vendored real one."""
    tags = set(tags)
    if "unclassifiable" in tags:
        return "terminal"
    if "awaiting-operator" in tags:
        return "parked"
    if any(is_classification_tag(t) for t in tags):
        return "classified"
    return "workable"


def decisive(tags, direction: str):
    """(row, key) that `tags` select among rows 6–13, or None for a conflict
    (row 5). The key is what an override must be a subset of (plan §D6)."""
    tags = frozenset(t for t in tags if is_classification_tag(t))
    flows = sorted(t for t in tags if t in FLOW)
    if len(flows) > 1:
        return None
    if flows:
        return FLOW[flows[0]], frozenset(flows)
    if direction == "DBIT":
        pay, stmt = tags & DBIT_PAYROLL, tags & DBIT_STATEMENT
        if pay and stmt:
            return None
        if pay:
            return 9, pay
        if stmt:
            return 10, stmt
        return 11, tags
    nodoc = tags & CRDT_NO_DOCUMENT
    if nodoc:
        return 12, nodoc
    return 13, tags


def _make(kind: str, tier: str | None, row: int) -> Expectation:
    return Expectation(kind, None if kind == "none" else tier, row)


def derive(direction: str, tags, *, exempt: bool = False,
           counterparty_override=None, chain_overrides=()) -> Expectation:
    if exempt:                                                   # row 1
        return Expectation("none", None, 1)
    if counterparty_override is not None:                        # row 2
        kind, tier = counterparty_override
        return _make(kind, tier, 2)
    state = classification_state(tags)
    if state == "terminal":                                      # row 3
        return Expectation("invoice" if direction == "DBIT" else "sales-invoice",
                           "required", 3)
    if state in ("parked", "workable"):                          # row 4
        return Expectation(None, "required", 4)
    chosen = decisive(tags, direction)
    if chosen is None:                                           # row 5
        return Expectation(None, "required", 5, conflict=True)
    row, key = chosen
    best = None
    for rows, okey, kind, tier in chain_overrides:
        if row not in rows or not okey <= key:
            continue
        rank = (len(okey), tuple(sorted(okey)))
        if best is None or rank > best[0]:
            best = (rank, kind, tier)
    kind, tier = (best[1], best[2]) if best else DEFAULTS[row]
    return _make(kind, tier, row)


KEYED_ROWS = (6, 7, 8, 9, 10, 12)


def normalize_scope(scope_tags) -> tuple | None:
    """Fix, ONCE, where an override written for `scope_tags` applies: the rows
    it decides at, and the key a row's own key must contain. Computed when the
    override is set, never re-derived per row (round p1: re-deriving "salary"
    for a CRDT landed it on row 13 and silenced every sales invoice for
    `income, salary`). A scope whose tags select a keyed row (a flow
    correction, a payroll/statement/no-document marker) applies there only;
    a plain chain applies to "anything else" in both directions (rows 11, 13).
    None when the scope's own tags conflict."""
    picks = [decisive(scope_tags, d) for d in ("DBIT", "CRDT")]
    if any(p is None for p in picks):
        return None
    keyed = [(r, k) for r, k in picks if r in KEYED_ROWS]
    if keyed:
        return frozenset(r for r, _ in keyed), keyed[0][1]
    return frozenset({11, 13}), picks[0][1]
