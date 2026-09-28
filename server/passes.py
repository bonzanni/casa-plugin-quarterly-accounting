"""The pass marker, probes and the bank-write gate.

A pass takes a marker; a second pass finding a live one does not duplicate
the work. A marker older than STALE_AFTER_S is a dead process and is
reclaimed: the displaced pass is ended `interrupted` and its package request
recovered. EVERY begin bumps the generation, and so does every claim that
continues a pass across turns (steps.claim) and every hand-over of a package
request (end_pass): the token is one integer drawn from that one monotonic
counter, so no two holders ever share it. check_token refuses any write
carrying a token that is not the live one — including the writes that are
not CAS'd on a match record (spec §"Running the pass on demand"). It fences
this store only; bank-feed's own writes are fenced by bank-feed's
workflow/expected_generation (the gate below says which).

Health is observed, never inferred: each probe is what the specialist or
Ellen actually saw this pass, stored with its time (spec §Setup)."""
from __future__ import annotations

import datetime as _dt
import json
import re

import db
import version

STALE_AFTER_S = 3 * 3600
OUTCOMES = ("complete", "interrupted", "stopped", "failed")
REPLIES = ("telegram", "silent")
OPEN_REQUEST = ("snapshot-done", "built", "staged")    # a package request a token may act on
BUSY = "A check is running — started {when}.\nAsk again in a few minutes."
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


def rotate(conn) -> int:
    """The next token: the one monotonic counter every token is drawn from
    (begin_pass, a claim, a package hand-over). Inside the caller's write
    transaction."""
    assert conn.in_transaction, "a token is drawn inside the write transaction"
    conn.execute("UPDATE counters SET value = value + 1 WHERE name='pass_generation'")
    return conn.execute("SELECT value FROM counters WHERE name='pass_generation'").fetchone()[0]


def begin_pass(conn, trigger: str, reply=None) -> dict:
    if reply is None:
        reply = "silent" if trigger == "cron" else "telegram"
    if reply not in REPLIES:
        raise db.Refusal("reply is 'telegram' or 'silent'")
    with db.tx(conn):
        m = _marker(conn)
        reclaimed, recovered = False, None
        if m is not None and m["live"]:
            age = _age_s(m["started_at"])
            if age < STALE_AFTER_S:
                minutes = int(age // 60)
                when = "a minute ago" if minutes <= 1 else f"{minutes} minutes ago"
                return {"status": "busy", "started_at": m["started_at"],
                        "text": BUSY.format(when=when)}
            reclaimed = True
            recovered = _terminalize(conn, m["pass_id"])
        gen = rotate(conn)
        now = db.now()
        pass_id = f"p{gen}"
        # the claim columns are cleared with every new marker
        conn.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id, trigger,"
                     " started_at, claimed_step, lease_at) VALUES (1, ?, 1, ?, ?, ?, NULL, NULL)",
                     (gen, pass_id, trigger, now))
        conn.execute("INSERT INTO passes(pass_id, generation, trigger, started_at, reply)"
                     " VALUES (?, ?, ?, ?, ?)", (pass_id, gen, trigger, now, reply))
        return {"status": "started", "pass_token": gen, "pass_id": pass_id,
                "reclaimed": reclaimed, "recovered": recovered}


def _terminalize(conn, pass_id: str):
    """A reclaimed pass is over: it is ended `interrupted` (so it no longer looks
    unended, and check_setup's last_pass shows it), and its package request is
    recovered — buildable when its snapshot step finished, else failed with a
    package notice. Returns the recovered request's id, or None."""
    import alerts
    now = db.now()
    report = {"reclaimed": True, **throughput(conn, pass_id)}
    conn.execute("UPDATE passes SET ended_at=?, outcome='interrupted', report_json=?"
                 " WHERE pass_id=? AND ended_at IS NULL", (now, db.canonical(report), pass_id))
    req = conn.execute("SELECT * FROM package_requests WHERE pass_id=? AND state='snapshot'",
                       (pass_id,)).fetchone()
    if req is None:
        return None
    step = conn.execute("SELECT finished_at, finish_json FROM pass_steps WHERE pass_id=? AND"
                        " step='snapshot'", (pass_id,)).fetchone()
    ok = step is not None and step["finished_at"] is not None and \
        not json.loads(step["finish_json"] or "{}").get("failed")
    if ok:
        conn.execute("UPDATE package_requests SET state='snapshot-done', token=NULL,"
                     " lease_at=NULL, pass_outcome='interrupted', updated_at=? WHERE request_id=?",
                     (now, req["request_id"]))
    else:
        conn.execute("UPDATE package_requests SET state='recovery-failed', token=NULL,"
                     " pass_outcome='interrupted', updated_at=? WHERE request_id=?",
                     (now, req["request_id"]))
        alerts.raise_package(conn, "package-failed", f"request:{req['request_id']}:failed",
                             quarter=req["quarter"])
    return req["request_id"]


def check_token(conn, token) -> None:
    """A pass-only write's fence. Accepting a token inside a write transaction also
    renews its holder's lease: a holder that keeps writing is never claimed over."""
    if token is None:
        return
    m = _marker(conn)
    if m is None or not m["live"] or int(token) != m["generation"]:
        raise db.Refusal("this pass is no longer the current one (another turn continued it, "
                         "a newer pass reclaimed its marker, or the store was reset); stop — "
                         "nothing was written")
    if conn.in_transaction:
        conn.execute("UPDATE pass_marker SET lease_at=? WHERE id=1", (db.now(),))


def open_request(conn, request_id):
    return conn.execute("SELECT * FROM package_requests WHERE request_id=?",
                        (request_id,)).fetchone()


