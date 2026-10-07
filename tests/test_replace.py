"""Rev 18.4 §R18.3: the replace card — bound to the pairing it displayed (r4 Terra S1), one
live question per payment, [Keep current] / [Use new] — through the real taps."""
import json

from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class ReplaceCard(StoreCase):
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
        import replace
        d = self.doc(vendor=None, document_date="2026-09-02", document_number=number)
        with db.tx(self.conn):
            mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                    (self.pid,)).fetchone()[0]
            self.conn.execute("UPDATE replace_questions SET state='superseded' WHERE pid=?"
                              " AND state='open'", (self.pid,))
            qid = self.conn.execute(
                "INSERT INTO replace_questions(job_id, pid, match_id, new_doc_id, state,"
                " created_seq) VALUES (?,?,?,?, 'open', ?)",
                (self.job_id, self.pid, mid, d, db.next_seq(self.conn))).lastrowid
        return d, qid, replace

    def card(self):
        import cards
        with db.tx(self.conn):
            end = cards.compose_end(self.conn, self.job_id, scheduled=False)
            return cards.deposit_of(self.conn, cards.card(self.conn, end, 0))

    def tap(self, deposit, label):
        import qa_server
        import tools  # noqa: F401
        b = next(b for b in deposit["buttons"] if b["label"] == label)
        return qa_server.TOOLS[b["call"]["tool"]]["fn"](dict(b["call"]["arguments"]))

    def holder(self):
        return tuple(self.conn.execute(
            "SELECT s.doc_id, s.author FROM projections p JOIN match_state s ON"
            " s.match_id=p.current_match WHERE p.pid=?", (self.pid,)).fetchone())

    def test_the_card_shows_both_documents_and_use_new_swaps(self):
        b, _, _ = self.ask("B2")
        card = self.card()
        self.assertIn("already has an invoice.", card["text"])
        self.assertIn("Current: invoice A1", card["text"])
        self.assertIn("(matched by the job)", card["text"])
        self.assertIn("New: invoice B2", card["text"])
        self.assertIn("(from you)", card["text"])
        out = self.tap(card, "Use new")
        self.assertIn("Used the new document", out["receipt"])
        self.assertEqual(self.holder(), (b, "operator"))
        import matches
        self.assertEqual(matches.holders(self.conn, self.a), [])

    def test_keep_current_files_the_new_one_and_changes_nothing(self):
        b, qid, _ = self.ask("B2")
        rev = self.rev(self.pid)
        out = self.tap(self.card(), "Keep current")
        self.assertIn("Kept the current document", out["receipt"])
        self.assertEqual(self.holder()[0], self.a)
        self.assertEqual(self.rev(self.pid), rev)
        self.assertEqual(self.conn.execute("SELECT state FROM replace_questions WHERE"
                                           " question_id=?", (qid,)).fetchone()[0], "kept")

    def test_a_superseded_card_commits_nothing(self):
        """r4 Terra S1: card A→B, then a newer question A→C; [Use new] on the A→B card
        commits nothing; the newer card still swaps to C."""
        self.ask("B2")
        older = self.card()
        c, _, _ = self.ask("C3")
        out = self.tap(older, "Use new")
        self.assertIn("Nothing was applied", out["receipt"])
        self.assertEqual(self.holder()[0], self.a)
        self.tap(self.card(), "Use new")
        self.assertEqual(self.holder(), (c, "operator"))

    def test_a_card_whose_pairing_changed_commits_nothing(self):
        """The card binds the pairing it displayed: once the payment holds another one (the
        operator answered it elsewhere), its [Use new] commits nothing."""
        self.ask("B2")
        card = self.card()
        import matches
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (self.pid,)).fetchone()[0]
        rid = self.show(self.pid)
        self.granted(lambda c, grant: matches.reject_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        out = self.tap(card, "Use new")
        self.assertNotIn("Used the new document", out["receipt"])
        self.assertIsNone(self.conn.execute("SELECT current_match FROM projections WHERE"
                                            " pid=?", (self.pid,)).fetchone()[0])

    def test_the_job_never_replaces_and_replace_needs_a_handed_document(self):
        import asks, decide, loop
        d = self.doc(vendor=None, document_date="2026-09-02", document_number="H1")
        other = self.doc(vendor=None, document_date="2026-09-02", document_number="X9")
        asks.request_work(self.conn, "handover", "operator", [d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
        loop.build_work(self.conn, self.job_id, handover_docs=[d])
        u = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual((u["pid"], u["why"], u["holds"]["doc_id"]), (self.pid, "handover",
                                                                       self.a))
        base = {"pid": self.pid, "expected_revision": u["revision"]}
        for e in ({**base, "outcome": "match", "doc_id": d, "document_date": "2026-09-02"},
                  {**base, "outcome": "propose", "doc_id": d, "document_date": "2026-09-02"},
                  {**base, "outcome": "replace", "doc_id": other}):
            out = decide.decide(self.conn, self.token, [e])
            self.assertEqual(out["refused"], 1, e)
        self.assertEqual(self.holder()[0], self.a)                  # nothing replaced
        out = decide.decide(self.conn, self.token, [{**base, "outcome": "replace",
                                                      "doc_id": d}])
        self.assertEqual(out["applied"], 1)
        self.assertEqual(self.holder()[0], self.a)                  # still: only asked
        self.assertEqual(json.loads(json.dumps([tuple(r) for r in self.conn.execute(
            "SELECT pid, new_doc_id, state FROM replace_questions")])),
            [[self.pid, d, "open"]])

    def test_a_card_whose_payment_changed_commits_nothing(self):
        """18.4: the card binds the payment's revision too — a payment that moved since the
        card was shown (its pairing unchanged) commits nothing on [Use new]."""
        self.ask("B2")
        card = self.card()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET revision=revision+1 WHERE pid=?",
                              (self.pid,))
        out = self.tap(card, "Use new")
        self.assertNotIn("Used the new document", out["receipt"])
        self.assertEqual(self.holder()[0], self.a)
