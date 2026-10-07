"""Diff round e5 (26b68ee..926235b, rev 18.4), each accepted finding reproduced:
- Astra S2a: a payment walked again (18.3) is handed the document that re-opened it, ahead
  of the candidate cap;
- Astra S2b: a scheduled handover-only message offers only its own run's questions."""
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class RewalkShowsItsTrigger(StoreCase):
    def test_the_document_that_reopened_the_payment_is_handed_first(self):
        import decide, loop
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Adobe", booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        for k in range(loop.CANDIDATES_MAX):           # eight nearer same-vendor look-alikes
            self.doc(vendor="Adobe", document_date="2026-09-0%d" % (k + 1))   # no unique fit
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id)
        u = loop.payment_unit(self.conn, self.job_id)
        decide.decide(self.conn, self.token, [{"pid": pid, "outcome": "missing",
                                                "reason": "x",
                                                "expected_revision": u["revision"]}])
        late = self.doc(vendor="Adobe", document_date="2026-09-29", document_number="IT")
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET filed_seq=? WHERE doc_id=?",
                              (db.next_seq(self.conn), late))
            self.assertEqual(loop.rewalk_missing_in_tx(self.conn, self.job_id), 1)
        u = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual(u["pid"], pid)
        self.assertEqual(u["candidates"][0]["doc_id"], late)


class ScheduledHandoverQuestions(StoreCase):
    def test_a_scheduled_handover_message_offers_only_its_own_questions(self):
        import cards
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Adobe", booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        a = self.doc(vendor="Adobe", document_date="2026-09-01", document_number="A1")
        self.machine_match(pid, a, self.token)
        b = self.doc(vendor=None, document_date="2026-09-02", document_number="B2")
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (pid,)).fetchone()[0]
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO replace_questions(job_id, pid, match_id,"
                              " new_doc_id, state, created_seq) VALUES ('earlier-run', ?, ?,"
                              " ?, 'open', ?)", (pid, mid, b, db.next_seq(self.conn)))
        other = self.doc(amount_minor=1, document_number="LONE")
        with db.tx(self.conn):
            rid = cards.compose_end(self.conn, self.job_id, scheduled=True,
                                    handover_docs=[other], standalone=True)
        self.assertNotIn("to check", self.render_text(rid))
