"""0.11.6: issue #68 — a check job posted a document to the operator's chat (prod
2026-10-08 22:19Z and 22:38Z: get_document inside the job, a random invoice each run).
While a job pass is open, get_document sends nothing and points at read_document; the
operator's own [See PDF] tap (its key, minted into the deposit only) still sends the file."""
import hashlib

from tests._base import LoopCase
import db                     # server/ is on sys.path once tests._base is imported
from tests.fakebroker import FakeBroker


class Case(LoopCase):
    def c(self, fn, *a, **k):
        with db.tx(self.conn):
            return fn(self.conn, *a, **k)

    def held(self, doc_id, data=b"%PDF-1.4\nhello\n%%EOF\n"):
        import documents
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET sha256=?, ext='pdf' WHERE doc_id=?",
                              (hashlib.sha256(data).hexdigest(), doc_id))
        path = documents.path_of(self.conn, doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def see_button(self):
        """A to-confirm card's [See PDF], as deposited: (its stored call, the doc_id)."""
        import cards
        p = self.pay()
        d = self.propose(p, document_number="INV-1")
        self.held(d)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        dep = self.c(cards.deposit_of, self.c(cards.card, end, 0))
        (b,) = [b for b in dep["buttons"] if b["label"] == "See PDF"]
        return b["call"], d

    def call(self, args):
        import qa_server, tools  # noqa: F401
        return qa_server.TOOLS["get_document"]["fn"](dict(args))

    def end_pass(self):
        import loop
        with db.tx(self.conn):
            loop.end_pass(self.conn, self.token, "complete")


class DuringAJobPass(Case):
    def test_the_jobs_get_document_sends_nothing_and_points_at_read_document(self):
        import job
        _call, d = self.see_button()
        self.assertIsNotNone(job.live_job_pass(self.conn))
        with FakeBroker() as broker:
            out = self.call({"doc_id": d})
        self.assertEqual(broker.deposits, [])
        self.assertIsNone(out["document"])
        self.assertIn("read_document", out["receipt"])
        self.assertIn("a check is running", out["receipt"])
        self.assertEqual(list(self.outbox.iterdir()), [])

    def test_the_operators_see_pdf_tap_still_sends_the_file_and_stays_usable(self):
        call, d = self.see_button()
        self.assertEqual(call["tool"], "get_document")
        with FakeBroker() as broker:
            first = self.call(call["arguments"])
            second = self.call(call["arguments"])            # the key is never spent
        self.assertEqual(len(broker.deposits), 2)
        for out in (first, second):
            self.assertTrue(out["document"].startswith("casa-cap-"), out)

    def test_a_key_of_another_document_or_a_made_up_key_is_refused(self):
        call, d = self.see_button()
        other = self.doc(document_number="INV-2")
        self.held(other, b"%PDF-1.4\nother\n%%EOF\n")
        with FakeBroker() as broker:
            wrong_doc = self.call({"doc_id": other, "key": call["arguments"]["key"]})
            made_up = self.call({"doc_id": d, "key": "0" * 32})
            junk = self.call({"doc_id": d, "key": 7})
        self.assertEqual(broker.deposits, [])
        for out in (wrong_doc, made_up, junk):
            self.assertIn("read_document", out["receipt"])

    def test_once_the_pass_ended_the_desk_sends_the_document_again(self):
        _call, d = self.see_button()
        self.end_pass()
        with FakeBroker() as broker:
            out = self.call({"doc_id": d})
        self.assertEqual(len(broker.deposits), 1)
        self.assertTrue(out["document"].startswith("casa-cap-"))


class Skill(Case):
    def test_the_job_skill_forbids_get_document_and_names_read_document(self):
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "skills/quarterly-job/SKILL.md").read_text()
        flat = " ".join(text.split())
        self.assertIn("Never call `get_document`", flat)
        self.assertIn("read with `read_document`", flat)
