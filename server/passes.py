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
LEASE_S = 600         # a claim with no progress may be claimed again (steps.LEASE_S)
OPEN_REQUEST = ("snapshot-done", "built", "staged")    # a package request a token may act on
CLOSED_WORD = {"delivered": "sent", "uncertain": "sent, though it may not have arrived",
               "failed": "tried, and it didn't go out", "stopped": "stopped",
               "recovery-failed": "not built: the bank couldn't be read",
               "revoked": "taken back when the bank was re-read"}
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
    if not conn.in_transaction:
        raise RuntimeError("a token is drawn inside the write transaction")
    conn.execute("UPDATE counters SET value = value + 1 WHERE name='pass_generation'")
    return conn.execute("SELECT value FROM counters WHERE name='pass_generation'").fetchone()[0]


def begin_pass(conn, trigger: str, reply=None) -> dict:
    if reply is None:
        reply = "silent" if trigger == "cron" else "telegram"
    if reply not in REPLIES:
        raise db.Refusal("reply is 'telegram' or 'silent'")
    with db.tx(conn):
        m = _marker(conn)
        reclaimed, recovered, owed = False, None, []
        if m is not None and m["live"]:
            age = _age_s(m["started_at"])
            # a claim holding a fresh lease is a live holder, however old the pass
            held = m["lease_at"] is not None and _age_s(m["lease_at"]) < LEASE_S
            if age < STALE_AFTER_S or held:
                minutes = int(age // 60)
                when = ("a minute ago" if minutes <= 1 else f"{minutes} minutes ago"
                        if minutes < 90 else f"{round(minutes / 60)} hours ago")
                return {"status": "busy", "started_at": m["started_at"],
                        "text": BUSY.format(when=when)}
            reclaimed = True
            displaced = m["pass_id"]
            recovered, notice = _terminalize(conn, displaced)
            import alerts
            owed = ([notice] if notice is not None else []) + alerts.pass_notices(conn, displaced)
        gen = rotate(conn)
        now = db.now()
        pass_id = f"p{gen}"
        # the claim columns are cleared with every new marker
        conn.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id, trigger,"
                     " started_at, claimed_step, lease_at) VALUES (1, ?, 1, ?, ?, ?, NULL, NULL)",
                     (gen, pass_id, trigger, now))
        conn.execute("INSERT INTO passes(pass_id, generation, trigger, started_at, reply)"
                     " VALUES (?, ?, ?, ?, ?)", (pass_id, gen, trigger, now, reply))
        out = {"status": "started", "pass_token": gen, "pass_id": pass_id,
               "reclaimed": reclaimed, "recovered": recovered}
        if reclaimed and owed:
            # what the reclaim raised is in this call's own message (a notice a call
            # raises is in that call's returned rendering)
            import alerts
            out["speak"] = alerts.pending_in_tx(conn, must=owed)
        return out


def _terminalize(conn, pass_id: str):
    """A reclaimed pass is over: it is ended `interrupted` (so it no longer looks
    unended, and check_setup's last_pass shows it), and its package request is
    settled by snapshot_fate (outcome `interrupted`), buildable or closed with its
    package notice. Returns (the recovered request's id or None, its notice or None)."""
    now = db.now()
    report = {"reclaimed": True, **throughput(conn, pass_id)}
    conn.execute("UPDATE passes SET ended_at=?, outcome='interrupted', report_json=?"
                 " WHERE pass_id=? AND ended_at IS NULL", (now, db.canonical(report), pass_id))
    req = conn.execute("SELECT * FROM package_requests WHERE pass_id=? AND state='snapshot'",
                       (pass_id,)).fetchone()
    if req is None:
        return None, None
    # recovered with no token and a lapsed lease: the next continue_pass claims it
    _, notice = settle_snapshot_request(conn, req, "interrupted", token=None)
    return req["request_id"], notice


def snapshot_fate(conn, pass_id: str, outcome: str) -> tuple:
    """THE rule for a package request leaving `snapshot` (every path that moves it
    uses this): its fate is decided from the STORED snapshot step, and from the
    pass outcome only where that is more restrictive. Returns (state, reason).

    - `snapshot-done` (buildable) only when THIS pass imported the bank (a
      `snapshots` row of its own), neither the stored finish nor the outcome says
      stopped, and the outcome is not `failed` — a build otherwise ships an older
      import as this request's. A step's finish never grants a build: a step that
      expired after its import builds from that import;
    - `stopped`, with the stored finish's reason (else a default), when the stored
      finish or the outcome says stopped;
    - otherwise `recovery-failed`: the bank was not read for it."""
    step = conn.execute("SELECT finished_at, finish_json FROM pass_steps WHERE pass_id=? AND"
                        " step='snapshot'", (pass_id,)).fetchone()
    fin = json.loads(step["finish_json"] or "{}") if step is not None else {}
    finished = step is not None and step["finished_at"] is not None
    if (finished and fin.get("stopped")) or outcome == "stopped":
        return "stopped", (fin.get("stopped") if finished else None) or "the bank check stopped"
    # the evidence the pass read the bank is its own import, never the step's say-so
    imported = conn.execute("SELECT 1 FROM snapshots WHERE pass_id=?",
                            (pass_id,)).fetchone() is not None
    if imported and outcome != "failed":
        return "snapshot-done", None
    return "recovery-failed", None


