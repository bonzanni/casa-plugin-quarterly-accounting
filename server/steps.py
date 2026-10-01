"""A pass that spans turns (issue #2). A delegation to the specialist can answer
after the turn that made it (Casa degrades a sync delegation to pending after
about 60 s, and cuts a delegated turn at about 600 s), so a pass's progress is
kept here, never in a message: the steps Ellen started (stamped before she
delegates), the finish the specialist (or Ellen) recorded, or the step's expiry.

Whoever looks next — a notification, a check, a handover, a package request —
calls claim() (the continue_pass tool). It continues exactly what is due, and
every claim rotates the token (passes.rotate): the one integer the stale-pass
fence (passes.check_token) compares on every write. So whatever a superseded
holder still does is refused by the check that already refuses a reclaimed
pass. A notification closes the step its delegation served, bound by the delegation
id recorded on the step (issue #17), and never anything else.

A package request (package_requests) carries its own token under the same
rule: end_pass hands it over in its own transaction, and claim() takes over
one whose holder went quiet. A staged send is never re-sent by a claim: its
staged bytes are taken back first (a superseded holder may still hold the
path, and Casa's send tools check no token of ours), the send is settled
`uncertain`, and the operator is offered "send it again"."""
from __future__ import annotations

import contextlib
import datetime as _dt
import json

import db
import passes

CEILING_ASSUMED_S = 600     # Casa's delegated-turn ceiling, as read (spec: assumption A2)
SWEEP_STOP_S = 450          # the sweep lists no row after this
RETURN_BY_S = 510           # wrap_up: the specialist finishes and returns
STEP_EXPIRY_S = 600         # an unfinished step is over (its stamp precedes Casa's launch)
LEASE_S = passes.LEASE_S    # a claim with no progress may be claimed again
ROW_COST_S = 10             # one row's read, record and repair, measured
STEPS = ("sweep", "judge", "handover", "snapshot")
FOR_TRIGGER = {"cron": ("sweep", "judge"), "operator": ("sweep", "judge"),
               "handover": ("handover",), "package": ("snapshot", "judge")}
BELONGS = {"sweep": "a check (cron or operator)", "judge": "a check or a package pass",
           "handover": "a handover pass", "snapshot": "a package pass"}
CARRY = ("quarter", "channel", "doc_ids", "report")
COUNTS = ("remaining_in_cycle", "triage_remaining")
STOPPED_MAX = 300
REPORT_KEYS = ("checked", "total", "not_searched")


