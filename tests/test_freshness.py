# tests/test_freshness.py
"""Classification freshness (fix E2): a live lineage's classification
observation is FRESH iff the sweep recorded it after the latest successful
import (compared by snapshot id, never by a 1-second timestamp). The sweep
enumerates exactly the non-fresh lineages; a machine match and a package
never act on a non-fresh one. Against the REAL bank-feed, through
qa_server.handle and the rendered get_transaction text where the skill is
exercised."""
import contextlib
import csv
import io
import multiprocessing
import os
import pathlib
import random
import sqlite3
import time
import unittest

from tests import _base
import zipfile

from tests import test_e2e
from tests import _procs, sim, test_sweep_real
import db  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import package  # noqa: E402
import sweep  # noqa: E402
import work  # noqa: E402
import views  # noqa: E402

call = test_e2e.TestPackagingSeesTheClassification.call
read = test_e2e.TestPackagingSeesTheClassification.read


class ToolPass(test_e2e.Base):
    """A pass through the tool layer, in the skill's order."""
    def setUp(self):
        super().setUp()
        from tests.test_tools import _fresh_conn
        _fresh_conn(self)._CONN = self.conn

    package_token = None

    def begin(self, trigger, do_import=True, channel="telegram", candidates=0):
        bf = self.bf
        if trigger == "package":        # issue #15: the ask names the quarter and channel
            out = call("begin_pass", trigger=trigger, quarter="2026-Q3", channel=channel)
            if out["status"] != "started":  # already on its way / queued: continue_pass
                c = call("continue_pass")["continue"]
                self.assertEqual(c["next"], "snapshot", c)
                out = c
            token = out["pass_token"]
            call("record_step", pass_token=token, step="snapshot", action="start")
        else:
            token = call("begin_pass", trigger=trigger)["pass_token"]
        accounts = [{"account_id": r["account_id"], "category": r["category"], "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        call("record_probe", pass_token=token, kind="bank_tools", ok=True)
        call("record_probe", pass_token=token, kind="bank_accounts", ok=True,
             data={"accounts": accounts})
        call("record_probe", pass_token=token, kind="bank_sync", ok=True)
        call("record_probe", pass_token=token, kind="ledger", ok=True,
             data=sim.ledger_state(bf.listing()))
        self.assertTrue(call("check_setup")["can_run"])
        if not do_import:
            return token
        imp = call("import_ledger_export", path=bf.export(), pass_token=token,
                   ledger_instance=bf.last_export_instance)
        self.assertEqual(len(imp["erase_candidates"]), candidates)
        return token

    def observe(self, token, item, snapshot_id):
        """The skill's step 5 for ONE item, from the rendered text."""
        bf, row_id = self.bf, item["row_id"]
        for _ in range(4):
            text = bf.call("get_transaction", row_id=row_id)
            if text.startswith("no transaction #"):
                call("record_observation", pid=item["pid"], pass_token=token,
                     snapshot_id=snapshot_id, not_found=True)
                return
            tags, notes, first_seen, rev = read(text)
            r = call("record_observation", pid=item["pid"], pass_token=token,
                     snapshot_id=snapshot_id,
                     observed_tags=tags, observed_notes=notes, observed_first_seen=first_seen,
                     observed_tag_revision=rev)
            ins = r["instructions"]
            if not ins:
                return
            kw = {k: ins[k] for k in ("workflow", "expected_generation", "expected_ledger")}
            if "untag" in ins:
                bf.call("untag_transaction", row_ids=[row_id], tags=ins["untag"], **kw)
            elif "tag" in ins:
                bf.call("tag_transaction", row_ids=[row_id], tags=ins["tag"], **kw)
            else:
                bf.call("add_note", row_ids=[row_id], note=ins["add_note"], author="agent", **kw)

    def sweep(self, token, budget=None):
        """Step 5 until remaining is 0, or until `budget` items were read."""
        n = 0
        while True:
            page = call("list_projections", pass_token=token,
                        limit=25 if budget is None else max(1, min(25, budget - n)))
            for item in page["projections"]:
                self.observe(token, item, page["snapshot_id"])
                n += 1
            if page["remaining_in_cycle"] == 0:
                return 0
            if budget is not None and n >= budget:
                return page["remaining_in_cycle"]

    def end(self, token, outcome):
        """end_pass; a package pass's answer hands over its request's package_token. The
        specialist's snapshot finish comes first (a request builds only from a finished
        snapshot step)."""
        if self.conn.execute("SELECT trigger FROM pass_marker").fetchone()[0] == "package":
            call("record_step", pass_token=token, step="snapshot", action="finish")
            # issue #15: the rest of the round — Ellen's Gmail round (its probe, each of
            # the quarter's items searched) and a whole judge step
            call("record_probe", pass_token=token, kind="gmail", ok=True)
            items = call("list_quarter_state", triage=True, quarter="2026-Q3",
                         pass_token=token)["triage"]
            _base.hand(self.conn, [it["pid"] for it in items])    # issue #26: handed work
            for it in items:
                call("record_search", pid=it["pid"], pass_token=token, queries=["q"])
            call("record_step", pass_token=token, step="judge", action="start")
            call("record_step", pass_token=token, step="judge", action="finish",
                 triage_remaining=0)
        out = call("end_pass", pass_token=token, outcome=outcome)
        if "package_token" in out:
            self.package_token = out["package_token"]
        return out

    def request(self, channel="telegram"):
        """A package request: begin_pass(package), the snapshot step and this pass's
        own import (a request builds only from its own pass's import), end_pass.
        Returns the package_token end_pass hands over."""
        token = self.begin("package", channel=channel)
        self.sweep(token)
        out = self.end(token, "complete")["package_token"]
        self.package_token = None
        return out

    def zip_of(self, quarter="2026-Q3", channel="telegram"):
        token, self.package_token = self.package_token, None
        if token is None:
            token = self.request(channel)
        pkg = call("build_quarterly_package", quarter=quarter, package_token=token)
        pkg["package_token"] = token
        z = zipfile.ZipFile(pkg["path"])
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        return pkg, sorted(n for n in z.namelist() if "/" in n), rows, z

    def pid_of(self, row_id):
        return self.conn.execute("SELECT pid FROM aliases WHERE row_id=?", (row_id,)).fetchone()[0]

    def two_rows_matched(self):
        """Adobe 10.00 (software, its invoice matched) and Zapier 20.00, pid order
        Adobe < Zapier."""
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe"),
                  bf.row("2026-07-06", ref="Z1", amount=2000, counterparty="Zapier")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["A1"], "software")
        self.classify(ids["Z1"], "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-05")
        self.assertEqual(len(sim.run_pass(self.conn, bf)["triage"]["matched"]), 1)
        self.assertLess(self.pid_of(ids["A1"]), self.pid_of(ids["Z1"]))
        return ids


class TestAstraInterruptedCycle(ToolPass):
    """Round E2 (Astra): an interrupted cycle reads row 1, not row 2; row 1 is then
    reclassified; the package's sweep used to resume the cursor, read only row 2,
    reach remaining 0 — and ship row 1's stale invoice as MATCHED/invoice."""
    def test_the_package_sweep_rereads_what_an_earlier_cycle_read(self):
        ids = self.two_rows_matched()
        bf = self.bf
        token = self.begin("cron")
        self.assertGreater(self.sweep(token, budget=1), 0)          # row 1 read, row 2 not
        self.end(token, "interrupted")
        bf.call("untag_transaction", row_ids=[ids["A1"]], tags=["software"])
        self.classify(ids["A1"], "refund")                           # now wants a credit note
        token = self.begin("package")
        first = call("list_projections", pass_token=token)
        self.assertEqual(sorted(i["row_id"] for i in first["projections"]),
                         sorted(ids.values()))                       # BOTH are due again
        self.assertEqual(self.sweep(token), 0)
        self.end(token, "complete")
        _, files, rows, _ = self.zip_of()
        self.assertEqual(files, [])                                  # the invoice does not ship
        self.assertEqual({r["counterparty"]: (r["status"], r["expectation_kind"]) for r in rows},
                         {"Adobe": ("MISSING", "credit-note"), "Zapier": ("MISSING", "invoice")})


class TestTerraPartialSweepThenTriage(ToolPass):
    """Round E2 (Terra): the sweep stopped with remaining > 0 and triage then judged
    from an unswept, stale classification: an invoice auto-matched where a credit note
    is required. Since issue #1 the import itself observes the reclassification: with
    no read of the row, triage already sees a credit note and the invoice is refused
    by kind."""
    def test_a_reclassification_is_known_at_the_import_without_a_read(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["Z1"], "software")
        self.classify(ids["A1"], "software")
        self.first_pass()
        bf.call("untag_transaction", row_ids=[ids["A1"]], tags=["software"])
        self.classify(ids["A1"], "refund")                           # wants a credit note now
        doc = self.file(amount_minor=1000, document_date="2026-07-06")   # an invoice
        token = self.begin("cron")                                   # the import; no read
        tri = {d["pid"]: d for d in call("list_quarter_state", triage=True)["triage"]}
        cur = tri[self.pid_of(ids["A1"])]
        self.assertTrue(cur["fresh"])
        self.assertEqual(cur["expectation"]["kind"], "credit-note")
        for tool in ("record_match", "propose_match"):
            args = dict(pid=cur["pid"], doc_id=doc, expected_revision=cur["revision"],
                        row_digest=cur["row_digest"], pass_token=token,
                        document_date="2026-07-01")
            if tool == "record_match":
                args["author"] = "auto"
            out = _raw(tool, **args)
            self.assertTrue(out.startswith("refused: "), out)
            self.assertIn("credit-note", out)                        # refused by kind
        self.assertIsNone(self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                            (cur["pid"],)).fetchone()[0])

    def test_the_machine_write_still_refuses_a_lineage_not_observed_at_the_import(self):
        # defense in depth: the guard stays for a lineage the latest import did not
        # observe (mutation check: without it this match would be recorded)
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe")])
        rid = self.active()[0]["row_id"]
        self.classify(rid, "software")
        self.first_pass()
        doc = self.file(amount_minor=1000, document_date="2026-07-05")
        token = self.begin("cron")
        d = next(x for x in call("list_quarter_state", triage=True)["triage"]
                 if x["pid"] == self.pid_of(rid))
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_snapshot=NULL WHERE pid=?",
                              (d["pid"],))
        out = _raw("record_match", pid=d["pid"], doc_id=doc, expected_revision=d["revision"],
                   row_digest=d["row_digest"], pass_token=token, author="auto",
                   document_date="2026-07-01")
        self.assertIn("was not in the latest bank import", out)


