"""Diff round e2 (26b68ee..2fa4d6a, rev 18.4), each accepted finding reproduced through the
real surface:
- Astra S1a: a payment settled just before the walk moves on still has its found invoice
  filed before the mirror and the post;
- Astra S1b: the vendor name a payment unit hands out is accepted by ingest_document;
- Astra S2a (BRAIN's R2 ruling): scheduled runs that keep leaving work incomplete say so
  once per streak of three (an alert, the scheduled run's failure channel);
- Astra S2b: a retired replace question never suppresses the handover's receipt;
- Astra S2c: a handover onto a paired payment listed for changed facts still goes through
  the replace card;
- Astra S2d: a superseded replace card's tap answers with the newer question's card."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported


class Owed(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_settled_payments_invoice_is_filed_before_the_post(self):
        import work
        drv = JobDriver(self, payments=1)
        drv.run_job("e2e2e2e2-a0")              # missing; the mirror is in sync after it
        ref = drv.gmail.invoice("Zapier", 99900, "EUR", drv.DATES[0], "ZAP-X")
        real, n = drv._payment, [0]

        def search_then_leave(u, token):
            n[0] += 1
            if n[0] == 1:
                drv._tool("record_search", {"pass_token": token, "pids": [u["pid"]],
                                            "search": "plain", "queries": ["Zapier"],
                                            "refs": [ref]})
                self.granted(lambda c, grant: work.leave_missing_in_tx(c, [u["pid"]],
                                                                       grant=grant))
                return None
            return real(u, token)
        drv._payment = search_then_leave
        units = [u["unit"] for u in drv.run_job("e2e2e2e2-a1")]
        files = max(i for i, u in enumerate(units) if u == "payment")
        self.assertLess(files, units.index("view") if "view" in units else len(units))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE"
                                           " source_ref=?", (ref,)).fetchone()[0], 1)

    def test_a_long_vendor_name_is_filed(self):
        drv = JobDriver(self, payments=0)
        name = "Very Long Vendor Name " * 4 + "BV"                 # 90 characters
        drv._add_rows([(name, 4242, "2026-07-05", "software")])
        drv.gmail.invoice(name, 4242, "EUR", "2026-07-05", "LONG-1")
        drv.run_job("e2e2e2e2-a2")
        self.assertEqual(self.conn.execute("SELECT status FROM projections").fetchone()[0],
                         "matched")

    def test_incomplete_scheduled_runs_speak_once_per_streak_of_three(self):
        """e2 Astra S2 under BRAIN's R2 ruling: one incomplete scheduled run stays silent;
        three in a row raise ONE `run-incomplete` alert (posted alone); a fourth says
        nothing new; a clean run ends the streak, and a new streak speaks again."""
        drv = JobDriver(self, payments=1)
        drv.run_job("e2e2e2e2-a3")                              # missing, shown
        real = drv._payment
        drv._payment = lambda u, token: None                   # never progresses

        def alerts():
            return [r[0] for r in self.conn.execute(
                "SELECT occurrence_key FROM alerts WHERE kind='run-incomplete'")]
        for k in range(2):
            drv.run_job(f"e2e2e2e2-5{k}", started_by="scheduled")
            self.assertEqual(alerts(), [], k)                  # silent: the next one retries
        units = drv.run_job("e2e2e2e2-52", started_by="scheduled")
        self.assertEqual(alerts(), ["incomplete:e2e2e2e2-50"])
        self.assertTrue([u for u in units if u["unit"] == "post"])
        text = self.conn.execute("SELECT r.text FROM renders r JOIN alerts a ON"
                                 " a.render_id=r.render_id WHERE a.kind='run-incomplete'"
                                 ).fetchone()[0]
        self.assertIn("search incomplete", text)
        drv.run_job("e2e2e2e2-53", started_by="scheduled")
        self.assertEqual(len(alerts()), 1)                      # once per streak
        drv._payment = real
        drv.run_job("e2e2e2e2-54", started_by="scheduled")     # clean: the streak ends
        drv._payment = lambda u, token: None
        for k in range(5, 8):
            drv.run_job(f"e2e2e2e2-5{k}", started_by="scheduled")
        self.assertEqual(alerts(), ["incomplete:e2e2e2e2-50", "incomplete:e2e2e2e2-55"])


class ReplaceFlow(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Adobe", booking_date="2026-09-02", value_date="2026-09-02")
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        self.a = self.doc(vendor="Adobe", document_date="2026-09-01", document_number="A1")
        self.machine_match(self.pid, self.a, self.token)

    def ask(self, number):
        d = self.doc(vendor=None, document_date="2026-09-02", document_number=number)
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (self.pid,)).fetchone()[0]
        with db.tx(self.conn):
            self.conn.execute("UPDATE replace_questions SET state='superseded' WHERE pid=?"
                              " AND state='open'", (self.pid,))
            self.conn.execute("INSERT INTO replace_questions(job_id, pid, match_id,"
                              " new_doc_id, state, created_seq) VALUES (?,?,?,?, 'open', ?)",
                              (self.job_id, self.pid, mid, d, db.next_seq(self.conn)))
        return d

    def tap(self, deposit, label):
        import qa_server
        import tools  # noqa: F401
        b = next(b for b in deposit["buttons"] if b["label"] == label)
        return qa_server.TOOLS[b["call"]["tool"]]["fn"](dict(b["call"]["arguments"]))

    def card(self):
        import cards
        with db.tx(self.conn):
            end = cards.compose_end(self.conn, self.job_id, scheduled=False)
            return cards.deposit_of(self.conn, cards.card(self.conn, end, 0))

    def test_a_superseded_tap_answers_with_the_newer_card(self):
        self.ask("B2")
        older = self.card()
        self.ask("C3")
        out = self.tap(older, "Use new")
        self.assertIn("Nothing was applied", out["receipt"])
        self.assertIn("already has an invoice.", out["next"]["text"])
        self.assertIn("New: invoice C3", out["next"]["text"])

    def test_a_retired_question_never_hides_the_handover_receipt(self):
        import cards, matches
        d = self.ask("B2")
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (self.pid,)).fetchone()[0]
        rid = self.show(self.pid)
        self.granted(lambda c, grant: matches.reject_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        with db.tx(self.conn):
            end = cards.compose_end(self.conn, self.job_id, scheduled=False,
                                    handover_docs=[d], standalone=True)
        text = self.render_text(end)
        self.assertIn("Filed.", text)
        self.assertNotIn("to check", text)

    def test_a_handover_onto_a_changed_paired_payment_goes_through_the_card(self):
        import asks, decide, loop
        self.row(1, counterparty="Adobe", amount_minor=11000, booking_date="2026-09-02",
                 value_date="2026-09-02")                      # the bank corrects it
        self.settle(self.pid)
        new = self.doc(vendor=None, amount_minor=11000, document_date="2026-09-02",
                       document_number="N1")
        asks.request_work(self.conn, "handover", "operator", [new])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id, handover_docs=[new])
        u = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual((u["why"], u["handed_over"]), ("handover", [new]))
        out = decide.decide(self.conn, self.token, [{
            "pid": self.pid, "outcome": "match", "doc_id": new, "document_date": "2026-09-02",
            "expected_revision": u["revision"]}])
        self.assertEqual(out["applied"], 0)
        out = decide.decide(self.conn, self.token, [{
            "pid": self.pid, "outcome": "replace", "doc_id": new,
            "expected_revision": u["revision"]}])
        self.assertEqual(out["applied"], 1)
