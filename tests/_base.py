"""Shared test scaffolding. Puts server/ first on sys.path; every test gets a
fresh data dir, handoff folder and outbox, and os.environ restored after."""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))


class TempEnv(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = pathlib.Path(self._tmpdir.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.handoff = self.tmp / "handoff"
        self.handoff.mkdir(mode=0o770)
        self.outbox = self.tmp / "outbox"
        self.outbox.mkdir(mode=0o770)
        saved = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(saved)
        self.addCleanup(restore)
        os.environ["CLAUDE_PLUGIN_DATA"] = str(self.data)
        os.environ["CASA_HANDOFF_DIR"] = str(self.handoff)
        os.environ["CASA_PLUGIN_OUTBOX_DIR"] = str(self.outbox)

    def publish(self, name, data, producer="gmail"):
        import casa_handoff
        return casa_handoff.publish(producer, name, data=data)["path"]


class StoreCase(TempEnv):
    def setUp(self):
        super().setUp()
        import db
        self.conn = db.open_store()
        self.addCleanup(self.conn.close)
        import tools
        tools._CONN = self.conn
        self.addCleanup(setattr, tools, "_CONN", None)

    def bind(self, account="acc-biz", label="Zakelijk", watermark="2026-07-01"):
        import binding
        import db
        binding.bind_account(self.conn, account, label)
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark=?", (watermark,))

    LEDGER = "a" * 32             # the bank-feed ledger instance id the fixtures bind to

    @contextlib.contextmanager
    def patch_clock(self, at):
        """db._clock returns `at` inside the block; the real clock is restored after."""
        import db
        real = db._clock
        db._clock = lambda: at
        try:
            yield at
        finally:
            db._clock = real

    def handed(self, *pids):
        """Put payments in the live pass's open Gmail chunk (issue #26, A4: a search is
        recorded only for handed work), as a continuation's hand-out would."""
        hand(self.conn, pids)

    def pass_(self, trigger="test", generation=0, registered=None, accounts=None,
              instance=None):
        """End any live pass, begin a new one, record the probes a real pass
        records first (the ledger probe carries list_backups' instance id).
        Returns the new pass token."""
        import passes
        self.end_live_pass()
        kw = {"quarter": "2026-Q3", "channel": "telegram"} if trigger == "package" else {}
        token = passes.begin_pass(self.conn, trigger, **kw)["pass_token"]
        b = self.conn.execute("SELECT account_id FROM binding").fetchone()
        accts = accounts if accounts is not None else (
            [{"account_id": b[0], "category": "company", "label": "Zakelijk"}] if b else [])
        passes.record_probe(self.conn, token, "bank_tools", True)
        passes.record_probe(self.conn, token, "bank_sync", True)
        passes.record_probe(self.conn, token, "bank_accounts", True, data={"accounts": accts})
        passes.record_probe(self.conn, token, "ledger", True,
                            data={"generation": generation, "registered": registered or {},
                                  "instance": instance or self.LEDGER})
        return token

    def start_job_pass(self, token, trigger="operator"):
        """A job pass started under claim `token` (S2 §3), held by that claim's job id —
        as the job cursor's _begin_next starts one. Returns its pass_id."""
        import db
        import json
        import passes
        with db.tx(self.conn):
            _, pass_id = passes.start_pass(self.conn, trigger,
                                           "silent" if trigger == "cron" else "telegram",
                                           protocol="job", token=token)
            job_id = self.conn.execute("SELECT job_id FROM claims WHERE gen=?",
                                       (token,)).fetchone()[0]
            self.conn.execute("UPDATE passes SET holder_job=?, adopters_json=? WHERE pass_id=?",
                              (job_id, json.dumps([job_id]), pass_id))
        return pass_id

    def bind_round_and_take(self, pass_id):
        """The queued package request's round runs in `pass_id` (passes._bind_round), and
        the pass takes what it may (asks.take_queued) — as the job cursor starts a round."""
        import asks
        import db
        import passes
        with db.tx(self.conn):
            rid = self.conn.execute("SELECT request_id FROM package_requests WHERE"
                                    " state='queued' ORDER BY request_id").fetchone()[0]
            passes._bind_round(self.conn, rid, pass_id)
            return asks.take_queued(self.conn, pass_id)

    def hand_empty_chunk(self):
        """The live pass's continuation hands out an empty Gmail chunk (nothing to search)."""
        hand(self.conn, [])

    def end_live_pass(self):
        """End the live pass, if any, with its current token (the marker's: a claim
        rotates it past the pass's own generation)."""
        import passes
        m = self.conn.execute("SELECT generation, live FROM pass_marker").fetchone()
        if m is not None and m["live"]:
            close_chunk(self.conn)
            passes.end_pass(self.conn, m["generation"], "complete", {})

    def end_with_counts(self, token, outcome, counts):
        """End the live pass and store `counts` as its report, as the server computes them
        for a check (issue #29: end_pass never stores the caller's) — for a view test that
        needs an interrupted check's counts without running its Gmail round."""
        import passes
        close_chunk(self.conn)
        out = passes.end_pass(self.conn, token, outcome, {})
        import db
        import json
        with db.tx(self.conn):
            row = self.conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                                    (out["ended"],)).fetchone()
            rep = {**json.loads(row[0] or "{}"), **counts}
            self.conn.execute("UPDATE passes SET report_json=? WHERE pass_id=?",
                              (db.canonical(rep), out["ended"]))
        return out

    def package_token(self, quarter="2026-Q3", channel="telegram"):
        """A package request as the skill makes one — begin_pass(package, quarter,
        channel), the snapshot step with this pass's own import (a bare snapshots row
        here), the Gmail round's probe, a whole judge step, end_pass (issue #15). Returns
        the package_token end_pass hands over."""
        import passes
        self.end_live_pass()
        token = passes.begin_pass(self.conn, "package", quarter=quarter,
                                  channel=channel)["pass_token"]
        import db
        import steps
        steps.start(self.conn, token, "snapshot", {})
        with db.tx(self.conn):          # this pass's own import: what makes a request buildable
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES ((SELECT pass_id FROM pass_marker), ?, 0, 0)",
                              (db.now(),))
        steps.finish(self.conn, token, "snapshot", counts={})
        self.check_round(token)
        return passes.end_pass(self.conn, token, "complete", {})["package_token"]

    def package_built_unsent(self, quarter="2026-Q3", channel="telegram"):
        """A package request whose package was built and never staged, its holder gone:
        package_token(), build_quarterly_package under it (state `built`), then the
        request's lease set to a lapsed time, so a sends claim may take it (S2 §6.4)."""
        import datetime as _dt
        import db
        import package
        import steps
        if self.conn.execute("SELECT 1 FROM binding").fetchone() is None:
            self.bind()                     # a build needs the bound account
        token = self.package_token(quarter, channel)
        package.build_quarterly_package(self.conn, quarter, token)
        lapsed = steps._stamp(db._clock() - _dt.timedelta(seconds=steps.LEASE_S + 60))
        with db.tx(self.conn):
            self.conn.execute("UPDATE package_requests SET lease_at=? WHERE state='built'",
                              (lapsed,))
        return token

    def check_round(self, token, gmail_ok=True, triage_remaining=0):
        """The rest of a package round (issue #15): Ellen's Gmail round (its probe) and
        the judge step, finished whole."""
        import passes
        import steps
        hand(self.conn, [])         # the continuation's hand-out (here, nothing to search)
        passes.record_probe(self.conn, token, "gmail", gmail_ok)
        steps.start(self.conn, token, "judge", {})
        steps.finish(self.conn, token, "judge", counts={"triage_remaining": triage_remaining})

    _doc_n = 0

    def row(self, row_id, **over):
        import db
        r = {"row_id": row_id, "account_id": "acc-biz", "first_seen": "2026-07-01T00:00:00Z",
             "booking_date": "2026-07-03", "value_date": "2026-07-03", "amount_minor": 10000,
             "currency": "EUR", "direction": "DBIT", "status": "BOOK", "counterparty": "Adobe",
             "remittance": "", "state": "active", "superseded_by": None, "needs_review": 0,
             "review_reason": None, "snapshot_id": 0}
        r.update(over)
        with db.tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO bank_rows(%s) VALUES (%s)"
                              % (",".join(r), ",".join("?" * len(r))), tuple(r.values()))
        return r

    def seed_payments(self, rows, tags=("software",)):
        """Bind, then admit one searched, classified, settled payment per dict in `rows`
        (bank-row overrides, e.g. counterparty / amount_minor). Returns the pids."""
        import db
        import work
        self.bind()
        token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p1', 'x', 0, 0, '2026-09-20')")
        pids = []
        for n, over in enumerate(rows, 1):
            self.row(n, booking_date="2026-09-14", value_date="2026-09-14", **over)
            pid = self.lineage_for(n)
            self.classify(pid, set(tags))
            with db.tx(self.conn):
                self.conn.execute("UPDATE projections SET class_observed_at=? WHERE pid=?",
                                  ("2026-09-20T10:00:00Z", pid))
            self.settle(pid)
            self.handed(pid)
            work.record_search(self.conn, pid=pid, token=token, queries=["x"])
            pids.append(pid)
        return pids

    def lineage_for(self, row_id):
        import db
        with db.tx(self.conn):
            cur = self.conn.execute("INSERT INTO projections(dest_row_id, admitted_at)"
                                    " VALUES (?, ?)", (row_id, db.now()))
            pid = cur.lastrowid
            self.conn.execute("INSERT INTO aliases(row_id, pid, first_seen) VALUES (?,?,?)",
                              (row_id, pid, "2026-07-01T00:00:00Z"))
        return pid

    def doc(self, kind="invoice", **over):
        import db
        StoreCase._doc_n += 1
        d = {"sha256": "%064x" % (StoreCase._doc_n + id(self)), "ext": "pdf", "size": 10,
             "kind": kind, "counterparty": "Adobe", "issuer": "Adobe",
             "document_date": "2026-07-02", "document_number": "N%d" % StoreCase._doc_n,
             "amount_minor": 10000, "currency": "EUR", "recipient": "Voorbeeld BV",
             "source": "gmail", "extraction_author": "resident",
             "ingested_at": db.now(), "ingest_quarter": "2026-Q3"}
        d.update(over)
        with db.tx(self.conn):
            cur = self.conn.execute("INSERT INTO documents(%s) VALUES (%s)"
                                    % (",".join(d), ",".join("?" * len(d))), tuple(d.values()))
        return cur.lastrowid

    def classify(self, pid, tags):
        import db
        import json
        with db.tx(self.conn):
            # an observation made now: after the latest import, so fresh (fix E2)
            self.conn.execute("UPDATE projections SET class_tags_json=?, class_observed_at=?,"
                              " class_observed_snapshot=(SELECT coalesce(max(snapshot_id), 0)"
                              " FROM snapshots) WHERE pid=?",
                              (json.dumps(sorted(tags)), db.now(), pid))

    def settle(self, pid):
        import db
        import lineage
        with db.tx(self.conn):
            return lineage.settle(self.conn, pid)

    EXPORT_COLS = ("row_id", "account_id", "provider_ref", "provider_ref_kind", "match_method",
                   "match_confidence", "needs_review", "review_reason", "state_reason",
                   "identity_key", "occurrence", "booking_date", "value_date", "amount_minor",
                   "currency", "direction", "status", "counterparty", "remittance",
                   "first_seen", "last_seen", "state", "superseded_by", "tags", "tag_revision")

    def export_csv(self, rows):
        """A synthetic export with bank-feed 0.20.0's column set (raw_json
        excluded, as its EXPORT_EXCLUDE says; each row's tags comma-joined and its
        tag_revision appended, casa-specialist-finance#86). Used only where a test pins
        this plugin's resolution logic; bank-feed behaviour is pinned in
        test_ledger_real.py against the real tree. Published straight through
        casa_handoff, as bank-feed's export_history does."""
        import casa_handoff
        import csv
        import io
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=self.EXPORT_COLS)
        w.writeheader()
        for r in rows:
            full = {"account_id": "acc-biz", "needs_review": 0, "identity_key": "ik%d" % r["row_id"],
                    "occurrence": 0, "booking_date": "2026-07-03", "value_date": "2026-07-03",
                    "amount_minor": 10000, "currency": "EUR", "direction": "DBIT",
                    "status": "BOOK", "counterparty": "Adobe", "remittance": "",
                    "first_seen": "2026-07-03T08:00:00Z", "last_seen": "2026-07-03T08:00:00Z",
                    "state": "active", "superseded_by": "", "tags": "", "tag_revision": 0}
            full.update(r)
            if isinstance(full["tags"], (list, tuple)):
                full["tags"] = ",".join(sorted(full["tags"]))
            w.writerow({k: ("" if full.get(k) is None else full.get(k, "")) for k in self.EXPORT_COLS})
        return casa_handoff.publish("bank-feed", "ledger-export-test.csv",
                                    data=buf.getvalue().encode())["path"]

    def sweep_to_zero(self):
        """List and observe the live pass's sweep until nothing is due, each read showing
        the row's desired tags and its note (no write owed). The last listing finds
        nothing due, so the cycle is complete and the import's sweep is stamped."""
        import sweep
        token = self.conn.execute("SELECT generation FROM pass_marker").fetchone()[0]
        for _ in range(50):
            page = sweep.list_projections(self.conn, token=token)
            if not page["projections"] and page["remaining_in_cycle"] == 0:
                return
            for item in page["projections"]:
                first_seen = self.conn.execute("SELECT first_seen FROM aliases WHERE row_id=?",
                                               (item["row_id"],)).fetchone()[0]
                out = sweep.record_observation(
                    self.conn, pid=item["pid"], token=token, snapshot_id=page["snapshot_id"],
                    observed_tags=item["desired"],
                    observed_notes=[item["note"]] if item["note"] else [],
                    observed_first_seen=first_seen, observed_tag_revision=0)
                assert not out.get("instructions"), out
        raise AssertionError("the sweep did not reach zero")

    def only_pid(self):
        """The single live lineage's pid."""
        import lineage
        (pid,) = lineage.live_pids(self.conn)
        return pid

    def machine_match(self, pid, doc_id, token):
        """A machine pairing of `doc_id` with payment `pid`, as triage makes one: the
        item's row_digest and revision from list_quarter_state, the document's date."""
        import matches
        import work
        item = work.list_quarter_state(self.conn, pid=pid)["item"]
        date = self.conn.execute("SELECT document_date FROM documents WHERE doc_id=?",
                                 (doc_id,)).fetchone()[0]
        return matches.record_match(self.conn, pid=pid, doc_id=doc_id, author="auto",
                                    expected_revision=item["revision"],
                                    row_digest=item["row_digest"], document_date=date,
                                    token=token)

    def show(self, *pids):
        """What build_review + a successful send + mark_rendering_delivered
        leave behind (Task 16 builds the real path; this fixture writes the
        same rows so the match tools can be tested before it exists)."""
        import db
        import json
        rid = "r-test-%d" % (self.conn.execute("SELECT COUNT(*) FROM renders").fetchone()[0] + 1)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " delivered_at, text, membership_json, delivered_seq)"
                              " VALUES (?,?,?,?,?,?,?,?)",
                              (rid, "status", "{}", db.now(), db.now(), "",
                               json.dumps(list(pids)), db.next_seq(self.conn)))
            for pid in pids:
                prev = self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                         (pid,)).fetchone()[0]
                mrevs = {str(r[0]): r[1] for r in self.conn.execute(
                    "SELECT match_id, revision FROM match_state WHERE pid=?", (pid,))}
                self.conn.execute("INSERT INTO render_items VALUES (?,?,?,?)",
                                  (rid, pid, prev, json.dumps(mrevs)))
                self.conn.execute("INSERT OR REPLACE INTO shown VALUES (?,?,?,?,?)",
                                  (pid, rid, prev, json.dumps(mrevs), db.now()))
        return rid

    def rev(self, pid=None, match_id=None):
        if match_id is not None:
            return self.conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                                     (match_id,)).fetchone()[0]
        return self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                 (pid,)).fetchone()[0]

    def snapshot(self, pid):
        import lineage
        return lineage.live_row(self.conn, lineage.projection(self.conn, pid))


