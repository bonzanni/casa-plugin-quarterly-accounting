"""import_ledger_export — the pass's bank snapshot. The file bank-feed's
export_history published is taken ONLY through casa_handoff.capture. It is
the one complete read of the bound account this design has, so every pass
imports it, and admission, lineage resolution, fan-in merges, vanished ends,
erase candidates and the delivered-row bank check all work from it.

An end is judged here on positive evidence only: `vanished` from the row's
own state; an absent destination is only a CANDIDATE, confirmed per row by
the specialist's get_transaction ("no transaction #N") through
record_observation in the same pass (spec §Match records, "An ended lineage
is judged ended only on positive evidence"). A surviving row whose
superseded_by names an absent id is a broken floor, never an end.

Instance continuity (plan §D4): a held alias's first_seen never changes;
otherwise nothing is imported."""
from __future__ import annotations

import csv
import io
import json

import casa_handoff
import db
import lineage
import reducer as R

REQUIRED_COLUMNS = ("row_id", "account_id", "first_seen", "booking_date", "value_date",
                    "amount_minor", "currency", "direction", "status", "counterparty",
                    "remittance", "state", "superseded_by", "needs_review", "review_reason")
_INT = ("row_id", "amount_minor", "superseded_by", "needs_review")


def parse(name: str, data: bytes) -> list:
    text = data.decode("utf-8")
    if name.endswith(".jsonl"):
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        header = set(rows[0]) if rows else set(REQUIRED_COLUMNS)
    else:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        header = set(reader.fieldnames or ())
        rows = list(reader)
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise db.Refusal("this is not a bank-feed ledger export (missing columns: "
                         + ", ".join(missing) + ")")
    out = []
    for r in rows:
        clean = {}
        for k in REQUIRED_COLUMNS:
            v = r.get(k)
            v = None if v in ("", None) else v
            if k in _INT and v is not None:
                v = int(v)
            clean[k] = v
        clean["needs_review"] = clean["needs_review"] or 0
        out.append(clean)
    return out


def end_lineage(conn, pid: int, how: str, snapshot_id=None) -> None:
    cur = conn.execute("UPDATE projections SET ended=?, ended_at=?, ended_snapshot=? WHERE pid=?"
                       " AND ended IS NULL", (how, db.now(), snapshot_id, pid))
    if cur.rowcount == 1:          # a lineage ends once; a second call records nothing
        lineage.add_residue(conn, pid, "ended", how)


def merge(conn, survivor: int, loser: int) -> None:
    for table in ("log", "aliases", "match_state", "residue"):
        conn.execute(f"UPDATE {table} SET pid=? WHERE pid=?", (survivor, loser))
    s = lineage.projection(conn, survivor)
    lo = lineage.projection(conn, loser)
    if (lo["class_observed_at"] or "") > (s["class_observed_at"] or ""):
        conn.execute("UPDATE projections SET class_tags_json=?, class_observed_at=?,"
                     " class_observed_snapshot=?,"
                     " last_known_kind=coalesce(?, last_known_kind) WHERE pid=?",
                     (lo["class_tags_json"], lo["class_observed_at"],
                      lo["class_observed_snapshot"], lo["last_known_kind"], survivor))
    if lo["search_state"] == "accepted-missing":
        conn.execute("UPDATE projections SET search_state='accepted-missing' WHERE pid=?",
                     (survivor,))
    conn.execute("UPDATE projections SET merged_into=? WHERE pid=?", (survivor, loser))
    lineage.add_residue(conn, survivor, "merged", f"#{loser}")


def _facts_fp(row: dict) -> str:
    return db.canonical(R.facts_of(row))


def check_delivered_bank_half(conn, by_id: dict) -> int:
    """For the latest DELIVERED package of each quarter, compare every
    delivered row's bank facts with the snapshot; a change raises one
    `delivered-changed` alert per occurrence (spec §"When the plugin may
    speak first"). Returns the number of new alerts."""
    latest = conn.execute(
        "SELECT p.package_id, p.quarter, p.filename FROM packages p"
        " JOIN deliveries d ON d.package_id=p.package_id AND d.status='delivered'"
        " WHERE p.package_id IN (SELECT max(p2.package_id) FROM packages p2 JOIN deliveries d2"
        "  ON d2.package_id=p2.package_id AND d2.status='delivered' GROUP BY p2.quarter)"
        " GROUP BY p.package_id").fetchall()
    return len(_delivered_changes(conn, by_id, latest))


