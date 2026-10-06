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
        self.tok = job.claim(self.conn, A)          # the run's pass starts with its claim
        self.pid = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]

    def probes(self, acq, sync_ok=True):
        import passes
        b = self.conn.execute("SELECT account_id FROM binding").fetchone()[0]
        passes.record_probe(self.conn, self.tok, "bank_tools", True)
        passes.record_probe(self.conn, self.tok, "bank_accounts", True,
                            data={"accounts": [{"account_id": b, "category": "company",
                                                "label": "Zakelijk"}]})
        passes.record_probe(self.conn, self.tok, "bank_sync", sync_ok,
                            "" if sync_ok else "HTTP 404 not_found; cached data unchanged",
                            acq=acq)
        passes.record_probe(self.conn, self.tok, "ledger", True,
                            data={"generation": 0, "registered": {}, "instance": self.LEDGER,
                                  "missing": []})

    def export(self, rows):
        """An export file in the handoff folder: StoreCase.export_csv publishes it and
        returns its path, as bank-feed's export_history does."""
        return self.export_csv(rows)

    def acquire(self):
        import db, loop
        with db.tx(self.conn):
            return loop.hand_acquisition(self.conn, self.tok, self.pid)

    def test_import_needs_its_acquisitions_sync_under_the_same_claim(self):
        import db, ledger
        acq = self.acquire()
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
        acq = self.acquire()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                    ledger_instance=self.LEDGER, acq=acq)
        acq2 = self.acquire()
        self.probes(acq2)
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq2)

    def test_a_superseded_acquisition_is_refused(self):
        import db, ledger
        acq1 = self.acquire()
        self.probes(acq1)
        acq2 = self.acquire()                            # re-handed in the same claim
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq1)

    def test_another_claims_acquisition_is_refused(self):
        import db, job, ledger
        acq = self.acquire()
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
        acq = self.acquire()
        self.probes(None)                               # a sync, but not this read's
        self.assertIn("record this bank read's sync", self.refusal(acq))

    def test_a_sync_recorded_under_another_claim_is_refused(self):
        import db
        acq = self.acquire()
        self.probes(acq)
        with db.tx(self.conn):                          # unreachable through record_probe
            self.conn.execute("UPDATE probes SET gen=gen-1 WHERE kind='bank_sync'")
        self.assertIn("record this bank read's sync", self.refusal(acq))

    # PLAY T7 F1: a failed sync binds the import as a successful one does (v0.8.0 never
    # required a successful sync); the identity checks are unchanged
    def test_a_failed_sync_of_this_acquisition_under_this_claim_binds_the_import(self):
        import ledger
        acq = self.acquire()
        self.probes(acq, sync_ok=False)
        out = ledger.import_ledger_export(self.conn, path=self.export([{"row_id": 1}]),
                                          token=self.tok, ledger_instance=self.LEDGER, acq=acq)
        self.assertEqual(out["rows"], 1)

    def test_a_failed_sync_of_another_acquisition_is_refused(self):
        acq = self.acquire()
        self.probes(acq - 1, sync_ok=False)              # a failed sync, not this read's
        self.assertIn("record this bank read's sync", self.refusal(acq))

    def test_a_failed_sync_recorded_under_another_claim_is_refused(self):
        import db
        acq = self.acquire()
        self.probes(acq, sync_ok=False)
        with db.tx(self.conn):                          # unreachable through record_probe
            self.conn.execute("UPDATE probes SET gen=gen-1 WHERE kind='bank_sync'")
        self.assertIn("record this bank read's sync", self.refusal(acq))

    def test_an_earlier_turns_acquisition_is_refused_as_such(self):
        import job
        acq = self.acquire()
        self.probes(acq)
        self.tok = job.claim(self.conn, A)
        self.assertIn("earlier turn", self.refusal(acq))

    def test_an_import_without_acq_is_refused_in_a_job_pass(self):
        acq = self.acquire()
        self.probes(acq)
        self.assertIn("call job_next", self.refusal(None))

    def test_a_replayed_export_is_refused_by_name(self):
        import ledger
        acq = self.acquire()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                    ledger_instance=self.LEDGER, acq=acq)
        acq2 = self.acquire()
        self.probes(acq2)
        self.assertIn("imported already", self.refusal(acq2, path))

    def test_the_import_records_its_read(self):
        import ledger, os, pathlib
        acq = self.acquire()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        sid = ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                          ledger_instance=self.LEDGER, acq=acq)["snapshot"]
        s = self.conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        self.assertEqual((s["job_id"], s["pass_id"], s["acq"], s["export_ref"]),
                         (A, self.pid, acq, pathlib.Path(os.path.realpath(path)).parent.name))


class ProbeArguments(StoreCase):
    """record_probe's S2 arguments (spec §4, §5.2, §6.4)."""

    def setUp(self):
        super().setUp()
        import job
        self.bind()
        self.tok = job.claim(self.conn, A)          # the run's pass starts with its claim

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
