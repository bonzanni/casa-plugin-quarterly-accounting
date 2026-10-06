"""The pass marker, probes and the bank-write gate.

One Casa job run is one pass (simple loop, design rev 17 §2): job.claim starts it under
the claim's token (start_pass) and every later claim of the run moves the marker's
generation to its own token. The token is one integer drawn from one monotonic counter
(rotate), so no two holders ever share it. check_token refuses any write carrying a
token that is not the live one — including the writes that are not CAS'd on a match
record. It fences this store only; bank-feed's own writes are fenced by bank-feed's
workflow/expected_generation (the gate below says which).

Health is observed, never inferred: each probe is what the specialist actually saw this
pass, stored with its time (spec §Setup)."""
from __future__ import annotations

import datetime as _dt
import json
import re

import db
import version

LEASE_S = 600         # a staged send no turn settled within this is recovered (job.claim)
PROBE_KINDS = ("bank_tools", "bank_accounts", "bank_sync", "ledger", "gmail")


def _marker(conn):
    return conn.execute("SELECT * FROM pass_marker WHERE id=1").fetchone()


def current_pass(conn):
    m = _marker(conn)
    if m is None or not m["live"]:
        return None
    return conn.execute("SELECT * FROM passes WHERE pass_id=?", (m["pass_id"],)).fetchone()


def _age_s(started_at: str) -> float:
    started = _dt.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc)
    return (db._clock() - started).total_seconds()


def ago(at: str) -> str:
    """How long ago `at` was ("12 minutes ago")."""
    minutes = int(_age_s(at) // 60)
    return ("a minute ago" if minutes <= 1 else f"{minutes} minutes ago"
            if minutes < 90 else f"{round(minutes / 60)} hours ago")


def rotate(conn) -> int:
    """The next token: the one monotonic counter every token is drawn from (job.claim).
    Inside the caller's write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("a token is drawn inside the write transaction")
    conn.execute("UPDATE counters SET value = value + 1 WHERE name='pass_generation'")
    return conn.execute("SELECT value FROM counters WHERE name='pass_generation'").fetchone()[0]


def start_pass(conn, trigger: str, reply: str, *, token) -> tuple:
    """The run's live pass (the caller checked none is live), started under its claim's
    token (no rotation, S2 §3): its marker, with the lease cleared, and its row. Returns
    (token, pass_id). Inside the caller's write transaction (job.claim, through
    loop.start_pass)."""
    if token is None:
        raise RuntimeError("a job pass starts under its claim's token")
    if reply not in ("telegram", "silent"):
        raise RuntimeError("reply is 'telegram' or 'silent'")
    gen, pass_id = token, f"j{token}.{db.next_seq(conn)}"
    now = db.now()
    conn.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id, trigger,"
                 " started_at, lease_at) VALUES (1, ?, 1, ?, ?, ?, NULL)",
                 (gen, pass_id, trigger, now))
    conn.execute("INSERT INTO passes(pass_id, generation, trigger, started_at, reply, protocol)"
                 " VALUES (?, ?, ?, ?, ?, 'job')", (pass_id, gen, trigger, now, reply))
    return gen, pass_id


def protocol_of(conn, pass_id) -> str:
    """The pass's protocol: 'job' or 'delegation' (a missing row is 'delegation')."""
    row = conn.execute("SELECT protocol FROM passes WHERE pass_id=?", (pass_id,)).fetchone()
    return row[0] if row is not None and row[0] is not None else "delegation"


def check_token(conn, token) -> None:
    """A pass-only write's fence. Accepting a token inside a write transaction also
    renews its holder's lease: a holder that keeps writing is never claimed over."""
    if token is None:
        return
    m = _marker(conn)
    if m is None or not m["live"] or int(token) != m["generation"]:
        raise db.Refusal("this pass is no longer the current one (it has ended, or a newer "
                         "one took its place); stop — nothing was written")
    if conn.in_transaction:
        conn.execute("UPDATE pass_marker SET lease_at=? WHERE id=1", (db.now(),))