def check_delivered_package(conn, package_id: int) -> list:
    """The pass's own change detection — BOTH halves, bank and classification —
    for ONE package that has just become delivered, against what the store holds
    now, inside the caller's transaction: a package reported delivered after a
    newer import or sweep is compared with them now, not one pass late. Returns
    the new alerts' ids."""
    pkg = conn.execute("SELECT package_id, quarter, filename FROM packages WHERE package_id=?",
                       (package_id,)).fetchall()
    by_id = {r["row_id"]: dict(r) for r in conn.execute("SELECT * FROM bank_rows")}
    return _delivered_changes(conn, by_id, pkg) + _kind_changes(conn, [package_id])


def _delivered_changes(conn, by_id: dict, packages) -> list:
    new = []
    for pkg in packages:
        for d in conn.execute("SELECT * FROM delivered_rows WHERE package_id=?",
                              (pkg["package_id"],)):
            row = by_id.get(d["row_id"])
            ended = None
            if d["pid"] is not None:
                p = lineage.projection(conn, lineage.resolve_pid(conn, d["pid"]))
                ended = p["ended"]
            if row is None or ended == "erased":
                # an erased lineage's row id may name another ledger's payment (re-bind)
                change = "erased"
            elif row["state"] != "active":
                change = row["state"]
            elif _facts_fp(row) != d["facts_fp"]:
                change = "corrected"
            else:
                continue
            key = f"delivered:{pkg['package_id']}:{d['row_id']}:{change}"
            cur = conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail,"
                               " raised_at) VALUES ('delivered-changed', ?, ?, ?)",
                               (key, db.canonical({"package": pkg["filename"],
                                                   "quarter": pkg["quarter"],
                                                   "row_id": d["row_id"], "change": change}),
                                db.now()))
            if cur.rowcount:
                new.append(cur.lastrowid)
    return new


def _require_same_ledger(conn, b, ledger_instance: str) -> None:
    """Ledger identity is bank-feed's ledger instance id (#69; plan §D4). The
    export names the instance its rows were read from, in the same snapshot;
    the store is bound to one instance. A fresh store binds at its first import
    (remember_ledger). A different instance — another file, or this one erased
    and re-minted by delete_all_data or delete_data_keep_signins, which cannot
    be told apart — imports nothing and ends nothing, unless the operator said
    "the bank ledger was reset": then the store RE-BINDS (every held lineage
    closed, the old aliases dropped). The acknowledgement is consumed by the
    next successful import whatever it met (round p3), and rolls back with a
    refused one."""
    import passes
    ack = b["ledger_reset_ack"]
    bound = b["ledger_instance"]
    if bound is not None and bound != ledger_instance and not ack:
        # Reachable mid-pass: the ledger probe is re-recorded with the new instance
        # after this pass's gate allowed the old one. The ledger switched under the
        # pass, so its bank writes stop. This runs before any write of the import:
        # poison rolls back the import's transaction, commits the verdict in its own
        # and re-opens BEGIN IMMEDIATE for the enclosing tx() to unwind.
        passes.poison(conn, "the bank ledger switched during this pass (instance "
                            f"{ledger_instance[:8]}…, bound to {bound[:8]}…); nothing more is "
                            "written until a pass proves the ledger again")
        raise db.Refusal(f"this export comes from ledger instance {ledger_instance[:8]}…, not "
                         f"the {bound[:8]}… this store was built on. Nothing was imported or "
                         "ended. If the bank ledger was wiped on purpose, the operator says "
                         "\"the bank ledger was reset\".")
    conn.execute("UPDATE binding SET ledger_reset_ack=0 WHERE id=1")   # rolls back with a refusal
    if bound is not None and bound != ledger_instance:
        _rebind(conn)


def _rebind(conn) -> None:
    """Close everything tied to the old ledger, inside the import's transaction.
    Every held lineage ends `erased` (a vanished one too: its row id belongs to
    the old ledger and must never be read or written again); aliases
    go, so nothing of the old ledger can address a row of the new one; the
    import's remember_ledger then binds the new instance."""
    for pid in [r[0] for r in conn.execute("SELECT pid FROM projections WHERE merged_into IS NULL")]:
        conn.execute("UPDATE projections SET ended='erased', ended_at=coalesce(ended_at, ?)"
                     " WHERE pid=?", (db.now(), pid))
        lineage.add_residue(conn, pid, "ended", "ledger reset")
        lineage.settle(conn, pid)
    conn.execute("DELETE FROM aliases")
    conn.execute("UPDATE binding SET ledger_instance=NULL, ledger_generation=NULL,"
                 " row_high_water=0 WHERE id=1")


