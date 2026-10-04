"""The bound account, its watermark and the package name — all defaulted,
never asked at install (spec §Setup, "What the plugin works out for
itself"); the self-check; reset."""
from __future__ import annotations

import json
import re
import shutil
import unicodedata

import dates
import db


def get(conn):
    return conn.execute("SELECT * FROM binding WHERE id=1").fetchone()


def slug(label: str) -> str:
    folded = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:32].strip("-")
    return s or "books"


def _bind(conn, account_id: str, label: str) -> None:
    """Inside an open transaction."""
    conn.execute("INSERT INTO binding(id, account_id, account_label, watermark, bound_at,"
                 " package_name) VALUES (1, ?, ?, ?, ?, ?)",
                 (account_id, label, dates.quarter_start(db.now()[:10]), db.now(), slug(label)))


def bind_account(conn, account_id: str, label: str = "", token=None) -> dict:
    import passes
    with db.tx(conn):
        passes.check_token(conn, token)
        b = get(conn)
        if b is not None:
            if b["account_id"] != account_id:
                raise db.Refusal("an account is already bound; rebinding to another one is "
                                 "not offered in v1")
            return {"bound": account_id, "changed": False}
        _bind(conn, account_id, label)
        return {"bound": account_id, "changed": True, "watermark": get(conn)["watermark"]}


def acknowledge_ledger_reset(conn) -> dict:
    """The operator's word that the bank ledger was wiped on purpose
    (delete_all_data, or everything purged before this plugin ever wrote). If
    the next import cannot prove it is the ledger the store was built on, it
    RE-BINDS the store to the ledger it reads (ledger._rebind); either way the
    word is consumed by that import (plan §D4)."""
    with db.tx(conn):
        if get(conn) is None:
            raise db.Refusal("no account is bound yet")
        conn.execute("UPDATE binding SET ledger_reset_ack=1 WHERE id=1")
    return {"acknowledged": True,
            "note": "At the next check, if the bank ledger is not the one I knew, every "
                    "payment I tracked is closed, its document freed, and I start again from "
                    "the ledger as it is now."}


def set_package_name(conn, name: str) -> dict:
    s = slug(name)                  # a name that slugs to nothing falls back to "books"
    with db.tx(conn):
        if get(conn) is None:
            raise db.Refusal("no account is bound yet")
        conn.execute("UPDATE binding SET package_name=?, package_name_announced=1 WHERE id=1",
                     (s,))
    return {"package_name": s}


def check_setup(conn) -> dict:
    import passes
    cur = passes.current_pass(conn)
    cur_id = cur["pass_id"] if cur else None
    probes = {r["kind"]: {"ok": bool(r["ok"]), "detail": r["detail"],
                          "observed_at": r["observed_at"], "failing_since": r["failing_since"],
                          "this_pass": r["pass_id"] == cur_id,
                          "data": json.loads(r["data_json"]) if r["data_json"] else None}
              for r in conn.execute("SELECT * FROM probes")}
    b = get(conn)
    conditions, can_run = [], True
    tools = probes.get("bank_tools")
    if tools is not None and not tools["ok"]:
        conditions.append("I can't see bank-feed's tools from here. Check that bank-feed is "
                          "installed on the finance specialist.")
        can_run = False
    import views        # views imports binding: a module-level import would be circular
    accounts = ((probes.get("bank_accounts") or {}).get("data") or {}).get("accounts")
    if b is None:
        can_run = False
        company = [a for a in (accounts or []) if a.get("category") == "company"]
        if accounts is None:
            conditions.append("No bank account is bound yet.")
        elif len(company) > 1:
            conditions.append("Several company accounts are linked — which one is the business "
                              "account? " + ", ".join(views.field(a.get("label") or a["account_id"])
                                                      for a in company))
        else:
            conditions.append("No company account is linked. bank-feed has: "
                              + (", ".join(views.field(a.get("label") or a["account_id"]) for a in accounts)
                                 or "no accounts")
                              + ". label_account is how an account becomes a company one.")
    elif accounts is not None and not any(a.get("account_id") == b["account_id"]
                                          for a in accounts):
        conditions.append("The bound account is gone from bank-feed.")
        can_run = False
    sync = probes.get("bank_sync")
    if b is not None and (sync is None or (not sync["ok"] and sync["detail"] == "never synced")):
        conditions.append("bank-feed is reachable but has never synced.")
    gmail = probes.get("gmail")
    searching = gmail is None or gmail["ok"]
    if gmail is not None and not gmail["ok"]:
        if (gmail["data"] or {}).get("absent"):
            # S2 §6.4: finance has no Gmail tools — not connected, nothing to re-authorise
            conditions.append("Gmail isn't connected for the finance specialist — invoices "
                              "aren't being searched.")
        else:
            conditions.append("Gmail isn't reachable — matching runs on documents already "
                              "held; searching is off.")
    gate = passes.bank_write_gate(conn)
    header = "Not set up yet."
    ledger_read = (probes.get("ledger") or {}).get("this_pass")
    if can_run and ledger_read and not gate["allowed"]:
        conditions.append("Stopped before writing anything: " + gate["reason"] + ".")
        can_run, header = False, "Stopped."
    if gate.get("older_workflows"):
        # spec §Testing: a version upgrade without a restore "reports the older
        # version's writes still present" — a report, not a stop
        conditions.append("The older version's writes are still present in bank-feed's ledger ("
                          + ", ".join(gate["older_workflows"]) + ").")
    last = conn.execute("SELECT pass_id, trigger, outcome, ended_at, report_json FROM passes"
                        " WHERE ended_at IS NOT NULL ORDER BY generation DESC LIMIT 1").fetchone()
    last_pass = None if last is None else {
        "pass_id": last["pass_id"], "trigger": last["trigger"], "outcome": last["outcome"],
        "ended_at": last["ended_at"], "report": json.loads(last["report_json"] or "{}")}
    return {"bound": dict(b) if b else None, "probes": probes, "conditions": conditions,
            "can_run": can_run, "header": header, "searching": searching, "bank_writes": gate,
            "last_pass": last_pass}


