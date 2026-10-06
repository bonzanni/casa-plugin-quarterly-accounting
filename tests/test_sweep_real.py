# tests/test_sweep_real.py
"""The four reproduced failures of spec §"Mirroring decisions", the ended
lineages and the unknown-expectation rule — each against the REAL
bank-feed (tag tools, apply_plan, purge), never a double."""
import datetime as _dt
import unittest
from unittest import mock

from tests._base import StoreCase
from tests import bankfeed, sim
import db  # noqa: E402
import documents  # noqa: E402
import ledger  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import passes  # noqa: E402
import sweep  # noqa: E402
import version  # noqa: E402

PDF = b"%PDF-1.4\n%%EOF\n"


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.close)
        self.bf.account()
        self.bind(account=bankfeed.Ledger.ACCOUNT)

    def new_pass(self):
        """A pass up to the sweep, in the skill's order: probes, gate, import, then
        the erase candidates confirmed with get_transaction BEFORE anything else."""
        self.token = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                                instance=self.bf.instance())
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])
        path = self.bf.export()
        out = ledger.import_ledger_export(self.conn, path=path, token=self.token,
                                          ledger_instance=self.bf.last_export_instance)
        self.snap_id = out["snapshot"]
        for c in out["erase_candidates"]:
            if self.bf.call("get_transaction", row_id=c["row_id"]).startswith("no transaction #"):
                sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=c["pid"], token=self.token,
                                         not_found=True)
        return out

    def job_pass(self, token):
        """A job pass under claim `token` up to the sweep (S2 §5): the pass started
        under the claim if none is live, the acquisition the cursor hands out, the
        probes recorded under the claim, and the import bound to that acquisition."""
        import job
        live = job.live_job_pass(self.conn)
        if live is None:
            self.end_live_pass()
            pass_id = self.start_job_pass(token)
        else:
            pass_id = live["pass_id"]
        with db.tx(self.conn):
            acq = job.hand_acquisition(self.conn, token, pass_id)
        accounts = [{"account_id": bankfeed.Ledger.ACCOUNT, "category": "company",
                     "label": "Zakelijk"}]
        passes.record_probe(self.conn, token, "bank_tools", True)
        passes.record_probe(self.conn, token, "bank_accounts", True, data={"accounts": accounts})
        passes.record_probe(self.conn, token, "bank_sync", True, acq=acq)
        passes.record_probe(self.conn, token, "ledger", True,
                            data=sim.ledger_state(self.bf.listing()))
        out = ledger.import_ledger_export(self.conn, path=self.bf.export(), token=token,
                                          ledger_instance=self.bf.last_export_instance, acq=acq)
        self.token, self.snap_id = token, out["snapshot"]
        return out

    def new_note_revision(self, pid):
        """The lineage's note text changes (a decision moved it): a new note_seq."""
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET note_body='(an older body)',"
                              " observed_revision=NULL WHERE pid=?", (pid,))
            lineage.settle(self.conn, pid)
        return lineage.projection(self.conn, pid)["note_seq"]

    def cycle(self):
        return sim.sweep_cycle(self.conn, self.bf, self.token)

    def rid(self, n=0):
        return self.bf.rows(state="active")[n]["row_id"]

    def pid_of(self, row_id):
        return self.conn.execute("SELECT pid FROM aliases WHERE row_id=?", (row_id,)).fetchone()[0]

    def owned(self, row_id):
        return sorted(t for t in self.bf.tags(row_id) if t in lineage.R.OWNED)

    def wf(self, tags, row_id, verb="tag_transaction"):
        return self.bf.call(verb, row_ids=[row_id], tags=tags, workflow=version.WORKFLOW,
                            expected_generation=self.bf.generation())

    def ingest(self, **kw):
        path = self.publish("inv-%d.pdf" % len(kw), PDF + repr(kw).encode())
        args = dict(source_path=path, kind="invoice", source="gmail",
                    extraction_author="desk", counterparty="Zapier", issuer="Zapier",
                    amount_minor=1000, currency="EUR", document_date="2026-07-05")
        args.update(kw)
        return documents.ingest_document(self.conn, **args)["doc_id"]