def import_ledger_export(conn, *, path: str, token, ledger_instance: str) -> dict:
    import passes
    if token is None:
        raise db.Refusal("an import belongs to a pass: pass the pass_token from begin_pass")
    if not isinstance(ledger_instance, str) or not passes.LEDGER_RE.match(ledger_instance):
        raise db.Refusal("pass the export's `Ledger instance:` id as ledger_instance")
    try:
        name, data = casa_handoff.capture(path)
    except casa_handoff.HandoffError as exc:
        raise db.Refusal(f"that is not a handoff file ({exc.kind}): {exc}")
    rows = parse(name, data)
    # The custody lock is taken BEFORE any transaction (lock order: custody, then the
    # SQLite write lock — as ingest, reset_store, reap_orphans, the package build and
    # delivery staging take it): the import withdraws the staged bytes of every first
    # send it revokes, and commits the revocation, while no staging can put bytes back
    # (round E6, Terra + Astra S1). Held past the bound: Busy, and nothing imported.
    if conn.in_transaction:
        raise RuntimeError("import_ledger_export takes the custody lock before its transaction")
    with db.custody_lock(bound_s=db.LOCK_BOUND_S):
        return _import(conn, rows, token, ledger_instance)


def _import(conn, rows, token, ledger_instance) -> dict:
    import binding
    import delivery
    import passes
    # The gate is decided and PERSISTED before the import's transaction opens: a
    # refusal (gate_json, meta.gate_refusal) recorded inside it would roll back
    # with the Refusal below and stop sticking (plan §D11; Task 8 review).
    passes.check_token(conn, token)
    gate = passes.bank_write_gate(conn)
    if not gate["allowed"]:
        raise db.Refusal("nothing imported: " + gate["reason"])
    with db.tx(conn):
        passes.check_token(conn, token)
        cur_pass = passes.current_pass(conn)
        b = binding.get(conn)
        if b is None:
            raise db.Refusal("no account is bound yet")
        if not cur_pass["account_seen"]:
            raise db.Refusal("the bound account was not seen in this pass's list_accounts, so "
                             "nothing was imported and nothing was ended (not checked)")
        gate = passes.bank_write_gate(conn)          # this pass's persisted verdict
        if not gate["allowed"]:
            raise db.Refusal("nothing imported: " + gate["reason"])
        mine = [r for r in rows if r["account_id"] == b["account_id"]]
        by_id = {r["row_id"]: r for r in mine}
        max_id = max((r["row_id"] for r in rows), default=0)
        # The identity checks come BEFORE any write in this transaction: each one
        # proves the ledger changed under this pass, so it poisons the pass's bank
        # writes (as record_observation does). poison ROLLS BACK this transaction,
        # commits the verdict in its own, and re-opens BEGIN IMMEDIATE so the
        # enclosing tx() unwinds cleanly on the Refusal raised right after.
        probe = conn.execute("SELECT data_json FROM probes WHERE kind='ledger'").fetchone()
        probed = json.loads(probe["data_json"] or "{}").get("instance")
        if ledger_instance != probed:
            passes.poison(conn, "the export came from another bank ledger than the one "
                                "list_backups showed this pass; nothing more is written until a "
                                "pass proves the ledger again")
            raise db.Refusal("the export's ledger instance is not the one list_backups showed "
                             "this pass — nothing was imported (re-run the pass)")
        if b["ledger_instance"] in (None, ledger_instance):   # a re-bind drops the aliases
            held = conn.execute("SELECT row_id, first_seen FROM aliases").fetchall()  # any state
            for a in held:
                r = by_id.get(a["row_id"])
                if r is not None and a["first_seen"] and r["first_seen"] != a["first_seen"]:
                    passes.poison(conn, "the bank ledger changed during this pass (row "
                                        f"#{a['row_id']} is a different transaction); nothing "
                                        "more is written until a pass proves the ledger again")
                    raise db.Refusal(f"row #{a['row_id']} now names a different transaction "
                                     "than the one this store holds — nothing was imported")
        _require_same_ledger(conn, b, ledger_instance)          # may re-bind (drops aliases)
        old_facts = {r["row_id"]: dict(r) for r in conn.execute("SELECT * FROM bank_rows")}
        sync = conn.execute("SELECT ok, pass_id FROM probes WHERE kind='bank_sync'").fetchone()
        prev = conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id DESC"
                            " LIMIT 1").fetchone()
        bank_through = (db.now()[:10] if sync is not None and sync["ok"]
                        and sync["pass_id"] == cur_pass["pass_id"]
                        else (prev["bank_through"] if prev else None))
        sid = conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                           " bank_through) VALUES (?,?,?,?,?)",
                           (cur_pass["pass_id"], db.now(), len(mine), max_id,
                            bank_through)).lastrowid
        conn.execute("DELETE FROM bank_rows")
        for r in mine:
            conn.execute("INSERT INTO bank_rows(row_id, account_id, first_seen, booking_date,"
                         " value_date, amount_minor, currency, direction, status, counterparty,"
                         " remittance, state, superseded_by, needs_review, review_reason,"
                         " snapshot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["row_id"], r["account_id"], r["first_seen"], r["booking_date"],
                          r["value_date"], r["amount_minor"], r["currency"], r["direction"],
                          r["status"], r["counterparty"], r["remittance"], r["state"],
                          r["superseded_by"], r["needs_review"], r["review_reason"], sid))
        conn.execute("UPDATE binding SET row_high_water=max(row_high_water, ?)", (max_id,))
        conn.execute("UPDATE passes SET snapshot_id=? WHERE pass_id=?",
                     (sid, cur_pass["pass_id"]))
        out = {"snapshot": sid, "rows": len(mine), "admitted": [], "merged": [],
               "ended_vanished": [], "erase_candidates": [], "broken_floor": [],
               "delivered_changes": 0}

        # 1. resolve every live, un-ended lineage along superseded_by
        for pid in lineage.live_pids(conn):
            p = lineage.projection(conn, pid)
            if p["ended"]:
                continue
            r = by_id.get(p["dest_row_id"])
            if r is None:
                out["erase_candidates"].append({"pid": pid, "row_id": p["dest_row_id"],
                                                "facts": old_facts.get(p["dest_row_id"])})
                continue
            broken = False
            walked = {r["row_id"]}          # a malformed export may carry a cycle
            while r["state"] == "superseded" and r["superseded_by"] is not None:
                nxt = by_id.get(r["superseded_by"])
                if nxt is None or nxt["row_id"] in walked:
                    why = "absent" if nxt is None else "a cycle"
                    out["broken_floor"].append({"pid": pid, "row_id": r["row_id"],
                                                "missing": r["superseded_by"]})
                    conn.execute("UPDATE projections SET broken_floor=? WHERE pid=?",
                                 (f"#{r['row_id']} → #{r['superseded_by']} ({why})", pid))
                    lineage.add_residue(conn, pid, "broken-floor", f"#{r['superseded_by']}")
                    broken = True
                    break
                conn.execute("INSERT OR IGNORE INTO aliases(row_id, pid, first_seen)"
                             " VALUES (?,?,?)", (nxt["row_id"], pid, nxt["first_seen"]))
                walked.add(nxt["row_id"])
                r = nxt
            if broken:
                conn.execute("UPDATE projections SET dest_row_id=? WHERE pid=?",
                             (r["row_id"], pid))
            else:           # a floor that healed (the successor arrived) is no longer broken
                conn.execute("UPDATE projections SET dest_row_id=?, broken_floor=NULL"
                             " WHERE pid=?", (r["row_id"], pid))
            if not broken and r["state"] == "vanished":
                end_lineage(conn, pid, "vanished", sid)
                out["ended_vanished"].append(pid)

        # 2. fan-in: lineages that now share a destination merge into the lowest pid
        groups: dict = {}
        for pid in lineage.live_pids(conn):
            p = lineage.projection(conn, pid)
            if p["ended"]:
                continue          # an ended lineage's row id may name another ledger's row (round p4)
            groups.setdefault(p["dest_row_id"], []).append(pid)
        for dest, pids in sorted(groups.items()):
            if len(pids) > 1:
                survivor = min(pids)
                for loser in sorted(pids):
                    if loser != survivor:
                        merge(conn, survivor, loser)
                        out["merged"].append([survivor, loser])

        # 3. admission: every eligible ACTIVE row without a projection
        aliased = {a[0] for a in conn.execute("SELECT row_id FROM aliases")}
        for r in mine:
            if r["row_id"] in aliased or not lineage.eligible(conn, r):
                continue
            pid = conn.execute("INSERT INTO projections(dest_row_id, admitted_at,"
                               " admitted_snapshot) VALUES (?,?,?)",
                               (r["row_id"], db.now(), sid)).lastrowid
            conn.execute("INSERT INTO aliases(row_id, pid, first_seen) VALUES (?,?,?)",
                         (r["row_id"], pid, r["first_seen"]))
            out["admitted"].append(pid)

        # 4. every live lineage re-reduced against this snapshot (fingerprints, eligibility)
        lineage.settle_all(conn)
        passes.remember_ledger(conn, cur_pass["pass_id"])   # identity proved above
        out["delivered_changes"] = check_delivered_bank_half(conn, by_id)
        # 5. an unsent first send staged under an earlier snapshot is revoked in this
        # same commit (round E5, Terra S1): the plugin cannot hold a lock across the
        # external send, so the import takes the send away instead. Its bytes are
        # withdrawn BEFORE the commit, under the custody lock (round E6): no moment has
        # the revocation committed and the bytes still sendable. A commit that then
        # fails leaves a send that fails visibly, never one recorded delivered; a
        # withdrawal that fails refuses the import whole (round E7).
        revoked = delivery.revoke_superseded_first_sends(conn, sid)
        out["revoked_deliveries"] = [r["delivery_id"] for r in revoked]
        # every revoked delivery whose bytes are still there is withdrawn now; one that
        # cannot be refuses the whole import, which rolls back (round E7)
        delivery.withdraw_revoked(conn)
        return out


