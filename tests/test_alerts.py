import multiprocessing
import unittest

from tests import _procs
from tests._base import StoreCase
import db  # noqa: E402
import passes  # noqa: E402
import views  # noqa: E402


def gmail_failed(case, token, detail=""):
    """Gmail's probe failed, on its third run in a row (D10: the line is said once the
    streak reaches alerts.GMAIL_RUNS runs; simple loop Task 10)."""
    import alerts
    passes.record_probe(case.conn, token, "gmail", False, detail)
    with db.tx(case.conn):
        case.conn.execute("UPDATE probes SET fail_runs=? WHERE kind='gmail'",
                          (alerts.GMAIL_RUNS,))


class TestAlerts(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def finish(self, token):
        return self.end_and_speak()

    def test_a_quiet_pass_says_nothing(self):
        self.assertIsNone(self.finish(self.pass_()))

    def test_gmail_failure_speaks_once_per_occurrence(self):
        """D10: said once the probe failed on GMAIL_RUNS runs in a row, once per streak."""
        for _ in range(2):
            t = self.pass_()
            passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
            self.assertIsNone(self.finish(t))                  # under the threshold
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        speak = self.finish(t)
        self.assertIn("Gmail", speak["text"])
        views.mark_rendering_delivered(self.conn, speak["render_id"])
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        self.assertIsNone(self.finish(t))                      # same streak: silent
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", True)
        self.assertIsNone(self.finish(t))                      # no "all better" message
        for _ in range(2):
            t = self.pass_()
            passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
            self.assertIsNone(self.finish(t))
        t = self.pass_()
        passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
        self.assertIsNotNone(self.finish(t))                   # a new streak

    def stale_sync(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-01-02')")

    def test_an_undelivered_alert_is_offered_again(self):
        self.stale_sync()
        first = self.finish(self.pass_())
        self.assertIn("Bank not synced since 2 Jan · bank-feed needs attention", first["text"])
        self.assertIsNotNone(self.finish(self.pass_()))        # the first send never landed

    def test_bound_account_gone(self):
        t = self.pass_(accounts=[{"account_id": "other", "category": "company", "label": "X"}])
        self.assertIn("bound account", self.finish(t)["text"])

    def test_empty_detail_omits_the_parenthetical(self):
        t = self.pass_()
        gmail_failed(self, t)                                  # no detail string
        text = self.finish(t)["text"]
        self.assertNotIn("()", text)
        self.assertIn("Gmail stopped letting me in — invoices aren't being searched.",
                      " ".join(text.split()))

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


class TestAlertBatching(StoreCase):
    """fix wave D (Astra S2): a changed-package alert over Telegram's 4096
    UTF-16 units was undeliverable forever. Renderings are batched within the
    limit; each rendering binds only the occurrences it prints."""
    N = 80

    def setUp(self):
        super().setUp()
        self.bind()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO packages(quarter, filename, path, built_at,"
                              " partial, digest, size, caption, manifest_json) VALUES"
                              " ('2026-Q3','books-2026-Q3-2026-10-14.zip','/x','x',0,'d',1,"
                              " 'c','{}')")
        for i in range(1, self.N + 1):
            self.row(i, counterparty="Supplier %02d Consulting Services" % i,
                     amount_minor=100000 + i, booking_date="2026-07-14")
            self.settle(self.lineage_for(i))
            with db.tx(self.conn):
                self.conn.execute(
                    "INSERT INTO alerts(kind, occurrence_key, detail, raised_at) VALUES"
                    " ('delivered-changed', ?, ?, 'x')",
                    ("k%d" % i, db.canonical({"package": "books-2026-Q3-2026-10-14.zip",
                                              "quarter": "2026-Q3", "row_id": i,
                                              "change": "corrected"})))

    def finish(self):
        return self.end_and_speak()

    def test_every_rendering_fits_and_every_occurrence_is_said_once(self):
        said, renders = {}, 0
        while True:
            speak = self.finish()
            if speak is None:
                break
            renders += 1
            self.assertLessEqual(views.utf16_len(speak["text"]), views.BODY_LIMIT)
            self.assertIn("books-2026-Q3-2026-10-14.zip", speak["text"])
            self.assertIn("ask me for a fresh Q3 package", speak["text"])
            again = self.finish()                         # not delivered yet: the same offer
            self.assertEqual(again, speak)
            views.mark_rendering_delivered(self.conn, speak["render_id"])
            for i in range(1, self.N + 1):
                if "Supplier %02d Consulting" % i in speak["text"]:
                    said[i] = said.get(i, 0) + 1
            self.assertLess(renders, 10)
        self.assertGreater(renders, 1)
        self.assertEqual(said, {i: 1 for i in range(1, self.N + 1)})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts WHERE sent_at IS NULL")
                         .fetchone()[0], 0)
        # each occurrence is bound to the rendering that printed it
        for a in self.conn.execute("SELECT render_id, detail FROM alerts"):
            import json
            i = json.loads(a["detail"])["row_id"]
            text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                     (a["render_id"],)).fetchone()[0]
            self.assertIn("Supplier %02d Consulting" % i, text)

    def test_a_new_occurrence_joins_the_next_rendering_when_it_fits(self):
        first = self.finish()
        views.mark_rendering_delivered(self.conn, first["render_id"])
        rest = self.finish()
        t = self.pass_()
        gmail_failed(self, t, "invalid_grant")
        joined = self.end_and_speak()
        self.assertNotEqual(joined["render_id"], rest["render_id"])
        self.assertIn("Gmail", joined["text"])
        self.assertLessEqual(views.utf16_len(joined["text"]), views.BODY_LIMIT)


