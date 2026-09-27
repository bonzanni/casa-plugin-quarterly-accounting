"""The lineage decision log and its fold — spec §Match records. PURE.

A lineage's match-record state is the fold of its log in store-wide
sequence order, from the empty state, through ONE transition table (the
spec's reducer step 2). The log holds writer entries (pair, propose, unpair,
exempt, lift) and the retirements the store recorded (`retire X@a -> state`,
bound to the ACTIVATION a it retires). The fold never decides anything the
log does not say: it reports, in `produced`, every retirement a transition
caused, and the store records the new ones so a re-fold can never undo them.

Rules the tests pin (each a reviewed finding):
- a retirement is monotone per activation and ignored for a later one;
- a retired pairing never returns on its own;
- clearing an exemption is derived (operator pair with a higher sequence),
  never recorded;
- machine-set normalization runs inside the transition that adds a machine
  candidate, not after the fold;
- `resolves=` rejects the named ids unconditionally at replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ACTIVE = ("matched", "proposed")


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str                       # pair | propose | unpair | exempt | lift | retire
    author: str                     # operator | auto | store
    match_id: int | None = None
    doc_id: int | None = None
    fp: str | None = None           # canonical JSON of the fingerprint it was made against
    resolves: tuple = ()
    retire_activation: int | None = None
    retire_to: str | None = None    # conflicted | rejected
    cause: str | None = None


@dataclass
class Cand:
    match_id: int
    doc_id: int
    author: str                     # operator | auto (of the CURRENT activation)
    state: str                      # matched | proposed | conflicted | rejected
    activation: int                 # sequence of the writer entry that activated it
    fp: str | None


@dataclass(frozen=True)
class Retirement:
    match_id: int
    activation: int
    to: str
    cause: str


@dataclass
class FoldState:
    exemption: int | None = None
    cands: dict = field(default_factory=dict)
    produced: list = field(default_factory=list)

    def active(self) -> list:
        return [c for c in self.cands.values() if c.state in ACTIVE]

    def operator_current(self):
        ops = [c for c in self.active() if c.author == "operator"]
        return ops[0] if ops else None

    def machine_set(self) -> list:
        return sorted((c for c in self.cands.values()
                       if c.author == "auto" and c.state in ACTIVE + ("conflicted",)),
                      key=lambda c: c.activation)

    def conflicted_ids(self) -> set:
        return {c.match_id for c in self.cands.values() if c.state == "conflicted"}


def _retire(st: FoldState, c: Cand, to: str, cause: str) -> None:
    if c.state == to or c.state == "rejected":
        return
    c.state = to
    st.produced.append(Retirement(c.match_id, c.activation, to, cause))


def _normalize(st: FoldState) -> None:
    ms = st.machine_set()
    if len(ms) > 1:
        for c in ms:
            _retire(st, c, "conflicted", "collision")


def _step(st: FoldState, e: Entry, occupied) -> None:
    if e.kind == "pair" and e.author == "operator":
        if st.exemption is not None and st.exemption < e.seq:
            st.exemption = None                                  # derived, never recorded
        p = Cand(e.match_id, e.doc_id, "operator", "matched", e.seq, e.fp)
        st.cands[e.match_id] = p
        if occupied(e.doc_id, e.match_id):
            _retire(st, p, "conflicted", "occupied")
        for c in list(st.cands.values()):
            if c is not p and c.state in ACTIVE + ("conflicted",):
                _retire(st, c, "conflicted", "operator-pair")
    elif e.kind in ("pair", "propose") and e.author == "auto":
        for mid in e.resolves:
            named = st.cands.get(mid)
            if named is not None:
                named.state = "rejected"                          # the writer's own decision
        # Operator precedence is judged BEFORE the machine entry touches any
        # candidate (round p1, Astra S1): a machine entry naming the pairing
        # the operator holds would otherwise replace the operator's activation.
        op = st.operator_current()
        if op is not None and op.match_id == e.match_id:
            return                    # the operator's pairing of this document stands, unchanged
        m = Cand(e.match_id, e.doc_id, "auto",
                 "matched" if e.kind == "pair" else "proposed", e.seq, e.fp)
        st.cands[e.match_id] = m
        if st.exemption is not None:
            _retire(st, m, "rejected", "exempt")
        elif op is not None:
            _retire(st, m, "conflicted", "operator-current")
        else:
            if occupied(e.doc_id, e.match_id):
                _retire(st, m, "conflicted", "occupied")
            _normalize(st)
    elif e.kind == "unpair":
        c = st.cands.get(e.match_id)
        if c is not None:
            c.state = "rejected"
    elif e.kind == "exempt":
        for c in list(st.cands.values()):
            if c.state in ACTIVE + ("conflicted",):
                _retire(st, c, "rejected", "exempt")
        st.exemption = e.seq
    elif e.kind == "lift":
        st.exemption = None
    elif e.kind == "retire":
        c = st.cands.get(e.match_id)
        if c is None or c.activation != e.retire_activation:
            return                                                # bound to an older activation
        if c.state == "rejected" or c.state == e.retire_to:
            return
        c.state = e.retire_to                                     # already recorded: not re-produced
    else:
        raise ValueError(f"unknown log entry kind {e.kind!r}/{e.author!r}")


def fold(entries, occupied=lambda doc_id, match_id: False) -> FoldState:
    st = FoldState()
    for e in sorted(entries, key=lambda e: e.seq):
        _step(st, e, occupied)
    return st
