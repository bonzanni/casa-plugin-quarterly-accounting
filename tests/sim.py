# tests/sim.py
"""The specialist's side of the sweep, done mechanically against a REAL
bank-feed (tests/bankfeed.py). This is the executable reference for the
procedure SKILL.md prescribes; the skill must say exactly this, in words:

  list_projections -> for each item (until remaining_in_cycle is 0 or the room runs
  out; triage then judges only fresh items — sweep_within, run_pass(sweep_budget=...)):
    get_transaction(row_id); "no transaction #N" -> record_observation(not_found)
    else record_observation(observed_tags, observed_notes, observed_first_seen)
    every record_observation carries list_projections' snapshot_id (the import's
      `snapshot` for an erase candidate); a newer import refuses it
    make the ONE returned write (untag, tag or add_note) with workflow,
      expected_generation and expected_ledger exactly as returned
    a write that did not take (the tags or the note are not on the row after it)
      -> record_observation(write_error=<reply>); nothing more for that row
    read the row again and record_observation again; repeat until nothing is
      returned (plan §D14; round p5: never two writes without a read between)
"""
from __future__ import annotations

import sweep


def _read(bf, row_id):
    out = bf.call("get_transaction", row_id=row_id)
    if out.startswith("no transaction #"):
        return None
    first_seen = bf.conn.execute("SELECT first_seen FROM transactions WHERE row_id=?",
                                 (row_id,)).fetchone()[0]
    return bf.tags(row_id), bf.notes(row_id), first_seen


def observe_and_repair(conn, bf, token, item, snapshot_id) -> dict:
    """Read, record, make the ONE returned write, read again, record again —
    until the server returns nothing to do (at most untag, tag and note). Every
    record carries the snapshot_id list_projections returned."""
    pid, row_id = item["pid"], item["row_id"]

    def rec(**kw):
        return sweep.record_observation(conn, pid=pid, token=token, snapshot_id=snapshot_id,
                                        **kw)

    if item["ended"] == "erased":
        return {}
    got = _read(bf, row_id)
    if got is None:
        return rec(not_found=True)
    r = {}
    for _ in range(4):
        tags, notes, first_seen = got
        r = rec(observed_tags=tags, observed_notes=notes, observed_first_seen=first_seen)
        ins = r.get("instructions") or {}
        if not ins:
            return r
        kw = {"workflow": ins["workflow"], "expected_generation": ins["expected_generation"],
              "expected_ledger": ins["expected_ledger"]}
        if "untag" in ins:
            out = bf.call("untag_transaction", row_ids=[row_id], tags=ins["untag"], **kw)
            if set(ins["untag"]) & set(bf.tags(row_id)):
                return rec(write_error=out)
        elif "tag" in ins:
            out = bf.call("tag_transaction", row_ids=[row_id], tags=ins["tag"], **kw)
            if not set(ins["tag"]) <= set(bf.tags(row_id)):
                return rec(write_error=out)
        else:
            out = bf.call("add_note", row_ids=[row_id], note=ins["add_note"], author="agent",
                          **kw)
            if ins["add_note"] not in bf.notes(row_id):
                return rec(write_error=out)
        got = _read(bf, row_id)
        if got is None:
            return rec(not_found=True)
    return r


def sweep_cycle(conn, bf, token, limit=25) -> int:
    n = 0
    while True:
        page = sweep.list_projections(conn, token=token, limit=limit)
        for item in page["projections"]:
            observe_and_repair(conn, bf, token, item, page["snapshot_id"])
            n += 1
        if page["remaining_in_cycle"] == 0:
            return n


def sweep_within(conn, bf, token, budget) -> int:
    """The sweep of SKILL.md step 5 for a specialist that has room for `budget`
    reads: stops when remaining_in_cycle is 0 or the budget is spent. Returns
    what remains (0: every payment was read since this pass's import)."""
    n = 0
    while True:
        page = sweep.list_projections(conn, token=token, limit=max(1, min(25, budget - n)))
        for item in page["projections"]:
            observe_and_repair(conn, bf, token, item, page["snapshot_id"])
            n += 1
        if page["remaining_in_cycle"] == 0 or n >= budget:
            return page["remaining_in_cycle"]


