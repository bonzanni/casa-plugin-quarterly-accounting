# tests/test_desk_loop.py
"""Simple loop §1: a typed "what's open" posts a fresh open-items card (recovery after a
broken walk, or after [Get package]); a typed "confirm all 3" on the end message commits on
Apply as Confirm all does; the desk skill names get_package for every package ask."""
import json
import pathlib
from tests._base import StoreCase, apply_now
import db                     # server/ is on sys.path once tests._base is imported
from tests.fakebroker import FakeBroker

DESK = (pathlib.Path(__file__).resolve().parents[1]
        / "skills/quarterly-accounting/SKILL.md")


def flat(text):
    return " ".join(text.split())


def section(text, head, until):
    start = text.index(head)
    return text[start:text.index(until, start + len(head))]


class Desk(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.pid = None

    def test_whats_open_posts_a_fresh_open_items_card(self):
        import qa_server, tools  # noqa: F401
        self.row(1, booking_date="2026-09-02", value_date="2026-09-02")
        p = self.lineage_for(1)
        self.classify(p, {"software"})
        self.settle(p)
        with FakeBroker() as broker:
            out = qa_server.TOOLS["show_view"]["fn"]({"view": "open"})
        self.assertTrue(out["view"].startswith("casa-cap-"))
        card = json.loads(broker.deposits[0]["value"])
        self.assertIn("still open", card["text"])
        self.assertEqual(card["buttons"][-1]["label"], "Get package")

    def test_typed_confirm_all_on_the_end_message_commits_on_apply(self):
        import cards, matches
        pids = []
        for n in (1, 2):
            self.row(n, counterparty="V%d" % n, amount_minor=1000 + n,
                     booking_date="2026-09-02", value_date="2026-09-02")
            p = self.lineage_for(n)
            self.classify(p, {"software"})
            self.settle(p)
            matches.propose_match(self.conn, pid=p, doc_id=self.doc(amount_minor=1000 + n),
                                  expected_revision=self.rev(p), token=self.token,
                                  document_date="2026-09-01")
            pids.append(p)
        with db.tx(self.conn):
            rid = cards.compose_end(self.conn, self.job_id, scheduled=False)
            cards.deposit_of(self.conn, rid)
        quoted = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                   (rid,)).fetchone()[0]
        apply_now(self.conn, "confirm all 2", quoted=quoted)
        for p in pids:
            self.assertEqual(self.conn.execute("SELECT author FROM match_state WHERE pid=? AND"
                                               " state='matched'", (p,)).fetchone()[0],
                             "operator")

    def test_the_desk_skill_routes_every_package_ask_to_get_package(self):
        text = DESK.read_text()
        for phrase in ("get_package", "what's open", 'show_view(view="open")', "#1305"):
            self.assertIn(phrase, text)
        self.assertNotIn("request_package", text)  # removed-name: asserted absent
        self.assertNotIn("note_render_id", text)  # removed-name: asserted absent
        self.assertLessEqual(len(text), 10_000)


