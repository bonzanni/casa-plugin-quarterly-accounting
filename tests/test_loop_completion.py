"""Simple loop §1 "Complete": the quarter ended; the ledger covers it (a row booked after
its last day, or a successful sync after it); nothing pending; nothing proposed; every
payment matched, needing no invoice, optional, or left missing by the operator. The ready
notice is owed until delivered, once per completion, "updated" after a reopening; a run
that completes a quarter posts exactly one message, the completion (D19)."""
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class Completion(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.row(9001, booking_date="2026-09-10", value_date="2026-09-10")
        self.pid = self.lineage_for(9001)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def snap(self, through):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', ?, 0, 0, ?)", (db.now(), through))

    def test_coverage_needs_a_row_booked_after_the_quarter_or_a_later_sync(self):
        import loop
        self.snap("2026-09-28")
        self.assertFalse(loop.covered(self.conn, "2026-Q3"))
        self.row(9002, booking_date="2026-09-30", value_date="2026-09-30")   # late Q3 row: no
        self.assertFalse(loop.covered(self.conn, "2026-Q3"))
        self.row(9003, booking_date="2026-10-02", value_date="2026-10-02")
        self.assertTrue(loop.covered(self.conn, "2026-Q3"))

    def test_a_left_missing_payment_completes_a_pending_one_does_not(self):
        import loop
        self.snap("2026-10-03")
        with self.patch_clock(_dt("2026-10-06")):
            self.assertFalse(loop.complete(self.conn, "2026-Q3"))     # missing, unanswered
            self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
                c, [self.pid], grant=grant))
            self.assertTrue(loop.complete(self.conn, "2026-Q3"))
            self.row(9004, booking_date=None, value_date="2026-09-29", status="PDNG")
            p4 = self.lineage_for(9004)
            self.settle(p4)
            self.assertFalse(loop.complete(self.conn, "2026-Q3"))

    def test_the_ready_notice_is_once_per_completion_and_updated_after_a_reopening(self):
        import cards, loop, views
        self.snap("2026-10-03")
        self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
            c, [self.pid], grant=grant))

        def owed():
            with db.tx(self.conn):
                return loop.owed_notices(self.conn)

        def notice():
            with db.tx(self.conn):
                return cards.compose_ready(self.conn, ["2026-Q3"])
        with self.patch_clock(_dt("2026-10-06")):
            self.assertEqual(owed(), ["2026-Q3"])
            first = notice()
            self.assertEqual(owed(), ["2026-Q3"])          # composed, not delivered: owed
            views.mark_rendering_delivered(self.conn, first)
            self.assertEqual(owed(), [])                   # once
            self.row(9005, booking_date="2026-09-20", value_date="2026-09-20")   # reopens Q3
            p5 = self.lineage_for(9005)
            self.classify(p5, {"software"})
            self.settle(p5)
            self.assertEqual(owed(), [])
            self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
                c, [p5], grant=grant))
            self.assertEqual(owed(), ["2026-Q3"])
            again = notice()
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (again,)).fetchone()[0]
        self.assertIn("Q3 complete · updated · package ready", text)

    def test_an_operator_run_that_completes_the_quarter_posts_one_message(self):
        import json
        from tests.sim_job import JobDriver
        drv = JobDriver(self, payments=0)
        drv.add_payments(["2026-09-10"])                 # one Q3 payment
        drv.add_payments(["2026-10-02"])                 # booked after Q3: coverage
        with self.patch_clock(_dt("2026-10-06")):
            drv.run_job("cccccccc-1", started_by="operator")
            for pid, in self.conn.execute("SELECT pid FROM projections WHERE status='open'"
                                          ).fetchall():
                self.granted(lambda c, grant, p=pid: __import__("work").leave_missing_in_tx(
                    c, [p], grant=grant))
            units = drv.run_job("cccccccc-2", started_by="operator")
        posts = [u for u in units if u["unit"] in ("view", "post")]
        self.assertEqual(len(posts), 1)
        r = self.conn.execute("SELECT text, scope_json FROM renders WHERE render_id=?",
                              (posts[0]["render_id"],)).fetchone()
        self.assertTrue(r[0].startswith("Q3 complete · "), r[0])
        self.assertEqual(json.loads(r[1])["ready_quarters"], ["2026-Q3"])
        self.assertEqual(self.conn.execute("SELECT times FROM quarter_notices WHERE"
                                           " quarter='2026-Q3'").fetchone()[0], 1)

    def test_wrong_then_rematched_is_notified_again(self):
        import cards, loop, matches, views
        self.snap("2026-10-03")
        self.classify(self.pid, {"software"})       # observed at that import: fresh
        a = self.doc(document_date="2026-09-09")
        mid = self.machine_match(self.pid, a, self.token)["match_id"]
        with self.patch_clock(_dt("2026-10-06")):
            with db.tx(self.conn):
                rid = cards.compose_ready(self.conn, ["2026-Q3"])
            views.mark_rendering_delivered(self.conn, rid)
            shown = self.show(self.pid)
            self.granted(lambda c, grant: matches.reject_in_tx(
                c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
                render_id=shown, bind="rendered"))                # Wrong: Q3 reopens
            with db.tx(self.conn):
                self.assertFalse(loop.complete(self.conn, "2026-Q3"))
            self.machine_match(self.pid, self.doc(document_date="2026-09-10"), self.token)
            with db.tx(self.conn):                                 # matched again: B
                self.assertEqual(loop.owed_notices(self.conn), ["2026-Q3"])

    def test_a_reopening_completed_within_one_run_is_notified_again(self):
        import cards, loop, views
        self.snap("2026-10-03")
        self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
            c, [self.pid], grant=grant))
        with self.patch_clock(_dt("2026-10-06")):
            with db.tx(self.conn):
                views_rid = cards.compose_ready(self.conn, ["2026-Q3"])
            views.mark_rendering_delivered(self.conn, views_rid)
            self.row(9006, booking_date="2026-09-25", value_date="2026-09-25")   # late Q3 row
            p6 = self.lineage_for(9006)
            self.classify(p6, {"software"})
            self.settle(p6)
            self.machine_match(p6, self.doc(document_date="2026-09-24"), self.token)
            with db.tx(self.conn):                       # no run ever saw Q3 incomplete
                self.assertEqual(loop.owed_notices(self.conn), ["2026-Q3"])


    def test_coverage_boundaries_a_pending_row_is_no_evidence(self):
        """§1: BOOKED after the quarter's last day — a pending row booked later is not; the
        first day after the quarter is; a sync through that day is."""
        import loop
        self.row(9007, booking_date="2026-10-04", value_date="2026-10-04", status="PDNG")
        self.assertFalse(loop.covered(self.conn, "2026-Q3"))
        self.row(9008, booking_date="2026-10-01", value_date="2026-09-28")
        self.assertTrue(loop.covered(self.conn, "2026-Q3"))
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM bank_rows WHERE row_id=9008")
        self.snap("2026-09-30")
        self.assertFalse(loop.covered(self.conn, "2026-Q3"))
        self.snap("2026-10-01")
        self.assertTrue(loop.covered(self.conn, "2026-Q3"))

    def test_a_pending_payment_keeps_the_quarter_open_whatever_its_answer(self):
        """§1 "no payment of the quarter is still pending": even one the operator left
        missing; and a quarter not yet ended is never complete."""
        import loop
        self.snap("2026-10-03")
        self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
            c, [self.pid], grant=grant))
        with self.patch_clock(_dt("2026-09-29")):
            self.assertFalse(loop.complete(self.conn, "2026-Q3"))     # Q3 has not ended
        with self.patch_clock(_dt("2026-10-06")):
            self.assertTrue(loop.complete(self.conn, "2026-Q3"))
            with db.tx(self.conn):
                self.conn.execute("UPDATE projections SET search_state='accepted-missing'")
                self.conn.execute("UPDATE bank_rows SET status='PDNG' WHERE row_id=9001")
            self.assertFalse(loop.complete(self.conn, "2026-Q3"))

    def test_an_uncovered_or_proposed_quarter_is_not_complete(self):
        """§1: the ledger must cover the quarter, and nothing may be proposed."""
        import loop
        self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
            c, [self.pid], grant=grant))
        with self.patch_clock(_dt("2026-10-06")):
            self.assertFalse(loop.complete(self.conn, "2026-Q3"))     # no coverage yet
            self.snap("2026-10-03")
            self.assertTrue(loop.complete(self.conn, "2026-Q3"))
            self.row(9009, booking_date="2026-09-12", value_date="2026-09-12")
            p9 = self.lineage_for(9009)
            self.classify(p9, {"software"})
            self.settle(p9)
            self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
                c, [p9], grant=grant))
            self.assertTrue(loop.complete(self.conn, "2026-Q3"))
            import matches
            d = self.doc(document_date="2026-09-11")
            matches.propose_match(self.conn, pid=p9, doc_id=d, expected_revision=self.rev(p9),
                                  token=self.token, document_date="2026-09-11")
            self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                               (p9,)).fetchone()[0], "proposed")
            self.assertFalse(loop.complete(self.conn, "2026-Q3"))

def _dt(day):
    import datetime as dt
    return dt.datetime.fromisoformat(day + "T12:00:00+00:00")