class TestPassWithAnUnfinishedSweep(ToolPass):
    """SKILL.md step 6 (fix E2; issue #1): a sweep cut short no longer holds triage
    back — the import observed every payment — so triage matches both in the pass
    whose sweep had room for one; the writes it owes are made by the next sweep."""
    def outcome(self, out):
        return self.conn.execute("SELECT outcome FROM passes WHERE pass_id=?",
                                 (out["end"]["ended"],)).fetchone()[0]

    def test_triage_matches_every_fresh_item_whatever_the_sweep_reached(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        for rid in ids.values():
            self.classify(rid, "software")
        self.first_pass()
        z, a = self.pid_of(ids["Z1"]), self.pid_of(ids["A1"])
        self.file(counterparty="Zapier", issuer="Zapier", amount_minor=2000,
                  document_date="2026-07-05")
        self.file(amount_minor=1000, document_date="2026-07-06")
        out = sim.run_pass(self.conn, bf, sweep_budget=1)
        self.assertEqual((sorted(out["triage"]["matched"]), out["triage"]["not_fresh"]),
                         (sorted([z, a]), []))
        self.assertEqual({p: lineage.projection(self.conn, p)["status"] for p in (z, a)},
                         {z: "matched", a: "matched"})
        out = sim.run_pass(self.conn, bf)                            # the owed writes
        self.assertEqual(self.outcome(out), "complete")
        self.assertEqual(self.owned(ids["Z1"]), ["acct::matched"])
        self.assertEqual(self.owned(ids["A1"]), ["acct::matched"])


class TestSnapshotBoundCommits(ToolPass):
    """Round E3 (Astra S1, Terra S1): a read or a build validated against snapshot N is
    refused at its commit once import N+1 has landed — never stamped fresh, never
    registered."""
    def import_in_another_process(self, token):
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        proc = ctx.Process(target=_procs.import_export,
                           args=(self.bf.export(), token, self.bf.last_export_instance, out))
        proc.start()
        proc.join(60)
        got = out.get(timeout=5)
        self.assertEqual(got[0], "ok", got)
        return got[1]

    def test_a_read_taken_before_another_sessions_import_is_refused(self):
        ids = self.two_rows_matched()
        bf, a = self.bf, ids["A1"]
        token = self.begin("handover")
        page = call("list_projections", pass_token=token)
        n = page["snapshot_id"]
        item = next(i for i in page["projections"] if i["row_id"] == a)
        tags, notes, first_seen, rev = read(bf.call("get_transaction", row_id=a))
        self.assertIn("software", tags)                              # the read, rendered
        bf.call("untag_transaction", row_ids=[a], tags=["software"])
        self.classify(a, "refund")                                   # reclassified after it
        self.assertEqual(self.import_in_another_process(token), n + 1)
        out = _raw("record_observation", pid=item["pid"], pass_token=token, snapshot_id=n,
                   observed_tags=tags, observed_notes=notes, observed_first_seen=first_seen,
                   observed_tag_revision=rev)
        self.assertEqual(out, "refused: the bank was re-read meanwhile — list the sweep again "
                              "and read this payment again; nothing was recorded")
        # nothing recorded from the read; import N+1 itself observed the refund (issue #1)
        p = lineage.projection(self.conn, item["pid"])
        self.assertEqual((p["class_observed_snapshot"], p["read_snapshot"] == n + 1), (n + 1, False))
        self.assertEqual(work.describe(self.conn, item["pid"])["expectation"]["kind"],
                         "credit-note")
        self.assertEqual(self.sweep(token), 0)                       # read again, under N+1
        self.end(token, "complete")
        _, files, rows, _ = self.zip_of()
        self.assertEqual(files, [])
        self.assertEqual({r["counterparty"]: (r["status"], r["expectation_kind"]) for r in rows},
                         {"Adobe": ("MISSING", "credit-note"), "Zapier": ("MISSING", "invoice")})

    def test_an_import_cannot_land_inside_a_build_in_flight(self):
        # round E6: the import takes the custody lock the build holds from its freeze
        # through its registration, so N+1 cannot land in between; it waits (here:
        # refuses Busy at a short bound) and lands after the build registered under N
        ids = self.two_rows_matched()
        bf, a = self.bf, ids["A1"]
        token = self.begin("package")
        self.assertEqual(self.sweep(token), 0)
        self.end(token, "complete")
        bf.call("untag_transaction", row_ids=[a], tags=["software"])
        self.classify(a, "refund")
        token = self.begin("cron")                   # its import is N; the build freezes N
        ctx = multiprocessing.get_context("spawn")
        rendered, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        proc = ctx.Process(target=_procs.build_paused, args=("2026-Q3", rendered, resume, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(rendered.wait(60), "the build never rendered")
        n = lineage.latest_import(self.conn)
        saved, db.LOCK_BOUND_S = db.LOCK_BOUND_S, 0.5
        try:
            out_text = _raw("import_ledger_export", path=bf.export(), pass_token=token,
                            ledger_instance=bf.last_export_instance)
        finally:
            db.LOCK_BOUND_S = saved
        self.assertTrue(out_text.startswith("refused: "), out_text)
        self.assertEqual(lineage.latest_import(self.conn), n)       # nothing imported
        resume.set()
        proc.join(60)
        got = out.get(timeout=5)
        self.assertEqual(got[0], "ok", got)                          # registered under N
        call("import_ledger_export", path=bf.export(), pass_token=token,
             ledger_instance=bf.last_export_instance)                # N+1 lands after it
        self.assertEqual(_raw("stage_for_delivery", channel="telegram",
                              package_id=got[1]["package_id"]),
                         "refused: the bank was re-read since this package was built — "
                         "build it again")
        self.assertEqual(self.sweep(token), 0)
        self.end(token, "complete")
        # issue #15 (design D2): the first request's check predates both imports, so its
        # token builds nothing — the request goes back to its check
        self.assertIn("the bank was re-read since the check",
                      _raw("build_quarterly_package", quarter="2026-Q3",
                           package_token=self.package_token))
        self.package_token = None
        _, files, rows, _ = self.zip_of()
        self.assertEqual(files, [])
        self.assertEqual({r["counterparty"]: r["expectation_kind"] for r in rows},
                         {"Adobe": "credit-note", "Zapier": "invoice"})

    def test_nothing_validated_under_snapshot_n_commits_after_import_n_plus_1(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-%02d" % (i + 1), ref="R%d" % i, amount=1000 + i,
                         counterparty="Vendor%d" % i) for i in range(5)])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        rng = random.Random(3)
        refused = 0
        for trial in range(4):
            token = self.begin("cron")
            self.sweep(token, budget=rng.randint(1, 5))
            page = call("list_projections", pass_token=token)
            n = page["snapshot_id"]
            reads = []
            for item in page["projections"]:
                reads.append((item, read(bf.call("get_transaction", row_id=item["row_id"]))))
            # the build freezes and renders under N, and N+1 lands before it registers
            real = package._render

            def racing(*a, **kw):
                # the registration check is defense in depth behind the custody lock
                # (round E6): an import that bypassed the lock is simulated here
                result = real(*a, **kw)
                held = db.custody_lock
                db.custody_lock = lambda **_: contextlib.nullcontext()
                try:
                    call("import_ledger_export", path=bf.export(), pass_token=token,
                         ledger_instance=bf.last_export_instance)
                finally:
                    db.custody_lock = held
                return result
            package._render = racing
            try:
                with self.assertRaises(db.Refusal):
                    package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
            finally:
                package._render = real
            self.assertEqual(list((db.data_dir() / "packages").iterdir()), [])
            for item, (tags, notes, first_seen, rev) in reads:
                out = _raw("record_observation", pid=item["pid"], pass_token=token,
                           snapshot_id=n, observed_tags=tags, observed_notes=notes,
                           observed_first_seen=first_seen, observed_tag_revision=rev)
                self.assertTrue(out.startswith("refused: the bank was re-read"), out)
                refused += 1
                self.assertNotEqual(lineage.projection(self.conn, item["pid"])["read_snapshot"],
                                    n + 1)                        # nothing recorded under N+1
            self.assertEqual(call("list_projections", pass_token=token)["snapshot_id"], n + 1)
            self.end(token, "interrupted")
        self.assertGreater(refused, 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 0)


class TestVanishedAndPurgedConverges(test_sweep_real.Base):
    """Round E3 (Astra S2): vanished lineages whose rows were later purged never left
    the due set (each confirmed absence only moved the cursor). A confirmed absence is
    the import's sweep work for them: one budgeted pass converges, and they stay
    `vanished`."""
    def test_twenty_six_vanished_purged_rows_converge_in_one_budgeted_pass(self):
        from tests import bankfeed
        self.bf.fetch([self.bf.row("2026-07-%02d" % (1 + d % 28), ref="R%d" % d, amount=100 + d)
                       for d in range(26)], cap=bankfeed.CAP_UNKNOWN)
        self.new_pass()
        self.cycle()
        self.bf.fetch([], cap=bankfeed.CAP_UNKNOWN)
        out = self.new_pass()
        self.assertEqual(len(out["ended_vanished"]), 26)
        self.cycle()
        self.bf.purge_before("2026-08-01")
        self.assertEqual(self.bf.rows(), [])
        self.new_pass()
        self.assertEqual(sim.sweep_within(self.conn, self.bf, self.token, 26), 0)
        again = sweep.list_projections(self.conn, token=self.token)
        self.assertEqual((again["projections"], again["remaining_in_cycle"]), ([], 0))
        ends = {r[0] for r in self.conn.execute("SELECT ended FROM projections")}
        self.assertEqual(ends, {"vanished"})

    def test_a_vanished_row_still_in_the_snapshot_is_not_confirmed_absent(self):
        from tests import bankfeed
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")], cap=bankfeed.CAP_UNKNOWN)
        self.new_pass()
        self.cycle()
        self.bf.fetch([], cap=bankfeed.CAP_UNKNOWN)
        self.new_pass()
        pid = self.pid_of(self.bf.rows()[0]["row_id"])        # vanished, still exported
        with self.assertRaises(db.Refusal):
            sweep.record_observation(self.conn, pid=pid, token=self.token,
                                     snapshot_id=self.snap_id, not_found=True)
        self.assertIn(pid, sweep._due(self.conn))


class TestFirstSendChecksTheBuildSnapshot(ToolPass):
    """Round E4 (Terra, Astra): a package built under snapshot N, then a real
    reclassification, import N+1 and a completed sweep — staging the still-unsent
    package carried the superseded invoice to the outbox. Its FIRST send is refused
    at the delivery-log write; a resend of a file already sent is that exact file."""
    def built_then_superseded(self, before_import=None):
        ids = self.two_rows_matched()
        bf, a = self.bf, ids["A1"]
        token = self.begin("package")
        self.assertEqual(self.sweep(token), 0)
        self.end(token, "complete")
        pkg, files, rows, _ = self.zip_of()
        self.assertEqual(files, ["invoices/2026-07-05_Adobe_10.00.pdf"])     # MATCHED/invoice
        if before_import:
            before_import(pkg)
        bf.call("untag_transaction", row_ids=[a], tags=["software"])
        self.classify(a, "refund")
        token = self.begin("cron")                                     # import N+1
        self.assertEqual(self.sweep(token), 0)
        self.end(token, "complete")
        return pkg

    def outbox_files(self):
        return sorted(os.listdir(self.outbox))

    def handoff_files(self):
        return sorted(str(p.relative_to(self.handoff)) for p in self.handoff.rglob("*")
                      if p.is_file() and "quarterly-accounting" in p.parts)

    def test_the_first_send_of_a_superseded_package_is_refused(self):
        pkg = self.built_then_superseded()
        handoff_before = self.handoff_files()
        out = _raw("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                   package_token=pkg["package_token"])
        # issue #15 (design D2): its request goes back to its check; the old build is
        # never sent as a first send again (D3)
        self.assertEqual(out, "refused: the bank was re-read since the check — the check "
                              "runs again, and the package follows it; call job_next")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")
        self.assertTrue(_raw("stage_for_delivery", channel="telegram",
                             package_id=pkg["package_id"],
                             package_token=pkg["package_token"]).startswith(
                                 "refused: this package is no longer the one its request"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.outbox_files(), [])
        self.assertEqual(self.handoff_files(), handoff_before)
        # built again after the sweep, it ships the truth and stages
        pkg2, files, rows, _ = self.zip_of()
        self.assertEqual(files, [])
        staged = call("stage_for_delivery", channel="telegram", package_id=pkg2["package_id"],
                      package_token=pkg2["package_token"])
        self.assertEqual(self.outbox_files(), [os.path.basename(staged["path"])])

    def test_a_resend_of_a_package_already_sent_still_works_after_a_newer_import(self):
        def send_uncertain(pkg):
            d = call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                     package_token=pkg["package_token"])
            call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                 package_token=pkg["package_token"])
            for f in os.listdir(self.outbox):              # Casa consumed the outbox copy
                os.unlink(self.outbox / f)
        pkg = self.built_then_superseded(before_import=send_uncertain)
        r = call("build_review", view="status", quarter="2026-Q3")
        self.assertIn(views.field(pkg["filename"]), r["text"])                     # offered again
        call("mark_rendering_delivered", render_id=r["render_id"])
        # S7 §6.3/§8: "send it again" is a direct — propose_reading returns it, posts nothing
        self.assertIn("resend", call("propose_reading", text="send it again")["instructions"])
        staged = call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(staged["filename"], pkg["filename"])
        self.assertEqual(pathlib.Path(staged["path"]).read_bytes(),
                         pathlib.Path(pkg["path"]).read_bytes())


class TestImportRevokesAnUnsentFirstSend(ToolPass):
    """Round E5 (Terra S1): a first send staged under snapshot N stayed sendable after
    import N+1. The import revokes it in its own commit: the staged bytes go, and
    record_delivery refuses it. A resend of a file already sent is never revoked."""
    def built(self):
        self.two_rows_matched()
        token = self.begin("package")
        self.assertEqual(self.sweep(token), 0)
        self.end(token, "complete")
        pkg, files, _, _ = self.zip_of()
        self.assertEqual(files, ["invoices/2026-07-05_Adobe_10.00.pdf"])
        return pkg

    def test_a_first_send_staged_before_an_import_is_revoked(self):
        pkg = self.built()
        tg = call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                  package_token=pkg["package_token"])
        # one staged send per request, and a second request would import again: the second
        # copy is a package built outside any request under the same import (its first send
        # is revoked all the same, with its notice)
        import package
        pkg2 = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        other = call("stage_for_delivery", channel="telegram", package_id=pkg2["package_id"])
        self.assertEqual(sorted(os.listdir(self.outbox)),
                         sorted(os.path.basename(d["path"]) for d in (tg, other)))
        token = self.begin("cron")                                  # import N+1
        self.end(token, "interrupted")
        self.assertEqual(os.listdir(self.outbox), [])                # nothing left to send
        for d in (tg, other):
            out = _raw("record_delivery", delivery_id=d["delivery_id"], outcome="delivered",
                       message_id="m-1")
            self.assertEqual(out, "refused: the bank was re-read before this was sent — "
                                  "nothing was recorded (a package you asked for "
                                  "follows its check)")
        rows = self.conn.execute("SELECT status, revoked_at IS NOT NULL FROM deliveries"
                                 " ORDER BY delivery_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("failed", 1), ("failed", 1)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM delivered_rows").fetchone()[0],
                         0)

    def test_a_resend_of_a_file_already_sent_is_not_revoked(self):
        pkg = self.built()
        d = call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                 package_token=pkg["package_token"])
        call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
             package_token=pkg["package_token"])
        for f in os.listdir(self.outbox):                  # Casa consumed the outbox copy
            os.unlink(self.outbox / f)
        r = call("build_review", view="status", quarter="2026-Q3")
        call("mark_rendering_delivered", render_id=r["render_id"])
        # S7 §6.3/§8: "send it again" is a direct — propose_reading returns it, posts nothing
        self.assertIn("resend", call("propose_reading", text="send it again")["instructions"])
        again = call("stage_for_delivery", channel="telegram", resend=True)
        token = self.begin("cron")                                  # import N+1
        self.end(token, "interrupted")
        self.assertEqual(os.listdir(self.outbox), [os.path.basename(again["path"])])
        self.assertEqual(call("record_delivery", delivery_id=again["delivery_id"],
                              outcome="delivered")["status"], "delivered")


