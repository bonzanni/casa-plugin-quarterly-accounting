"""#99 (+ #93 #94 #96): one light house style for every card, shared in one place
(views.title / views.groups): a bold title line, the question on its own line above the
list it asks about, one tight line per item, blank lines only between groups, no legend
for the buttons, short names, no empty section. Close goes beside real actions and never
alone (a card with nothing to act on goes plain); Get package only where the desk judged
the operator wants the package; a proposed pair is stated once."""

import json

from tests._base import LoopCase, StoreCase, apply_now
from tests.fakebroker import FakeBroker
import db
import views

SKILL = (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "skills/quarterly-accounting/SKILL.md").read_text()


def flat(s):
    return " ".join(s.split())


class Helpers(StoreCase):
    def test_title_groups_and_what_the_operator_sees(self):
        self.assertEqual(views.title("Q3 · 2 payments"), "**Q3 · 2 payments**")
        self.assertEqual(views.groups(["a", "b"], [], ["c"], ["d"]),
                         ["a", "b", "", "c", "", "d"])
        text = views.title(views.field("*Acme* BV")) + "\n1 missing"
        self.assertEqual(views.displayed(text), "*Acme* BV\n1 missing")
        self.assertEqual(views.bold_spans(text), ["*Acme* BV"])


class Cards(LoopCase):
    def c(self, fn, *a, **kw):
        with db.tx(self.conn):
            return fn(self.conn, *a, **kw)

    def text(self, rid):
        return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]

    def labels(self, rid):
        import cards
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        with db.tx(self.conn):
            return [b[0] for b in cards.buttons(self.conn, r)]

    def test_an_end_card_has_a_title_a_question_tight_rows_and_no_legend(self):
        import cards
        for i in range(3):
            p = self.pay("V%d" % i, 1000 + i)
            self.propose(p, amount_minor=1000 + i, document_number="pi_3UKZ%d" % i)
        self.pay("Twilio", 2000)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        lines = self.text(rid).split("\n")
        self.assertTrue(lines[0].startswith("**Q3 checked · 4 payments**"), lines[0])
        i = lines.index("**" + cards.CONFIRM_Q + "**")
        self.assertEqual(lines[i - 1], "")                       # its own group
        rows = lines[i + 1:i + 4]
        self.assertEqual([r[:3] for r in rows], ["1. ", "2. ", "3. "])   # tight, no blank
        self.assertNotIn("pi_3UKZ", self.text(rid))                    # short names
        self.assertNotIn("Review:", self.text(rid))                    # no legend
        self.assertEqual(self.labels(rid), ["Review", "Confirm all", "Invoice links", "Close"])

    def test_a_quote_of_a_bold_titled_card_binds_it(self):
        import cards
        p = self.pay("Zapier", 1958)
        self.propose(p, amount_minor=1958)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.c(cards.deposit_of, rid)
        quote = "\U0001f4ca Finance\n" + views.displayed(self.text(rid))
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], rid)
        out = apply_now(self.conn, "all good", quoted=self.text(rid))
        self.assertTrue(out["applied"])

    def test_a_handover_card_names_what_happened_and_states_a_pair_once(self):
        import cards
        q = self.pay("Runpod.io", 8792)
        held = self.propose(q, amount_minor=8792, document_number="pi_3UKZabc",
                            issuer="Runpod Inc")
        rid = self.c(cards.compose_end, self.job_id, scheduled=False, handover_docs=[held])
        lines = self.text(rid).split("\n")
        self.assertTrue(lines[0].startswith("**1 document you sent**"), lines[0])
        self.assertEqual(sum("Runpod" in ln for ln in lines), 1, lines)   # #96: once
        self.assertNotIn("— confirm?", self.text(rid))

    def test_a_job_end_card_and_a_completion_notice_offer_no_package(self):
        import cards
        p = self.pay("Adobe", 100)
        self.machine_match(p, self.doc(amount_minor=100), self.token)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertEqual(self.labels(end), [])                          # #94, #93: plain
        ready = self.c(cards.compose_ready, ["2026-Q3"])
        self.assertEqual(self.labels(ready), [])