# --- a whole pass, mechanically (Task 23) -----------------------------------
# The specialist's delegation in the order plan §D5 fixes: the probes (with
# sync), tx-classifier's own drain (waited for, never done here),
# export_history + import_ledger_export, erase confirmations, the sweep
# (observations and tag repair), triage of the fresh items. Only triage's judgment is
# replaced: `triage` applies the auto-match bar of SKILL.md step 6 to filed
# metadata (the real specialist reads the PDFs). Every bank-feed interaction
# is the real one.
import re  # noqa: E402

import binding  # noqa: E402
import db  # noqa: E402
import documents  # noqa: E402
import ledger  # noqa: E402
import matches  # noqa: E402
import passes  # noqa: E402
import work  # noqa: E402


def ledger_state(listing: str) -> dict:
    """The ledger probe's data from ONE list_backups answer (spec: generation,
    registrations and instance are captured together, under bank-feed's locks),
    each value read by its label — bank-feed may prepend sentences."""
    gen = re.search(r"^Restore generation: (\d+)$", listing, re.M)
    inst = re.search(r"^Ledger instance: ([0-9a-f]{32})$", listing, re.M)
    registered = {}
    if "Registered workflows:" in listing:
        block = listing.split("Registered workflows:", 1)[1].split("Restores:", 1)[0]
        for line in block.splitlines():
            m = re.match(r"\s+(\S+) -> (\S+)", line)
            if m:
                registered[m.group(1)] = m.group(2)
    return {"generation": int(gen.group(1)) if gen else None, "registered": registered,
            "instance": inst.group(1) if inst else None}