class TestFourFailures(Base):
    def test_unmatched_row_is_open_and_stays_in_the_classifier_queue(self):
        import rules
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        self.assertEqual(self.owned(self.rid()), ["acct::open"])
        self.assertEqual(rules.queue_totals(self.bf.conn), (1, 0))
        self.assertTrue(any(n.startswith("Accounting revision ") for n in self.bf.notes(self.rid())))

    def test_fixed_point_from_any_start_keeps_foreign_tags(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        r = self.rid()
        self.new_pass()          # populate the store first: an acct@ write on a fresh store is
                                 # exactly what the bank-write gate refuses (Task 8)
        self.wf(["acct::matched", "acct::proposed"], r)
        self.bf.call("tag_transaction", row_ids=[r], tags=["acct::custom"], workflow="hand@1",
                     expected_generation=self.bf.generation())
        self.bf.call("tag_transaction", row_ids=[r], tags=["acct-matched"])
        self.new_pass()
        self.cycle()
        self.assertEqual(self.owned(r), ["acct::open"])
        self.assertIn("acct::custom", self.bf.tags(r))
        self.assertIn("acct-matched", self.bf.tags(r))

    def test_a_stale_writer_is_repaired_by_the_next_cycle(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        self.wf(["acct::matched"], self.rid())          # a paused writer lands late
        self.new_pass()
        self.cycle()
        self.assertEqual(self.owned(self.rid()), ["acct::open"])

    def test_an_owned_tag_removed_by_hand_is_restored(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        self.wf(["acct::open"], self.rid(), verb="untag_transaction")
        self.new_pass()
        self.cycle()
        self.assertEqual(self.owned(self.rid()), ["acct::open"])

    def test_the_lineage_not_the_row_id_keeps_a_superseded_record_enumerable(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", status="PDNG")])
        self.new_pass()
        self.cycle()
        pending = self.rid()
        self.wf(["acct::matched"], pending)             # stale write on the predecessor
        self.bf.fetch([self.bf.row("2026-07-06", ref="R1", status="BOOK")])   # tags migrate
        self.new_pass()
        self.cycle()
        booked = self.rid()
        self.assertNotEqual(booked, pending)
        self.assertEqual(self.owned(booked), ["acct::open"])

    def test_rejected_and_accepted_on_one_transaction_reach_one_fixed_point(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        r = self.rid()
        self.bf.call("tag_transaction", row_ids=[r], tags=["software"])    # the classifier
        self.new_pass()
        self.cycle()
        pid = self.pid_of(r)
        a, b = self.ingest(document_number="A"), self.ingest(document_number="B")
        mid_a = matches.record_match(self.conn, pid=pid, doc_id=a, author="auto",
                                     expected_revision=self.rev(pid), token=self.token,
                                     row_snapshot=self.snapshot(pid))["match_id"]
        rid = self.show(pid)
        self.granted(matches.reject_in_tx, match_id=mid_a,
                     expected_revision=self.rev(match_id=mid_a),
                     render_id=rid)
        rid = self.show(pid)
        self.operator_pair(pid=pid, doc_id=b, expected_revision=self.rev(pid), render_id=rid)
        for start in (["acct::open"], ["acct::matched", "acct::proposed"]):
            cur = self.owned(r)
            if cur:
                self.wf(cur, r, verb="untag_transaction")
            self.wf(start, r)
            self.new_pass()
            self.cycle()
            self.assertEqual(self.owned(r), ["acct::matched"], start)


class TestCapacity(Base):
    def test_a_full_namespace_budget_is_reported_unprojectable_not_retried(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        r = self.rid()
        for owner in ("aa", "bb", "cc", "dd"):
            self.bf.call("tag_transaction", row_ids=[r],
                         tags=[f"{owner}::t{i}" for i in range(16)],
                         workflow=f"{owner}@1", expected_generation=self.bf.generation())
        self.new_pass()
        self.cycle()
        p = lineage.projection(self.conn, self.pid_of(r))
        self.assertIsNotNone(p["unprojectable"])
        self.new_pass()
        page = sweep.list_projections(self.conn, token=self.token)
        item = page["projections"][0]
        again = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=item["pid"], token=self.token,
                                         observed_tags=self.bf.tags(r),
                                         observed_notes=self.bf.notes(r),
                                         observed_first_seen=self.bf.rows()[0]["first_seen"],
                                         observed_tag_revision=self.bf.tag_revision(r))
        self.assertEqual((again["instructions"] or {}).get("tag", []), [])


class TestEndsAndErasure(Base):
    def test_vanished_row_loses_owned_tags_and_a_later_stale_open_goes_too(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")], cap=bankfeed.CAP_UNKNOWN)
        self.new_pass()
        self.cycle()
        r = self.rid()
        self.bf.fetch([], cap=bankfeed.CAP_UNKNOWN)
        out = self.new_pass()
        self.assertEqual(len(out["ended_vanished"]), 1)
        self.cycle()
        self.assertEqual(self.owned(r), [])
        self.wf(["acct::open"], r)
        self.new_pass()
        self.cycle()
        self.assertEqual(self.owned(r), [])

    def test_erasure_frees_the_document_and_the_returning_payment_is_a_machine_pick(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.bf.call("tag_transaction", row_ids=[self.rid()], tags=["software"])
        self.new_pass()
        self.cycle()
        old_pid = self.pid_of(self.rid())
        d = self.ingest()
        rid = self.show(old_pid)
        self.operator_pair(pid=old_pid, doc_id=d, expected_revision=self.rev(old_pid),
                           render_id=rid)
        self.new_pass()
        self.bf.purge_before("2026-08-01")                         # before the pass
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])      # this pass's own sync
        self.bf.call("tag_transaction", row_ids=[self.rid()], tags=["software"])  # classifier
        out = self.new_pass()                                      # confirms the end first
        self.assertEqual([c["pid"] for c in out["erase_candidates"]], [old_pid])
        self.assertEqual(lineage.projection(self.conn, old_pid)["ended"], "erased")
        self.cycle()
        self.assertEqual(documents.status(self.conn, d), "unmatched")
        new_pid = self.pid_of(self.rid())
        r = matches.record_match(self.conn, pid=new_pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(new_pid), token=self.token,
                                 row_snapshot=self.snapshot(new_pid))
        self.assertEqual((r["state"], r["status"]), ("matched", "matched"))
        author = self.conn.execute("SELECT author FROM match_state WHERE match_id=?",
                                   (r["match_id"],)).fetchone()[0]
        self.assertEqual(author, "auto")

    def test_not_found_without_this_pass_import_ends_nothing(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        pid = self.pid_of(self.rid())
        passes.end_pass(self.conn, self.token, "complete", {})
        self.token = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                                instance=self.bf.instance())
        with self.assertRaises(db.Refusal):
            sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=pid, token=self.token, not_found=True)
        self.assertIsNone(lineage.projection(self.conn, pid)["ended"])

    def test_an_earlier_import_that_omitted_the_row_is_not_evidence_now(self):
        # round p3 (Astra S2): the omission was seen by a previous pass whose
        # confirmation never happened; a later pass without its own import ends nothing
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.bf.call("tag_transaction", row_ids=[self.rid()], tags=["software"])
        self.new_pass()
        self.cycle()
        pid = self.pid_of(self.rid())
        d = self.ingest()
        matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                             expected_revision=self.rev(pid), token=self.token,
                             row_snapshot=self.snapshot(pid))
        self.new_pass()
        self.bf.purge_before("2026-08-01")
        self.token = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                                instance=self.bf.instance())
        path = self.bf.export()
        ledger.import_ledger_export(self.conn, path=path, token=self.token,
                                    ledger_instance=self.bf.last_export_instance)
        passes.end_pass(self.conn, self.token, "interrupted", {})    # confirmation never ran
        self.token = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                                instance=self.bf.instance())
        with self.assertRaises(db.Refusal):
            sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=pid, token=self.token, not_found=True)
        with self.assertRaises(db.Refusal):
            sweep.list_projections(self.conn, token=self.token)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM match_state WHERE state IN"
                                           " ('matched','proposed')").fetchone()[0], 1)
        self.assertEqual(documents.status(self.conn, d), "matched")

    def test_a_different_transaction_under_the_row_id_stops_the_pass(self):
        # round p4 (Astra S1, mitigated): the ledger switched after the import
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        page = sweep.list_projections(self.conn, token=self.token)
        item = page["projections"][0]
        with self.assertRaises(db.Refusal):
            sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=item["pid"], token=self.token,
                                     observed_tags=[], observed_notes=[],
                                     observed_first_seen="2030-01-01T00:00:00Z",
                                     observed_tag_revision=0)
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        with self.assertRaises(db.Refusal):
            sweep.list_projections(self.conn, token=self.token)

    def test_a_ledger_that_changes_after_the_first_write_takes_no_second(self):
        # round p5 (Terra S1): the row's identity changes between two repair writes
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        item = sweep.list_projections(self.conn, token=self.token)["projections"][0]
        rid = item["row_id"]
        real_call = self.bf.call
        writes = []

        def swapping_call(tool, **args):
            if tool in ("tag_transaction", "untag_transaction", "add_note"):
                writes.append(tool)
            out = real_call(tool, **args)
            if tool in ("tag_transaction", "untag_transaction"):
                self.bf.conn.execute("UPDATE transactions SET first_seen='2030-01-01T00:00:00Z'"
                                     " WHERE row_id=?", (rid,))
                self.bf.conn.commit()
            return out
        self.bf.call = swapping_call
        with self.assertRaises(db.Refusal):
            sim.observe_and_repair(self.conn, self.bf, self.token, item, self.snap_id)
        self.assertEqual(writes, ["tag_transaction"])                  # exactly one write
        self.assertEqual(self.owned(rid), ["acct::open"])
        self.assertFalse([n for n in self.bf.notes(rid) if n.startswith("Accounting revision")])
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])

    def test_a_late_stale_note_is_restated(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        rid = self.rid()
        current = [n for n in self.bf.notes(rid) if n.startswith("Accounting revision ")][-1]
        self.bf.call("add_note", row_ids=[rid], note="Accounting revision 1: stale.",
                     author="agent", workflow=version.WORKFLOW,
                     expected_generation=self.bf.generation())
        self.new_pass()
        self.cycle()
        self.assertEqual([n for n in self.bf.notes(rid)
                          if n.startswith("Accounting revision ")][-1], current)

    def _remint_before_the_write(self, rid, expected_tool, prepare):
        """The ledger is re-minted between the observation and the ONE write the
        server asks for; with expected_ledger on that write nothing lands."""
        import store as bf_store
        prepare(rid)
        self.new_pass()
        item = next(i for i in sweep.list_projections(self.conn, token=self.token)["projections"]
                    if i["row_id"] == rid)
        real_call = self.bf.call
        calls = []

        def reminting_call(tool, **args):
            if tool in ("tag_transaction", "untag_transaction", "add_note"):
                calls.append(tool)
                self.bf.conn.execute("UPDATE meta SET value=? WHERE key=?",
                                     ("f" * 32, bf_store.LEDGER_INSTANCE_KEY))
                self.bf.conn.commit()
            return real_call(tool, **args)
        self.bf.call = reminting_call
        before = (sorted(self.bf.tags(rid)), list(self.bf.notes(rid)))
        try:
            sim.observe_and_repair(self.conn, self.bf, self.token, item, self.snap_id)
        except db.Refusal:
            pass
        finally:
            self.bf.call = real_call
        self.assertEqual(calls[:1], [expected_tool])
        self.assertEqual((sorted(self.bf.tags(rid)), list(self.bf.notes(rid))), before)

    def test_every_repair_write_carries_the_ledger_fence(self):
        # rounds p11/p12 (Astra S2): each of the three annotation tools, as the FIRST
        # write the server requests, is refused on a re-minted ledger
        self.bf.fetch([self.bf.row("2026-07-%02d" % d, ref="R%d" % d, amount=100 + d)
                       for d in (5, 6, 7)])
        self.new_pass()
        ids = [r["row_id"] for r in self.bf.rows(state="active")]
        wf = {"workflow": version.WORKFLOW}

        def stale_matched(rid):              # first request: untag acct::matched
            self.bf.call("tag_transaction", row_ids=[rid], tags=["acct::matched"],
                         expected_generation=self.bf.generation(), **wf)

        def nothing(rid):                    # first request: tag acct::open
            pass

        def tagged_no_note(rid):             # first request: add the accounting note
            self.bf.call("tag_transaction", row_ids=[rid], tags=["acct::open"],
                         expected_generation=self.bf.generation(), **wf)
        cases = (("untag_transaction", stale_matched), ("tag_transaction", nothing),
                 ("add_note", tagged_no_note))
        import store as bf_store
        original = self.bf.instance()
        for rid, (tool, prepare) in zip(ids, cases):
            with self.subTest(tool=tool):
                # back to the bound ledger before each case (the previous one re-minted it)
                self.bf.conn.execute("UPDATE meta SET value=? WHERE key=?",
                                     (original, bf_store.LEDGER_INSTANCE_KEY))
                self.bf.conn.commit()
                self._remint_before_the_write(rid, tool, prepare)

    def test_not_found_for_a_row_still_in_the_snapshot_is_refused(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        with self.assertRaises(db.Refusal):
            sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=self.pid_of(self.rid()), token=self.token,
                                     not_found=True)

    def test_omitted_observed_notes_is_refused_not_a_duplicate_note(self):
        # round C1 (Terra S1): observed_notes is not required, so an omitted read-back
        # made the server believe the current note was never seen and return add_note
        # again -- the fenced write would raise a real bank-feed row's note count.
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()                                 # one accounting note is now on the row
        r = self.rid()
        before = list(self.bf.notes(r))
        self.assertTrue(any(n.startswith("Accounting revision ") for n in before))
        first_seen = self.bf.conn.execute("SELECT first_seen FROM transactions WHERE row_id=?",
                                          (r,)).fetchone()[0]
        self.new_pass()
        with self.assertRaises(db.Refusal):
            sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=self.pid_of(r), token=self.token,
                                     observed_tags=self.bf.tags(r), observed_first_seen=first_seen)
        self.assertEqual(list(self.bf.notes(r)), before)          # no duplicate write happened

    def ended_residue(self, pid):
        return [tuple(r) for r in self.conn.execute(
            "SELECT reason, detail FROM residue WHERE pid=? AND reason='ended' ORDER BY rowid",
            (pid,))]

    def test_a_vanished_row_purged_later_stays_vanished_and_gains_no_residue(self):
        # fix round 1: _confirm_erased on an already-ended lineage recorded a false
        # "ended erased" residue on every pass
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")], cap=bankfeed.CAP_UNKNOWN)
        self.new_pass()
        self.cycle()
        pid = self.pid_of(self.rid())
        self.bf.fetch([], cap=bankfeed.CAP_UNKNOWN)
        self.new_pass()
        self.cycle()
        self.bf.purge_before("2026-12-31")
        for _ in range(3):
            self.new_pass()
            self.cycle()
        out = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=pid, token=self.token, not_found=True)
        self.assertEqual((out["ended"], out["instructions"]), ("vanished", {}))
        self.assertEqual(lineage.projection(self.conn, pid)["ended"], "vanished")
        self.assertEqual(self.ended_residue(pid), [("ended", "vanished")])

    def test_confirming_an_erasure_twice_records_one_end(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        pid = self.pid_of(self.rid())
        self.bf.purge_before("2026-08-01")
        self.new_pass()
        self.assertEqual(lineage.projection(self.conn, pid)["ended"], "erased")
        out = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=pid, token=self.token, not_found=True)
        self.assertEqual(out["instructions"], {})
        self.assertEqual(self.ended_residue(pid), [("ended", "erased")])

    def test_an_observation_of_an_erased_lineage_reads_and_writes_nothing(self):
        # spec §The sweep step 2: an erased row id may name another ledger's payment;
        # an observation of it is a no-op, never an identity check that stops the pass
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        pid = self.pid_of(self.rid())
        self.bf.purge_before("2026-08-01")
        self.new_pass()
        before = lineage.projection(self.conn, pid)
        out = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=pid, token=self.token,
                                       observed_tags=["acct::open"], observed_notes=[],
                                       observed_first_seen="2030-01-01T00:00:00Z",
                                       observed_tag_revision=0)
        self.assertEqual(out["instructions"], {})
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])
        after = lineage.projection(self.conn, pid)
        self.assertEqual((after["observed_tags_json"], after["revision"]),
                         (before["observed_tags_json"], before["revision"]))

    def test_a_note_that_did_not_land_is_recorded_not_retried(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        item = sweep.list_projections(self.conn, token=self.token)["projections"][0]
        real_call = self.bf.call
        notes = []

        def refusing_note(tool, **args):
            if tool == "add_note":
                notes.append(tool)
                return "refused: the journal is full"
            return real_call(tool, **args)
        self.bf.call = refusing_note
        try:
            out = sim.observe_and_repair(self.conn, self.bf, self.token, item, self.snap_id)
        finally:
            self.bf.call = real_call
        self.assertEqual(notes, ["add_note"])
        self.assertEqual(out.get("recorded"), "the write was refused; reported, not retried")
        self.assertIn("journal is full",
                      lineage.projection(self.conn, item["pid"])["last_error"])


class TestUnknownExpectation(Base):
    def test_purge_erase_keeps_machine_matches_even_for_a_retagged_row(self):
        rows = [self.bf.row("2026-07-%02d" % d, ref="R%d" % d, amount=1000 + d) for d in (5, 6, 7)]
        self.bf.fetch(rows)
        ids = [r["row_id"] for r in self.bf.rows(state="active")]
        self.bf.call("tag_transaction", row_ids=ids, tags=["software"])
        self.new_pass()
        self.cycle()
        mids = []
        for n, rid_ in enumerate(ids):
            pid = self.pid_of(rid_)
            d = self.ingest(amount_minor=1005 + n, document_number="N%d" % n)
            mids.append(matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                             expected_revision=self.rev(pid), token=self.token,
                                             row_snapshot=self.snapshot(pid))["match_id"])
        self.cycle()
        self.bf.call("purge", before_date="2020-01-01", user_work="erase")   # deletes 0 rows
        self.new_pass()
        self.cycle()
        matched = self.conn.execute("SELECT COUNT(*) FROM match_state WHERE state='matched'"
                                    ).fetchone()[0]
        self.assertEqual(matched, 3)
        for rid_ in ids:
            self.assertEqual(self.owned(rid_), ["acct::matched"])
            self.assertTrue(any(n.startswith("Accounting revision ") for n in self.bf.notes(rid_)))
        self.bf.call("tag_transaction", row_ids=[ids[0]], tags=["internal-transfer"])
        self.new_pass()
        self.cycle()
        states = [self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                    (m,)).fetchone()[0] for m in mids]
        self.assertEqual(states, ["matched", "matched", "matched"])   # §2: no kind gate


