import datetime as dt
import unittest
from unittest import mock

from tests._base import StoreCase
import binding  # noqa: E402
import db  # noqa: E402
import passes  # noqa: E402
import version  # noqa: E402


class TestPassMarker(StoreCase):
    def test_second_pass_while_one_is_live_is_busy(self):
        first = passes.begin_pass(self.conn, "cron")
        second = passes.begin_pass(self.conn, "operator")
        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "busy")
        self.assertIn("Already checking", second["text"])

    def test_a_stale_marker_is_reclaimed_and_the_old_token_refused_everywhere(self):
        old = passes.begin_pass(self.conn, "cron")["pass_token"]
        later = db._clock() + dt.timedelta(seconds=passes.STALE_AFTER_S + 1)
        with mock.patch.object(db, "_clock", lambda: later):
            new = passes.begin_pass(self.conn, "operator")
        self.assertTrue(new["reclaimed"])
        self.assertNotEqual(old, new["pass_token"])
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                passes.check_token(self.conn, old)
        with db.tx(self.conn):
            passes.check_token(self.conn, new["pass_token"])
            passes.check_token(self.conn, None)       # operator-side writes carry none

    def test_end_pass_releases_the_marker(self):
        t = passes.begin_pass(self.conn, "cron")["pass_token"]
        passes.end_pass(self.conn, t, "complete", {"checked": 3})
        self.assertEqual(passes.begin_pass(self.conn, "cron")["status"], "started")


class TestBindingDefaults(StoreCase):
    def test_exactly_one_company_account_binds_silently(self):
        t = passes.begin_pass(self.conn, "cron")["pass_token"]
        passes.record_probe(self.conn, t, "bank_accounts", True, data={"accounts": [
            {"account_id": "p1", "category": "personal", "label": "Privé"},
            {"account_id": "c1", "category": "company", "label": "Voorbeeld BV Zakelijk"}]})
        b = binding.get(self.conn)
        self.assertEqual((b["account_id"], b["package_name"]), ("c1", "voorbeeld-bv-zakelijk"))
        self.assertEqual(b["watermark"], __import__("dates").quarter_start(db.now()[:10]))

    def test_several_company_accounts_do_not_bind(self):
        t = passes.begin_pass(self.conn, "cron")["pass_token"]
        passes.record_probe(self.conn, t, "bank_accounts", True, data={"accounts": [
            {"account_id": "c1", "category": "company", "label": "A"},
            {"account_id": "c2", "category": "company", "label": "B"}]})
        self.assertIsNone(binding.get(self.conn))
        setup = binding.check_setup(self.conn)
        self.assertIn("several", " ".join(setup["conditions"]).lower())

    def test_slug_defaults_to_books(self):
        self.assertEqual(binding.slug("€€€"), "books")
        self.assertEqual(binding.slug("Café Zakelijk"), "cafe-zakelijk")

    def test_rebinding_to_another_account_is_refused(self):
        binding.bind_account(self.conn, "c1", "A")
        with self.assertRaises(db.Refusal):
            binding.bind_account(self.conn, "c2", "B")