def _parse(stamp: str) -> _dt.datetime:
    return _dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def _stamp(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(stamp) -> float:
    return (db._clock() - _parse(stamp)).total_seconds()


def _live_pass(conn):
    m = passes._marker(conn)
    return m if m is not None and m["live"] else None


def latest(conn, pass_id):
    return conn.execute("SELECT * FROM pass_steps WHERE pass_id=? ORDER BY rowid DESC LIMIT 1",
                        (pass_id,)).fetchone()


def _finish(step) -> dict:
    return json.loads(step["finish_json"] or "{}") if step is not None else {}


def _ended(step):
    """'finished' / 'errored' / 'expired', or None while the step runs."""
    if step["finished_at"] is not None:
        return "errored" if _finish(step).get("failed") else "finished"
    if _age(step["started_at"]) >= STEP_EXPIRY_S:
        return "expired"
    return None


# --- record_step -----------------------------------------------------------------------
def _carry(step: str, carry: dict) -> dict:
    given = {k: v for k, v in carry.items() if v is not None}
    allowed = {"sweep": (), "judge": ("report",), "handover": ("doc_ids",),
               "snapshot": ()}[step]
    for k in given:
        if k not in allowed:
            raise db.Refusal({"doc_ids": "doc_ids go with a handover start",
                              "report": "report goes with a judge start",
                              "quarter": "a package's quarter goes with begin_pass",
                              "channel": "a package's channel goes with begin_pass"}[k])
    if step == "handover":
        ids = given.get("doc_ids")
        if not ids or not isinstance(ids, list) or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise db.Refusal("a handover start names the handed-over documents: doc_ids=[…]")
    if step == "judge" and "report" in given:
        rep = given["report"]
        if not isinstance(rep, dict) or any(k not in REPORT_KEYS for k in rep) or any(
                isinstance(v, bool) or not isinstance(v, int) for v in rep.values()):
            raise db.Refusal("report is counts only: checked, total, not_searched")
    return given


def start(conn, token, step: str, carry: dict) -> dict:
    """Ellen, just before delegate_to_agent: stamp the step with the token she
    holds (and passes on to the specialist)."""
    if step not in STEPS:
        raise db.Refusal("step is sweep, judge, handover or snapshot")
    if token is None:
        raise db.Refusal("a step belongs to a pass: pass the pass_token")
    carry = _carry(step, carry)
    with db.tx(conn):
        passes.check_token(conn, token)
        m = passes._marker(conn)
        if step not in FOR_TRIGGER.get(m["trigger"], ()):
            raise db.Refusal(f"a {step} step belongs to {BELONGS[step]}, not to this "
                             f"{m['trigger']} pass")
        prior = conn.execute("SELECT * FROM pass_steps WHERE pass_id=? AND step=?",
                             (m["pass_id"], step)).fetchone()
        req = round_request(conn, m["pass_id"])
        # issue #17: a package round judges after every Gmail chunk, and so does a check
        # (issue #21). A judge step whose judgment finished is started again on the same
        # row — the row is always the pass's latest judgment, which is what its check
        # (package_check, _judgment_owed) reads
        restart = (prior is not None and step == "judge" and _ended(prior) == "finished")
        if prior is not None and not restart:
            raise db.Refusal(f"the {step} step was already started in this pass")
        now = db._clock().replace(microsecond=0)
        if step == "snapshot" and req is None:
            raise db.Refusal("this package pass holds no package request: ask for the package "
                             "with begin_pass(trigger=\"package\", quarter, channel)")
        if step == "judge":
            # the payments this judgment covers (issue #3, C6): end_pass lets the pass
            # be complete only if every payment judge-due at its end was due here; a
            # package round's judgment covers its quarter (issue #15)
            import work
            q = req["quarter"] if req is not None else None
            carry = {**carry, "due_at_start": {str(p): v for p, v in
                                               work.judge_due_state(conn, q).items()}}
        if restart:
            # the delegations this row served before, so a notice naming one of them can
            # never be read as this judgment's (D2)
            old = json.loads(prior["carry_json"] or "{}")
            gone = old.get("delegations_before", []) + (
                [old["delegation"]] if old.get("delegation") else [])
            if gone:
                carry = {**carry, "delegations_before": gone}
        if restart:
            conn.execute("UPDATE pass_steps SET started_at=?, finished_at=NULL, finished_by=NULL,"
                         " finish_json=NULL, carry_json=? WHERE pass_id=? AND step=?",
                         (_stamp(now), db.canonical(carry), m["pass_id"], step))
            # a claim keys on the step's name: the restarted judgment is unclaimed
            conn.execute("UPDATE pass_marker SET claimed_step=NULL WHERE id=1")
        else:
            conn.execute("INSERT INTO pass_steps(pass_id, step, started_at, carry_json)"
                         " VALUES (?,?,?,?)",
                         (m["pass_id"], step, _stamp(now), db.canonical(carry)))
        return {"step": step, "started_at": _stamp(now),
                "sweep_stop_at": _stamp(now + _dt.timedelta(seconds=SWEEP_STOP_S)),
                "return_by": _stamp(now + _dt.timedelta(seconds=RETURN_BY_S))}


def round_request(conn, pass_id):
    """The package request whose round runs in this pass (issue #15), or None."""
    return conn.execute("SELECT * FROM package_requests WHERE pass_id=? AND state='snapshot'",
                        (pass_id,)).fetchone()


def finish(conn, token, step: str, *, counts: dict, stopped=None, failed=False,
           by_refusal=False, out_of_time=False) -> dict:
    """The specialist's last action (or Ellen's, when the delegation came back in
    her turn without one). A superseded specialist is refused here like anywhere."""
    if step not in STEPS:
        raise db.Refusal("step is sweep, judge, handover or snapshot")
    if token is None:
        raise db.Refusal("a step belongs to a pass: pass the pass_token")
    for k, v in counts.items():
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise db.Refusal(f"{k} is a count")
    if out_of_time and (stopped or by_refusal):
        raise db.Refusal("out_of_time=true is a finish without `stopped`: time ran out, "
                         "nothing refused")
    with db.tx(conn):
        passes.check_token(conn, token)
        m = passes._marker(conn)
        row = conn.execute("SELECT * FROM pass_steps WHERE pass_id=? AND step=?",
                           (m["pass_id"], step)).fetchone()
        if row is None:
            raise db.Refusal(f"the {step} step was not started in this pass")
        if row["finished_at"] is not None:
            return {"step": step, "finished": True, "already": True}
        body = {k: v for k, v in counts.items() if v is not None}
        # issue #18: a judgment is whole only when it says how far triage got. A judge
        # finish that carries counts carries that one; a countless finish (Ellen's, the
        # delegation back without one) is accepted and counts as not whole
        if (step == "judge" and body and not failed and not stopped
                and body.get("triage_remaining") is None):
            raise db.Refusal("a judge step's finish carries triage_remaining: the last triage "
                             "page's `remaining` (0 when every page was judged) — finish "
                             "again with it")
        # issue #10: running out of time is not a stop. Once the step's time is up (the
        # sweep pages `time_up`) after this pass's import, a `stopped` is refused unless
        # the specialist says a refusal stopped it: a time-out said as a stop is put
        # right, and a real refusal (the ledger changed, was restored) still stops the
        # pass. Before the import nothing goes on anyway, so any stop is taken as said.
        # A refused stop is KEPT on the unfinished step (R2): only a finish that says
        # `out_of_time=true` clears it (R6: never inferred from who seems to finish), and
        # a step that expires instead ends stopped — as the stop said
        late = _age(row["started_at"]) >= SWEEP_STOP_S and conn.execute(
            "SELECT 1 FROM snapshots WHERE pass_id=?", (m["pass_id"],)).fetchone() is not None
        if stopped and late and not by_refusal:
            import views
            conn.execute("UPDATE pass_steps SET finish_json=? WHERE pass_id=? AND step=?",
                         (db.canonical({"stopped": views.clip(str(stopped), STOPPED_MAX)}),
                          m["pass_id"], step))
            refused = ("your step's time is up, and running out of time is not a stop: "
                       "finish again with the counts, no `stopped`, and `out_of_time=true` "
                       "— the pass goes on and a later pass resumes. Only if a refusal "
                       "stopped you, finish again with `stopped=<the refusal>` and "
                       "`stopped_by_refusal=true`")
        else:
            refused = None
        if refused is None:
            if stopped:
                import views
                body["stopped"] = views.clip(str(stopped), STOPPED_MAX)
            if failed:
                body["failed"] = True
            if out_of_time:
                body["out_of_time"] = True   # a timed-out judgment covers nothing (#15, D3)
            by = "resident" if failed or not body else "specialist"
            kept = _finish(row).get("stopped")      # an unfinished step holds only a kept stop
            if kept and not out_of_time and "stopped" not in body:
                body["stopped"] = kept              # only `out_of_time=true` clears it
            conn.execute("UPDATE pass_steps SET finished_at=?, finished_by=?, finish_json=?"
                         " WHERE pass_id=? AND step=?",
                         (db.now(), by, db.canonical(body), m["pass_id"], step))
    if refused is not None:
        raise db.Refusal(refused)       # after the commit: the kept stop stays
    return {"step": step, "finished": True, "already": False}


# --- a delegation's end ends its step (issue #17, D1) --------------------------------------
DELEGATION_PREFIX = 8          # Casa's notice prints the first 8 characters of the id
_DELEGATION_CHARS = set("0123456789abcdefABCDEF-")


def _delegation_id(value) -> str:
    if (not isinstance(value, str) or not DELEGATION_PREFIX <= len(value) <= 64
            or set(value) - _DELEGATION_CHARS):
        raise db.Refusal("delegation_id is the id delegate_to_agent returned (or the one the "
                         "notification names), as given")
    return value


def delegated(conn, token, step: str, delegation_id) -> dict:
    """Ellen, right after delegate_to_agent answered: the step is served by that
    delegation, so its notification can close the step (continue_pass)."""
    if step not in STEPS:
        raise db.Refusal("step is sweep, judge, handover or snapshot")
    if token is None:
        raise db.Refusal("a step belongs to a pass: pass the pass_token")
    did = _delegation_id(delegation_id)
    with db.tx(conn):
        passes.check_token(conn, token)
        m = passes._marker(conn)
        row = conn.execute("SELECT * FROM pass_steps WHERE pass_id=? AND step=?",
                           (m["pass_id"], step)).fetchone()
        if row is None:
            raise db.Refusal(f"the {step} step was not started in this pass")
        if row["finished_at"] is not None:
            # the delegation came back in the turn and the step is already finished
            # (a sync answer): nothing is left for a notification to close
            return {"step": step, "bound": False, "finished": True}
        cur = latest(conn, m["pass_id"])
        if cur["step"] != step or _ended(row) is not None:
            raise db.Refusal(f"the {step} step is not the one running in this pass")
        carry = json.loads(row["carry_json"] or "{}")
        if carry.get("delegation") and carry["delegation"] != did:
            carry["delegations_before"] = carry.get("delegations_before", []) + [
                carry["delegation"]]
        carry["delegation"] = did
        conn.execute("UPDATE pass_steps SET carry_json=? WHERE pass_id=? AND step=?",
                     (db.canonical(carry), m["pass_id"], step))
        return {"step": step, "bound": True}


def _recorded_delegations(conn) -> list:
    """Every delegation id ever bound to a step, in any pass (C1, Astra S1: a notice
    replayed from an older pass must not bind by a prefix a newer delegation shares)."""
    out = []
    for r in conn.execute("SELECT carry_json FROM pass_steps"):
        c = json.loads(r["carry_json"] or "{}")
        out += c.get("delegations_before", []) + ([c["delegation"]] if c.get("delegation")
                                                   else [])
    return out


def _close_delegated(conn, delegation_id, status) -> None:
    """A notification says delegation `delegation_id` ended: the running step it served
    is over. Bound by the id recorded on that step (a prefix of at least
    DELEGATION_PREFIX characters that no other delegation ever recorded shares);
    anything else — a stale or replayed notice, a finished step, a restarted judgment
    served by a newer delegation — changes nothing."""
    did = _delegation_id(delegation_id)
    if status not in ("ok", "error"):
        raise db.Refusal("delegation_status is 'ok' or 'error' (a restart orphan is 'error')")
    with db.tx(conn):
        m = _live_pass(conn)
        if m is None:
            return
        row = latest(conn, m["pass_id"])
        if row is None or _ended(row) is not None:
            return
        mine = json.loads(row["carry_json"] or "{}").get("delegation")
        if not mine or not mine.startswith(did):
            return
        if sum(1 for d in _recorded_delegations(conn) if d.startswith(did)) != 1:
            return      # ambiguous: another delegation shares it — the step expires, as in 0.4.0
        body = {"failed": True} if status == "error" else {}
        kept = _finish(row).get("stopped")  # a kept stop (issue #10) stays a stop
        if kept:
            body["stopped"] = kept
        conn.execute("UPDATE pass_steps SET finished_at=?, finished_by='resident', finish_json=?"
                     " WHERE pass_id=? AND step=?",
                     (db.now(), db.canonical(body), m["pass_id"], row["step"]))


# --- the clock ---------------------------------------------------------------------------
def _running(conn, m=None):
    m = m or _live_pass(conn)
    if m is None:
        return None
    step = latest(conn, m["pass_id"])
    if step is None or step["finished_at"] is not None:
        return None
    return step


def clock(conn, token):
    """Time left for the step the token is working on: None unless the token is
    live and the pass's latest step started and has not finished."""
    m = _live_pass(conn)
    if token is None or m is None or int(token) != m["generation"]:
        return None
    step = _running(conn, m)
    if step is None:
        return None
    e = int(_age(step["started_at"]))
    return {"elapsed_s": e, "time_left_s": max(0, RETURN_BY_S - e), "wrap_up": e >= RETURN_BY_S}


def sweep_allowance(conn):
    """Seconds the sweep may still list rows for: SWEEP_STOP_S minus the running
    step's elapsed time. None when no step runs (the sweep pages as it always did)."""
    step = _running(conn)
    if step is None:
        return None
    return SWEEP_STOP_S - _age(step["started_at"])


# --- the claim ---------------------------------------------------------------------------
class _Retry(Exception):
    """The candidate changed between the read and the transaction."""


def _lease_fresh(lease_at) -> bool:
    return lease_at is not None and _age(lease_at) < LEASE_S


def _choose(conn):
    """What a claim would take now: ("pass", marker, step), ("delivery", a stalled
    staged send), ("request", row), or ("none", answer)."""
    m = _live_pass(conn)
    fresh = m is not None and _lease_fresh(m["lease_at"])
    running = None
    if m is not None:
        step = latest(conn, m["pass_id"])
        if step is not None:
            ended = _ended(step)
            if ended is not None:
                if m["claimed_step"] != step["step"] or not fresh:
                    return ("pass", m, step)
            else:
                running = {"step": step["step"], "trigger": m["trigger"],
                           "started_at": step["started_at"],
                           "due_in_s": max(0, int(STEP_EXPIRY_S - _age(step["started_at"])))}
        elif not fresh:
            if _age(m["started_at"]) >= STEP_EXPIRY_S:
                return ("pass", m, None)
            running = {"step": None, "trigger": m["trigger"], "started_at": m["started_at"],
                       "due_in_s": max(0, int(STEP_EXPIRY_S - _age(m["started_at"])))}
    import delivery
    stalled = delivery.stalled_sends(conn, LEASE_S)
    if stalled:
        return ("delivery", stalled[0])
    for req in conn.execute("SELECT * FROM package_requests WHERE state IN ('snapshot-done',"
                            " 'built') ORDER BY request_id").fetchall():
        if not _lease_fresh(req["lease_at"]):
            return ("request", req)
    if m is None:
        # issue #15: no pass is live and a package request waits for a round of its check
        req = conn.execute("SELECT * FROM package_requests WHERE state='queued'"
                           " ORDER BY request_id LIMIT 1").fetchone()
        if req is not None:
            return ("round", req)
    if running is not None:
        return ("none", {"continue": None, "running": running})
    if m is not None or conn.execute(
            "SELECT 1 FROM package_requests WHERE state IN ('snapshot-done', 'built', 'staged')"
            ).fetchone() is not None:
        return ("none", {"continue": None, "held": True})
    return ("none", {"continue": None})


def _same(a, b) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "pass":
        return (a[1]["generation"], a[2]["step"] if a[2] else None) == \
            (b[1]["generation"], b[2]["step"] if b[2] else None)
    if a[0] == "delivery":
        return (a[1]["delivery_id"], a[1]["status"]) == (b[1]["delivery_id"], b[1]["status"])
    if a[0] in ("request", "round"):
        return (a[1]["request_id"], a[1]["state"], a[1]["token"]) == \
            (b[1]["request_id"], b[1]["state"], b[1]["token"])
    return True


def claim(conn, delegation_id=None, delegation_status=None) -> dict:
    """continue_pass: in one write transaction, claim what is due — the live pass
    (younger than STALE_AFTER_S) whose latest step is over and not already
    claimed by a holder with a fresh lease; else an open package request whose
    lease lapsed — and rotate its token. A staged request is claimed under the
    custody lock, taken BEFORE the transaction (the store's lock order). The
    candidate is read first and re-checked inside the transaction."""
    import alerts
    if conn.in_transaction:
        raise RuntimeError("continue_pass opens its own transactions")
    if delegation_id is not None or delegation_status is not None:
        # issue #17 (D1): a notification's claim first closes the step its delegation
        # served, if that step is still running
        _close_delegated(conn, delegation_id, delegation_status)
    for _ in range(5):
        cand = _choose(conn)
        custody = cand[0] == "delivery"
        try:
            with (db.custody_lock() if custody else contextlib.nullcontext()):
                with db.tx(conn):
                    now_cand = _choose(conn)
                    if not _same(cand, now_cand):
                        raise _Retry
                    kind = now_cand[0]
                    if kind == "none":
                        out = dict(now_cand[1])
                        if "running" in out or "held" in out:
                            return out
                        out["speak"] = alerts.pending_in_tx(conn)
                        return out
                    if kind == "pass":
                        out = _claim_pass(conn, now_cand[1], now_cand[2])
                    elif kind == "delivery":
                        out = _claim_delivery(conn, now_cand[1])
                    elif kind == "round":
                        out = _claim_round(conn, now_cand[1])
                    else:
                        out = _claim_request(conn, now_cand[1])
                    notice = out.pop("_notice", None)
                    out["speak"] = alerts.pending_in_tx(conn, must=notice)
                    # issue #15 (code round C1, Astra S1): every continuation says whether a
                    # queued package waits for continue_pass, whatever it continued
                    out["more"] = passes.queued_waiting(conn)
                    _fits(out)
                    return out
        except _Retry:
            continue
    raise db.Busy("the accounting store kept changing under this call; nothing was claimed "
                  "— ask again")


class Oversized(RuntimeError):
    """A claim's answer over budget.RESULT_LIMIT (issue #3). A bug: raised inside the
    claiming transaction, so it rolls back and nothing is claimed — the rotated token
    either reaches the agent or was never rotated."""


def _fits(out) -> None:
    import budget
    n = budget.size(out)
    if n > budget.RESULT_LIMIT:
        raise Oversized(f"the continuation's answer is {n} characters, over the "
                        f"{budget.RESULT_LIMIT} an agent can read; nothing was claimed")


def _claim_pass(conn, m, step) -> dict:
    import binding
    import work
    token = passes.rotate(conn)
    conn.execute("UPDATE pass_marker SET generation=?, claimed_step=?, lease_at=? WHERE id=1",
                 (token, step["step"] if step else "none", db.now()))
    p = conn.execute("SELECT * FROM passes WHERE pass_id=?", (m["pass_id"],)).fetchone()
    base = {"pass_token": token, "pass_id": m["pass_id"], "trigger": m["trigger"],
            "reply": p["reply"]}
    if step is None:
        return {"continue": {**base, "step": None, "next": "end-pass", "outcome": "failed",
                             "ended": "expired"}}
    ended = _ended(step)
    fin = _finish(step)
    imported = conn.execute("SELECT 1 FROM snapshots WHERE pass_id=?",
                            (m["pass_id"],)).fetchone() is not None
    carry = json.loads(step["carry_json"] or "{}")
    c = {**base, "step": step["step"], "ended": ended, "finish": fin, "imported": imported,
         "throughput": passes.throughput(conn, m["pass_id"])}
    if step["step"] == "sweep":
        can_run = binding.check_setup(conn)["can_run"]
        c["next"] = ("end-pass" if fin.get("stopped") or not can_run or not imported
                     else "gmail-round")
        # issue #21: a check's Gmail round comes in chunks too, each ended by a judgment
        c.update(can_run=can_run, judge_due=work.judge_due(conn),
                 work=(_hand_chunk(conn, m["pass_id"], None, True) if c["next"] == "gmail-round"
                       else work.work_list(conn)))
        if c["next"] == "gmail-round":
            c["filed_refs"] = work.filed_refs(conn)      # issue #24 (D4): the filing skips them
    elif step["step"] == "judge":
        c.update(next="end-pass", report=carry.get("report", {}))
        req = round_request(conn, m["pass_id"])
        if req is not None:             # a package round (issue #15): end_pass decides it
            c["request"] = {"id": req["request_id"], "quarter": req["quarter"],
                            "channel": req["channel"], "round": req["round"] + 1}
        another = _another_chunk(conn, m["pass_id"], req, step)
        if another:
            # issue #17 (#21 for a check): the next Gmail chunk of this pass, then another
            # judgment
            can_run = binding.check_setup(conn)["can_run"]
            if can_run:
                c.update(can_run=can_run, work=_hand_chunk(conn, m["pass_id"], req, False),
                         judge_due=(len(work.judge_due_state(conn, req["quarter"]))
                                    if req is not None else work.judge_due(conn)),
                         next="gmail-round")
        if req is None and _first_carry(conn, m["pass_id"]).get("since_seq") is not None:
            # issue #21 (D1): a check's report is the server's, over the searches it owes
            c["report"] = _check_report(conn, m["pass_id"])
    elif step["step"] == "handover":
        c.update(next="end-pass-then-case",
                 documents=[_pairing(conn, d) for d in carry.get("doc_ids", [])])
    else:
        # a package round's snapshot (issue #15): its quarter's Gmail round, then judging
        req = round_request(conn, m["pass_id"])
        can_run = binding.check_setup(conn)["can_run"]
        c["request"] = None if req is None else {"id": req["request_id"],
                                                 "quarter": req["quarter"],
                                                 "channel": req["channel"],
                                                 "round": req["round"] + 1}
        if fin.get("stopped") or not can_run or not imported or req is None:
            c.update(can_run=can_run, next="end-pass")
        else:
            c.update(can_run=can_run, work=_hand_chunk(conn, m["pass_id"], req, True),
                     judge_due=len(work.judge_due_state(conn, req["quarter"])),
                     next="gmail-round", filed_refs=work.filed_refs(conn))
    return {"continue": c}


def _first_step(conn, pass_id) -> str:
    """The step that opens the pass's Gmail round: a package round's snapshot, a
    check's sweep. Its row keeps the round's chunk bookkeeping."""
    return "snapshot" if round_request(conn, pass_id) is not None else "sweep"


def _first_carry(conn, pass_id) -> dict:
    row = conn.execute("SELECT carry_json FROM pass_steps WHERE pass_id=? AND step=?",
                       (pass_id, _first_step(conn, pass_id))).fetchone()
    return json.loads(row["carry_json"] or "{}") if row is not None else {}


def _set_first_carry(conn, pass_id, carry) -> None:
    conn.execute("UPDATE pass_steps SET carry_json=? WHERE pass_id=? AND step=?",
                 (db.canonical(carry), pass_id, _first_step(conn, pass_id)))


def _hand_chunk(conn, pass_id, req, first) -> dict:
    """A Gmail chunk (issue #17: a package round's; issue #21: a check's), with the count
    of items still to search kept on the pass's first step row: the next chunk is handed
    out only if a chunk brought that count down (_another_chunk). A check measures from
    one origin, `since_seq`, allocated at its first hand-out and kept (a re-claimed
    continuation keeps it), and grows the searches it owes (`owed`, by pid) at every
    hand-out, for its report — and its work (issue #24). `first`: the continuation of
    the pass's first step, whose turn also files (a smaller chunk)."""
    import work
    carry = _first_carry(conn, pass_id)
    if req is not None:
        w = work.work_list(conn, req, first=first)
    else:
        if carry.get("since_seq") is None:
            carry["since_seq"] = db.next_seq(conn)
        carry["owed"] = work.grow_owed(conn, carry.get("owed", []), carry["since_seq"])
        w = work.work_list(conn, since_seq=carry["since_seq"], owed=carry["owed"],
                           first=first)
    carry["handed"] = w["total"]
    _set_first_carry(conn, pass_id, carry)
    return w


def _check_report(conn, pass_id) -> dict:
    """Issue #21 (D1): a check's {checked, total, not_searched}, over the searches it
    owes — grown here too, so an item that joined the work after the last hand-out is
    counted as not searched rather than left out."""
    import work
    carry = _first_carry(conn, pass_id)
    carry["owed"] = work.grow_owed(conn, carry.get("owed", []), carry["since_seq"])
    _set_first_carry(conn, pass_id, carry)
    return work.check_report(conn, carry["owed"], carry["since_seq"])


def _another_chunk(conn, pass_id, req, step) -> bool:
    """Issue #17 (#21 for a check): after a judgment, another Gmail chunk runs in the same
    pass iff the judgment finished (not failed, stopped or expired), this pass's Gmail
    probe was ok, and the items still to search (a package: the quarter's, unsearched for
    the request; a check: unsearched since its origin) are fewer than when the last
    chunk was handed out, yet not none — so the loop ends: that count is a non-negative
    integer that must fall with every chunk (a search of an item already searched is not
    progress), and what a pass leaves, round_fate or the next check takes from there."""
    import work
    if _ended(step) != "finished" or _finish(step).get("stopped"):
        return False
    probe = conn.execute("SELECT ok FROM probes WHERE kind='gmail' AND pass_id=?",
                         (pass_id,)).fetchone()
    if probe is None or not probe["ok"]:
        return False
    carry = _first_carry(conn, pass_id)
    handed = carry.get("handed")
    if handed is None:
        return False
    if req is not None:
        left = len(work.package_work(conn, req))
    elif carry.get("since_seq") is not None:
        left = len(work.check_work(conn, carry["since_seq"], owed=carry.get("owed", [])))
    else:
        return False
    return 0 < left < handed


def _claim_round(conn, req) -> dict:
    """Issue #15: no pass is live and a package request is queued — start the next
    round of its check: a package pass of its own (reply silent: the operator heard
    the one line when they asked), bound to the request."""
    token, pass_id = passes.start_pass(conn, "package", "silent")
    conn.execute("UPDATE pass_marker SET claimed_step='none', lease_at=? WHERE id=1",
                 (db.now(),))
    passes._bind_round(conn, req["request_id"], pass_id)
    return {"continue": {"pass_token": token, "pass_id": pass_id, "trigger": "package",
                         "reply": "silent", "step": None, "next": "snapshot",
                         "request": {"id": req["request_id"], "quarter": req["quarter"],
                                     "channel": req["channel"], "round": req["round"] + 1}}}


def _pairing(conn, doc_id: int) -> dict:
    """The handed-over document's REAL pairing, from the match records — never
    inferred from its absence in a (capped) list of unmatched documents."""
    import work
    d = conn.execute("SELECT irrelevant FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if d is None:
        return {"doc_id": doc_id, "pairing": "unknown"}
    s = conn.execute("SELECT pid, state FROM match_state WHERE doc_id=? AND state IN"
                     " ('matched', 'proposed')", (doc_id,)).fetchone()
    if s is None:
        return {"doc_id": doc_id, "pairing": "irrelevant" if d["irrelevant"] else "unpaired"}
    p = work.describe(conn, s["pid"])
    return {"doc_id": doc_id, "pairing": s["state"],
            "payment": {"date": p["date"], "amount_minor": p["amount_minor"],
                        "currency": p["currency"], "payee": p["counterparty"]}}


def _claim_request(conn, req) -> dict:
    token = passes.rotate(conn)
    now = db.now()
    conn.execute("UPDATE package_requests SET token=?, lease_at=?, updated_at=? WHERE"
                 " request_id=?", (token, now, now, req["request_id"]))
    info = {"id": req["request_id"], "quarter": req["quarter"], "channel": req["channel"],
            "package_id": req["package_id"]}
    return {"continue": {"package_token": token, "request": info,
                         "next": "build" if req["state"] == "snapshot-done" else "stage"}}


def _claim_delivery(conn, d) -> dict:
    """A stalled staged send: recovered on the delivery (delivery.recover_staged), with
    its package request's own change on top when one is linked — the request is
    `withdrawn` under a rotated token, so its stalled holder is refused everywhere."""
    notice = __import__("delivery").recover_staged(conn, d)
    req = conn.execute("SELECT * FROM package_requests WHERE delivery_id=? AND state='staged'",
                       (d["delivery_id"],)).fetchone()
    if req is None:
        return {"continue": {"delivery_id": d["delivery_id"], "next": None},
                "_notice": notice}
    token, now = passes.rotate(conn), db.now()
    conn.execute("UPDATE package_requests SET token=?, lease_at=?, state='withdrawn',"
                 " updated_at=? WHERE request_id=?", (token, now, now, req["request_id"]))
    info = {"id": req["request_id"], "quarter": req["quarter"], "channel": req["channel"],
            "package_id": req["package_id"]}
    return {"continue": {"package_token": token, "request": info, "next": None},
            "_notice": notice}