def probe(conn, bf, token, sync=None):
    """SKILL.md step 1, in its order: bank-feed's tools, list_accounts (with the
    category label_account wrote), sync and the probe of ITS outcome (the
    import stamps bank_through from it), then ONE list_backups for the ledger
    probe. `sync` stands in for bank-feed's sync: None means the ledger was
    already fetched (the tests' bf.fetch); a callable returning False or
    raising is a failed sync."""
    accounts = [{"account_id": r["account_id"], "category": r["category"], "label": r["name"]}
                for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
    passes.record_probe(conn, token, "bank_tools", True)
    passes.record_probe(conn, token, "bank_accounts", True, data={"accounts": accounts})
    ok, detail = True, ""
    if sync is not None:
        try:
            ok = sync() is not False
            detail = "" if ok else "sync failed"
        except Exception as exc:          # what a failed sync reply reports
            ok, detail = False, f"sync failed: {exc}"
    passes.record_probe(conn, token, "bank_sync", ok, detail)
    state = ledger_state(bf.listing())
    passes.record_probe(conn, token, "ledger", True, data=state)   # the gate judges a missing instance


def _fits(item, doc):
    if doc["kind"] != item["expectation"]["kind"] or doc["amount_minor"] != item["amount_minor"]:
        return False
    if doc.get("currency") and doc["currency"] != item["currency"]:
        return False
    from datetime import date
    a, b = date.fromisoformat(item["date"]), date.fromisoformat(doc["document_date"])
    return abs((a - b).days) <= 10


def lineage_row(conn, pid):
    return conn.execute("SELECT dest_row_id FROM projections WHERE pid=?", (pid,)).fetchone()[0]


def _bank_row(bf, row_id):
    """The row as get_transaction reads it now (the pass's re-read before a match)."""
    return dict(bf.conn.execute("SELECT * FROM transactions WHERE row_id=?",
                                (row_id,)).fetchone())


def _identical_pairing(conn, item, doc):
    """A machine pairing of an identical payment with an identical document: the
    new pair is ambiguous, so neither is picked (spec §Weekly pass, the
    ambiguous pair across two passes)."""
    for pid in [r[0] for r in conn.execute("SELECT pid FROM projections WHERE status='matched'"
                                           " AND merged_into IS NULL AND pid<>?", (item["pid"],))]:
        other = work.describe(conn, pid)
        if other["current"]["author"] != "auto":
            continue                  # an operator's pairing is never demoted by the machine
        od = other["current"]["document"]
        if (other["counterparty"], other["amount_minor"], other["date"]) == (
                item["counterparty"], item["amount_minor"], item["date"]) and (
                od["issuer"], od["amount_minor"], od["date"]) == (
                doc["issuer"], doc["amount_minor"], doc["document_date"]):
            return pid
    return None


def triage(conn, bf, token) -> dict:
    """The auto-match bar of SKILL.md step 6, on filed metadata (the real
    specialist reads the PDFs). Deterministic, so a test can predict it."""
    done = {"matched": [], "proposed": [], "not_fresh": []}
    for item in work.triage(conn):
        if item["pending"]:
            continue
        if not item["fresh"]:             # not read since this import: a later pass judges it
            done["not_fresh"].append(item["pid"])
            continue
        docs = [d for d in documents.list_unmatched(conn, limit=500)["documents"] if _fits(item, d)]
        if not docs:
            continue
        doc = docs[0]
        snap = _bank_row(bf, lineage_row(conn, item["pid"]))
        twin = _identical_pairing(conn, item, doc)
        kw = dict(pid=item["pid"], doc_id=doc["doc_id"], expected_revision=item["revision"],
                  row_snapshot=snap, token=token,
                  runners_up=[f"{d['document_number']} ({d['document_date']})" for d in docs[1:]],
                  labels=("guessed",) if len(docs) > 1 else ("clean",))
        if twin is not None:
            matches.propose_match(conn, **kw)
            other = work.describe(conn, twin)
            matches.propose_match(conn, pid=twin, doc_id=other["current"]["document"]["doc_id"],
                                  expected_revision=other["revision"],
                                  row_snapshot=_bank_row(bf, lineage_row(conn, twin)),
                                  token=token)
            done["proposed"] += [item["pid"], twin]
        else:
            matches.record_match(conn, author="auto", **kw)
            done["matched"].append(item["pid"])
    return done


def run_pass(conn, bf, trigger="cron", sync=None, sweep_budget=None) -> dict:
    """One specialist delegation, in plan §D5's order. `sync` stands in for
    bank-feed's sync (run between list_accounts and list_backups). With a
    `sweep_budget` the sweep may stop short; then, as SKILL.md step 6 says,
    triage judges only the fresh items and the pass ends `interrupted` (fix E2)."""
    token = passes.begin_pass(conn, trigger)["pass_token"]
    probe(conn, bf, token, sync)
    setup = binding.check_setup(conn)
    gate = setup["bank_writes"]
    if not setup["can_run"] or not gate["allowed"]:
        end = passes.end_pass(conn, token, "stopped", {"conditions": setup["conditions"]})
        return {"token": token, "import": None, "gate": gate, "triage": None, "end": end,
                "conditions": setup["conditions"]}
    # tx-classifier drains its own queue on sync's trailer, in this same
    # session: the pass waits for it and never classifies (spec §Weekly pass
    # step 2). In these tests the rows are tagged before the pass.
    path = bf.export()
    try:
        imp = ledger.import_ledger_export(conn, path=path, token=token,
                                          ledger_instance=bf.last_export_instance)
    except db.Refusal as exc:
        # the ledger switched or changed under the pass: nothing was imported,
        # the pass stops (its bank writes were poisoned by the import)
        return _stopped(conn, token, gate, None, None, exc)
    try:
        for c in imp["erase_candidates"]:
            if bf.call("get_transaction", row_id=c["row_id"]).startswith("no transaction #"):
                sweep.record_observation(conn, pid=c["pid"], token=token,
                                         snapshot_id=imp["snapshot"], not_found=True)
        if sweep_budget is None:
            sweep_cycle(conn, bf, token)
            remaining = 0
        else:
            remaining = sweep_within(conn, bf, token, sweep_budget)
    except db.Refusal as exc:
        return _stopped(conn, token, gate, imp, None, exc)
    tri = triage(conn, bf, token)
    if remaining:
        end = passes.end_pass(conn, token, "interrupted", {})
        return {"token": token, "import": imp, "gate": gate, "triage": tri, "end": end,
                "remaining": remaining}
    try:
        sweep_cycle(conn, bf, token)      # the annotations for what triage just decided
    except db.Refusal as exc:
        return _stopped(conn, token, gate, imp, tri, exc)
    end = passes.end_pass(conn, token, "complete", {})
    return {"token": token, "import": imp, "gate": gate, "triage": tri, "end": end}


def _stopped(conn, token, gate, imp, tri, exc) -> dict:
    """A refusal is the server saying stop (the ledger switched or changed under
    the pass, and its bank writes are poisoned): END the pass, stopped, so its
    marker does not answer "Already checking" to the operator for hours."""
    end = passes.end_pass(conn, token, "stopped", {"refused": str(exc)})
    return {"token": token, "import": imp, "gate": gate, "triage": tri, "end": end,
            "refused": str(exc)}
