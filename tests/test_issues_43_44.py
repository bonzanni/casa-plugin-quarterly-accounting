"""T16 phase 1 (live test of S7): #43 — the job's deliver unit, carried out the way the
quarterly-job skill says, through the real tools; #44 — "send it again" on the package
note and on the file. (#121: the polite-form and partial-name tests pinned the phrase
grammar, gone.)"""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker
import views

A = "aaaaaaaa-1"


LABEL = "\U0001f4ca Alex"        # Casa's label line: compose_operator_message / compose_file_caption


class SendItAgainOnThePackage(StoreCase):
    """#44: a swipe-reply "send it again" on the package note or on the file itself reaches
    the resend rule (delivery.resend_target; #121: via reading_context's render_id): after a send that arrived it says so, after an
    uncertain one it stages the file again. Quotes are composed as Casa composes them: the
    label line, then the body (the note, as displayed) or the caption (plain)."""

    def _send_last(self, outcome):
        """A send-last of the delivered package, posted, settled `outcome`. Returns the
        file's caption (simple loop §1: no package note follows the file)."""
        import delivery, posting
        st = delivery.stage_for_delivery(self.conn, last_built=True)
        with FakeBroker() as b:
            posting.post_package(self.conn, st["delivery_id"])
        delivery.record_delivery(self.conn, delivery_id=st["delivery_id"], outcome=outcome)
        return b.deposits[0]["caption"]

    def deliveries(self):
        return self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]

    def resend(self, text, quoted):
        """#121: "send it again" is the desk's — reading_context(quoted) binds the quoted
        post, then stage_for_delivery(resend=true, render_id=…), as the desk skill says.
        `text` is the operator's words (the desk reads them; code no longer does)."""
        import posting, qa_server, tools  # noqa: F401
        rid = posting.reading_context(self.conn, quoted)["render_id"]
        self.assertIsNotNone(rid)
        return qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True, "render_id": rid})

    def test_a_quote_of_the_file_after_delivered_says_it_did_arrive(self):
        import db
        self.sent_package()
        cap = self._send_last("delivered")
        n = self.deliveries()
        with self.assertRaises(db.Refusal) as cm:
            self.resend("send it again", LABEL + "\n" + cap)
        self.assertIn("did arrive", str(cm.exception))
        self.assertEqual(self.deliveries(), n)

    def test_a_quote_of_the_file_after_uncertain_sends_it_again(self):
        pkg = self.sent_package(first_outcome="uncertain")
        import views
        cap = views.displayed(self.conn.execute("SELECT text FROM renders WHERE"
                                            " kind='package-file'").fetchone()[0])
        n = self.deliveries()
        st = self.resend("Can you send it again?", LABEL + "\n" + cap)
        self.assertEqual(self.deliveries(), n + 1)
        self.assertEqual(self.conn.execute("SELECT package_id FROM deliveries WHERE"
                                           " delivery_id=?", (st["delivery_id"],)).fetchone()[0],
                         pkg)

    def test_the_file_is_a_tagged_rendering_offering_its_package(self):
        import json, views
        pkg = self.sent_package()
        r = self.conn.execute("SELECT * FROM renders WHERE kind='package-file'").fetchone()
        sc = json.loads(r["scope_json"])
        self.assertEqual(sc["offers"], [pkg])
        # #53: the tag is the composition time (" · 8 Oct 21:04:37")
        self.assertRegex(r["text"], r" \u00b7 \d{1,2} [A-Z][a-z]{2} \d\d:\d\d:\d\d$")
        self.assertIsNotNone(r["posted_seq"])
        # simple loop §1: the caption is the whole message — no package note follows
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE"
                                           " kind='package-note'").fetchone()[0], 0)  # removed-name: asserted absent


class DeskRouting(StoreCase):
    def test_the_desk_routes_send_it_again_to_stage_for_delivery(self):
        """Ruling A3 / UX: the desk sends "send it again" through the resend rule — never
        from memory. #121: no longer via propose_reading's `resend` instruction; a reply to
        one post binds it with reading_context's render_id."""
        import pathlib
        desk = (pathlib.Path(__file__).resolve().parents[1]
                / "skills/quarterly-accounting/SKILL.md").read_text()
        send = " ".join(desk[desk.index("## Sending again"):desk.index("## Setup")].split())
        self.assertIn('"Send it again": `stage_for_delivery(resend=true)` (a reply to one post: '
                      "also `render_id` from `reading_context`)", send)
        self.assertLessEqual(len(desk), 10_000)


class RefusedFileBindsNothing(StoreCase):
    """Terra r1 S2 (9b317ef): a post_package deposit Casa refuses leaves no bindable
    `package-file` rendering — the operator never saw that caption."""

    def test_a_refused_deposit_leaves_no_caption_to_bind_and_a_good_post_still_binds(self):
        import db, delivery, posting, views
        self.sent_package()
        st = delivery.stage_for_delivery(self.conn, last_built=True)
        with FakeBroker() as b:
            b.refuse = "bad_filename"
            with self.assertRaises(db.Refusal):
                posting.post_package(self.conn, st["delivery_id"])
        refused_caption = b.deposits[0]["caption"]
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (st["delivery_id"],)).fetchone()[0], "failed")
        with self.assertRaises(views.QuoteRefusal) as cm:
            views.bound_rendering(self.conn, LABEL + "\n" + refused_caption)
        self.assertEqual(cm.exception.line, views.UNMATCHED)
        files = self.conn.execute("SELECT count(*) FROM renders WHERE kind='package-file'"
                                  ).fetchone()[0]
        self.assertEqual(files, 1)                      # the first, delivered send's only
        # a successful post of the same package still binds its own caption
        st = delivery.stage_for_delivery(self.conn, last_built=True)
        with FakeBroker() as b:
            posting.post_package(self.conn, st["delivery_id"])
        rid = self.conn.execute("SELECT render_id FROM renders WHERE kind='package-file'"
                                " ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertEqual(views.bound_rendering(self.conn, LABEL + "\n" + b.deposits[0]["caption"])
                         ["render_id"], rid)