class Views(StoreCase):
    def test_a_view_with_nothing_to_act_on_goes_plain(self):
        import posting
        self.seed_payments([{"counterparty": "Adobe"}])
        with FakeBroker() as b:
            out = posting.show_view(self.conn, view="missing", quarter="2026-Q3")
        self.assertEqual((out["view"], b.deposits), (None, []))
        self.assertIn("post_results", out["note"])
        with FakeBroker() as b:
            posting.post_results(self.conn, out["post"][0])
        self.assertEqual(b.deposits[0]["slot"], "results")
        self.assertTrue(b.deposits[0]["value"].startswith("**Missing · Q3 2026**"))

    def test_missing_rows_are_tight(self):
        rows = [{"counterparty": "Vendor %d" % i, "amount_minor": 100 + i} for i in range(4)]
        self.seed_payments(rows)
        text = views.build_review(self.conn, "missing", quarter="2026-Q3")["text"]
        lines = text.split("\n")
        # #102: the missing view's title is its only heading
        self.assertNotIn("**Missing**", lines)
        at = next(i for i, ln in enumerate(lines) if ln.startswith("Vendor"))
        self.assertEqual(len([ln for ln in lines[at:at + 4] if ln.startswith("Vendor")]), 4)

    def test_the_open_card_offers_the_package_only_when_the_desk_asks(self):
        import posting
        self.seed_payments([{"counterparty": "Adobe"}])
        for package, want in ((False, False), (True, True)):
            with FakeBroker() as b:
                posting.show_view(self.conn, view="open", quarter="2026-Q3", package=package)
            labels = [x["label"] for x in b.proposal()["buttons"]]
            self.assertEqual("Get package" in labels, want, labels)
            self.assertEqual(labels[-1], "Close")


class Links(LoopCase):
    def test_no_known_link_is_one_line_not_a_list_of_unknowns(self):
        import cards
        self.pay("Runpod", 3000)
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3", links=True)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]
        self.assertIn("No download links known for these vendors.", text)
        self.assertNotIn("no link known", text)


class Skill(StoreCase):
    def test_the_desk_skill_carries_the_style_post_and_package(self):
        s = flat(SKILL)
        for phrase in ("One with `post` has nothing to act on: post it with `post_results` as "
                       "its note says.",
                       "Your own replies about the books take the cards' shape: a bold first "
                       "line, a question on its own line, one line per item.",
                       "Add `package=true` only when their words make clear they want the "
                       "package"):
            self.assertIn(phrase, s, phrase)
        self.assertLessEqual(len(SKILL), 10_000)