class TestUnboundedDetail(StoreCase):
    """fix wave D round 2 (Astra + Terra S2): a probe's diagnostic has no bound;
    the first occurrence of a batch was taken unchecked and rendered 4363/5097
    units, offered again unchanged forever."""
    def setUp(self):
        super().setUp()
        self.bind()

    def check(self, detail):
        t = self.pass_()
        gmail_failed(self, t, detail)
        speak = self.end_and_speak()
        self.assertLessEqual(views.utf16_len(speak["text"]), views.BODY_LIMIT)
        flat = speak["text"].replace("\n", " ")
        self.assertTrue(flat.startswith("Gmail stopped letting me in ("), flat[:80])
        self.assertTrue(flat.endswith("Re-authorise Gmail when you can."), flat[-80:])
        self.assertIn(views.CLIP_MARK, speak["text"])
        views.mark_rendering_delivered(self.conn, speak["render_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts WHERE sent_at IS NULL")
                         .fetchone()[0], 0)                      # the occurrence was said

    def test_a_repeated_long_diagnostic(self):
        self.check("Upstream error: " + "gateway timeout; " * 250)

    def test_a_five_thousand_character_diagnostic(self):
        self.check("E" * 5000)

    def test_a_non_bmp_diagnostic_is_cut_between_characters(self):
        self.check("\U0001f6a8" * 3000)


class TestOversizedParkedRendering(StoreCase):
    """fix wave D round 3 (Astra S2): an undelivered alert rendering saved
    oversized by earlier code is never reused; it is re-composed through the fit."""
    def setUp(self):
        super().setUp()
        self.bind()

    def test_an_oversized_parked_rendering_is_recomposed(self):
        t = self.pass_()
        gmail_failed(self, t, "invalid_grant")
        first = self.end_and_speak()
        with db.tx(self.conn):                  # what pre-fix code could have saved
            self.conn.execute("UPDATE renders SET text=? WHERE render_id=?",
                              ("x" * 5000, first["render_id"]))
        t = self.pass_()
        gmail_failed(self, t, "invalid_grant")
        again = self.end_and_speak()
        self.assertNotEqual(again["render_id"], first["render_id"])
        self.assertLessEqual(views.utf16_len(again["text"]), views.BODY_LIMIT)
        self.assertIn("Gmail", again["text"])
        views.mark_rendering_delivered(self.conn, again["render_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts WHERE sent_at IS NULL")
                         .fetchone()[0], 0)


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
        gmail_failed(self, t, "invalid_grant")
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
