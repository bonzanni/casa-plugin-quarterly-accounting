"""Diff round e1 (26b68ee..e2b1e51, rev 18.4), each accepted finding reproduced through the
real surface (qa_server.TOOLS, a real bank-feed, the real taps):
- Astra S1a: a payment settled meanwhile (the operator's [Leave missing]) keeps its found
  attachments owed — they are filed (a files-only hand-out), never silently dropped;
- Astra S1b: [Use new] binds the handed document's facts as the card showed them;
- Astra S1c: a question whose pairing changed is retired, never advertised;
- Astra S1d: the payment unit names the handed-over documents (`handed_over`), whatever the
  candidates' order;
- Astra S1e: the 11 -> 12 upgrade ends a live legacy pass and queues its taken requests
  again;
- Astra S2: a scheduled run offers only the questions it asked;
- Terra S2: once the work list exists, decide is the handed payment's alone."""
import json

from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported


class FoundAttachmentsOutliveTheirPayment(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_settled_payments_found_attachment_is_still_filed(self):
        import work
        drv = JobDriver(self, payments=1)
        ref = drv.gmail.invoice("Zapier", 99900, "EUR", drv.DATES[0], "ZAP-X")
        real, n = drv._payment, [0]

        def search_then_leave(u, token):
            n[0] += 1
            if n[0] == 1:                       # search, record the find, then cut …
                drv._tool("record_search", {"pass_token": token, "pids": [u["pid"]],
                                            "search": "plain", "queries": ["Zapier"],
                                            "refs": [ref]})
                # … and the operator answers the payment meanwhile
                self.granted(lambda c, grant: work.leave_missing_in_tx(c, [u["pid"]],
                                                                       grant=grant))
                return None
            return real(u, token)
        drv._payment = search_then_leave
        units = drv.run_job("e1e1e1e1-a1")
        self.assertTrue(any(u["unit"] == "payment" and u.get("decided") for u in units))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE"
                                           " source_ref=?", (ref,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_items WHERE"
                                           " state='queued'").fetchone()[0], 0)


