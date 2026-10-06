"""T16 phase 1 (live test of S7): #43 — the job's deliver unit, carried out the way the
quarterly-job skill says, through the real tools; #44 — "send it again" on the package
note and on the file; the desk's "show me the X payment" and partial vendor names."""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker

A = "aaaaaaaa-1"


LABEL = "\U0001f4ca Alex"        # Casa's label line: compose_operator_message / compose_file_caption


class SendItAgainOnThePackage(StoreCase):
    """#44: a swipe-reply "send it again" on the package note or on the file itself reaches
    the resend rule (delivery.resend_target): after a send that arrived it says so, after an
    uncertain one it stages the file again. Quotes are composed as Casa composes them: the
    label line, then the body (the note, as displayed) or the caption (plain)."""

    def _send_last(self, outcome):
        """A send-last of the delivered package, posted, settled `outcome`; its note (on
        `delivered`) posted and marked. Returns (the file's caption, the note's render id)."""
        import delivery, posting, views
        st = delivery.stage_for_delivery(self.conn, last_built=True)
        with FakeBroker() as b:
            posting.post_package(self.conn, st["delivery_id"])
        out = delivery.record_delivery(self.conn, delivery_id=st["delivery_id"], outcome=outcome)
        if out.get("note_render_id"):
            views.mark_rendering_delivered(self.conn, out["note_render_id"])
        return b.deposits[0]["caption"], out.get("note_render_id")

    def deliveries(self):
        return self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]

    def resend(self, text, quoted):
        """propose_reading, then each instruction run as the desk skill says."""
        import posting, qa_server, tools  # noqa: F401
        ins = posting.propose_reading(self.conn, text, quoted)["instructions"]
        self.assertEqual(len(ins), 1, ins)
        self.assertIsInstance(ins[0], dict, ins)
        return qa_server.TOOLS["stage_for_delivery"]["fn"](dict(ins[0]["stage_for_delivery"]))

    def test_a_quote_of_the_file_after_delivered_says_it_did_arrive(self):
        import db
        self.sent_package()
        cap, _ = self._send_last("delivered")
        n = self.deliveries()
        with self.assertRaises(db.Refusal) as cm:
            self.resend("send it again", LABEL + "\n" + cap)
        self.assertIn("did arrive", str(cm.exception))
        self.assertEqual(self.deliveries(), n)

    def test_a_quote_of_the_file_after_uncertain_sends_it_again(self):
        pkg = self.sent_package(first_outcome="uncertain")
        import views
        cap = views.unesc(self.conn.execute("SELECT text FROM renders WHERE"
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
        self.assertTrue(r["text"].endswith(views.tag_for(r["render_id"])), r["text"])
        self.assertIsNotNone(r["posted_seq"])
        # simple loop §1: the caption is the whole message — no package note follows
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE"
                                           " kind='package-note'").fetchone()[0], 0)


class PoliteDirectives(StoreCase):
    """Ruling A3: a polite request form of a directive is the directive, not a question."""

    def instructions(self, text):
        import posting
        out = posting.propose_reading(self.conn, text)
        return out["instructions"], out["not_a_reply"]

    def test_polite_resend_forms_reach_the_rule(self):
        self.sent_package()
        for t in ("can you send it again?", "Could you send it again please?",
                  "send it again please", "please send it again", "would you send it again?",
                  "can you send the package again?", "send that again"):
            self.assertEqual(self.instructions(t), (["resend"], False), t)

    def test_polite_send_last_and_show_forms(self):
        self.sent_package()
        self.assertEqual(self.instructions("could you send me the last package you built?"),
                         (["send last"], False))
        self.assertEqual(self.instructions("can you show the rest?"), (["show the rest"], False))

    def test_real_questions_stay_questions(self):
        self.sent_package()
        for t in ("did you send it again?", "why did you send it again?",
                  "can you tell me if it arrived?", "more?", "send it again?"):
            self.assertEqual(self.instructions(t), ([], True), t)