def record_probe(conn, token, kind: str, ok: bool, detail: str = "", data=None, *,
                 acq=None, absent=False) -> dict:
    """What the specialist saw, stored under the claim that saw it (`gen`, S2 §5.2). A
    `bank_sync` carries the acquisition it belongs to (`acq`); a `gmail` probe that
    failed because finance has no Gmail tools at all says `absent` (S2 §6.4); a `ledger`
    probe may carry `missing`, the workflows list_backups marks FILE MISSING (S2 §4)."""
    if kind not in PROBE_KINDS:
        raise db.Refusal(f"probe kind must be one of {', '.join(PROBE_KINDS)}")
    if absent:
        if kind != "gmail":
            raise db.Refusal("absent goes only with the gmail probe")
        if ok:
            raise db.Refusal("absent=true records that Gmail is not connected: pass ok=false")
        data = {**(data or {}), "absent": True}
    if acq is not None:
        if kind != "bank_sync":
            raise db.Refusal("acq goes only with the bank_sync probe")
        if isinstance(acq, bool) or not isinstance(acq, int):
            raise db.Refusal("acq is the number job_next handed out with the bank read")
        data = {**(data or {}), "acq": acq}
    if kind == "ledger" and data is not None and "missing" in data:
        missing = data["missing"]
        if not isinstance(missing, list) or not all(isinstance(w, str) for w in missing):
            raise db.Refusal("the ledger probe's missing is a list of workflow names")
    import binding
    with db.tx(conn):
        check_token(conn, token)
        m = _marker(conn)
        pass_id = m["pass_id"] if m and m["live"] else None
        prev = conn.execute("SELECT * FROM probes WHERE kind=?", (kind,)).fetchone()
        now = db.now()
        failing_since = None
        if not ok:
            # an occurrence id, not only a time: two failures starting within one second
            # are still two occurrences (spec: "a condition that clears and recurs is new")
            failing_since = prev["failing_since"] if (prev and not prev["ok"]
                                                      and prev["failing_since"]) \
                else f"{now}#{db.next_seq(conn)}"
        # D10: the Gmail streak counts RUNS (one pass each), not probes — a failure in a run
        # whose pass already recorded one does not count again; a success ends the streak
        fail_runs = 0
        if not ok:
            fail_runs = (prev["fail_runs"] if prev is not None and not prev["ok"] else 0)
            if prev is None or prev["ok"] or prev["pass_id"] != pass_id:
                fail_runs += 1
        conn.execute("INSERT OR REPLACE INTO probes(kind, ok, detail, data_json, observed_at,"
                     " pass_id, failing_since, gen, fail_runs) VALUES (?,?,?,?,?,?,?,?,?)",
                     (kind, 1 if ok else 0, detail, db.canonical(data) if data is not None
                      else None, now, pass_id, failing_since,
                      int(token) if token is not None else None, fail_runs))
        if kind == "bank_accounts" and ok and data is not None:
            accounts = data.get("accounts") or []
            b = binding.get(conn)
            if b is None:
                company = [a for a in accounts if a.get("category") == "company"]
                if len(company) == 1:
                    binding._bind(conn, company[0]["account_id"], company[0].get("label") or "")
                    b = binding.get(conn)
            if b is not None and pass_id and any(a.get("account_id") == b["account_id"]
                                                 for a in accounts):
                conn.execute("UPDATE passes SET account_seen=1 WHERE pass_id=?", (pass_id,))
            if b is not None:
                present = any(a.get("account_id") == b["account_id"] for a in accounts)
                prev_b = conn.execute("SELECT * FROM probes WHERE kind='bound_account'").fetchone()
                since = None
                if not present:
                    since = (prev_b["failing_since"] if prev_b is not None and not prev_b["ok"]
                             and prev_b["failing_since"] else f"{now}#{db.next_seq(conn)}")
                conn.execute("INSERT OR REPLACE INTO probes(kind, ok, detail, data_json,"
                             " observed_at, pass_id, failing_since) VALUES"
                             " ('bound_account', ?, '', NULL, ?, ?, ?)",
                             (1 if present else 0, now, pass_id, since))
        return {"recorded": kind, "ok": bool(ok), "observed_at": now}