class TestWithdrawalUnderTheCustodyLock(ToolPass):
    """Round E6 (Terra, Astra S1): the import withdraws the staged bytes before it
    commits the revocation, under the custody lock taken before its transaction."""
    built = TestImportRevokesAnUnsentFirstSend.built
    def staged(self):
        pkg = self.built()
        d = call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                 package_token=pkg["package_token"])
        self.assertEqual(os.listdir(self.outbox), [os.path.basename(d["path"])])
        return pkg, d

    def state(self, delivery_id):
        # what another session sees: committed rows (a plain reader: open_store would
        # wait for the write lock the paused import holds)
        c = sqlite3.connect(str(db.data_dir() / db.DB_NAME))
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT status, revoked_at FROM deliveries WHERE delivery_id=?",
                          (delivery_id,)).fetchone()
            return r["status"], r["revoked_at"] is not None
        finally:
            c.close()

    def spawn(self, target, *args):
        ctx = multiprocessing.get_context("spawn")
        ev, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        proc = ctx.Process(target=target, args=(*args, ev, resume, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(ev.wait(60), f"{target.__name__} never reached its pause")
        return proc, resume, out

    def short_bound(self):
        saved = db.LOCK_BOUND_S
        db.LOCK_BOUND_S = 0.5
        self.addCleanup(setattr, db, "LOCK_BOUND_S", saved)

    def test_the_bytes_are_gone_before_the_revocation_commits(self):
        _, d = self.staged()
        token = self.begin("cron", do_import=False)
        proc, resume, out = self.spawn(_procs.import_paused, self.bf.export(), token,
                                       self.bf.last_export_instance)
        # paused between withdrawal and commit: nothing sendable, nothing revoked yet
        self.assertEqual(os.listdir(self.outbox), [])
        self.assertEqual(self.state(d["delivery_id"]), ("staged", False))
        resume.set()
        proc.join(60)
        self.assertEqual(out.get(timeout=5), ("ok", [d["delivery_id"]]))
        self.assertEqual(self.state(d["delivery_id"]), ("failed", True))
        self.assertEqual(os.listdir(self.outbox), [])

    def test_an_import_refuses_whole_while_the_custody_lock_is_held(self):
        pkg, d = self.staged()
        token = self.begin("cron", do_import=False)
        n = lineage.latest_import(self.conn)
        proc, release, out = self.spawn(_procs.hold_custody)
        self.short_bound()
        text = _raw("import_ledger_export", path=self.bf.export(), pass_token=token,
                    ledger_instance=self.bf.last_export_instance)
        self.assertTrue(text.startswith("refused: "), text)
        self.assertEqual(lineage.latest_import(self.conn), n)       # nothing imported
        self.assertEqual(self.state(d["delivery_id"]), ("staged", False))
        self.assertEqual(pathlib.Path(d["path"]).read_bytes(),
                         pathlib.Path(pkg["path"]).read_bytes())    # intact, still consistent
        release.set()
        proc.join(60)
        imp = call("import_ledger_export", path=self.bf.export(), pass_token=token,
                   ledger_instance=self.bf.last_export_instance)
        self.assertEqual(imp["revoked_deliveries"], [d["delivery_id"]])
        self.assertEqual(os.listdir(self.outbox), [])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores the mode")
    def test_a_failed_withdrawal_refuses_the_whole_import(self):
        # round E7 (Terra, Astra S1): a withdrawal that fails must not leave the
        # superseded package sendable under a committed newer snapshot
        for channel in ("telegram",):             # email went with S7 §6.2
            with self.subTest(channel=channel):
                pkg = self.built()
                d = call("stage_for_delivery", channel=channel, package_id=pkg["package_id"],
                         package_token=pkg["package_token"])
                staged = pathlib.Path(d["path"])
                locked = staged.parent          # the outbox
                os.chmod(locked, 0o500)                      # the withdrawal will fail
                self.addCleanup(lambda p=locked: p.exists() and os.chmod(p, 0o770))
                token = self.begin("cron", do_import=False)
                n = lineage.latest_import(self.conn)
                text = _raw("import_ledger_export", path=self.bf.export(), pass_token=token,
                            ledger_instance=self.bf.last_export_instance)
                self.assertTrue(text.startswith("refused: could not withdraw a staged package "
                                                "— nothing was imported"), text)
                self.assertEqual(lineage.latest_import(self.conn), n)   # snapshot unchanged
                self.assertEqual(self.state(d["delivery_id"]), ("staged", False))  # not revoked
                self.assertEqual(staged.read_bytes(), pathlib.Path(pkg["path"]).read_bytes())
                os.chmod(locked, 0o770)                      # recovered: the retry commits
                imp = call("import_ledger_export", path=self.bf.export(), pass_token=token,
                           ledger_instance=self.bf.last_export_instance)
                self.assertEqual(imp["snapshot"], n + 1)
                self.assertEqual(imp["revoked_deliveries"], [d["delivery_id"]])
                self.assertFalse(staged.exists())
                self.assertEqual(self.state(d["delivery_id"]), ("failed", True))
                self.end(token, "interrupted")

    def test_imports_and_builds_in_two_processes_never_deadlock(self):
        self.built()
        token = self.begin("cron")
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        proc = ctx.Process(target=_procs.build_repeatedly, args=("2026-Q3", 6, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        snaps, deadline = [], time.monotonic() + 120
        while (proc.is_alive() or len(snaps) < 2) and time.monotonic() < deadline:
            snaps.append(call("import_ledger_export", path=self.bf.export(), pass_token=token,
                              ledger_instance=self.bf.last_export_instance)["snapshot"])
        proc.join(120)
        self.assertFalse(proc.is_alive(), "the builds never finished")
        got = out.get(timeout=5)
        self.assertEqual(got[0], "ok", got)
        # every build ran whole between two imports (the lock serializes them), so none
        # was superseded before registering, and none waited past the bound
        self.assertEqual(got[1], ["ok"] * 6)
        self.assertEqual(snaps, sorted(set(snaps)))


def _raw(name, **args):
    import qa_server
    from tests import legacy_tools
    legacy_tools.posted_first(name, args)          # r3 #2: as post_package marks it
    out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    return out["result"]["content"][0]["text"]


class TestPackageWithANonFreshMember(ToolPass):
    """A build never ships a non-fresh row's documents as MATCHED: the row ships as
    UNCLASSIFIED, its documents under unresolved/, and the caption counts it (spec
    §Error handling: packaging ships rather than blocking). Since issue #1 every row
    the export carries is observed at the import, so this is defense in depth: the
    lineage is made unobserved by hand (mutation check of the build's guard)."""
    def test_a_matched_row_not_observed_at_the_import_does_not_ship_its_invoice(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["Z1"], "software")
        self.classify(ids["A1"], "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-06")
        self.assertEqual(len(sim.run_pass(self.conn, bf)["triage"]["matched"]), 1)
        token = self.begin("package")
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_snapshot=NULL WHERE pid=?",
                              (self.pid_of(ids["A1"]),))
        self.end(token, "interrupted")
        pkg, files, rows, z = self.zip_of()
        self.assertEqual(files, ["unresolved/2026-07-06_Adobe_10.00.pdf"])
        st = {r["counterparty"]: (r["status"], r["expectation_kind"], r["document"]) for r in rows}
        self.assertEqual(st["Adobe"], ("UNCLASSIFIED", "", ""))
        self.assertEqual(st["Zapier"], ("MISSING", "invoice", ""))
        self.assertIn("1 not seen in the last bank check", pkg["caption"])
        self.assertIn("## Not seen in the last bank check", z.read("notes.md").decode())


class TestFreshnessProperty(ToolPass):
    """Issue #1's invariant, over random reclassifications, sweep budgets and purges
    (fixed seeds): after ANY import, every lineage whose row the export carries is
    fresh with the kind bank-feed's live tags derive — whatever the sweep read — and
    the package ships that kind's truth; a lineage whose row the export dropped is
    not fresh, never machine-matched, and ships UNCLASSIFIED with no document."""
    KIND = {"software": "invoice", "refund": "credit-note"}

    def test_every_exported_row_is_fresh_with_its_live_kind_after_any_import(self):
        bf = self.bf
        refs = ["R%d" % i for i in range(6)]
        bf.fetch([bf.row("2026-07-%02d" % (i + 1), ref=r, amount=1000 + i,
                         counterparty="Vendor%d" % i) for i, r in enumerate(refs)])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        live = {}
        for ref, rid in ids.items():
            self.classify(rid, "software")
            live[ref] = "software"
        self.first_pass()
        docs = {r: self.file(counterparty="Vendor%d" % i, issuer="Vendor%d" % i,
                             amount_minor=1000 + i, document_date="2026-07-%02d" % (i + 1))
                for i, r in enumerate(refs)}
        rng = random.Random(20260928)
        purged = set()
        for trial in range(5):
            for ref in rng.sample(sorted(set(refs) - purged), 2):     # reclassify two
                new = "refund" if live[ref] == "software" else "software"
                bf.call("untag_transaction", row_ids=[ids[ref]], tags=[live[ref]])
                self.classify(ids[ref], new)
                live[ref] = new
            if trial == 2:                                             # drop the earliest row
                bf.purge_before("2026-07-02")
                purged.add("R0")
            token = self.begin("cron", candidates=1 if trial == 2 else 0)
            budget = rng.randint(0, len(refs))
            if budget:
                self.sweep(token, budget=budget)
            for d in work.list_quarter_state(self.conn, "2026-Q3")["items"]:
                p = lineage.projection(self.conn, d["pid"])
                ref = next(k for k, v in ids.items() if v == p["dest_row_id"])
                self.assertEqual(d["fresh"], ref not in purged, (trial, ref))
                if ref in purged:
                    continue
                self.assertEqual(d["expectation"]["kind"], self.KIND[live[ref]], (trial, ref))
                if d["status"] != "open":
                    continue
                try:
                    matches.record_match(self.conn, pid=d["pid"], doc_id=docs[ref], author="auto",
                                         expected_revision=d["revision"],
                                         row_digest=d["row_digest"], token=token)
                    ok = True
                except db.Refusal:
                    ok = False
                self.assertEqual(ok, live[ref] == "software", (trial, ref))   # an invoice fits
            self.end(token, "interrupted")
            _, files, rows, _ = self.zip_of()
            for r in rows:
                ref = next(k for k in refs if r["counterparty"] == "Vendor%s" % k[1:])
                if ref in purged:
                    self.assertEqual((r["status"], r["document"]), ("UNCLASSIFIED", ""))
                else:
                    self.assertEqual(r["expectation_kind"], self.KIND[live[ref]], (trial, ref))
                    if live[ref] == "refund":
                        self.assertEqual(r["document"], "", (trial, ref))    # no invoice


if __name__ == "__main__":
    unittest.main()