def settle_snapshot_request(conn, req, outcome: str, *, token):
    """Move `req` out of `snapshot` by snapshot_fate, inside the caller's transaction:
    buildable (holding `token`, fresh lease when there is one) or closed with its
    package notice. Returns (state, the notice's alert_id or None)."""
    state, reason = snapshot_fate(conn, req["pass_id"], outcome)
    now = db.now()
    conn.execute("UPDATE package_requests SET token=?, lease_at=?, pass_outcome=?, state=?,"
                 " updated_at=? WHERE request_id=?",
                 (token, now if token is not None else None, outcome, "snapshot-done", now,
                  req["request_id"]))
    if state == "snapshot-done":
        return state, None
    return state, _close(conn, req, state, outcome, reason=reason)


def _close(conn, req, state: str, outcome: str, reason=None):
    """Close a package request whose bank read did not give it a package: `stopped`
    (with its reason) or `recovery-failed` (the bank was not read), with its package
    notice raised in the caller's transaction. Returns the notice's alert_id."""
    import alerts
    now = db.now()
    conn.execute("UPDATE package_requests SET state=?, pass_outcome=?, reason=?, lease_at=?,"
                 " updated_at=? WHERE request_id=?",
                 (state, outcome, reason, now, now, req["request_id"]))
    if state == "stopped":
        return alerts.raise_package(conn, "package-stopped", f"request:{req['request_id']}:stopped",
                                    quarter=req["quarter"], reason=reason or "")
    return alerts.raise_package(conn, "package-failed", f"request:{req['request_id']}:failed",
                                quarter=req["quarter"])


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
            or req["state"] in ("superseded", "withdrawn"):
        raise db.Refusal("this package request has been taken over by a later turn — stop, "
                         "nothing was written")
    if req["state"] not in OPEN_REQUEST:
        # the holder's own request, finished: said plainly, never as "stop in silence"
        import dates
        raise db.Refusal(f"the {dates.quarter_label(req['quarter'])} package was already "
                         f"{CLOSED_WORD.get(req['state'], 'dealt with')} — nothing changed. To "
                         "have it again, or by email, ask for the package again.")
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
    # a READ, not the import's stamp (issue #1: the import observes every present row)
    swept = conn.execute("SELECT COUNT(*) FROM projections WHERE merged_into IS NULL AND"
                         " read_snapshot IN (SELECT snapshot_id FROM snapshots"
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
        if outcome == "complete":
            _judgment_owed(conn, m["pass_id"])
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
    # the notices this pass raised — its request's, its import's revocations — are
    # always in this rendering (older alerts may wait)
    owed = ([notice] if notice is not None else []) + alerts.pass_notices(conn, m["pass_id"])
    out["speak"] = alerts.pending_rendering(conn, must=owed)
    return out


def _judgment_owed(conn, pass_id: str) -> None:
    """A pass that swept is `complete` only if every payment judge-due at its end
    (work.judge_due_pids, checked live in end_pass's own transaction) was covered by a
    judgment in this pass (issue #3, code rounds C4-C7). An operator can reopen a
    payment whose document is already filed at any moment — during triage, the Gmail
    round, the judge step — so no count taken earlier can promise it; the pass's end
    can. Covered = due, in the same state (work.judge_due_state: the payment's revision
    and its fitting documents), when a judge step that then FINISHED started. With no judge step
    yet, Ellen runs it; otherwise (a judge step expired, or the payment reopened after
    it started) the pass is `interrupted` and the next pass judges it. Never asks for a
    second judge step, so it cannot loop."""
    import work
    steps = {r["step"]: r for r in conn.execute(
        "SELECT step, finished_at, carry_json FROM pass_steps WHERE pass_id=?", (pass_id,))}
    if "sweep" not in steps:
        return
    due = work.judge_due_state(conn)
    judge = steps.get("judge")
    if judge is not None and judge["finished_at"] is not None:
        # covered: due when the judgment started, in exactly the state it saw (C7)
        seen = json.loads(judge["carry_json"] or "{}").get("due_at_start", {})
        due = {p: v for p, v in due.items() if seen.get(str(p)) != v}
    if not due:
        return
    n = len(due)
    what = (f"{n} payment{'s' if n > 1 else ''} with a filed document that may fit "
            f"{'were' if n > 1 else 'was'} not judged in this pass")
    if judge is None:
        raise db.Refusal(f"not ended: {what}. Start the judge step (record_step "
                         "step=\"judge\") and end the pass after it")
    raise db.Refusal(f"not ended: {what}. End it interrupted: the next pass judges "
                     f"{'them' if n > 1 else 'it'}")


def _hand_over(conn, pass_id: str, outcome: str):
    """The package authority transfer, inside end_pass's transaction: the pass's
    open request gets a token of its own (fresh lease, so it is never claimable
    in between), and the request is buildable — or, when the pass stopped, closed
    `stopped` (or, when it failed to read the bank, `recovery-failed`) with its
    package notice raised in this same transaction."""
    req = conn.execute("SELECT * FROM package_requests WHERE pass_id=? AND state='snapshot'",
                       (pass_id,)).fetchone()
    if req is None:
        return None
    token = rotate(conn)
    state, notice = settle_snapshot_request(conn, req, outcome, token=token)
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