class TestCursor(Base):
    def test_the_cycle_resumes_across_passes(self):
        self.bf.fetch([self.bf.row("2026-07-%02d" % (1 + d % 28), ref="R%d" % d, amount=100 + d)
                       for d in range(30)])
        self.new_pass()
        first = sweep.list_projections(self.conn, token=self.token, limit=25)
        for item in first["projections"]:
            sim.observe_and_repair(self.conn, self.bf, self.token, item, self.snap_id)
        self.assertEqual(first["remaining_in_cycle"], 5)
        read_first = [i["pid"] for i in first["projections"]]
        self.new_pass()
        # the new import made every lineage due again (fix E2); the cursor starts the
        # new pass where the last one stopped, so the five never read come first
        second = sweep.list_projections(self.conn, token=self.token, limit=25)
        pids = [i["pid"] for i in second["projections"]]
        unread = sorted(set(lineage.live_pids(self.conn)) - set(read_first))
        self.assertEqual(pids[:5], unread)
        self.assertEqual(pids[5:], read_first[:20])
        self.assertEqual(second["remaining_in_cycle"], 5)
        for item in second["projections"]:
            sim.observe_and_repair(self.conn, self.bf, self.token, item, self.snap_id)
        third = sweep.list_projections(self.conn, token=self.token, limit=25)
        self.assertEqual([i["pid"] for i in third["projections"]], read_first[20:])
        self.assertEqual(third["remaining_in_cycle"], 0)
        for item in third["projections"]:
            sim.observe_and_repair(self.conn, self.bf, self.token, item, self.snap_id)
        fourth = sweep.list_projections(self.conn, token=self.token, limit=25)
        self.assertEqual((fourth["projections"], fourth["remaining_in_cycle"]), ([], 0))
        c = self.conn.execute("SELECT last_cycle_completed_at FROM cursor").fetchone()[0]
        self.assertIsNotNone(c)

    def test_a_read_exempts_a_lineage_only_until_it_changes_or_the_next_import(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1"), self.bf.row("2026-07-06", ref="R2")])
        self.new_pass()
        self.cycle()
        again = sweep.list_projections(self.conn, token=self.token)
        self.assertEqual((again["projections"], again["remaining_in_cycle"]), ([], 0))
        pid = self.pid_of(self.rid(0))
        with db.tx(self.conn):                 # a decision moves the lineage's revision
            self.conn.execute("UPDATE projections SET revision=revision+1 WHERE pid=?", (pid,))
        again = sweep.list_projections(self.conn, token=self.token)
        self.assertEqual([i["pid"] for i in again["projections"]], [pid])
        self.new_pass()
        again = sweep.list_projections(self.conn, token=self.token)
        self.assertEqual(len(again["projections"]), 2)


class TestNoteAsRendered(Base):
    """Task 22 review, item 1: the specialist transcribes get_transaction's TEXT,
    where bank-feed fences each note and prefixes it with [author, date]. That
    text must read as the current note, or every pass appends a duplicate."""
    @staticmethod
    def parse(text):
        import re
        tags, notes, in_notes = [], [], False
        for line in text.splitlines():
            if line.startswith("Tags: ") and line != "Tags: none":
                tags += line[len("Tags: "):].split(", ")
            elif line.startswith("Other workflows' tags (not classifications): "):
                tags += line.split(": ", 1)[1].split(", ")
            elif line.startswith("Notes"):
                in_notes = line != "Notes: none"
            elif in_notes and line.startswith("  ["):
                notes.append(line[2:])
        first_seen = re.search(r"first seen (\S+), last seen", text).group(1)
        return tags, notes, first_seen

    def test_a_note_transcribed_from_get_transaction_needs_no_new_note(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        rid = self.rid()
        before = len(self.bf.notes(rid))
        text = self.bf.call("get_transaction", row_id=rid)
        self.assertIn(sweep.FENCE_OPEN + "Accounting revision ", text)   # really fenced
        tags, notes, first_seen = self.parse(text)
        rev = self.bf.tag_revision(rid)
        self.assertEqual(sorted(tags), sorted(self.bf.tags(rid)))
        r = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=self.pid_of(rid), token=self.token,
                                     observed_tags=tags, observed_notes=notes,
                                     observed_first_seen=first_seen,
                                     observed_tag_revision=rev)
        self.assertEqual(r["instructions"], {})
        # markers removed by the reader: also the current note
        bare = [sweep.shown_note(n) for n in notes]
        r = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=self.pid_of(rid), token=self.token,
                                     observed_tags=tags, observed_notes=bare,
                                     observed_first_seen=first_seen,
                                     observed_tag_revision=rev)
        self.assertEqual(r["instructions"], {})
        self.assertEqual(len(self.bf.notes(rid)), before)

    def test_a_stale_rendered_note_still_asks_for_the_current_one(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        self.cycle()
        rid = self.rid()
        tags, notes, first_seen = self.parse(self.bf.call("get_transaction", row_id=rid))
        rev = self.bf.tag_revision(rid)
        stale = [n.replace("Accounting revision ", "Accounting revision 0") for n in notes]
        r = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=self.pid_of(rid), token=self.token,
                                     observed_tags=tags, observed_notes=stale,
                                     observed_first_seen=first_seen,
                                     observed_tag_revision=rev)
        self.assertIn("add_note", r["instructions"])