class DeskMore(StoreCase):
    """The carried items of Task 12: the recovery of a reply bound to a card, the sheet-wide
    approval on cards, the desk's quarter on a check, and the account the job no longer asks."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()

    def proposal(self, n):
        import matches
        self.row(9000 + n, counterparty="V%d" % n, amount_minor=1000 + n,
                 booking_date="2026-09-02", value_date="2026-09-02")
        p = self.lineage_for(9000 + n)
        self.classify(p, {"software"})
        self.settle(p)
        matches.propose_match(self.conn, pid=p, doc_id=self.doc(amount_minor=1000 + n),
                              expected_revision=self.rev(p), token=self.token,
                              document_date="2026-09-01")
        return p

    def end_message(self):
        import cards
        with db.tx(self.conn):
            rid = cards.compose_end(self.conn, self.job_id, scheduled=False)
            cards.deposit_of(self.conn, rid)
        return self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()

    def test_a_reply_bound_to_a_card_recovers_with_a_view_show_view_takes(self):
        """Task 7 carry: the recovery of a reading bound to an end message (or any card) is
        the open-items card, never the card's own kind, which show_view refuses."""
        import qa_server, reply, tools  # noqa: F401
        self.proposal(1)
        r = self.end_message()
        rec = reply._Scope(self.conn, r).recovery()
        self.assertEqual(rec, {"view": "open", "quarter": "2026-Q3"})
        self.assertEqual(reply._Scope(self.conn, r).recovery(more=True), rec)
        with FakeBroker() as broker:
            out = qa_server.TOOLS["show_view"]["fn"](rec)
        posted = self.conn.execute("SELECT kind FROM renders WHERE render_id=?",
                                   (out["render_id"],)).fetchone()[0]
        self.assertEqual(posted, "open-items")
        self.assertIn("Q3 · still open: 1 to confirm",
                      json.loads(broker.deposits[0]["value"])["text"])

    def test_an_ambiguous_quote_among_cards_recovers_with_the_open_items_card(self):
        import views
        self.proposal(1)
        a, b = self.end_message(), self.end_message()
        self.assertEqual(views._common_view([a, b]), {"view": "open", "quarter": "2026-Q3"})

    def test_all_good_does_not_bind_a_review_card(self):
        """The sheet-wide approval binds the sheets and the end / open-items messages only:
        a Review card is answered by its own buttons."""
        import cards
        p = self.proposal(1)
        r = self.end_message()
        with db.tx(self.conn):
            card = cards.next_after(self.conn, r["render_id"], -1)
            cards.deposit_of(self.conn, card)
        self.assertEqual(self.conn.execute("SELECT kind FROM renders WHERE render_id=?",
                                           (card,)).fetchone()[0], "review")
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (card,)).fetchone()[0]
        out = apply_now(self.conn, "all good", quoted=text)
        self.assertEqual(out["applied"], [])
        self.assertIn("not a sheet to approve", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT author FROM match_state WHERE pid=? AND"
                                           " state='proposed'", (p,)).fetchone()[0], "auto")

    def test_all_good_on_an_open_items_card_confirms_its_proposals(self):
        import qa_server, tools  # noqa: F401
        pids = [self.proposal(1), self.proposal(2)]
        with FakeBroker() as broker:
            qa_server.TOOLS["show_view"]["fn"]({"view": "open"})
        quoted = json.loads(broker.deposits[0]["value"])["text"]
        apply_now(self.conn, "all good", quoted=quoted)
        for p in pids:
            self.assertEqual(self.conn.execute("SELECT author FROM match_state WHERE pid=? AND"
                                               " state='matched'", (p,)).fetchone()[0],
                             "operator")

    def test_the_desk_check_names_the_quarter_the_operator_named(self):
        """Ruling Q2: "check Q2" carries quarter to request_work, which stores it."""
        import qa_server, tools  # noqa: F401
        asks = flat(section(DESK.read_text(), "## Asks", "## A file the operator sent"))
        self.assertIn('"Check Q2": `request_work(kind="check", trigger="operator", '
                      'quarter="2026-Q2")`', asks)
        out = qa_server.TOOLS["request_work"]["fn"]({"kind": "check", "trigger": "operator",
                                                    "quarter": "Q2 2026"})
        self.assertEqual(self.conn.execute("SELECT quarter FROM work_requests WHERE"
                                           " request_id=?", (out["request_id"],)).fetchone()[0],
                         "2026-Q2")

    def test_the_desk_asks_for_the_business_account_the_check_no_longer_asks(self):
        """Task 10 ruling: the job never offers the account choice; the desk's check_setup →
        propose_account is the way, also after a check stopped for it."""
        setup = flat(section(DESK.read_text(), "## Setup", "## Test install"))
        self.assertIn("The check never asks which account is the business account", setup)
        self.assertIn("`check_setup()`, and when it asks which company account is the business "
                      "account, call `propose_account()`", setup)
        self.assertLess(setup.index("`check_setup()`"), setup.index("`propose_account()`"))

    def test_the_desk_names_the_recovery_of_an_expired_button(self):
        ans = flat(section(DESK.read_text(), "## Answering", "## The operator's words"))
        self.assertIn('"What\'s open?", "review", "what\'s left to check?": '
                      '`show_view(view="open")`', ans)
        self.assertIn('A button that answers "expired" (a card whose send timed out, Casa '
                      '#1305) is recovered the same way: say "review" or "what\'s open" and the '
                      'card comes again.', ans)
