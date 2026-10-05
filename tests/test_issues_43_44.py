"""T16 phase 1 (live test of S7): #43 — the job's deliver unit, carried out the way the
quarterly-job skill says, through the real tools; #44 — "send it again" on the package
note and on the file; the desk's "show me the X payment" and partial vendor names."""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker

A = "aaaaaaaa-1"


class DeliverWithPassToken(StoreCase):
    """#43: job._choose ends the package pass before _sends hands out build/deliver, so the
    unit's pass_token names no live pass; the deliver writes are fenced by the claim's
    package_token (check_package_token), never by the dead pass marker."""

    def test_the_deliver_unit_stages_posts_and_records_with_the_units_pass_token(self):
        import asks, db, tools, qa_server  # noqa: F401
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        self.assertEqual(u["unit"], "deliver")
        # the package pass ended at the build: no live marker while deliver is handed out
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)
        self.assertIn("pass_token", u)                       # job._account hands it out
        T = qa_server.TOOLS
        try:
            st = T["stage_for_delivery"]["fn"]({"package_id": u["package_id"],
                                                "package_token": u["package_token"],
                                                "pass_token": u["pass_token"]})
        except db.Refusal as e:
            self.fail(f"#43: deliver unit refused at staging: {e}")
        with FakeBroker():
            out = T["post_package"]["fn"]({"delivery_id": st["delivery_id"],
                                           "package_token": u["package_token"],
                                           "pass_token": u["pass_token"]})
        self.assertIsNotNone(out["package"], out)
        T["record_delivery"]["fn"]({"delivery_id": st["delivery_id"], "outcome": "delivered",
                                    "package_token": u["package_token"],
                                    "pass_token": u["pass_token"]})
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "delivered")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_staging_twice_with_the_pass_token_returns_the_same_send(self):
        import asks, qa_server
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        args = {"package_id": u["package_id"], "package_token": u["package_token"],
                "pass_token": u["pass_token"]}
        st = qa_server.TOOLS["stage_for_delivery"]["fn"](dict(args))
        again = qa_server.TOOLS["stage_for_delivery"]["fn"](dict(args))
        self.assertEqual((again["delivery_id"], again.get("already")), (st["delivery_id"], True))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_a_superseded_claims_tokens_still_stage_nothing(self):
        """The fence the fix leans on: check_package_token refuses an older claim's gen."""
        import asks, db, job, qa_server
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        job.claim(self.conn, A)                                  # a newer turn claims
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"package_id": u["package_id"],
                                                         "package_token": u["package_token"],
                                                         "pass_token": u["pass_token"]})
        self.assertIn("no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_a_superseded_claim_posts_and_records_nothing(self):
        """post_package and record_delivery under a superseded claim's tokens (pass_token ==
        package_token) are refused: the send stays staged, never posted. (A send already
        posted is recovered `uncertain` by the newer claim, and its `delivered` upgrade is
        accepted on evidence alone — with or without tokens, as before.)"""
        import asks, db, job, qa_server
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        T = qa_server.TOOLS
        st = T["stage_for_delivery"]["fn"]({"package_id": u["package_id"],
                                            "package_token": u["package_token"],
                                            "pass_token": u["pass_token"]})
        job.claim(self.conn, A)                                  # a newer turn claims
        args = {"delivery_id": st["delivery_id"], "package_token": u["package_token"],
                "pass_token": u["pass_token"]}
        with FakeBroker() as b:
            out = T["post_package"]["fn"](dict(args))
        self.assertEqual((out["package"], len(b.deposits)), (None, 0))
        self.assertIn("no longer the current one", out["refused"])
        with self.assertRaises(db.Refusal):
            T["record_delivery"]["fn"]({**args, "outcome": "uncertain"})
        self.assertEqual(self.conn.execute("SELECT status, posted_at FROM deliveries")
                         .fetchone()[:], ("staged", None))

    def test_a_pass_token_never_admits_a_send_bound_to_no_request(self):
        """last_built with a dead pass_token is refused, even when it equals the
        package_token: no request, no package fence to lean on."""
        import db, qa_server
        self.delivered_package()
        n = self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        g = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"last_built": True, "pass_token": g,
                                                         "package_token": g})
        self.assertIn("this pass is no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n)

    def test_a_pass_token_never_admits_a_resend(self):
        """A resend ("send it again") is bound to no request: a dead pass_token is refused
        even when it equals the package_token, early and in the committing transaction."""
        import db, delivery, qa_server
        self.delivered_package(first_outcome="uncertain")
        g = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        n = self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True, "pass_token": g,
                                                         "package_token": g})
        self.assertIn("this pass is no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n)
        # the committing transaction's own fence (past the early check)
        from unittest import mock
        real, calls = delivery._check_pass, []

        def late_only(*a):
            calls.append(a)
            if len(calls) > 1:
                real(*a)
        with mock.patch.object(delivery, "_check_pass", late_only):
            with self.assertRaises(db.Refusal):
                qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True, "pass_token": g,
                                                             "package_token": g})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n)
        # control: without the dead token the resend stages
        st = qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n + 1)
        with FakeBroker():
            qa_server.TOOLS["post_package"]["fn"]({"delivery_id": st["delivery_id"]})
        # its record, too, keeps the pass fence: no request, so no package fence instead
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["record_delivery"]["fn"]({"delivery_id": st["delivery_id"],
                                                      "outcome": "delivered",
                                                      "pass_token": g, "package_token": g})
        self.assertIn("this pass is no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (st["delivery_id"],)).fetchone()[0], "staged")

    def test_a_dead_pass_says_so_in_plain_words(self):
        """Fix B: a dead marker never claims that another turn continued the pass."""
        import db, passes
        self.delivered_package()
        with self.assertRaises(db.Refusal) as cm:
            passes.check_token(self.conn, 999)
        self.assertTrue(str(cm.exception).startswith("this pass is no longer the current one"))
        self.assertNotIn("another turn", str(cm.exception))

    def test_the_skill_and_the_tools_say_the_deliver_unit_passes_its_package_token(self):
        """Fix B: the deliver section and the three tools' descriptions name the
        package_token as what admits a deliver unit's calls."""
        import pathlib, qa_server, tools  # noqa: F401
        root = pathlib.Path(__file__).resolve().parents[1]
        job = (root / "skills/quarterly-job/SKILL.md").read_text()
        deliver = " ".join(job[job.index("### `deliver`"):job.index("## Never")].split())
        self.assertIn("Each of the three calls below takes the unit's `package_token`: the "
                      "check's pass has ended, so that token is what admits them.", deliver)
        for n in ("stage_for_delivery", "post_package", "record_delivery"):
            d = qa_server.TOOLS[n]["description"]
            self.assertIn("deliver unit", d, n)
            self.assertIn("package_token", d, n)


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

    def test_a_quote_of_the_package_note_after_delivered_says_it_did_arrive(self):
        import db, views
        self.delivered_package()
        _, note = self._send_last("delivered")
        n = self.deliveries()
        q = LABEL + "\n" + views.unesc(self.render_text(note))
        with self.assertRaises(db.Refusal) as cm:
            self.resend("send it again", q)
        self.assertIn("did arrive", str(cm.exception))
        self.assertEqual(self.deliveries(), n)

    def test_a_quote_of_the_file_after_delivered_says_it_did_arrive(self):
        import db
        self.delivered_package()
        cap, _ = self._send_last("delivered")
        n = self.deliveries()
        with self.assertRaises(db.Refusal) as cm:
            self.resend("send it again", LABEL + "\n" + cap)
        self.assertIn("did arrive", str(cm.exception))
        self.assertEqual(self.deliveries(), n)

    def test_a_quote_of_the_file_after_uncertain_sends_it_again(self):
        pkg = self.delivered_package(first_outcome="uncertain")
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
        pkg = self.delivered_package()
        r = self.conn.execute("SELECT * FROM renders WHERE kind='package-file'").fetchone()
        sc = json.loads(r["scope_json"])
        self.assertEqual(sc["offers"], [pkg])
        self.assertTrue(r["text"].endswith(views.tag_for(r["render_id"])), r["text"])
        self.assertIsNotNone(r["posted_seq"])
        note = self.conn.execute("SELECT * FROM renders WHERE kind='package-note'").fetchone()
        self.assertEqual(json.loads(note["scope_json"])["offers"], [pkg])
        self.assertIn(views.tag_for(note["render_id"]), note["text"].split("\n")[0])

    def test_two_packages_identical_notes_each_bind_their_own(self):
        """V2: the tag tells apart two notes whose text is otherwise the same (two packages
        of one quarter): each quote binds its own note, never AMBIGUOUS."""
        import asks, views
        self.delivered_package()
        import db
        asks.request_package(self.conn, "2026-Q3")
        self.drive(A, deliver=True, stop_before="deliver")
        with db.tx(self.conn):          # the second build's lines, word for word the first's
            self.conn.execute("UPDATE packages SET caption=(SELECT caption FROM packages"
                              " WHERE package_id=1) WHERE package_id=2")
        with FakeBroker():
            self.drive(A, deliver=True)
        notes = self.conn.execute("SELECT render_id, text FROM renders WHERE"
                                  " kind='package-note' ORDER BY rowid").fetchall()
        self.assertEqual(len(notes), 2)
        for rid, text in notes:
            views.mark_rendering_delivered(self.conn, rid)
        strip = [t.split("\n")[0][:-len(views.tag_for(r))] + "\n" + t.split("\n", 1)[1]
                 for r, t in notes]
        self.assertEqual(strip[0], strip[1])                 # the same words, but the tag
        for rid, text in notes:
            self.assertEqual(views.bound_rendering(self.conn, LABEL + "\n" + views.unesc(text))
                             ["render_id"], rid)

    def test_unquoted_words_still_skip_the_note_and_the_file(self):
        """The note and the file stay informational for words with no quote: they never
        take the reply from the view before them."""
        import db, views
        self.delivered_package()
        r = views.build_review(self.conn, view="status")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        self._send_last("delivered")
        # both packages' notes delivered (the job posted the first) after the view
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE kind IN"
                                           " ('package-note', 'package-file')"
                                           " AND delivered_at IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(db.last_delivered(self.conn)["render_id"], r["render_id"])
        self.assertEqual(views.bound_rendering(self.conn, None)["render_id"], r["render_id"])


class PoliteDirectives(StoreCase):
    """Ruling A3: a polite request form of a directive is the directive, not a question."""

    def instructions(self, text):
        import posting
        out = posting.propose_reading(self.conn, text)
        return out["instructions"], out["not_a_reply"]

    def test_polite_resend_forms_reach_the_rule(self):
        self.delivered_package()
        for t in ("can you send it again?", "Could you send it again please?",
                  "send it again please", "please send it again", "would you send it again?",
                  "can you send the package again?", "send that again"):
            self.assertEqual(self.instructions(t), (["resend"], False), t)

    def test_polite_send_last_and_show_forms(self):
        self.delivered_package()
        self.assertEqual(self.instructions("could you send me the last package you built?"),
                         (["send last"], False))
        self.assertEqual(self.instructions("can you show the rest?"), (["show the rest"], False))

    def test_real_questions_stay_questions(self):
        self.delivered_package()
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
