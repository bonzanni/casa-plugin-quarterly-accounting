"""Shared test scaffolding. Puts server/ first on sys.path; every test gets a
fresh data dir, handoff folder and outbox, and os.environ restored after."""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))

import db  # noqa: E402  (server/ is on sys.path just above)


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
        with db.tx(self.conn):
            binding.bind_in_tx(self.conn, account, label, grant=self.grant())
            self.conn.execute("UPDATE binding SET watermark=?", (watermark,))

    @staticmethod
    def grant():
        """S7 §8.1: the operator's authority, as a keyed tap holds it. Tests are the one
        place outside taps.py that construct one (the grep pin reads server/ only)."""
        import authority
        return authority.OperatorGrant("verdict", "test")

    def granted(self, fn, *args, **kw):
        """An operator write (an *_in_tx form) in its own transaction, under a test grant —
        what the public wrappers did before S7 §8.1 removed them."""
        import db
        with db.tx(self.conn):
            return fn(self.conn, *args, grant=self.grant(), **kw)

    def run_job_to_complete(self, job_id, started_by="operator") -> list:
        """Job run `job_id`, driven by the simple-loop simulator (tests/sim_job.py) from its
        claim until `complete`. Binds the account and builds the driver on first use."""
        return self.drive(job_id, started_by=started_by)

    def drive(self, job_id, deliver=True, bank_tools=True, stop_before=None,
              started_by="operator") -> list:
        """Job run `job_id` through the simulator: `deliver` — each posted rendering's
        receipt arrives and is marked; `bank_tools=False` — the probes find no bank-feed
        tools; `stop_before` — return when a unit of that kind is handed out (not done).
        The units handed out."""
        if getattr(self, "_job_driver", None) is None:
            from tests.sim_job import JobDriver
            if self.conn.execute("SELECT 1 FROM binding").fetchone() is None:
                self.bind()
            self._job_driver = JobDriver(self)
        drv = self._job_driver
        drv.deliver = deliver
        if not bank_tools:
            drv.no_bank_tools()
        else:
            drv._no_tools = False
        if stop_before is None:
            return drv.run_job(job_id, started_by)
        import job
        drv.claim(job_id, started_by)
        units = []
        for _ in range(200):
            u = drv.next()
            drv.calls += 1
            units.append(u)
            if u["unit"] in (stop_before, "complete"):
                return units
            drv.do(u, drv.token)
        raise AssertionError(f"no {stop_before} unit: {[u['unit'] for u in units]}")

    def staged_package(self, quarter="2026-Q3", lapsed=False):
        """A package built from the store as it is (a bare import first, when none was made)
        and its first send staged, nothing posted; `lapsed`: its lease older than
        passes.LEASE_S, as a turn that died between staging and its record leaves it (the
        stalled send any claim recovers, S7 §6.1). Returns the delivery_id."""
        import delivery
        import package
        import passes
        if self.conn.execute("SELECT 1 FROM binding").fetchone() is None:
            self.bind()
        if self.conn.execute("SELECT 1 FROM snapshots").fetchone() is None:
            self.import_again()
        pk = package.build_quarterly_package(self.conn, quarter)
        d = delivery.stage_for_delivery(self.conn, channel="telegram",
                                        package_id=pk["package_id"])
        if lapsed:
            import datetime as _dt
            at = (db._clock() - _dt.timedelta(seconds=passes.LEASE_S + 60)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
            with db.tx(self.conn):
                self.conn.execute("UPDATE deliveries SET lease_at=?, created_at=? WHERE"
                                  " delivery_id=?", (at, at, d["delivery_id"]))
        return d["delivery_id"]

    def sent_package(self, first_outcome="delivered", quarter="2026-Q3"):
        """A package built for a request and posted as a file, its first send recorded
        `first_outcome` (`uncertain`: the receipt was withheld; its notice is then delivered,
        as a run's post would deliver it): build, stage, post_package and record_delivery.
        Returns the package_id."""
        import delivery
        import posting
        from tests.fakebroker import FakeBroker
        did = self.staged_package(quarter)
        with FakeBroker():
            posting.post_package(self.conn, did)
        delivery.record_delivery(self.conn, delivery_id=did, outcome=first_outcome)
        if first_outcome != "delivered":           # its notice is posted and delivered
            import alerts
            import views
            r = alerts.pending_rendering(self.conn)
            views.mark_rendering_delivered(self.conn, r["render_id"])
        return self.conn.execute("SELECT package_id FROM deliveries WHERE delivery_id=?",
                                 (did,)).fetchone()[0]

    def import_again(self):
        """A newer import lands (a bare snapshots row): a package built before it is no
        longer the bank's latest (fix E4)."""
        import db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES (NULL, ?, 0, 0)", (db.now(),))

    def patch(self, obj, name, value):
        """`obj.name = value` for this test, restored at cleanup."""
        from unittest import mock
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def insert_render(self, kind, text) -> str:
        """A stored rendering of `kind` holding `text`, undelivered. Its render id."""
        import db
        with db.tx(self.conn):
            rid = f"r{db.next_seq(self.conn)}"
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " text, membership_json) VALUES (?,?, '{}', ?, ?, '[]')",
                              (rid, kind, db.now(), text))
        return rid

    def render_text(self, render_id) -> str:
        return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (render_id,)).fetchone()[0]

    def operator_pair(self, *, pid, doc_id, expected_revision, render_id):
        """The operator pairs a document with a payment they were shown (what
        record_match(author="operator") did before S7 §8.1: in the server only a tap's
        confirm_in_tx reaches matches._operator_pair; tests drive it directly)."""
        import authorship
        import db
        import matches
        with db.tx(self.conn):
            pid = matches._operator_pid(self.conn, pid)
            authorship.require_projection(self.conn, pid, render_id, expected_revision)
            return matches._operator_pair(self.conn, pid, doc_id, render_id)

    EXTRA_PAYEES = ("Notion", "Figma", "Slack", "Linear")

    def sheet_fixture(self, payee="Zapier", amount=9900, day="2026-09-17", guesses=1):
        """A bound store with `guesses` payments each holding one guessed machine pairing
        (the first to `payee`, the rest to EXTRA_PAYEES), and a delivered check view that
        shows them (S7 sheet tests, under a Q3 clock — Ruling F3). Returns {render_id, pid,
        match_id, doc_id, revision, match_revision, payee} for the first payment, and
        `pids`: every guessed payment, in printed order."""
        import datetime as dt
        import db
        import views
        made = []
        with self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)):
            self.bind()
            self._fixture_token = self.pass_()
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                                  " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
            self._fixture_guess = (payee, amount, day, 0)
            for _ in range(guesses):
                made.append(self.add_guess())
            r = views.build_review(self.conn, view="check", quarter="2026-Q3")
            views.mark_rendering_delivered(self.conn, r["render_id"])
        listed = views.render_items(self.conn, r["render_id"])
        assert all(m[1] in listed for m in made), r["text"]
        who, pid, mid, doc_id = made[0]
        return {"render_id": r["render_id"], "pid": pid, "match_id": mid, "doc_id": doc_id,
                "revision": self.rev(pid), "match_revision": self.rev(match_id=mid),
                "payee": payee,
                "pids": [m[1] for m in sorted(made, key=lambda m: r["text"].index(m[0]))]}

    def add_guess(self):
        """One more payment with a guessed machine pairing, after sheet_fixture's: the first
        to its payee, the next to EXTRA_PAYEES in turn, each EUR 10 more. Returns (payee,
        pid, match_id, doc_id). Not shown until a view of it is delivered."""
        import datetime as dt
        import matches
        payee, amount, day, i = self._fixture_guess
        self._fixture_guess = (payee, amount, day, i + 1)
        who = payee if i == 0 else self.EXTRA_PAYEES[i - 1]
        cents = amount + 1000 * i
        with self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)):
            n = 1 + (self.conn.execute("SELECT max(row_id) FROM bank_rows").fetchone()[0] or 0)
            self.row(n, counterparty=who, amount_minor=cents, booking_date=day, value_date=day)
            pid = self.lineage_for(n)
            self.classify(pid, {"software"})
            self.settle(pid)
            doc_id = self.doc(counterparty=who, issuer=who, amount_minor=cents, document_date=day)
            mid = matches.record_match(self.conn, pid=pid, doc_id=doc_id, author="auto",
                                       expected_revision=self.rev(pid),
                                       row_snapshot=StoreCase.snapshot(self, pid),
                                       token=self._fixture_token,
                                       labels=("guessed",))["match_id"]
        return who, pid, mid, doc_id

    def rejudge(self, pid):
        """A machine relabel of `pid`'s current pairing under the fixture's pass token, as
        the job re-judging a payment would: it moves that match's revision."""
        import matches
        mid = self.conn.execute("SELECT match_id FROM match_state WHERE pid=? AND state IN"
                                " ('matched','proposed')", (pid,)).fetchone()[0]
        return matches.relabel_match(self.conn, match_id=mid, labels=("no-ref",),
                                     token=self._fixture_token)

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

    def pass_(self, trigger="operator", generation=0, registered=None, accounts=None,
              instance=None):
        """run_claim's alias for the tests written before the simple loop: end any live pass
        (end_live_pass), claim a new run (`trigger` "cron" starts it scheduled) with its
        four probes, and hand its pass an acquisition with the bank_sync probe recorded for
        it (self.acq), so an import under the token is the run's own read. Returns the
        token."""
        import loop
        self.end_live_pass()
        token = self.run_claim(started_by="scheduled" if trigger == "cron" else "operator",
                               instance=instance, generation=generation,
                               registered=registered, accounts=accounts)
        with db.tx(self.conn):
            self.acq = loop.hand_acquisition(self.conn, token, self.pass_id)
        import passes
        passes.record_probe(self.conn, token, "bank_sync", True, acq=self.acq)
        return token

    _runs = 0

    def run_claim(self, started_by="operator", instance=None, generation=0, job_id=None,
                  registered=None, accounts=None):
        """A job run as the real cursor makes one: job.claim's claim, the run's pass under
        the claim's token, its runs row, and the four probes a run records first (the
        ledger probe carries `instance`). Sets self.job_id, self.token, self.pass_id and
        returns the token. Every new test of this plan starts its run here; none inserts
        into claims, runs or run_work itself (work_rows below is the one exception).
        `registered` and `accounts`: the ledger probe's registrations and the accounts the
        bank_accounts probe lists (default: the bound account)."""
        import job, passes
        StoreCase._runs += 1
        job_id = job_id or "%08x-0000-4000-8000-%012x" % (StoreCase._runs, id(self) % 10**12)
        token = job.claim(self.conn, job_id, started_by=f"Started by: {started_by}")
        run = self.conn.execute("SELECT * FROM runs WHERE job_id=?", (job_id,)).fetchone()
        m = self.conn.execute("SELECT * FROM pass_marker WHERE id=1").fetchone()
        # job.claim makes the run and starts its one pass (simple loop §2)
        assert run is not None and m["live"] and run["pass_id"] == m["pass_id"], (run, m)
        b = self.conn.execute("SELECT account_id FROM binding").fetchone()
        accts = accounts if accounts is not None else (
            [{"account_id": b[0], "category": "company", "label": "Zakelijk"}] if b else [])
        passes.record_probe(self.conn, token, "bank_tools", True)
        passes.record_probe(self.conn, token, "bank_sync", True)
        passes.record_probe(self.conn, token, "bank_accounts", True, data={"accounts": accts})
        passes.record_probe(self.conn, token, "ledger", True,
                            data={"generation": generation, "registered": registered or {},
                                  "instance": instance or self.LEDGER})
        self.job_id, self.token = job_id, token
        self.pass_id = self.conn.execute("SELECT pass_id FROM pass_marker WHERE id=1"
                                         ).fetchone()[0]
        return token

    def work_rows(self, pids, vendor="Adobe", why="open"):
        """The run's work-list entries for `pids`, bound to self.job_id (before Task 6's
        loop.build_work exists; later tests call build_work)."""
        with db.tx(self.conn):
            for pid in pids:
                self.conn.execute("INSERT OR IGNORE INTO run_work(job_id, pid, vendor, why)"
                                  " VALUES (?,?,?,?)", (self.job_id, pid, vendor, why))

    def accounts_probe(self, accounts):
        """A bank_accounts probe carrying `accounts`, recorded in a pass of its own (then
        ended), as a check records list_accounts' answer."""
        self.pass_("operator", accounts=accounts)
        self.end_live_pass()

    def end_live_pass(self, outcome="complete", report=None):
        """End the live pass, if any, `outcome` (with `report` stored as its report) under the
        marker's token (the run's latest claim), as the cursor ends a run's pass
        (loop.end_pass)."""
        import loop
        m = self.conn.execute("SELECT generation, live FROM pass_marker").fetchone()
        if m is not None and m["live"]:
            with db.tx(self.conn):
                loop.end_pass(self.conn, m["generation"], outcome, report)

    def end_and_speak(self):
        """End the live pass (end_live_pass) and compose what the alerts owe the operator,
        as a run's post does: the pending alerts rendering, or None."""
        import alerts
        self.end_live_pass()
        return alerts.pending_rendering(self.conn)

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

    def machine_entry(self, pid, doc_id, kind="pair", labels=("clean",), runners_up=()):
        """A machine pairing laid down in the log directly, bypassing the floor: what a
        store from before the floor (design rev 17 §2) or a merge of two lineages that each
        paired the document holds — a joint machine set, or a document held twice. The floor
        refuses both at the write. Returns the match id."""
        import lineage
        import matches
        import reducer as R
        with db.tx(self.conn):
            mid = matches._match_id_for(self.conn, pid, doc_id)
            self.conn.execute("UPDATE matches SET label=?, runners_up_json=? WHERE match_id=?",
                              (matches._labels(labels), json.dumps(list(runners_up)), mid))
            proj = lineage.projection(self.conn, pid)
            row = lineage.live_row(self.conn, proj)
            exp = lineage.expectation_for(self.conn, proj, row, exempt=False)
            lineage.append(self.conn, pid, kind, "auto", match_id=mid, doc_id=doc_id,
                           fp=R.fingerprint(R.facts_of(row), exp.kind))
            lineage.settle(self.conn, pid)
        return mid

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
                              (rid, "status", S7_EMPTY_SCOPE, db.now(), db.now(), "",
                               json.dumps(list(pids)), db.next_seq(self.conn)))
            for pid in pids:
                prev = self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                         (pid,)).fetchone()[0]
                mrevs = {str(r[0]): r[1] for r in self.conn.execute(
                    "SELECT match_id, revision FROM match_state WHERE pid=?", (pid,))}
                self.conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                                  " match_revisions_json) VALUES (?,?,?,?)",
                                  (rid, pid, prev, json.dumps(mrevs)))
                self.conn.execute("INSERT OR REPLACE INTO shown(pid, render_id, projection_revision,"
                                  " match_revisions_json, delivered_at) VALUES (?,?,?,?,?)",
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


# what an S7 rendering stores for its grammar-read fields when it prints nothing that fills
# them (reply r5: a view that LACKS one was composed by an earlier version)
S7_EMPTY_SCOPE = ('{"names": {}, "next": null, "offers": [], "pid": null, "proposed": [],'
                  ' "quarter": null, "refs": {}, "walk": null}')


def untag(text: str) -> str:
    """A rendering's text without its first-line tag (binding V2: " · <n>", the render id's
    digits) — for pins of composed text that predate the tag."""
    import re
    first, sep, rest = text.partition("\n")
    return re.sub(r" \u00b7 \d+$", "", first) + sep + rest


def apply_now(conn, text, quoted=None) -> dict:
    """S7 §8: the operator's words read (propose_reading) and, when a reading was posted,
    its Apply tapped — what apply_reply did in one call before S7. The result is shaped
    like the old one: `receipt` is the Apply receipt, or `say` when nothing was proposed;
    `applied` is the reading's plan once applied ([] otherwise); `instructions`, `reshow`,
    `understood` and `not_a_reply` come from the proposal; `proposal` is the posted text."""
    import json
    import posting
    import qa_server
    import tools  # noqa: F401 — registers the tools
    from tests.fakebroker import FakeBroker
    with FakeBroker() as b:
        out = posting.propose_reading(conn, text, quoted)
    res = dict(out, proposal=None, applied=[])
    if out["reading"] is None:
        res["receipt"] = out["say"]
        return res
    prop = json.loads(b.deposits[-1]["value"])
    res["proposal"] = prop["text"]
    call = next(x for x in prop["buttons"] if x["label"] == "Apply")["call"]
    res["receipt"] = qa_server.TOOLS[call["tool"]]["fn"](call["arguments"])["receipt"]
    row = conn.execute("SELECT state, plan_json FROM readings WHERE reading_id=?",
                       (out["reading_id"],)).fetchone()
    if row["state"] == "applied":
        res["applied"] = json.loads(row["plan_json"])
    return res


class LoopCase(StoreCase):
    """A simple-loop store (design rev 17 §1): bound, a run claimed (self.token,
    self.job_id), and one classified, settled payment per pay(). The one fixture the cards
    and taps tests share (P12: no copied pay/propose helpers)."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.n = 0

    def pay(self, who="Adobe", amount=10000, day="2026-09-02"):
        self.n += 1
        self.row(self.n, counterparty=who, amount_minor=amount, booking_date=day,
                 value_date=day)
        pid = self.lineage_for(self.n)
        self.classify(pid, {"software"})
        self.settle(pid)
        return pid

    def stored_date(self, doc_id):
        return self.conn.execute("SELECT document_date FROM documents WHERE doc_id=?",
                                 (doc_id,)).fetchone()[0]

    def propose(self, pid, alternatives=(), **doc):
        """The document's own stored date is the date read (plan round 3, Astra S1: a fixed
        date rewrote INV-88's 2 Aug, and a retry under another date is changed evidence)."""
        import matches
        d = self.doc(**doc)
        matches.propose_match(self.conn, pid=pid, doc_id=d, expected_revision=self.rev(pid),
                              token=self.token, document_date=self.stored_date(d),
                              alternatives=list(alternatives))
        return d