class TestNoteClaims(Base):
    """INV-J11 end to end (spec §4): a note write of another text issued under a claim
    keeps its lineage due until a read under a LATER claim, recorded at least Z after
    the first claim that followed the issue's."""
    JOB = "aaaaaaaa-1"
    T0 = _dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def setUp(self):
        super().setUp()
        self.now = self.T0
        p = mock.patch.object(db, "_clock", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        with db.tx(self.conn):          # a store installed well before these passes
            self.conn.execute("UPDATE meta SET value=? WHERE key='store_epoch_at'",
                              ((self.T0 - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),))

    def later(self, seconds):
        self.now += _dt.timedelta(seconds=seconds)

    def test_an_other_text_issue_holds_until_z_after_a_later_claim(self):
        import job
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        rid = self.rid()
        self.new_pass()
        self.cycle()                    # a delegation's first write mints the registration
        self.assertIn(version.WORKFLOW, self.bf.registered())
        pid = self.pid_of(rid)
        self.later(3600)
        t1 = job.claim(self.conn, self.JOB)
        self.job_pass(t1)
        self.new_note_revision(pid)
        self.cycle()                    # revision A: issued, written, re-read under t1
        seq_b = self.new_note_revision(pid)
        self.cycle()                    # revision B: issued, written, re-read under t1
        p = lineage.projection(self.conn, pid)
        self.assertEqual((p["note_other_issued_gen"], p["note_issued_gen"],
                          p["note_seen_gen"], p["note_seen_seq"]), (t1, t1, t1, seq_b))
        self.assertTrue(self.bf.notes(rid)[-1].startswith(f"Accounting revision {seq_b}:"))
        self.later(sweep.Z_S + 100)
        self.job_pass(t1)               # an import Z after the issue, still under t1
        self.cycle()                    # re-read under t1, well past the time margin
        self.assertEqual(lineage.projection(self.conn, pid)["note_seen_gen"], t1)
        self.later(3600)
        self.new_pass()                 # a fresh import, no later claim: still due
        self.assertIn(pid, sweep._due(self.conn))
        t2 = job.claim(self.conn, self.JOB)
        claimed = self.now
        self.later(sweep.Z_S - 1)
        self.job_pass(t2)
        self.cycle()                    # read under t2, its import 1 s inside Z
        self.assertEqual(lineage.projection(self.conn, pid)["note_seen_gen"], t2)
        self.later(1)
        self.job_pass(t2)
        self.assertIn(pid, sweep._due(self.conn))
        self.assertEqual(self.now, claimed + _dt.timedelta(seconds=sweep.Z_S))
        self.cycle()                    # read under t2, its import exactly Z after t2
        self.later(60)
        self.job_pass(t2)
        self.assertNotIn(pid, sweep._due(self.conn))


if __name__ == "__main__":
    unittest.main()
