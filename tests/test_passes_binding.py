import datetime as dt
import json
import multiprocessing
import threading
import unittest
from unittest import mock

from tests._base import StoreCase
from tests import _procs
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
        self.assertEqual(second["text"], "A check is running — started a minute ago.\n"
                                         "Ask again in a few minutes.")

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
        self.granted(binding.bind_in_tx, "c1", "A")
        with self.assertRaises(db.Refusal):
            self.granted(binding.bind_in_tx, "c2", "B")


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
        self.assertIn("0.20.0", g["reason"])

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

    def test_a_stale_ledger_probe_from_an_earlier_pass_does_not_decide_the_gate(self):
        # fix round 1, M1: a probe row survives (PK on kind) after its pass ends;
        # the gate must require it to belong to THIS pass, not merely be ok.
        t = self.pass_(generation=0, registered={})
        passes.end_pass(self.conn, t, "complete", {})
        passes.begin_pass(self.conn, "cron")            # a new pass, no probes recorded yet
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("list_backups", g["reason"])

    def test_a_different_ledger_instance_after_an_import_is_refused_until_acked(self):
        # fix round 1, M2+M3: the other-ledger branch and its "not ack" sticky guard.
        self.pass_(generation=0, registered={})
        passes.bank_write_gate(self.conn)
        with db.tx(self.conn):
            passes.remember_ledger(self.conn, passes.current_pass(self.conn)["pass_id"])

        other = "b" * 32
        self.pass_(generation=0, registered={}, instance=other)
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("not the one this store was built on", g["reason"])

        self.pass_(generation=0, registered={}, instance=other)      # still refused, next pass
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])

        self.granted(binding.acknowledge_ledger_reset_in_tx)
        self.pass_(generation=0, registered={}, instance=other)      # allowed after the ack
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])

        self.pass_(generation=0, registered={})                      # back to the bound instance
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])

    def test_a_restored_refusal_stays_sticky_even_when_a_later_pass_reports_the_old_generation(self):
        # fix round 1, M4: the cross-pass meta.gate_refusal cache, not a fresh recompute
        # (a fresh recompute in the third pass below would NOT itself refuse, because
        # the reported generation happens to match the remembered one again).
        self.pass_(generation=0, registered={})
        passes.bank_write_gate(self.conn)
        with db.tx(self.conn):
            passes.remember_ledger(self.conn, passes.current_pass(self.conn)["pass_id"])
        self._populate()
        passes.end_pass(self.conn, passes.current_pass(self.conn)["generation"], "complete", {})

        self.pass_(generation=1, registered={})            # a restore: remembered 0 != seen 1
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("restored", g["reason"].lower())
        passes.end_pass(self.conn, passes.current_pass(self.conn)["generation"], "complete", {})

        self.pass_(generation=0, registered={})            # generation reverts to match remembered
        g2 = passes.bank_write_gate(self.conn)
        self.assertFalse(g2["allowed"])                    # still refused: the sticky, not a recompute
        self.assertEqual(g2["reason"], g["reason"])

    def test_older_workflows_filters_out_non_acct_prefixed_registrations(self):
        # fix round 1, M7: the "acct@" prefix filter on older_workflows.
        self.pass_(generation=0, registered={"acct@0.0.9": "b0", "someother-plugin@1.0": "b1"})
        g = passes.bank_write_gate(self.conn)
        self.assertTrue(g["allowed"], g)
        self.assertEqual(g["older_workflows"], ["acct@0.0.9"])


