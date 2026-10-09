"""0.11.4: issue #56 — [See PDF] on a to-confirm card (operator ruling 2026-10-08: the card
keeps its buttons; tapping it sends the PDF and [Confirm] and the rest stay live). The button
stores get_document(doc_id), a file-delivering capability, and is deposited with
"keep_card": true (Casa #1362, v0.344.58)."""
import hashlib
import json

from tests._base import LoopCase
import db                     # server/ is on sys.path once tests._base is imported
from tests.fakebroker import FakeBroker


class Case(LoopCase):
    def c(self, fn, *a, **k):
        with db.tx(self.conn):
            return fn(self.conn, *a, **k)

    def held(self, doc_id, data, ext="pdf"):
        """Give document `doc_id` real held bytes: the row's hash and extension match them."""
        import documents
        sha = hashlib.sha256(data).hexdigest()
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET sha256=?, ext=? WHERE doc_id=?",
                              (sha, ext, doc_id))
        path = documents.path_of(self.conn, doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def confirm_card(self, **doc):
        import cards
        p = self.pay()
        d = self.propose(p, document_number="INV-1", **doc)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        return self.c(cards.card, end, 0), d

    def deposit(self, rid):
        import cards
        return self.c(cards.deposit_of, rid)

    def get(self, doc_id):
        import qa_server, tools  # noqa: F401
        return qa_server.TOOLS["get_document"]["fn"]({"doc_id": doc_id})


class SeeButton(Case):
    def test_a_confirm_card_carries_see_pdf_with_keep_card_and_a_legend_entry(self):
        rid, d = self.confirm_card()
        dep = self.deposit(rid)
        see = [b for b in dep["buttons"] if b["label"] == "See PDF"]
        key = see[0]["call"]["arguments"].get("key", "")
        self.assertRegex(key, r"^[0-9a-f]{32}$")            # #68: the operator's tap key
        self.assertEqual(see, [{"label": "See PDF", "keep_card": True,
                                "call": {"tool": "get_document",
                                         "arguments": {"doc_id": d, "key": key}}}])
        # every other button is deposited exactly as before (no keep_card)
        self.assertTrue(all("keep_card" not in b for b in dep["buttons"] if b is not see[0]))
        self.assertNotIn("See PDF: the document, sent here; this card stays", dep["text"])

    def test_an_image_document_is_see_document(self):
        import cards
        rid, _ = self.confirm_card(ext="png")
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        self.assertIn("See document", [b[0] for b in cards.buttons(self.conn, r)])

    def test_a_document_casa_cannot_send_has_no_button(self):
        import cards
        rid, _ = self.confirm_card(ext="html")
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        self.assertEqual([b[0] for b in cards.buttons(self.conn, r)],
                         ["Confirm", "Wrong", "Leave for now", "Close"])

    def test_a_card_offering_several_documents_has_no_see_button(self):
        import cards
        p = self.pay()
        alt = self.doc(document_number="INV-91", document_date="2026-08-04")
        self.propose(p, alternatives=[alt], document_number="INV-88",
                     document_date="2026-08-02")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        rid = self.c(cards.card, end, 0)
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        labels = [b[0] for b in cards.buttons(self.conn, r)]
        self.assertNotIn("See PDF", labels)
        self.assertNotIn("see", json.loads(r["scope_json"]))
        self.assertNotIn("keep_card", json.dumps(self.deposit(rid)))

    def test_a_card_stored_before_0_11_4_renders_as_it_did(self):
        import cards
        rid, _ = self.confirm_card()
        with db.tx(self.conn):
            scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                                 " render_id=?", (rid,)).fetchone()[0])
            scope.pop("see")
            self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                              (db.canonical(scope), rid))
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        self.assertEqual([b[0] for b in cards.buttons(self.conn, r)],
                         ["Confirm", "Wrong", "Leave for now", "Close"])


class GetDocument(Case):
    def setUp(self):
        super().setUp()
        import loop                  # #68: no job pass open — the desk's get_document
        with db.tx(self.conn):
            loop.end_pass(self.conn, self.token, "complete")

    def test_the_filed_pdf_is_deposited_as_one_document_under_its_name(self):
        d = self.doc(issuer="Adobe", document_date="2026-07-02", amount_minor=4000)
        data = b"%PDF-1.4\nhello\n%%EOF\n"
        self.held(d, data)
        with FakeBroker() as broker:
            out = self.get(d)
        (dep,) = broker.deposits
        self.assertEqual((dep["slot"], dep["kind"]), ("document", "document"))
        self.assertTrue(dep["filename"].endswith(".pdf"))
        self.assertEqual(out, {"document": out["document"], "filename": dep["filename"]})
        self.assertTrue(out["document"].startswith("casa-cap-"))
        staged = self.outbox / dep["value"].rsplit("/", 1)[-1]
        self.assertEqual(staged.read_bytes(), data)
        # nothing recorded: showing a document is not a delivery
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_a_prefixed_pdf_is_shown_from_its_header(self):
        d = self.doc()
        self.held(d, b"\xef\xbb\xbf%PDF-1.4\n%%EOF\n")
        with FakeBroker() as broker:
            self.get(d)
        staged = self.outbox / broker.deposits[0]["value"].rsplit("/", 1)[-1]
        self.assertEqual(staged.read_bytes(), b"%PDF-1.4\n%%EOF\n")

    def test_an_image_goes_as_a_photo(self):
        d = self.doc()
        self.held(d, b"\x89PNG\r\n\x1a\nxx", ext="png")
        with FakeBroker() as broker:
            self.get(d)
        self.assertEqual(broker.deposits[0]["kind"], "photo")

    def test_refusals_are_words_with_nothing_sent(self):
        d = self.doc()
        self.held(d, b"%PDF-1.4\n").write_bytes(b"%PDF-1.4\nchanged\n")
        other = self.doc()
        self.held(other, b"<html>", ext="html")
        with FakeBroker() as broker:
            missing = self.get(99999)
            tampered = self.get(d)
            html = self.get(other)
        self.assertEqual(broker.deposits, [])
        for out in (missing, tampered, html):
            self.assertIsNone(out["document"])
            self.assertEqual(out["receipt"], out["refused"])
        self.assertIn("there is no document #99999", missing["receipt"])
        self.assertIn("no longer matches", tampered["receipt"])
        self.assertIn(".html", html["receipt"])
        self.assertEqual(list(self.outbox.iterdir()), [])

    def test_a_deposit_casa_refuses_takes_the_copy_back(self):
        d = self.doc()
        self.held(d, b"%PDF-1.4\n")
        with FakeBroker() as broker:
            broker.refuse = "bad_kind"
            out = self.get(d)
        self.assertIsNone(out["document"])
        self.assertIn("bad_kind", out["receipt"])
        self.assertEqual(list(self.outbox.iterdir()), [])