class ReplaceBinding(StoreCase):
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
        self.b = self.doc(vendor=None, document_date="2026-09-02", document_number="B2")
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (self.pid,)).fetchone()[0]
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO replace_questions(job_id, pid, match_id,"
                              " new_doc_id, state, created_seq) VALUES (?,?,?,?, 'open', ?)",
                              (self.job_id, self.pid, mid, self.b, db.next_seq(self.conn)))

    def end(self, scheduled=False):
        import cards
        with db.tx(self.conn):
            return cards.compose_end(self.conn, self.job_id, scheduled=scheduled)

    def deposit(self, rid):
        import cards
        with db.tx(self.conn):
            return cards.deposit_of(self.conn, rid)

    def card(self):
        import cards
        end = self.end()
        with db.tx(self.conn):
            return cards.deposit_of(self.conn, cards.card(self.conn, end, 0))

    def tap(self, deposit, label):
        import qa_server
        import tools  # noqa: F401
        b = next(b for b in deposit["buttons"] if b["label"] == label)
        return qa_server.TOOLS[b["call"]["tool"]]["fn"](dict(b["call"]["arguments"]))

    def test_use_new_refuses_a_document_corrected_since_the_card(self):
        import documents
        card = self.card()
        documents.update_document_metadata(self.conn, self.b, document_number="CORRECTED",
                                           amount_minor=90000, currency="USD")
        out = self.tap(card, "Use new")
        self.assertIn("Nothing was applied", out["receipt"])
        cur = self.conn.execute("SELECT s.doc_id FROM projections p JOIN match_state s ON"
                                " s.match_id=p.current_match WHERE p.pid=?",
                                (self.pid,)).fetchone()[0]
        self.assertEqual(cur, self.a)

    def test_a_question_whose_pairing_changed_is_retired(self):
        import matches
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (self.pid,)).fetchone()[0]
        rid = self.show(self.pid)
        self.granted(lambda c, grant: matches.reject_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        text = self.render_text(self.end())
        self.assertNotIn("to check", text)
        self.assertEqual(self.conn.execute("SELECT state FROM replace_questions"
                                           ).fetchone()[0], "superseded")

    def test_a_scheduled_run_offers_only_its_own_questions(self):
        import views
        views.mark_rendering_delivered(self.conn, self.end())       # shown, unanswered
        self.row(2, counterparty="Zapier", booking_date="2026-09-05",
                 value_date="2026-09-05")
        p2 = self.lineage_for(2)
        self.classify(p2, {"software"})
        self.settle(p2)
        with db.tx(self.conn):
            self.conn.execute("UPDATE replace_questions SET job_id='earlier-run'")
        rid = self.end(scheduled=True)
        self.assertIsNotNone(rid)
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        self.assertFalse([o for o in scope["order"] if "q" in o])
        self.assertNotIn("to check", self.render_text(rid))


class HandoverIsNamed(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_the_handed_document_is_named_whatever_the_candidates_order(self):
        """Astra e1 S1d: P holds A; a same-vendor mail invoice B is nearer; the handed H is
        named in `handed_over`, and the skill's replace is H's."""
        import asks, decide, loop
        self.token = self.run_claim()
        self.row(1, counterparty="Adobe", booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        a = self.doc(vendor="Adobe", document_date="2026-08-20", document_number="A1")
        self.machine_match(pid, a, self.token)
        self.doc(vendor="Adobe", document_date="2026-09-02", document_number="B2")
        h = self.doc(vendor=None, document_date="2026-09-10", document_number="H3")
        asks.request_work(self.conn, "handover", "operator", [h])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id, handover_docs=[h])
        u = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual(u["handed_over"], [h])
        out = decide.decide(self.conn, self.token, [{"pid": pid, "outcome": "replace",
                                                      "doc_id": h,
                                                      "expected_revision": u["revision"]}])
        self.assertEqual(out["applied"], 1)


class DecideIsTheHandedPayments(StoreCase):
    def test_another_payment_or_two_entries_are_refused_once_listed(self):
        import decide, loop
        self.bind()
        self.token = self.run_claim()
        pids = []
        for n in (1, 2):
            self.row(n, counterparty="Adobe", booking_date="2026-09-0%d" % n,
                     value_date="2026-09-0%d" % n)
            pid = self.lineage_for(n)
            self.classify(pid, {"software"})
            self.settle(pid)
            pids.append(pid)
        d = self.doc(vendor="Adobe", document_date="2026-09-02")
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id)
        u = loop.payment_unit(self.conn, self.job_id)
        other = [p for p in pids if p != u["pid"]][0]
        with self.assertRaisesRegex(db.Refusal, "handed out now"):
            decide.decide(self.conn, self.token, [{
                "pid": other, "outcome": "match", "doc_id": d, "document_date": "2026-09-02",
                "expected_revision": self.rev(other)}])
        with self.assertRaisesRegex(db.Refusal, "handed out now"):
            decide.decide(self.conn, self.token, [
                {"pid": u["pid"], "outcome": "missing", "reason": "x",
                 "expected_revision": u["revision"]},
                {"pid": other, "outcome": "missing", "reason": "x",
                 "expected_revision": self.rev(other)}])
        self.assertEqual(decide.decide(self.conn, self.token, [{
            "pid": u["pid"], "outcome": "missing", "reason": "x",
            "expected_revision": u["revision"]}])["applied"], 1)


class UpgradeRequeuesTakenWork(StoreCase):
    def test_a_live_legacy_pass_ends_and_its_taken_handover_is_queued_again(self):
        import sqlite3
        from tests.schema_history import build_v11_store
        path = self.tmp / "v11e1" / "accounting.sqlite"
        path.parent.mkdir()
        build_v11_store(path)
        c = sqlite3.connect(path)
        c.execute("INSERT INTO passes(pass_id, generation, trigger, started_at) VALUES"
                  " ('old', 1, 'cron', 'x')")
        c.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id) VALUES"
                  " (1, 1, 1, 'old')")
        c.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                  " created_at, state, pass_id) VALUES ('handover', 'operator', '[7]', 1,"
                  " 'x', 'taken', 'old')")
        c.commit()
        c.close()
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        self.assertEqual(tuple(conn.execute("SELECT state, pass_id FROM work_requests"
                                            ).fetchone()), ("queued", None))
        self.assertEqual(conn.execute("SELECT outcome FROM passes WHERE pass_id='old'"
                                      ).fetchone()[0], "interrupted")
        self.assertEqual(conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)