class ShowMissing(StoreCase):
    """r1 (Astra S2 ×3): [Show missing invoices] decides when tapped — a card when the list
    has something to act on, else the list's first page as the plain answer, binding only
    what it shows."""

    def status(self):
        import posting
        with FakeBroker() as b:
            posting.show_view(self.conn, view="status", quarter="2026-Q3")
        return b.proposal()

    def tap(self, prop):
        import qa_server, tools  # noqa: F401
        call = next(x["call"] for x in prop["buttons"]
                    if x["label"].startswith("Show missing invoices"))
        self.assertEqual(call["arguments"]["action"], "show-missing")
        return qa_server.TOOLS["verdict"]["fn"](dict(call["arguments"]))

    def test_a_long_list_says_how_to_see_it_and_binds_nothing(self):
        self.seed_payments([{"counterparty": f"Vendor {i:02d} Holdings International",
                             "amount_minor": 1000 + i} for i in range(120)])
        out = self.tap(self.status())
        self.assertNotIn("next", out)
        self.assertIn("ask me to show the missing invoices", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE kind='missing'"
                                           " AND posted_seq IS NOT NULL").fetchone()[0], 0)

    def test_a_short_list_is_the_answer_and_a_quote_of_it_binds_it(self):
        self.seed_payments([{"counterparty": "Adobe"}, {"counterparty": "Zapier"}])
        out = self.tap(self.status())
        self.assertNotIn("next", out)
        r = views.bound_rendering(self.conn, views.displayed(out["receipt"]))
        self.assertEqual(r["kind"], "missing")
        self.assertEqual(len(views.render_items(self.conn, r["render_id"])), 2)

    def test_a_list_with_something_to_confirm_is_the_next_card(self):
        import matches
        self.seed_payments([{"counterparty": "Adobe"}, {"counterparty": "Zapier",
                                                        "amount_minor": 999}])
        p = self.conn.execute("SELECT pid FROM projections ORDER BY pid DESC").fetchone()[0]
        matches.propose_match(self.conn, pid=p, doc_id=self.doc(amount_minor=999),
                              expected_revision=self.rev(p), token=self.pass_(),
                              document_date="2026-09-14")
        out = self.tap(self.status())
        self.assertIn("next", out)
        # #102: it replaces the tapped card; its receipt never reads like the card
        self.assertIs(out.get("in_place"), True)
        self.assertEqual(out["receipt"], "Showing the missing invoices.")
        self.assertIn("to confirm", json.dumps(out["next"]))

    def test_a_list_that_lost_its_action_since_the_button_was_shown_is_plain(self):
        import matches
        self.seed_payments([{"counterparty": "Adobe"}, {"counterparty": "Zapier",
                                                        "amount_minor": 999}])
        p = self.conn.execute("SELECT pid FROM projections ORDER BY pid DESC").fetchone()[0]
        token = self.pass_()
        mid = matches.propose_match(self.conn, pid=p, doc_id=self.doc(amount_minor=999),
                                    expected_revision=self.rev(p), token=token,
                                    document_date="2026-09-14")["match_id"]
        prop = self.status()
        self.granted(lambda c, grant: matches.confirm_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=prop["buttons"][0]["call"]["arguments"]["render_id"], bind="rendered"))
        out = self.tap(prop)
        self.assertNotIn("next", out)
        self.assertIn("Adobe", out["receipt"])


