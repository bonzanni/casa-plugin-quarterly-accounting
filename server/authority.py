"""S7 §8.1: operator authority — an operator-authored lineage entry (pair, unpair, lift,
exempt), an operator expectation, the business-account binding, and the store-wide operator
settings (watermark, stop chasing, package name, the ledger-reset word) — is written only
under a grant. An OperatorGrant is constructed only by a keyed handler (taps.verdict,
taps.apply_reading, taps.bind_account) after its key check; a public tool never constructs
one (a grep pin). A Rehearsal lets propose_reading run the grammar's writes to learn what
they would do, inside a savepoint that is always rolled back: nothing it writes survives."""
from __future__ import annotations

import contextlib

import db

TAP_ONLY = ("that is the operator's decision: it is made with a button they tap, never by a "
            "tool call — show them the view, or read their words with propose_reading")


class OperatorGrant:
    __slots__ = ("handler", "key")

    def __init__(self, handler: str, key: str):
        if handler not in ("verdict", "apply_reading", "bind_account"):
            raise ValueError(handler)
        self.handler, self.key = handler, key


class Rehearsal:
    __slots__ = ("conn", "active")

    def __init__(self, conn):
        self.conn, self.active = conn, True


@contextlib.contextmanager
def rehearsal(conn):
    assert conn.in_transaction, "a rehearsal runs inside the caller's write transaction"
    conn.execute("SAVEPOINT rehearsal")
    r = Rehearsal(conn)
    try:
        yield r
    finally:
        r.active = False
        conn.execute("ROLLBACK TO rehearsal")
        conn.execute("RELEASE rehearsal")


def require(conn, grant) -> None:
    if isinstance(grant, OperatorGrant):
        return
    if isinstance(grant, Rehearsal) and grant.active and grant.conn is conn:
        return
    raise db.Refusal(TAP_ONLY)
