# tests/test_desk_loop.py
"""Simple loop §1: a typed "what's open" posts a fresh open-items card (recovery after a
broken walk, or after [Get package]); a typed "confirm all 3" on the end message commits on
Apply as Confirm all does; the desk skill names get_package for every package ask."""
import json
import pathlib
from tests._base import StoreCase, apply_now, untag
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
        # 0.11.2: the quarter status card ("Q3 · 1 payment" + the non-zero counts)
        self.assertTrue(untag(card["text"]).startswith("Q3 · 1 payment\n1 missing"), card["text"])
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
        for phrase in ("get_package", "Where a quarter stands", 'show_view(view="open")',
                       "#1305"):
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
        self.assertIn("Q3 · 1 payment\n1 to confirm\n",
                      untag(json.loads(broker.deposits[0]["value"])["text"]))

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
        self.assertIn('`request_work(kind="check", trigger="operator", quarter=<the quarter, '
                      'when one is meant>)`', asks)
        # 0.11.2 (#55): a quarter before the books (Q2 here) is not asked while a run is
        # live (setUp's claim); a quarter in the books is carried and stored
        out = qa_server.TOOLS["request_work"]["fn"]({"kind": "check", "trigger": "operator",
                                                    "quarter": "Q3 2026"})
        self.assertEqual(self.conn.execute("SELECT quarter FROM work_requests WHERE"
                                           " request_id=?", (out["request_id"],)).fetchone()[0],
                         "2026-Q3")

    def test_the_desk_asks_for_the_business_account_the_check_no_longer_asks(self):
        """Task 10 ruling: the job never offers the account choice; the desk's check_setup →
        propose_account is the way, also after a check stopped for it."""
        setup = flat(section(DESK.read_text(), "## Setup", "## Test install"))
        self.assertIn("The check never asks which account is the business account", setup)
        self.assertIn("`check_setup()`, and when it asks which company account is the business "
                      "account, call `propose_account()`", setup)
        self.assertLess(setup.index("`check_setup()`"), setup.index("`propose_account()`"))

    def test_the_desk_names_the_recovery_of_an_expired_button(self):
        # 0.11.2: the intent "where a quarter stands" posts the status card, which also
        # recovers an expired button
        ans = flat(section(DESK.read_text(), "## Two intents about a quarter",
                           "## The operator's words"))
        self.assertIn('`show_view(view="open")`', ans)
        self.assertIn('The same card recovers a walk of cards that stopped, or a button that '
                      'answered "expired" (Casa #1305).', ans)


def _dt(day):
    import datetime as dt
    return dt.datetime.fromisoformat(day + "T12:00:00+00:00")


class NamedQuarter(StoreCase):
    """Ruling Q2b (review round 1): after an operator's "check Q2", a desk view or a bare
    "send the package" with no quarter is Q2's (runs.quarter of the newest run), not D11's
    latest in-scope quarter; a later run that named none gives D11 back."""

    def setUp(self):
        super().setUp()
        from tests.sim_job import JobDriver
        self.bind(watermark="2026-04-01")
        self.drv = JobDriver(self, payments=0)
        self.drv.add_payments(["2026-05-10"])
        self.drv.add_payments(["2026-08-10"])

    def check(self, job_id, quarter=None):
        import qa_server, tools  # noqa: F401
        args = {"kind": "check", "trigger": "operator"}
        if quarter:
            args["quarter"] = quarter
        qa_server.TOOLS["request_work"]["fn"](args)
        self.drv.run_job(job_id)

    def open_card(self):
        import qa_server, tools  # noqa: F401
        with FakeBroker() as broker:
            out = qa_server.TOOLS["show_view"]["fn"]({"view": "open"})
        return out, json.loads(broker.deposits[0]["value"])

    def test_whats_open_and_a_bare_send_the_package_follow_the_checked_quarter(self):
        import qa_server, tools  # noqa: F401
        with self.patch_clock(_dt("2026-10-06")):
            self.check("eeeeeeee-1", quarter="Q2")
            out, card = self.open_card()
            self.assertTrue(card["text"].startswith("Q2 · "), card["text"])
            get = card["buttons"][-1]
            self.assertEqual((get["label"], get["call"]["tool"],
                              get["call"]["arguments"]["quarter"]),
                             ("Get package", "get_package", "2026-Q2"))
            with FakeBroker():
                pkg = qa_server.TOOLS["get_package"]["fn"](get["call"]["arguments"])
            self.assertIn("-2026-Q2-", pkg["filename"])
            with FakeBroker():
                bare = qa_server.TOOLS["get_package"]["fn"]({})
            self.assertIn("-2026-Q2-", bare["filename"])
            with FakeBroker() as broker:
                st = qa_server.TOOLS["show_view"]["fn"]({"view": "status"})
            self.assertEqual(json.loads(self.conn.execute(
                "SELECT scope_json FROM renders WHERE render_id=?",
                (st["render_id"],)).fetchone()[0])["quarter"], "2026-Q2")
            # a later check that names no quarter gives D11's latest in-scope quarter back
            self.check("eeeeeeee-2")
            _, card = self.open_card()
            self.assertTrue(card["text"].startswith("Q3 · "), card["text"])
            with FakeBroker():
                bare = qa_server.TOOLS["get_package"]["fn"]({})
            self.assertIn("-2026-Q3-", bare["filename"])


class DeskHandover(StoreCase):
    """§2.5 at the desk: a file filed at the desk and handed over (request_work
    kind=handover) starts a continuation whose work list is the payments it could fit."""

    def test_a_desk_handover_lists_the_payments_the_document_could_fit(self):
        import qa_server, tools  # noqa: F401
        from tests.sim_job import JobDriver
        self.bind()
        drv = JobDriver(self, payments=2)            # Zapier 1000 on 5 Jul, 2000 on 5 Aug
        with self.patch_clock(_dt("2026-10-06")):
            drv.run_job("ffffffff-1")                # both searched, both missing
            listed = [r[0] for r in self.conn.execute("SELECT pid FROM run_work WHERE"
                                                      " job_id='ffffffff-1' ORDER BY pid")]
            self.assertEqual(len(listed), 2)
            # the operator left both missing: neither is open work for a later run
            self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
                c, listed, grant=grant))
            path = self.publish("handed.pdf", b"%PDF-1.4 handed\n", producer="telegram")
            doc = qa_server.TOOLS["ingest_document"]["fn"]({
                "source_path": path, "kind": "invoice", "source": "manual-telegram",
                "extraction_author": "desk", "issuer": "Zapier", "amount_minor": 2000,
                "currency": "EUR", "document_date": "2026-08-04", "document_number": "Z-2"})
            ask = qa_server.TOOLS["request_work"]["fn"]({"kind": "handover",
                                                         "trigger": "operator",
                                                         "doc_ids": [doc["doc_id"]]})
            self.assertEqual(ask["start_job"]["job"], "quarterly-accounting:work")
            drv.run_job("ffffffff-2")
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT pid, why, outcome FROM run_work WHERE job_id='ffffffff-2'")]
        # the continuation's work list is the one payment the document fits (the 2000 one)
        self.assertEqual(rows, [(listed[1], "handover", "propose")])     # #67: confirmed by a tap
        self.assertEqual(tuple(self.conn.execute(
            "SELECT kind, state FROM work_requests WHERE request_id=?",
            (ask["request_id"],)).fetchone()), ("handover", "reported"))
