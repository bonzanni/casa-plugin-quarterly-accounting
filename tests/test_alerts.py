import unittest

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


if __name__ == "__main__":
    unittest.main()