class TestBankWriteGate(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def _populate(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO projections(dest_row_id, admitted_at) VALUES (1, 'x')")

    def test_fresh_store_fresh_ledger_is_allowed(self):
        self.pass_(generation=0, registered={})
        g = passes.bank_write_gate(self.conn)
        self.assertTrue(g["allowed"], g)
        self.assertEqual((g["expected_generation"], g["expected_ledger"], g["workflow"]),
                         (0, self.LEDGER, version.WORKFLOW))

    def test_a_bank_feed_without_a_ledger_instance_is_below_the_floor(self):
        import passes as _p
        t = self.pass_()
        _p.record_probe(self.conn, t, "ledger", True, data={"generation": 0, "registered": {}})
        with db.tx(self.conn):
            self.conn.execute("UPDATE passes SET gate_json=NULL")
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("0.15.0", g["reason"])

    def test_fresh_store_with_our_workflow_registered_is_refused_naming_the_backup(self):
        self.pass_(generation=1, registered={version.WORKFLOW: "b-20260922-01"})
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("restore backup b-20260922-01", g["reason"])

    def test_restored_ledger_under_a_populated_store_stops(self):
        self.pass_(generation=0)
        passes.bank_write_gate(self.conn)
        with db.tx(self.conn):                       # what a successful import records
            passes.remember_ledger(self.conn, passes.current_pass(self.conn)["pass_id"])
        self._populate()
        passes.end_pass(self.conn, passes.current_pass(self.conn)["generation"], "complete", {})
        self.pass_(generation=1, registered={})
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("reset", g["reason"].lower())

    def test_registered_workflows_clear_under_the_same_ledger_instance_is_not_a_restore(self):
        self.pass_(generation=2, registered={})
        passes.bank_write_gate(self.conn)
        with db.tx(self.conn):
            passes.remember_ledger(self.conn, passes.current_pass(self.conn)["pass_id"])
        self._populate()
        passes.end_pass(self.conn, passes.current_pass(self.conn)["generation"], "complete", {})
        # same generation, same ledger instance (self.LEDGER, unchanged) — only the
        # registered workflows cleared, which is not what a restore or an erasure
        # (delete_all_data mints a NEW instance, D4) would leave behind
        self.pass_(generation=2, registered={})
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])

    def test_upgrade_without_restore_reports_older_writes(self):
        self.pass_(generation=0, registered={"acct@0.0.9": "b0"})
        g = passes.bank_write_gate(self.conn)
        self.assertTrue(g["allowed"])
        self.assertEqual(g["older_workflows"], ["acct@0.0.9"])

    def test_a_refusal_is_sticky_for_the_pass(self):
        self.pass_(generation=1, registered={version.WORKFLOW: "b-1"})
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        self._populate()                                   # e.g. something imported anyway
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        self.assertFalse(binding.check_setup(self.conn)["can_run"])

    def test_filing_a_document_between_passes_does_not_lift_a_refusal(self):
        self.pass_(generation=1, registered={version.WORKFLOW: "b-1"})
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO documents(sha256, ext, size, kind, source,"
                              " extraction_author, ingested_at, ingest_quarter) VALUES"
                              " ('ab', 'pdf', 1, 'invoice', 'gmail', 'resident', 'x', '2026-Q3')")
        self.pass_(generation=1, registered={version.WORKFLOW: "b-1"})
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        self.pass_(generation=2, registered={})                 # the operator restored
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])

    def test_no_ledger_probe_this_pass_is_refused(self):
        passes.begin_pass(self.conn, "cron")
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])


class TestSelfCheck(StoreCase):
    def test_conditions_each_have_their_own_sentence(self):
        t = passes.begin_pass(self.conn, "cron")["pass_token"]
        passes.record_probe(self.conn, t, "bank_tools", False, "tools not visible")
        s = binding.check_setup(self.conn)
        self.assertFalse(s["can_run"])
        self.assertTrue(any("bank-feed" in c for c in s["conditions"]))

    def test_bound_account_gone(self):
        self.bind()
        self.pass_(accounts=[{"account_id": "other", "category": "company", "label": "X"}])
        s = binding.check_setup(self.conn)
        self.assertFalse(s["can_run"])
        self.assertTrue(any("gone" in c for c in s["conditions"]))

    def test_gmail_down_still_runs_with_searching_off(self):
        self.bind()
        t = self.pass_()
        passes.record_probe(self.conn, t, "bank_sync", True)
        passes.record_probe(self.conn, t, "gmail", False, "auth failed")
        s = binding.check_setup(self.conn)
        self.assertTrue(s["can_run"])
        self.assertFalse(s["searching"])

    def test_observations_are_timestamped_not_inferred(self):
        self.bind()
        self.pass_()
        s = binding.check_setup(self.conn)
        self.assertIn("observed_at", s["probes"]["bank_accounts"])


class TestReset(StoreCase):
    def test_a_reader_holding_the_log_makes_the_erasure_incomplete(self):
        import sqlite3
        self.bind()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                              " VALUES ('SENTINEL-NAME', '[]', 'x')")
        reader = sqlite3.connect(str(self.data / db.DB_NAME), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM counterparties").fetchone()   # holds a snapshot
        out = binding.reset_store(self.conn)
        self.assertEqual(out["erasure"], "incomplete")
        reader.execute("COMMIT")
        reader.close()
        out = binding.reset_store(self.conn)
        self.assertEqual(out["erasure"], "complete")
        for f in self.data.glob(db.DB_NAME + "*"):
            self.assertNotIn(b"SENTINEL-NAME", f.read_bytes(), f.name)

    def test_reset_wipes_to_fresh_and_fences_a_stale_pass(self):
        self.bind()
        t = self.pass_()
        (self.data / "documents" / "ab").mkdir(parents=True)
        (self.data / "documents" / "ab" / "x.pdf").write_bytes(b"%PDF")
        out = binding.reset_store(self.conn)
        self.assertEqual(out["erasure"], "complete")
        self.assertIn("bank-feed", out["report"])
        self.assertIsNone(binding.get(self.conn))
        self.assertFalse((self.data / "documents").exists())
        self.assertFalse(passes.store_populated(self.conn))
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                passes.check_token(self.conn, t)


if __name__ == "__main__":
    unittest.main()