class TestGateConcurrency(StoreCase):
    def test_a_poison_committed_while_another_process_decides_is_never_overwritten(self):
        # fix wave B (Astra S1): A decided "allowed" and paused before persisting;
        # B committed poison; A resumed and persisted its allow over the refusal.
        self.bind()
        self.pass_(generation=0, registered={})
        path = str(self.data / db.DB_NAME)
        ctx = multiprocessing.get_context("spawn")
        decided, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        a = ctx.Process(target=_procs.gate_paused, args=(path, decided, resume, out))
        a.start()
        self.addCleanup(a.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(decided.wait(30), "the gate never reached its decision")

        def poisoner():
            c = db.open_store(path)
            try:
                with db.tx(c):
                    passes.poison(c, "SENTINEL-POISON: a bank-feed write failed")
                    raise db.Refusal("a bank-feed write failed")
            except db.Refusal:
                pass
            finally:
                c.close()
        b = threading.Thread(target=poisoner)
        b.start()
        b.join(1.0)                 # pre-fix, the poison lands here, while A is paused
        resume.set()
        a.join(30)
        b.join(30)
        self.assertEqual(out.get(timeout=5)[0], "ok")
        refused = self.conn.execute(
            "SELECT COUNT(*) FROM passes WHERE json_extract(gate_json, '$.allowed') = 0"
        ).fetchone()[0]
        self.assertEqual(refused, 1)
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("SENTINEL-POISON", g["reason"])

    def test_the_gate_reuses_an_open_caller_transaction(self):
        # an import calls it inside its own write transaction; it must not nest tx()
        self.bind()
        self.pass_(generation=0, registered={})
        with db.tx(self.conn):
            self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])
        self.assertIsNotNone(passes.current_pass(self.conn)["gate_json"])


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
        # fix wave B (Astra S2): a pass trigger is operator data too, and the pass
        # marker row outlived the wipe of the passes table
        t0 = passes.begin_pass(self.conn, "operator: check SENTINEL-CLIENT's invoice")["pass_token"]
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
            self.assertFalse(b"SENTINEL-CLIENT" in f.read_bytes(), f.name)
        self.assertIsNone(self.conn.execute("SELECT * FROM pass_marker").fetchone())
        started = passes.begin_pass(self.conn, "cron")      # a new pass still starts,
        self.assertEqual(started["status"], "started")      # on a fresh generation
        self.assertGreater(started["pass_token"], t0)

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

    def test_reset_wipes_the_steps_and_the_package_requests(self):
        # issue #2: a pass's steps and a package request name a quarter and documents
        import steps
        self.bind()
        self.package_token()
        t = passes.begin_pass(self.conn, "handover")["pass_token"]
        steps.start(self.conn, t, "handover", {"doc_ids": [1]})
        for table in ("pass_steps", "package_requests"):
            self.assertGreater(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                               0, table)
        self.assertEqual(binding.reset_store(self.conn)["erasure"], "complete")
        for table in ("pass_steps", "package_requests"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                             0, table)

    def test_reset_store_clears_a_restored_gate_refusal_but_keeps_a_dirty_ledger_one(self):
        # fix round 1, M5: reset_store's DELETE keeps the dirty-ledger row (the ledger's
        # own condition is not this store's to clear) but drops every other kind.
        self.bind()
        self.pass_(generation=0, registered={})
        passes.bank_write_gate(self.conn)
        with db.tx(self.conn):
            passes.remember_ledger(self.conn, passes.current_pass(self.conn)["pass_id"])
        self._populate_projection()
        self.pass_(generation=1, registered={})            # a restore: sticks a "restored" row
        passes.bank_write_gate(self.conn)
        row = self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
        self.assertEqual(json.loads(row[0])["kind"], "restored")

        binding.reset_store(self.conn)
        row = self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
        self.assertIsNone(row)                             # the store-only reset clears it

        self.bind()
        self.pass_(generation=2, registered={version.WORKFLOW: "b-9"})
        passes.bank_write_gate(self.conn)                  # sticks a "dirty-ledger" row
        row = self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
        self.assertEqual(json.loads(row[0])["kind"], "dirty-ledger")

        binding.reset_store(self.conn)
        row = self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
        self.assertIsNotNone(row)                          # the ledger's own condition survives
        self.assertEqual(json.loads(row[0])["kind"], "dirty-ledger")

    def _populate_projection(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO projections(dest_row_id, admitted_at) VALUES (1, 'x')")


class TestPoison(StoreCase):
    def test_poison_rolls_back_the_callers_partial_write_but_persists_the_verdict(self):
        # fix round 2: poison must ROLLBACK the caller's in-flight writes (they are
        # about to be discarded by the Refusal anyway), commit the verdict on its
        # own, and leave a transaction open so the caller's `with db.tx` unwinds
        # cleanly instead of masking the real error with a bad ROLLBACK.
        self.bind()
        self.pass_()
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                                  " VALUES ('SHOULD-NOT-SURVIVE', '[]', 'x')")
                passes.poison(self.conn, "a bank-feed write failed mid-pass")
                raise db.Refusal("a bank-feed write failed mid-pass")
        row = self.conn.execute("SELECT 1 FROM counterparties WHERE"
                                " name='SHOULD-NOT-SURVIVE'").fetchone()
        self.assertIsNone(row)                     # the partial write did not survive
        g = passes.bank_write_gate(self.conn)
        self.assertFalse(g["allowed"])
        self.assertIn("a bank-feed write failed mid-pass", g["reason"])
        # the connection is left usable — a later write in the same pass still works
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                              " VALUES ('AFTER-POISON', '[]', 'x')")
        row = self.conn.execute("SELECT 1 FROM counterparties WHERE"
                                " name='AFTER-POISON'").fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
