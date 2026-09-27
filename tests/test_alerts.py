import multiprocessing
import unittest

from tests import _procs
from tests._base import StoreCase
import db  # noqa: E402
import passes  # noqa: E402
import views  # noqa: E402


class TestAlerts(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def finish(self, token):
        return passes.end_pass(self.conn, token, "complete", {})["speak"]

    def test_a_quiet_pass_says_nothing(self):
        self.assertIsNone(self.finish(self.pass_()))

    def test_gmail_failure_speaks_once_per_occurrence(self):
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        speak = self.finish(t)
        self.assertIn("Gmail", speak["text"])
        views.mark_rendering_delivered(self.conn, speak["render_id"])
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        self.assertIsNone(self.finish(t))                      # same occurrence: silent
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", True)
        self.assertIsNone(self.finish(t))                      # no "all better" message
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        self.assertIsNotNone(self.finish(t))                   # a new occurrence

    def test_an_undelivered_alert_is_offered_again(self):
        t = self.pass_()
        passes.record_probe(self.conn, t, "bank_sync", False, "consent expired")
        first = self.finish(t)
        self.assertIn("re-authorise", first["text"].lower())
        t = self.pass_()
        passes.record_probe(self.conn, t, "bank_sync", False, "consent expired")
        self.assertIsNotNone(self.finish(t))                   # the first send never landed

    def test_bound_account_gone(self):
        t = self.pass_(accounts=[{"account_id": "other", "category": "company", "label": "X"}])
        self.assertIn("bound account", self.finish(t)["text"])

    def test_empty_detail_omits_the_parenthetical(self):
        t = self.pass_()
        passes.record_probe(self.conn, t, "bank_sync", False)          # no detail string
        text = self.finish(t)["text"]
        self.assertNotIn("()", text)
        self.assertIn("The bank connection stopped — new payments aren't coming in.", text)

    def test_delivered_quarter_changed_names_package_and_rows_once(self):
        self.row(1, counterparty="Adobe", amount_minor=5445, booking_date="2026-07-14")
        pid = self.lineage_for(1)
        self.settle(pid)
        with db.tx(self.conn):
            pk = self.conn.execute("INSERT INTO packages(quarter, filename, path, built_at,"
                                   " partial, digest, size, caption, manifest_json) VALUES"
                                   " ('2026-Q3','books-2026-Q3-2026-10-14.zip','/x','x',0,'d',1,"
                                   " 'c','{}')").lastrowid
            self.conn.execute("INSERT INTO alerts(kind, occurrence_key, detail, raised_at) VALUES"
                              " ('delivered-changed', 'k1', ?, 'x')",
                              (db.canonical({"package": "books-2026-Q3-2026-10-14.zip",
                                             "quarter": "2026-Q3", "row_id": 1,
                                             "change": "corrected"}),))
        t = self.pass_()
        speak = self.finish(t)
        self.assertIn("books-2026-Q3-2026-10-14.zip", speak["text"])
        self.assertIn("Adobe · EUR 54.45 · 14 Jul", speak["text"])
        views.mark_rendering_delivered(self.conn, speak["render_id"])
        self.assertIsNone(self.finish(self.pass_()))
        del pk


class TestAlertRace(StoreCase):
    """end_pass commits pass_marker.live=0 before calling pending_rendering, so
    a second pass can begin, re-observe the same still-failing occurrence and
    reach pending_rendering while the first pass's own call is still in
    flight (fix round 1). Both must see the SAME render for that occurrence —
    never two."""
    def setUp(self):
        super().setUp()
        self.bind()

    def test_two_processes_do_not_double_render_the_same_occurrence(self):
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        path = str(self.data / db.DB_NAME)
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        q = ctx.Queue()
        procs = [ctx.Process(target=_procs.pending_rendering, args=(path, barrier, q))
                 for _ in range(2)]
        for p in procs:
            p.start()
        results = [q.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(30)
        self.assertTrue(all(r is not None for r in results), results)
        self.assertEqual(len({r["render_id"] for r in results}), 1, results)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM renders WHERE kind='alert'"
                                           ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT render_id) FROM alerts"
                                           " WHERE kind='gmail'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
