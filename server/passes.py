"""The pass marker, probes and the bank-write gate.

A pass takes a marker; a second pass finding a live one does not duplicate
the work. A marker older than STALE_AFTER_S is a dead process and is
reclaimed. EVERY begin bumps the generation, and check_token refuses any
write carrying a token that is not the live generation — including the
writes that are not CAS'd on a match record (spec §"Running the pass on
demand"). It fences this store only; bank-feed's own writes are fenced by
bank-feed's workflow/expected_generation (the gate below says which).

Health is observed, never inferred: each probe is what the specialist or
Ellen actually saw this pass, stored with its time (spec §Setup)."""
from __future__ import annotations

import datetime as _dt
import json
import re

import db
import version

STALE_AFTER_S = 3 * 3600
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


def begin_pass(conn, trigger: str) -> dict:
    with db.tx(conn):
        m = _marker(conn)
        reclaimed = False
        if m is not None and m["live"]:
            age = _age_s(m["started_at"])
            if age < STALE_AFTER_S:
                minutes = int(age // 60)
                when = "a minute ago" if minutes <= 1 else f"{minutes} minutes ago"
                return {"status": "busy", "started_at": m["started_at"],
                        "text": f"Already checking — started {when}.\n"
                                "I'll have the answer shortly."}
            reclaimed = True
        conn.execute("UPDATE counters SET value = value + 1 WHERE name='pass_generation'")
        gen = conn.execute("SELECT value FROM counters WHERE name='pass_generation'").fetchone()[0]
        now = db.now()
        pass_id = f"p{gen}"
        conn.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id, trigger,"
                     " started_at) VALUES (1, ?, 1, ?, ?, ?)", (gen, pass_id, trigger, now))
        conn.execute("INSERT INTO passes(pass_id, generation, trigger, started_at)"
                     " VALUES (?, ?, ?, ?)", (pass_id, gen, trigger, now))
        return {"status": "started", "pass_token": gen, "pass_id": pass_id,
                "reclaimed": reclaimed}


def check_token(conn, token) -> None:
    if token is None:
        return
    m = _marker(conn)
    if m is None or not m["live"] or int(token) != m["generation"]:
        raise db.Refusal("this pass is no longer the current one (a newer pass reclaimed "
                         "its marker, or the store was reset); stop — nothing was written")


def end_pass(conn, token, outcome: str, report: dict) -> dict:
    with db.tx(conn):
        check_token(conn, token)
        m = _marker(conn)
        conn.execute("UPDATE passes SET ended_at=?, outcome=?, report_json=? WHERE pass_id=?",
                     (db.now(), outcome, db.canonical(report or {}), m["pass_id"]))
        conn.execute("UPDATE pass_marker SET live=0 WHERE id=1")
        return {"ended": m["pass_id"], "outcome": outcome}


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
    across passes either."""
    cur = current_pass(conn)
    if cur is not None and cur["gate_json"]:
        return json.loads(cur["gate_json"])
    out = _decide_gate(conn)
    probe = conn.execute("SELECT pass_id FROM probes WHERE kind='ledger'").fetchone()
    if cur is not None and probe is not None and probe["pass_id"] == cur["pass_id"]:
        _write(conn, "UPDATE passes SET gate_json=? WHERE pass_id=?",
               (db.canonical(out), cur["pass_id"]))
    return out


def poison(conn, reason: str) -> None:
    """Refuse every further bank-feed write in this pass (inside a transaction:
    the caller's refusal would roll this back, so it commits on its own)."""
    cur = current_pass(conn)
    if cur is None:
        return
    verdict = db.canonical({"allowed": False, "reason": reason, "expected_generation": None,
                            "expected_ledger": None, "workflow": version.WORKFLOW,
                            "install_backup": None, "older_workflows": []})
    conn.execute("UPDATE passes SET gate_json=?, snapshot_id=NULL WHERE pass_id=?",
                 (verdict, cur["pass_id"]))
    conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")


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