class EveryShowButtonDecidesWhenTapped(StoreCase):
    """r2 (Astra S2): the same shape as show-missing — every button that shows another view
    is a keyed tap; none is a stored show_view call that could answer `post`."""

    def test_a_stale_show_matches_tap_answers_plain(self):
        import matches, posting, qa_server, tools  # noqa: F401
        self.seed_payments([{"counterparty": "Adobe", "amount_minor": 999}])
        p = self.conn.execute("SELECT pid FROM projections").fetchone()[0]
        mid = matches.propose_match(self.conn, pid=p, doc_id=self.doc(amount_minor=999),
                                    expected_revision=self.rev(p), token=self.pass_(),
                                    document_date="2026-09-14")["match_id"]
        with FakeBroker() as b:
            posting.show_view(self.conn, view="status", quarter="2026-Q3")
        status = b.proposal()
        rid = status["buttons"][0]["call"]["arguments"]["render_id"]
        self.granted(lambda c, grant: matches.confirm_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        call = next(x["call"] for x in status["buttons"]
                    if x["label"].startswith("Show matches to confirm"))
        out = qa_server.TOOLS[call["tool"]]["fn"](dict(call["arguments"]))
        self.assertNotIn("next", out)
        self.assertIn("Nothing to check", views.displayed(out["receipt"]))

    def test_no_stored_show_view_call_on_any_view(self):
        import posting
        self.sheet_fixture(guesses=2)
        for view in ("status", "check", "missing"):
            with FakeBroker() as b:
                out = posting.show_view(self.conn, view=view, quarter="2026-Q3")
            if out["view"] is None:
                continue
            tools = [x["call"]["tool"] for x in b.proposal()["buttons"] if "call" in x]
            self.assertNotIn("show_view", tools, view)


class PackageAfterAWalk(LoopCase):
    """r3 (Terra, upheld by Astra's defence): Review clears the package card's keyboard, so
    a walk started from a card answering a package request ends on the quarter's card with
    [Get package]; any other walk ends on its receipt (#80)."""

    def walk_end(self, package):
        import posting, qa_server, tools  # noqa: F401
        p = self.pay()
        self.propose(p, document_date="2026-09-02")
        with FakeBroker() as b:
            posting.show_view(self.conn, view="open", quarter="2026-Q3", package=package)
        card = b.proposal()

        def tap(dep, label):
            call = next(x["call"] for x in dep["buttons"] if x["label"] == label)
            return qa_server.TOOLS[call["tool"]]["fn"](dict(call["arguments"]))
        review = tap(card, "Review")["next"]
        return tap(review, "Confirm")

    def test_a_package_walk_ends_on_the_card_with_get_package(self):
        out = self.walk_end(True)
        self.assertIn("Get package", [x["label"] for x in out["next"]["buttons"]])

    def test_any_other_walk_ends_on_its_receipt(self):
        self.assertNotIn("next", self.walk_end(False))


class Views102(LoopCase):
    """#102: the views #99 did not cover."""

    def c(self, fn, *a, **kw):
        with db.tx(self.conn):
            return fn(self.conn, *a, **kw)

    def test_the_check_view_asks_once_and_no_item_repeats_it(self):
        for i in range(3):
            p = self.pay("V%d" % i, 1000 + i)
            self.propose(p, amount_minor=1000 + i)
        text = views.build_review(self.conn, "check", quarter="2026-Q3")["text"]
        self.assertEqual(text.count("**" + views.CHECK_Q + "**"), 1)
        self.assertNotIn("Not sure", text)

    def test_the_check_label_counts_this_quarter_and_the_earlier_ones(self):
        q3 = self.pay("Now", 1000, "2026-08-01")
        self.propose(q3, amount_minor=1000, document_date="2026-08-01")
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        q2 = self.pay("Then", 2000, "2026-05-01")
        self.propose(q2, amount_minor=2000, document_date="2026-05-01")
        self.assertEqual(views.check_label(self.conn, "2026-Q3", 2),
                         "Show 1 to confirm (+1 earlier)")

    def test_labels_stay_within_casas_32_characters(self):
        import unittest.mock as m
        with m.patch.object(views, "membership", lambda *a: []):
            self.assertEqual(views.check_label(self.conn, "2026-Q3", 200),
                             "Show 200 earlier to confirm")
        fake = [{"quarter": "2026-Q3"}] * 150
        with m.patch.object(views, "membership", lambda *a: range(150)), \
                m.patch.object(views.work, "describe", lambda c, p: fake[0]), \
                m.patch.object(views, "_needs_check", lambda d: True):
            label = views.check_label(self.conn, "2026-Q3", 300)
        self.assertEqual(label, "Show 300 to confirm")         # the split would be 34
        self.assertLessEqual(len(label), 32)

    def test_all_good_receipt_has_a_title_and_the_cards_names(self):
        import posting, qa_server, tools  # noqa: F401
        for i in range(2):
            p = self.pay("Elevenlabs.io", 1000 + i)
            self.propose(p, amount_minor=1000 + i, issuer="Eleven Labs Inc.")
        with FakeBroker() as b:
            posting.show_view(self.conn, view="check", quarter="2026-Q3")
        call = next(x["call"] for x in b.proposal()["buttons"] if x["label"] == "All good")
        out = qa_server.TOOLS["verdict"]["fn"](dict(call["arguments"]))
        lines = out["receipt"].split("\n")
        self.assertEqual(lines[0], "**Confirmed 2 matches**")
        self.assertTrue(all(ln.startswith("Elevenlabs.io · ") for ln in lines[1:3]), lines)

    def test_is_it_ready_gets_a_sentence(self):
        import cards
        self.pay("Adobe", 100)
        rid = self.c(cards.compose_open, "2026-Q3", package=True)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]
        self.assertEqual(text.split("\n")[1], "Not ready yet.")
