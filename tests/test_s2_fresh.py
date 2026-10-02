"""S2 Task 5 (spec §5.2, INV-J10, INV-J14): an import is bound to its acquisition by
identity, and F gates every machine decision of a live job pass."""
from tests._base import StoreCase

A = "aaaaaaaa-1"


class Acquisition(StoreCase):
    """The import is bound to its acquisition by identity (spec §5.2, INV-J14)."""

    def setUp(self):
        super().setUp()
        import job
        self.bind()
        self.tok = job.claim(self.conn, A)
        self.pid = self.start_job_pass(self.tok)

    def probes(self, acq):
        import passes
        b = self.conn.execute("SELECT account_id FROM binding").fetchone()[0]
        passes.record_probe(self.conn, self.tok, "bank_tools", True)
        passes.record_probe(self.conn, self.tok, "bank_accounts", True,
                            data={"accounts": [{"account_id": b, "category": "company",
                                                "label": "Zakelijk"}]})
        passes.record_probe(self.conn, self.tok, "bank_sync", True, acq=acq)
        passes.record_probe(self.conn, self.tok, "ledger", True,
                            data={"generation": 0, "registered": {}, "instance": self.LEDGER,
                                  "missing": []})

    def export(self, rows):
        """An export file in the handoff folder: StoreCase.export_csv publishes it and
        returns its path, as bank-feed's export_history does."""
        return self.export_csv(rows)

    def handed(self):
        import db, job
        with db.tx(self.conn):
            return job.hand_acquisition(self.conn, self.tok, self.pid)

    def test_import_needs_its_acquisitions_sync_under_the_same_claim(self):
        import db, ledger
        acq = self.handed()
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):            # no bank_sync for this acq yet
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq)
        self.probes(acq)
        out = ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                          ledger_instance=self.LEDGER, acq=acq)
        self.assertEqual(out["rows"], 1)

    def test_an_export_is_imported_once(self):
        import db, ledger
        acq = self.handed()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                    ledger_instance=self.LEDGER, acq=acq)
        acq2 = self.handed()
        self.probes(acq2)
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq2)

    def test_a_superseded_acquisition_is_refused(self):
        import db, ledger
        acq1 = self.handed()
        self.probes(acq1)
        acq2 = self.handed()                            # re-handed in the same claim
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq1)

    def test_another_claims_acquisition_is_refused(self):
        import db, job, ledger
        acq = self.handed()
        self.probes(acq)
        self.tok = job.claim(self.conn, A)              # the turn ended; a new turn
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq)

    def refusal(self, acq, path=None):
        import db, ledger
        with self.assertRaises(db.Refusal) as cm:
            ledger.import_ledger_export(self.conn, path=path or self.export([{"row_id": 1}]),
                                        token=self.tok, ledger_instance=self.LEDGER, acq=acq)
        return str(cm.exception)

    def test_a_sync_not_naming_this_acquisition_is_refused(self):
        acq = self.handed()
        self.probes(None)                               # a sync, but not this read's
        self.assertIn("record this bank read's sync", self.refusal(acq))

    def test_a_sync_recorded_under_another_claim_is_refused(self):
        import db
        acq = self.handed()
        self.probes(acq)
        with db.tx(self.conn):                          # unreachable through record_probe
            self.conn.execute("UPDATE probes SET gen=gen-1 WHERE kind='bank_sync'")
        self.assertIn("record this bank read's sync", self.refusal(acq))

    def test_an_earlier_turns_acquisition_is_refused_as_such(self):
        import job
        acq = self.handed()
        self.probes(acq)
        self.tok = job.claim(self.conn, A)
        self.assertIn("earlier turn", self.refusal(acq))

    def test_an_import_without_acq_is_refused_in_a_job_pass(self):
        acq = self.handed()
        self.probes(acq)
        self.assertIn("call job_next", self.refusal(None))

    def test_a_replayed_export_is_refused_by_name(self):
        import ledger
        acq = self.handed()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                    ledger_instance=self.LEDGER, acq=acq)
        acq2 = self.handed()
        self.probes(acq2)
        self.assertIn("imported already", self.refusal(acq2, path))

    def test_the_import_records_its_read(self):
        import ledger, os, pathlib
        acq = self.handed()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        sid = ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                          ledger_instance=self.LEDGER, acq=acq)["snapshot"]
        s = self.conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        p = self.conn.execute("SELECT read_seq FROM passes WHERE pass_id=?",
                              (self.pid,)).fetchone()
        self.assertEqual((s["job_id"], s["read_seq"], s["acq"], s["export_ref"]),
                         (A, p["read_seq"], acq,
                          pathlib.Path(os.path.realpath(path)).parent.name))
        self.assertIsNotNone(s["read_seq"])
        self.assertIsNone(s["swept_at"])