def store_populated(conn) -> bool:
    """Lineage state only. Filed documents say nothing about which ledger this
    store runs against, so they never change the gate's verdict (round p2)."""
    return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM projections)"
                             " OR EXISTS (SELECT 1 FROM log)").fetchone()[0])


def _write(conn, sql, args=()) -> None:
    """Callable inside a write transaction (an import) or
    outside one (check_setup)."""
    if conn.in_transaction:
        conn.execute(sql, args)
    else:
        with db.tx(conn):
            conn.execute(sql, args)


def bank_write_gate(conn) -> dict:
    """May this pass write to bank-feed, and with which expected_generation?
    Computed here so the skill obeys one answer (plan §D11).

    The verdict is decided ONCE per pass, from that pass's own ledger probe,
    and a refusal is sticky for the rest of the pass (round p1, Astra S1: a
    refused fresh store that then imported a snapshot became "populated" and
    the next call allowed the writes the first had refused). The import is
    refused while the gate refuses, so the condition cannot erase itself
    across passes either.

    The read of the stored verdict, the decision and its persistence run under
    ONE write lock — the caller's transaction when one is open (an import),
    else a transaction of its own (check_setup, or ledger.py before it opens
    its import transaction). Otherwise a decision computed before a concurrent
    poison() committed was persisted after it, overwriting the pass's refusal
    with an allow (fix wave B, Astra S1, reproduced across processes)."""
    if conn.in_transaction:
        return _gate_in_tx(conn)
    with db.tx(conn):
        return _gate_in_tx(conn)


def _gate_in_tx(conn) -> dict:
    cur = current_pass(conn)
    if cur is not None and cur["gate_json"]:
        return json.loads(cur["gate_json"])
    out = _decide_gate(conn)
    probe = conn.execute("SELECT pass_id FROM probes WHERE kind='ledger'").fetchone()
    if cur is not None and probe is not None and probe["pass_id"] == cur["pass_id"]:
        conn.execute("UPDATE passes SET gate_json=? WHERE pass_id=?",
                     (db.canonical(out), cur["pass_id"]))
    return out


def poison(conn, reason: str) -> None:
    """Refuse every further bank-feed write in this pass.

    Called from inside the caller's own open write transaction, immediately
    before the caller raises Refusal — so whatever that transaction already
    wrote is about to be discarded anyway. poison ROLLS BACK that transaction
    itself (never COMMITs it: the caller's partial writes must not land),
    writes and commits the verdict in its OWN short transaction, then re-opens
    BEGIN IMMEDIATE — through the bounded db._retry_locked, so contention
    surfaces as Busy rather than hanging — so the caller's enclosing
    `with db.tx(conn):` still finds a transaction open when it unwinds and its
    own ROLLBACK does not itself raise (fix round 1 finding: a raw re-BEGIN
    whose failure left no transaction open made that ROLLBACK error with
    "cannot rollback - no transaction is active", masking the real one)."""
    cur = current_pass(conn)
    if cur is None:
        return
    pass_id = cur["pass_id"]
    # the token the caller's transaction checked: the live generation right now
    checked = _marker(conn)["generation"]
    verdict = db.canonical({"allowed": False, "reason": reason, "expected_generation": None,
                            "expected_ledger": None, "workflow": version.WORKFLOW,
                            "install_backup": None, "older_workflows": []})
    conn.execute("ROLLBACK")
    with db.tx(conn):
        # A write outside the transaction that checked the token re-validates it: a
        # caller superseded in between (a claim rotated the token) poisons nothing —
        # the live holder's pass keeps its import. The caller still raises its Refusal.
        m = _marker(conn)
        if m is not None and m["live"] and m["generation"] == checked \
                and m["pass_id"] == pass_id:
            conn.execute("UPDATE passes SET gate_json=?, snapshot_id=NULL WHERE pass_id=?",
                         (verdict, pass_id))
    db._retry_locked(lambda: conn.execute("BEGIN IMMEDIATE"), db.LOCK_BOUND_S)