def check_delivered_kind_half(conn, pid: int) -> int:
    """The classification half of "a delivered quarter changed underneath":
    the expectation kind a delivered row shipped under, against the one the
    lineage's latest classification observation derives (spec §"What a pass
    works on"; round 26 — the snapshot carries no tags). Unknown is not a
    change (the last known kind stands). A row shipped unclassified — its
    expectation unknown, or not re-read since the import — is compared against
    the last kind known when it shipped (delivered_rows.kind); one shipped with
    no kind ever known is skipped: there is nothing to have changed from (fix
    wave F: a row read again unchanged raised "now categorised differently").

    delivered_rows.pid is not re-pointed by a merge, so a delivered row
    belongs to this lineage when its pid RESOLVES to it (as in
    check_delivered_bank_half). An ended lineage derives no kind: an erased
    one is reported once as "erased" by the bank half."""
    latest = [r[0] for r in conn.execute(
        "SELECT max(p2.package_id) FROM packages p2 JOIN deliveries d2"
        " ON d2.package_id=p2.package_id AND d2.status='delivered' GROUP BY p2.quarter")]
    return len(_kind_changes(conn, latest, pid))


def _kind_changes(conn, package_ids, pid=None) -> list:
    """The classification half over `package_ids`' delivered rows (only those of the
    lineage `pid`, when given). Returns the new alerts' ids."""
    new = []
    marks = ",".join("?" * len(package_ids)) or "NULL"
    for d in conn.execute(
            "SELECT d.*, pk.filename, pk.quarter FROM delivered_rows d JOIN packages pk"
            " ON pk.package_id=d.package_id WHERE d.pid IS NOT NULL AND d.package_id IN"
            f" ({marks}) ORDER BY d.package_id, d.row_id", list(package_ids)).fetchall():
        own = lineage.resolve_pid(conn, d["pid"])
        if pid is not None and own != lineage.resolve_pid(conn, pid):
            continue
        p = lineage.projection(conn, own)
        if p["exp_kind"] is None or p["ended"] or d["kind"] is None \
                or d["kind"] == p["exp_kind"]:
            continue
        key = f"delivered:{d['package_id']}:{d['row_id']}:kind:{p['exp_kind']}"
        cur = conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                           " VALUES ('delivered-changed', ?, ?, ?)",
                           (key, db.canonical({"package": d["filename"], "quarter": d["quarter"],
                                               "row_id": d["row_id"], "change": "reclassified"}),
                            db.now()))
        if cur.rowcount:
            new.append(cur.lastrowid)
    return new