def hand(conn, pids):
    """The open Gmail chunk of the live pass, holding `pids` too (see StoreCase.handed).
    A pass with no first step gets a finished sweep step to carry it."""
    import json
    import db
    import passes
    m = passes._marker(conn)
    first = "snapshot" if conn.execute(
        "SELECT 1 FROM package_requests WHERE pass_id=? AND state='snapshot'",
        (m["pass_id"],)).fetchone() else "sweep"
    with db.tx(conn):
        row = conn.execute("SELECT carry_json FROM pass_steps WHERE pass_id=? AND step=?",
                           (m["pass_id"], first)).fetchone()
        carry = json.loads(row[0] or "{}") if row is not None else {}
        old = carry.get("chunk") or {}
        keep = old.get("pids", []) if old.get("open") else []
        carry["chunk"] = {"pids": sorted(set(keep) | set(pids)), "recorded": [],
                          "open": True, "calls": None, "more": 0}
        if row is None:
            conn.execute("INSERT INTO pass_steps(pass_id, step, started_at, finished_at,"
                         " finished_by, finish_json, carry_json) VALUES (?,?,?,?,?,?,?)",
                         (m["pass_id"], first, db.now(), db.now(), "specialist", "{}",
                          db.canonical(carry)))
        else:
            conn.execute("UPDATE pass_steps SET carry_json=? WHERE pass_id=? AND step=?",
                         (db.canonical(carry), m["pass_id"], first))


def close_chunk(conn):
    """Close the live pass's open Gmail chunk, as a judge start does (test setup only)."""
    import json
    import db
    import passes
    m = passes._marker(conn)
    if m is None or not m["live"]:
        return
    with db.tx(conn):
        for r in conn.execute("SELECT step, carry_json FROM pass_steps WHERE pass_id=?"
                              " AND step IN ('sweep', 'snapshot')", (m["pass_id"],)).fetchall():
            carry = json.loads(r["carry_json"] or "{}")
            if carry.get("chunk", {}).get("open"):
                carry["chunk"]["open"] = False
                conn.execute("UPDATE pass_steps SET carry_json=? WHERE pass_id=? AND step=?",
                             (db.canonical(carry), m["pass_id"], r["step"]))