class FreshnessF(Acquisition):
    def imported(self):
        import ledger
        acq = self.handed()
        self.probes(acq)
        ledger.import_ledger_export(self.conn, path=self.export([{"row_id": 1}]),
                                    token=self.tok, ledger_instance=self.LEDGER, acq=acq)

    def reason(self):
        import db, job
        with db.tx(self.conn):
            return job.fresh_reason(self.conn)

    def test_condition_1_another_jobs_import(self):
        import job
        self.imported()
        self.sweep_to_zero()
        self.assertIsNone(self.reason())
        self.tok = job.claim(self.conn, "bbbbbbbb-2")    # adoption by another job
        self.assertIn("another job", self.reason())

    def test_condition_2_a_request_after_the_watermark(self):
        import asks
        self.imported()
        self.sweep_to_zero()
        asks.request_work(self.conn, "check", "operator")
        import db
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pid)
        self.assertIn("asked after", self.reason())

    def test_condition_3_w_from_the_sweeps_completion_and_its_cap(self):
        import datetime as _dt, db, job
        self.imported()
        self.sweep_to_zero()
        later = db._clock() + _dt.timedelta(seconds=job.W_S + 1)
        with self.patch_clock(later):
            self.assertIn("older than", self.reason())
            with db.tx(self.conn):
                self.conn.execute("UPDATE passes SET w_refreshes=? WHERE pass_id=?",
                                  (job.W_REFRESH_MAX, self.pid))
            self.assertIsNone(self.reason())           # waived after two W refreshes

    def test_machine_pairing_refuses_while_f_fails(self):
        import db, job
        self.imported()
        self.sweep_to_zero()
        self.tok = job.claim(self.conn, "bbbbbbbb-2")    # condition 1 now fails
        pid = self.only_pid()
        doc = self.doc()
        with self.assertRaises(db.Refusal) as cm:
            self.machine_match(pid, doc, self.tok)
        self.assertIn("call job_next", str(cm.exception))

    def test_not_read_before_the_pass_imports(self):
        self.assertIn("not read", self.reason())

    def test_unswept_until_a_listing_finds_nothing_due(self):
        import sweep
        self.imported()
        self.assertIn("not finished", self.reason())
        sweep.list_projections(self.conn, token=self.tok)   # lists the one due row
        self.assertIn("not finished", self.reason())
        self.sweep_to_zero()
        self.assertIsNone(self.reason())

    def test_a_quarters_sweep_completes_its_read_for_a_package(self):
        import ledger, sweep
        acq = self.handed()
        self.probes(acq)
        ledger.import_ledger_export(
            self.conn, path=self.export([{"row_id": 1},
                                         {"row_id": 2, "booking_date": "2026-10-01",
                                          "value_date": "2026-10-01"}]),
            token=self.tok, ledger_instance=self.LEDGER, acq=acq)
        page = sweep.list_projections(self.conn, token=self.tok, quarter="2026-Q3")
        (item,) = page["projections"]
        sweep.record_observation(self.conn, pid=item["pid"], token=self.tok,
                                 snapshot_id=page["snapshot_id"], observed_tags=item["desired"],
                                 observed_notes=[item["note"]],
                                 observed_first_seen="2026-07-03T08:00:00Z",
                                 observed_tag_revision=0)
        self.assertTrue(sweep._due(self.conn))        # the other quarter's row is still due
        sweep.list_projections(self.conn, token=self.tok, quarter="2026-Q3")
        self.assertIsNone(self.reason())

    def test_proposal_and_relabel_refuse_while_f_fails(self):
        import db, job, matches, work
        self.imported()
        self.sweep_to_zero()
        self.tok = job.claim(self.conn, "bbbbbbbb-2")    # condition 1 now fails
        pid = self.only_pid()
        item = work.list_quarter_state(self.conn, pid=pid)["item"]
        with self.assertRaises(db.Refusal) as cm:
            matches.propose_match(self.conn, pid=pid, doc_id=self.doc(),
                                  expected_revision=item["revision"],
                                  row_digest=item["row_digest"], token=self.tok)
        self.assertIn("call job_next", str(cm.exception))
        with self.assertRaises(db.Refusal) as cm:
            matches.relabel_match(self.conn, match_id=1, labels=["clean"], token=self.tok)
        self.assertIn("call job_next", str(cm.exception))


class DelegationPass(StoreCase):
    def test_f_never_gates_a_delegation_pass(self):
        import db, job
        self.bind()
        self.pass_()
        with db.tx(self.conn):
            self.assertIsNone(job.fresh_reason(self.conn))
            job.require_fresh(self.conn)


class ProbeArguments(StoreCase):
    """record_probe's S2 arguments (spec §4, §5.2, §6.4)."""

    def setUp(self):
        super().setUp()
        import job
        self.bind()
        self.tok = job.claim(self.conn, A)
        self.start_job_pass(self.tok)

    def probe(self, kind):
        import json
        r = self.conn.execute("SELECT * FROM probes WHERE kind=?", (kind,)).fetchone()
        return r["gen"], json.loads(r["data_json"] or "null")

    def test_bank_sync_carries_its_acquisition_under_its_claim(self):
        import passes
        passes.record_probe(self.conn, self.tok, "bank_sync", True, acq=7)
        self.assertEqual(self.probe("bank_sync"), (self.tok, {"acq": 7}))

    def test_acq_goes_only_with_bank_sync(self):
        import db, passes
        with self.assertRaises(db.Refusal):
            passes.record_probe(self.conn, self.tok, "ledger", True, acq=7)

    def test_absent_gmail_is_a_failed_probe(self):
        import db, passes
        with self.assertRaises(db.Refusal):
            passes.record_probe(self.conn, self.tok, "gmail", True, absent=True)
        with self.assertRaises(db.Refusal):
            passes.record_probe(self.conn, self.tok, "bank_tools", False, absent=True)
        passes.record_probe(self.conn, self.tok, "gmail", False, absent=True)
        self.assertEqual(self.probe("gmail"), (self.tok, {"absent": True}))

    def test_the_ledger_probe_keeps_missing_as_given(self):
        import db, passes
        data = {"generation": 0, "registered": {}, "instance": self.LEDGER,
                "missing": ["acct@0.9.0"]}
        passes.record_probe(self.conn, self.tok, "ledger", True, data=data)
        self.assertEqual(self.probe("ledger")[1]["missing"], ["acct@0.9.0"])
        with self.assertRaises(db.Refusal):
            passes.record_probe(self.conn, self.tok, "ledger", True,
                                data={**data, "missing": "acct@0.9.0"})