_TABLES_TO_WIPE = ("binding", "passes", "probes", "documents", "counterparties",
                   "chain_overrides", "snapshots", "bank_rows", "projections", "aliases",
                   "matches", "log", "match_state", "residue", "renders", "render_items",
                   "shown", "packages", "deliveries", "delivered_rows", "alerts", "pass_steps",
                   "package_requests", "operator_refs", "claims", "work_requests", "credits",
                   "runs", "readings", "render_keys", "account_choices", "post_offers")


ERASE_REPORT_KEEPS = (
    "Not erased by this: the acct:: tags and accounting notes in bank-feed's ledger (restore "
    "its install backup to remove them), Home Assistant backups taken earlier, and copies in "
    "Casa's handoff folder and plugin outbox, which Casa removes after 7 days and 2 hours.")


def reset_store(conn) -> dict:
    """Wipe to the fresh-install state (spec §Setup, "Test install"), and the
    plugin's uninstall eraser (casa.eraseTool, Casa v0.329.0+): argument-free,
    protected (one Casa tap), answering {"erasure", "report"}. No precondition:
    the server could not check one. In place, under the write lock, so other
    processes see an empty store at their next transaction rather than writing
    into an unlinked file; the pass generation is bumped so any running pass is
    refused at its next write. Then the documents and packages are deleted and
    the freed pages reclaimed (VACUUM, WAL truncate), so erased rows do not
    stay readable in free pages. `complete` only when every step finished."""
    problems = []
    # The custody lock spans the row wipe AND the file erasure (fix wave B, Astra +
    # Terra S1): an ingest either commits its row before the wipe (and both go) or
    # installs its bytes after the erasure (and both stay).
    with db.custody_lock():
        with db.tx(conn):
            for t in _TABLES_TO_WIPE:
                conn.execute(f"DELETE FROM {t}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)"
                         % ",".join("'%s'" % t for t in _TABLES_TO_WIPE))
            conn.execute("UPDATE counters SET value=0 WHERE name='seq'")
            # the note texts restart with the sequence: a write handed out before the
            # reset may still land with a text the new store will issue again (#14)
            db.set_epoch(conn)
            conn.execute("UPDATE counters SET value = value + 1 WHERE name='pass_generation'")
            # S2 §6.3 (Astra plan-r3 S1): with `claims` empty every old job token is refused
            # (check_claim), and the drain names no job of the wiped store
            conn.execute("DELETE FROM meta WHERE key='drain'")
            # the marker row carries the last pass's trigger, id and start time —
            # operator data (fix wave B, Astra S2); the monotonic generation that
            # fences a running pass lives in counters, bumped above
            conn.execute("DELETE FROM pass_marker")
            conn.execute("UPDATE cursor SET last_pid=0, cycle_started_at=NULL,"
                         " last_cycle_completed_at=NULL")
            # A "restored" or "other-ledger" refusal concerned the store just wiped; a
            # dirty-ledger one concerns the ledger, which a store reset does not clean.
            conn.execute("DELETE FROM meta WHERE key='gate_refusal' AND"
                         " json_extract(value, '$.kind')<>'dirty-ledger'")
        for sub in ("documents", "packages"):
            try:
                shutil.rmtree(db.data_dir() / sub)
            except FileNotFoundError:
                pass
            except OSError as exc:
                problems.append(f"{sub}/ could not be removed: {exc}")
    try:
        conn.execute("VACUUM")
        busy, log_frames, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy or log_frames:
            # Another session still reads an older snapshot: erased rows stay in the
            # WAL until it lets go (round p11, Astra S2). Never report that as complete.
            problems.append("another session is still reading the store, so erased rows "
                            "remain in its write-ahead log until it closes; try again")
    except Exception as exc:  # the rows are gone; their free pages may not be
        problems.append(f"the space reclaim did not finish: {exc}")
    if problems:
        return {"erasure": "incomplete",
                "report": "The accounting store was emptied, but: " + "; ".join(problems)
                          + ". " + ERASE_REPORT_KEEPS}
    return {"erasure": "complete",
            "report": "The accounting store is empty: documents, decisions, views and packages "
                      "are gone. The next pass refuses every bank-feed write until the ledger "
                      "is clean. " + ERASE_REPORT_KEEPS}