class DeskNames(StoreCase):
    """Ruling UX: "show (me) the X payment" shows that item of the bound rendering; a name's
    first words find a payment of the bound rendering when no whole name does — the exact
    name wins, several are asked about ("Which one?") with nothing applied; `never` stays
    exact."""

    def setUp(self):
        """Ruling F3: the sheet fixture's data sits in 2026-Q3 (as tests/test_s7_readings)."""
        super().setUp()
        import datetime as dt
        cm = self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def propose(self, text, quoted=None):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, quoted)
        return out, (b.proposal() if b.deposits else None)

    def tap(self, prop, label):
        import qa_server, tools  # noqa: F401
        call = next(x for x in prop["buttons"] if x["label"] == label)["call"]
        return qa_server.TOOLS[call["tool"]]["fn"](call["arguments"])

    def operator_rows(self):
        return self.conn.execute("SELECT count(*) FROM log WHERE author='operator'").fetchone()[0]

    def test_show_me_the_x_payment_shows_its_item(self):
        fx = self.sheet_fixture(payee="Snelstart Software", guesses=3)
        for t in ("show me the Snelstart Software payment and the invoice you paired it with",
                  "show the snelstart software one", "now show me the snelstart payment",
                  "open the Snelstart item"):
            out, prop = self.propose(t)
            self.assertEqual((out["instructions"], prop), ([f"show item {fx['pid']}"], None), t)

    def test_a_partial_name_binds_the_one_payment_it_begins(self):
        fx = self.sheet_fixture(payee="Snelstart Software", guesses=3)
        out, prop = self.propose("the SnelStart one is wrong")
        self.assertIsNotNone(prop, out)
        self.assertIn("Unpair", prop["text"])
        self.tap(prop, "Apply")
        self.assertEqual(self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                           (fx["match_id"],)).fetchone()[0], "rejected")

    def test_several_payments_a_partial_name_begins_are_asked_about(self):
        self.EXTRA_PAYEES = ("Snelstart Hosting", "Figma")
        self.sheet_fixture(payee="Snelstart Software", guesses=2)
        for t in ("the snelstart one is wrong", "show me the snelstart payment"):
            out, prop = self.propose(t)
            self.assertIsNone(prop, t)
            self.assertEqual(out["instructions"], [], t)
            self.assertTrue(out["say"].startswith("Which one?"), out["say"])
        self.assertEqual(self.operator_rows(), 0)

    def test_the_exact_name_wins_over_a_longer_one_it_begins(self):
        self.EXTRA_PAYEES = ("Snelstart Software", "Figma")
        fx = self.sheet_fixture(payee="Snelstart", guesses=2)
        out, prop = self.propose("show me the snelstart payment")
        self.assertEqual(out["instructions"], [f"show item {fx['pid']}"])

    def test_a_word_fragment_matches_nothing(self):
        self.sheet_fixture(payee="Snelstart Software", guesses=3)
        for t in ("the soft one is wrong", "the snel one is wrong"):
            out, prop = self.propose(t)
            self.assertIsNone(prop, t)
            self.assertIn("Nothing open matches", out["say"], t)

    def test_never_stays_on_the_exact_name(self):
        self.sheet_fixture(payee="Snelstart Software", guesses=3)
        out, prop = self.propose("no invoices ever for snelstart")
        self.assertIsNone(prop, out)
        out, prop = self.propose("no invoices ever for snelstart software")
        self.assertIsNotNone(prop, out)


class DeskRouting(StoreCase):
    def test_the_desk_routes_send_it_again_and_one_payment_to_propose_reading(self):
        """Ruling A3 / UX: the desk sends "send it again" and "show me the X payment" to
        propose_reading — never straight to stage_for_delivery or from memory."""
        import pathlib
        desk = (pathlib.Path(__file__).resolve().parents[1]
                / "skills/quarterly-accounting/SKILL.md").read_text()
        words = " ".join(desk[desk.index("## The operator's words"):desk.index("## Asks")]
                         .split())
        self.assertIn('"show me the Zapier payment", "send it again" in any words, quoted or '
                      "not): call `propose_reading(", words)
        send = " ".join(desk[desk.index("## Sending again"):desk.index("## Setup")].split())
        self.assertIn('"Send it again" goes to `propose_reading` first, never straight here. '
                      "Its `resend` instruction: `stage_for_delivery(resend=true)`", send)
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