def remember_ledger(conn, pass_id) -> None:
    """Called ONLY by an import whose export named the bound ledger instance
    (or bound a fresh store to it), inside its transaction — never at gate
    time, where an unproven ledger could make itself remembered (round p2).
    Records the ledger instance and the restore generation this store now runs
    against (plan §D4)."""
    probe = conn.execute("SELECT data_json FROM probes WHERE kind='ledger'").fetchone()
    data = json.loads(probe["data_json"] or "{}")
    conn.execute("UPDATE binding SET ledger_generation=?, ledger_instance=? WHERE id=1",
                 (int(data.get("generation", -1)), data.get("instance")))


LEDGER_RE = re.compile(r"^[0-9a-f]{32}$")     # bank-feed's LEDGER_RE (#69), parity-tested


def _decide_gate(conn) -> dict:
    import binding
    out = {"allowed": False, "reason": None, "expected_generation": None,
           "expected_ledger": None, "workflow": version.WORKFLOW, "install_backup": None,
           "older_workflows": []}
    b = binding.get(conn)
    if b is None:
        out["reason"] = "no account is bound yet"
        return out
    m = _marker(conn)
    probe = conn.execute("SELECT * FROM probes WHERE kind='ledger'").fetchone()
    if (m is None or not m["live"] or probe is None or probe["pass_id"] != m["pass_id"]
            or not probe["ok"]):
        out["reason"] = "the ledger's backup state was not read this pass (list_backups)"
        return out
    data = json.loads(probe["data_json"] or "{}")
    gen = int(data.get("generation", -1))
    instance = data.get("instance")
    if not isinstance(instance, str) or not LEDGER_RE.match(instance):
        out["reason"] = ("bank-feed reports no ledger instance id: it is below this plugin's "
                         "floor (bank-feed 0.20.0)")
        return out
    registered = dict(data.get("registered") or {})
    out["install_backup"] = registered.get(version.WORKFLOW)
    out["older_workflows"] = sorted(w for w in registered
                                    if w.startswith("acct@") and w != version.WORKFLOW)
    # A refusal persists ACROSS passes until the ledger condition that caused it
    # clears (round p2, Astra S1: filing a document between passes had flipped
    # "fresh" to "populated" and lifted a dirty-ledger refusal without a restore).
    prior = conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
    ack = bool(b["ledger_reset_ack"])
    if prior is not None:
        pr = json.loads(prior[0])
        if pr["kind"] == "restored" or (
                pr["kind"] == "dirty-ledger" and gen == pr["generation"]
                and registered.get(version.WORKFLOW) == pr["backup"]) or (
                pr["kind"] == "other-ledger" and instance == pr["instance"] and not ack):
            out["reason"] = pr["reason"]
            return out
        _write(conn, "DELETE FROM meta WHERE key='gate_refusal'")
    populated = store_populated(conn)
    remembered = b["ledger_generation"]
    bound = b["ledger_instance"]
    refusal = None
    if bound is not None and instance != bound:
        # A different ledger instance: another file, or this one erased and
        # re-minted (delete_all_data, delete_data_keep_signins). The two cannot be
        # told apart from outside, so only the operator's sentence re-binds (D4).
        if not ack:
            refusal = {"kind": "other-ledger", "instance": instance,
                       "reason": ("the bank ledger is not the one this store was built on "
                                  f"(instance {instance[:8]}…, bound to {bound[:8]}…). If it "
                                  "was wiped on purpose, the operator says \"the bank ledger "
                                  "was reset\"")}
    elif populated and remembered is not None and gen != remembered:
        refusal = {"kind": "restored",
                   "reason": ("the ledger was restored since this store last ran "
                              f"(restore generation {remembered} → {gen}) — reset the "
                              "accounting store (reset_store) before anything is written")}
    elif not populated and version.WORKFLOW in registered:
        refusal = {"kind": "dirty-ledger", "generation": gen,
                   "backup": registered[version.WORKFLOW],
                   "reason": (f"the ledger still carries writes from {version.WORKFLOW} after "
                              f"its restore point — restore backup "
                              f"{registered[version.WORKFLOW]} first")}
    if refusal is not None:
        _write(conn, "INSERT OR REPLACE INTO meta(key, value) VALUES ('gate_refusal', ?)",
               (db.canonical(refusal),))
        out["reason"] = refusal["reason"]
        return out
    out.update(allowed=True, expected_generation=gen, expected_ledger=instance)
    return out