def check_package_token(conn, request_id, token):
    """A package request's fence: the token end_pass or continue_pass handed over,
    and the request still open. Accepting it inside a write transaction renews the
    request's lease. Returns the request row."""
    req = open_request(conn, request_id)
    if token is None:
        raise db.Refusal("this package belongs to a package request: pass the package_token "
                         "end_pass or continue_pass gave you")
    if req is None or req["token"] is None or int(token) != req["token"] \
            or req["state"] not in OPEN_REQUEST:
        raise db.Refusal("this package request has been taken over by a later turn — stop, "
                         "nothing was written")
    if conn.in_transaction:
        conn.execute("UPDATE package_requests SET lease_at=? WHERE request_id=?",
                     (db.now(), request_id))
    return req


def throughput(conn, pass_id: str) -> dict:
    """What this pass's sweep got through (fix wave F): the lineages read since an
    import THIS pass made (a read stamps the latest import, and only this pass's
    reads can stamp one of its own imports), and what the cycle still owes —
    None when the pass imported nothing. The first test-install pass measures
    throughput with it; check_setup shows the last pass's report."""
    import sweep
    swept = conn.execute("SELECT COUNT(*) FROM projections WHERE merged_into IS NULL AND"
                         " class_observed_snapshot IN (SELECT snapshot_id FROM snapshots"
                         " WHERE pass_id=?)", (pass_id,)).fetchone()[0]
    imported = conn.execute("SELECT 1 FROM snapshots WHERE pass_id=?",
                            (pass_id,)).fetchone() is not None
    return {"swept_this_pass": swept,
            "remaining_in_cycle": len(sweep._due(conn)) if imported else None}


def end_pass(conn, token, outcome: str, report: dict) -> dict:
    import alerts
    import documents
    if outcome not in OUTCOMES:
        raise db.Refusal(f"outcome is one of {', '.join(OUTCOMES)}")
    if token is None:
        raise db.Refusal("ending a pass needs its pass_token")
    with db.tx(conn):
        check_token(conn, token)
        m = _marker(conn)
        full = {**(report or {}), **throughput(conn, m["pass_id"])}
        conn.execute("UPDATE passes SET ended_at=?, outcome=?, report_json=? WHERE pass_id=?",
                     (db.now(), outcome, db.canonical(full), m["pass_id"]))
        conn.execute("UPDATE pass_marker SET live=0, claimed_step=NULL, lease_at=NULL"
                     " WHERE id=1")
        handed = _hand_over(conn, m["pass_id"], outcome)
    # The pass ended in the commit above. The reap is housekeeping: another session
    # holding the documents lock past the bound must not make this answer "NOT
    # applied — ask again" (fix wave F); the next pass's end reaps instead.
    try:
        documents.reap_orphans(conn)
    except db.Busy:
        pass
    notice = handed.pop("_notice") if handed else None
    out = {"ended": m["pass_id"], "outcome": outcome, **(handed or {})}
    # a stopped package's notice is always in this rendering (older alerts may wait)
    out["speak"] = alerts.pending_rendering(conn, must=notice)
    return out


def _hand_over(conn, pass_id: str, outcome: str):
    """The package authority transfer, inside end_pass's transaction: the pass's
    open request gets a token of its own (fresh lease, so it is never claimable
    in between), and the request is buildable — or, when the pass stopped, closed
    `stopped` with its package notice raised in this same transaction."""
    import alerts
    req = conn.execute("SELECT * FROM package_requests WHERE pass_id=? AND state='snapshot'",
                       (pass_id,)).fetchone()
    if req is None:
        return None
    token, now = rotate(conn), db.now()
    notice = None
    if outcome == "stopped":
        state = "stopped"
        step = conn.execute("SELECT finish_json FROM pass_steps WHERE pass_id=? AND"
                            " step='snapshot'", (pass_id,)).fetchone()
        reason = (json.loads(step["finish_json"] or "{}").get("stopped") if step else None) \
            or "the bank check stopped"
        conn.execute("UPDATE package_requests SET reason=? WHERE request_id=?",
                     (reason, req["request_id"]))
        notice = alerts.raise_package(conn, "package-stopped",
                                      f"request:{req['request_id']}:stopped",
                                      quarter=req["quarter"], reason=reason)
    else:
        state = "snapshot-done"
    conn.execute("UPDATE package_requests SET token=?, lease_at=?, pass_outcome=?, state=?,"
                 " updated_at=? WHERE request_id=?",
                 (token, now, outcome, state, now, req["request_id"]))
    return {"package_token": token, "next": "build" if state == "snapshot-done" else None,
            "request": {"id": req["request_id"], "quarter": req["quarter"],
                        "channel": req["channel"], "state": state},
            "_notice": notice}


def record_probe(conn, token, kind: str, ok: bool, detail: str = "", data=None) -> dict:
    if kind not in PROBE_KINDS:
        raise db.Refusal(f"probe kind must be one of {', '.join(PROBE_KINDS)}")
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
        conn.execute("INSERT OR REPLACE INTO probes(kind, ok, detail, data_json, observed_at,"
                     " pass_id, failing_since) VALUES (?,?,?,?,?,?,?)",
                     (kind, 1 if ok else 0, detail, db.canonical(data) if data is not None
                      else None, now, pass_id, failing_since))
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
    """Callable inside a write transaction (record_observation, an import) or
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
    verdict = db.canonical({"allowed": False, "reason": reason, "expected_generation": None,
                            "expected_ledger": None, "workflow": version.WORKFLOW,
                            "install_backup": None, "older_workflows": []})
    conn.execute("ROLLBACK")
    with db.tx(conn):
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
                         "floor (bank-feed 0.15.0)")
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
