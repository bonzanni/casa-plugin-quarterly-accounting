"""0.11.2, the approved conversation script (docs/superpowers/specs/2026-10-08-*): #53 the
render tag is the composition time in seconds; #55 two intents (where a quarter stands: a
status card and nothing runs; get a quarter done: run it, moving the books' start when it
lies before it); #52 one purchase is one line and one button on the Review card too; no
zero counts; short labels with counts in parentheses and one legend line per card."""
import datetime as dt
import json
import os
from tests._base import LoopCase, StoreCase
import db

AT = dt.datetime(2026, 10, 8, 19, 4, 37, tzinfo=dt.timezone.utc)


class _Cards(LoopCase):
    def setUp(self):
        super().setUp()
        self.patch(os, "environ", {**os.environ, "CASA_TZ": "Europe/Amsterdam"})

    def c(self, fn, *a, **k):
        with db.tx(self.conn):
            return fn(self.conn, *a, **k)

    def row_of(self, rid):
        return self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()

    def text(self, rid):
        return self.row_of(rid)["text"]

    def labels(self, rid):
        import cards
        return [b[0] for b in cards.buttons(self.conn, self.row_of(rid))]


class TimeTag(_Cards):
    def test_the_tag_is_the_composition_time_in_seconds_in_casa_tz(self):
        import cards
        self.pay()
        with self.patch_clock(AT):
            rid = self.c(cards.compose_open, "2026-Q3")
        self.assertTrue(self.text(rid).split("\n")[0].endswith(" · 8 Oct 21:04:37"), self.text(rid))

    def test_tz_falls_back_to_tz_then_utc(self):
        import views
        self.patch(os, "environ", {k: v for k, v in os.environ.items()
                                   if k not in ("CASA_TZ", "TZ")})
        with self.patch_clock(AT):
            self.assertEqual(views.tag_now(), " · 8 Oct 19:04:37")
        self.patch(os, "environ", {"TZ": "Europe/Amsterdam"})
        with self.patch_clock(AT):
            self.assertEqual(views.tag_now(), " · 8 Oct 21:04:37")
        self.patch(os, "environ", {"CASA_TZ": "Not/AZone"})
        with self.patch_clock(AT):
            self.assertEqual(views.tag_now(), " · 8 Oct 19:04:37")

class SameSecond(StoreCase):
    """#53's accepted residual: a same-second twin with other facts refuses; a later one binds."""

    def test_a_same_second_twin_with_other_facts_is_refused_and_a_later_one_binds(self):
        import views
        self.seed_payments([{"counterparty": "Vendor%02d" % i, "amount_minor": 1000 + i}
                            for i in range(12)])
        views.mark_rendering_delivered(self.conn, views.build_review(
            self.conn, "status", quarter="2026-Q3")["render_id"])   # the announcement, once
        with self.patch_clock(AT):
            a = views.build_review(self.conn, "status", quarter="2026-Q3")
            b = views.build_review(self.conn, "status", quarter="2026-Q3")
        for r in (a, b):
            views.mark_rendering_delivered(self.conn, r["render_id"])
        self.assertEqual(a["text"], b["text"])
        with self.assertRaises(views.QuoteRefusal):
            views.bound_rendering(self.conn, b["text"])
        with self.patch_clock(AT + dt.timedelta(days=1)):     # d1 Terra S2: a day later
            d = views.build_review(self.conn, "status", quarter="2026-Q3")
        views.mark_rendering_delivered(self.conn, d["render_id"])
        self.assertEqual(views.bound_rendering(self.conn, d["text"])["render_id"],
                         d["render_id"])
        with self.patch_clock(AT + dt.timedelta(seconds=1)):
            c = views.build_review(self.conn, "status", quarter="2026-Q3")
        views.mark_rendering_delivered(self.conn, c["render_id"])
        self.assertEqual(views.bound_rendering(self.conn, c["text"])["render_id"],
                         c["render_id"])


class StatusCard(_Cards):
    def test_a_status_card_lists_only_its_quarter_and_drops_zero_counts(self):
        import cards
        q3 = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(q3, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Notion", 900, "2026-10-02")
        self.propose(q4, issuer="Notion", document_number="NO-1", amount_minor=900,
                     document_date="2026-10-01")          # #72: inside its window
        with self.patch_clock(AT):
            rid = self.c(cards.compose_open, "2026-Q3")
        lines = self.text(rid).split("\n")
        self.assertEqual(lines[0], "Q3 · 2 payments · 8 Oct 21:04:37")
        self.assertEqual(lines[1], "1 to confirm · 1 missing")
        self.assertIn("1. Zapier · 1 Sep · EUR 19.58 ↔ invoice ZAP\\-114 · EUR 19.58", lines)
        self.assertNotIn("Notion", self.text(rid).split("Q4 so far")[0])
        self.assertIn("Q4 so far: 1 to confirm", lines)
        self.assertEqual(self.labels(rid), ["Review", "Confirm all", "Invoice links",
                                            "Get package"])                  # #57
        self.assertEqual(lines[-1], "Review: go through the 1 to confirm and the missing invoices, "
                                    "one at a time · Confirm all: "
                                    "accept the suggested documents listed above · Invoice links: "
                                    "where to download each missing invoice · Get package: the Q3 "
                                    "zip for your accountant")
        scope = json.loads(self.row_of(rid)["scope_json"])
        self.assertEqual([o.get("p") for o in scope["order"] if "p" in o], [q3])

    def test_a_quarter_with_no_payment_offers_no_package(self):
        import cards
        self.pay("Notion", 900, "2026-10-02")
        rid = self.c(cards.compose_open, "2026-Q3")
        self.assertNotIn("Get package", self.labels(rid))

    def test_a_quarter_before_the_books_is_one_line_and_nothing_is_posted(self):
        import posting
        from tests.fakebroker import FakeBroker
        self.pay()
        before = self.conn.execute("SELECT count(*) FROM renders").fetchone()[0]
        with FakeBroker() as b:
            out = posting.show_view(self.conn, view="open", quarter="2026-Q2")
        self.assertEqual(out["say"], "Q2 2026 isn't in the books yet: they start 1 Jul 2026. "
                                     "Ask me to do Q2 and I'll start the books from 1 Apr.")
        self.assertIsNone(out["view"])          # Casa's no-deposit statement (#1015)
        self.assertEqual(b.deposits, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders").fetchone()[0], before)


class GetItDone(StoreCase):
    """No run is live: the ask moves the start (d1: a live run refuses, below)."""

    def setUp(self):
        super().setUp()
        self.bind()

    def wm(self):
        return self.conn.execute("SELECT watermark FROM binding").fetchone()[0]

    def test_a_quarter_before_the_books_moves_the_start_and_checks_it(self):
        import asks
        out = asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
        self.assertEqual(self.wm(), "2026-04-01")
        self.assertEqual(out["line"], "Starting the books from 1 Apr 2026 and checking Q2 — "
                                      "I'll post the result here.")
        self.assertEqual(self.conn.execute("SELECT quarter FROM work_requests WHERE "
                                           "request_id=?", (out["request_id"],)).fetchone()[0],
                         "2026-Q2")

    def test_inside_the_books_nothing_moves(self):
        import asks
        out = asks.request_work(self.conn, "check", "operator", quarter="2026-Q3")
        self.assertEqual(self.wm(), "2026-07-01")
        self.assertEqual(out["line"], asks.LINES["check"])

    def test_a_cron_ask_never_moves_the_start(self):
        import asks
        asks.request_work(self.conn, "check", "cron", quarter="2026-Q2")
        self.assertEqual(self.wm(), "2026-07-01")

    def test_the_start_from_reading_is_gone(self):
        from tests._base import apply_now
        res = apply_now(self.conn, "start from Q2")
        self.assertIsNone(res["proposal"])
        self.assertEqual(self.wm(), "2026-07-01")


class OnePurchaseOnTheReviewCard(_Cards):
    def test_an_invoice_and_its_receipt_are_one_line_and_one_choice(self):
        import cards
        p = self.pay("Elevenlabs.io", 1958, "2026-09-01")
        rec = self.doc(kind="receipt", issuer="Elevenlabs", document_number="CUWVSRB8-0007",
                       amount_minor=2200, currency="USD", document_date="2026-09-01")
        inv = self.propose(p, alternatives=[rec], issuer="Elevenlabs",
                           document_number="CUWVSRB8-0007", document_date="2026-09-01",
                           amount_minor=2200, currency="USD")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        rid = self.c(cards.card, end, 0)
        text = self.text(rid)
        self.assertEqual(text.count("CUWVSRB8"), 2, text)     # the candidate line + evidence
        self.assertNotIn("receipt", text.split("\n")[2])
        self.assertEqual(self.labels(rid), ["Confirm", "See PDF", "Wrong", "Leave for now"])  # #56
        scope = json.loads(self.row_of(rid)["scope_json"])
        self.assertEqual(scope["alternatives"], [])
        self.assertTrue(text.split("\n")[-1].startswith("Confirm: "), text)
        self.assertNotEqual(inv, rec)


class PlainWords(_Cards):
    def test_wrong_reads_as_removing_the_match(self):
        import cards, views
        from tests._base import apply_now
        p = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(p, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        views.mark_rendering_delivered(self.conn, end)
        res = apply_now(self.conn, "the Zapier one is wrong",
                        "\U0001f4ca Alex\n" + views.unesc(self.text(end)))
        self.assertIn("Reject the suggested document for Zapier · EUR 19.58 · 1 Sep.", res["proposal"])
        self.assertEqual(res["receipt"],
                         "Rejected the suggested document for Zapier · EUR 19.58 · 1 Sep.")


class GetItDoneWhileARunIsLive(_Cards):
    """d1 (Astra S2): a running check read the bank from the old start; nothing moves and
    nothing is asked, and the operator is told to ask again."""

    def test_nothing_moves_and_nothing_is_queued(self):
        import asks
        n = self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0]
        out = asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
        self.assertIsNone(out["start_job"])
        self.assertEqual(out["line"], "A check is running right now. When it has finished, "
                                      "ask me again to do Q2.")
        self.assertEqual(self.conn.execute("SELECT watermark FROM binding").fetchone()[0],
                         "2026-07-01")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0],
                         n)


class EndCardIsTheQuartersCard(_Cards):
    """d1 (Astra S2): the run's end card lists only its quarter's items."""

    def test_a_q3_run_lists_q3_and_summarises_q4(self):
        import cards
        q3 = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(q3, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        q4 = self.pay("Notion", 900, "2026-10-02")
        self.propose(q4, issuer="Notion", document_number="NO-1", amount_minor=900,
                     document_date="2026-10-01")          # #72: inside its window
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?", (self.job_id,))
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        lines = self.text(rid).split("\n")
        self.assertTrue(lines[0].startswith("Q3 checked · 1 payment · "), lines)
        self.assertNotIn("Notion", self.text(rid).split("Q4 so far")[0])
        self.assertIn("Q4 so far: 1 to confirm", lines)
        self.assertEqual(self.labels(rid), ["Review", "Confirm all", "Get package"])


class CompletionText(_Cards):
    """d1 (Astra S1): the completion says the card is posted only when it was delivered."""

    def end(self):
        import cards, job
        self.pay("Twilio", 2000, "2026-08-14")
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET end_render_id=? WHERE job_id=?",
                              (rid, self.job_id))
        return rid

    def test_an_undelivered_card_keeps_the_checked_line(self):
        import job
        self.end()
        self.assertNotEqual(job.run_end(self.conn, self.job_id)[0], job.CARD_POSTED)

    def test_a_delivered_card_is_not_repeated(self):
        import job, views
        views.mark_rendering_delivered(self.conn, self.end())
        self.assertEqual(job.run_end(self.conn, self.job_id)[0], job.CARD_POSTED)


class AnOperatorsAskGetsTheOperatorsCard(_Cards):
    """A run someone else started that took an operator's check naming a quarter answers the
    operator: their quarter's card, never the scheduled "· new:" form (pin review, 0.11.2)."""

    def test_an_agent_started_run_with_an_operator_ask(self):
        import asks, loop
        self.pay("Twilio", 2000, "2026-08-14")
        self.pay("Notion", 900, "2026-10-02")
        self.end_live_pass()
        asks.request_work(self.conn, "check", "operator", quarter="2026-Q3")
        self.run_claim(started_by="agent")
        run = self.conn.execute("SELECT * FROM runs WHERE job_id=?", (self.job_id,)).fetchone()
        with db.tx(self.conn):
            rid = loop.run_message(self.conn, self.job_id, run)
        text = self.text(rid)
        self.assertTrue(text.startswith("Q3 checked · 1 payment"), text)
        self.assertIn("Q4 so far: 1 missing", text)

    def test_a_plain_scheduled_run_keeps_its_new_form(self):
        import loop
        self.pay("Twilio", 2000, "2026-08-14")
        self.end_live_pass()
        self.run_claim(started_by="scheduled")
        run = self.conn.execute("SELECT * FROM runs WHERE job_id=?", (self.job_id,)).fetchone()
        with db.tx(self.conn):
            rid = loop.run_message(self.conn, self.job_id, run)
        self.assertIn("· new: 1 missing", self.text(rid))


class WalkLegend(_Cards):
    def test_review_says_what_the_walk_covers_and_buttons_carry_no_count(self):
        import cards
        for who in ("A", "B"):
            self.pay(who, 2000, "2026-08-14")
        p = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(p, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        rid = self.c(cards.compose_open, "2026-Q3")
        self.assertEqual(self.labels(rid), ["Review", "Confirm all", "Invoice links",
                                            "Get package"])                  # #57
        self.assertTrue(self.text(rid).split("\n")[-1].startswith(
            "Review: go through the 1 to confirm and the missing invoices, one at a time · "))

    def test_a_proposal_reads_as_suggested_not_matched(self):
        import cards
        p = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(p, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        rid = self.c(cards.card, end, 0)
        self.assertIn("Suggested: invoice ZAP\\-114", self.text(rid))
        self.assertNotIn("Matched to", self.text(rid))


class LegendMatchesTheWalk(_Cards):
    """§B (operator ruling): a quarter's vendor card lists only that quarter's payments, so
    the legend counts the quarter's own missing payments the walk lists."""

    def test_two_quarters_one_vendor(self):
        import cards
        self.pay("Adobe", 2000, "2026-08-14")
        self.pay("Adobe", 2100, "2026-10-14")
        rid = self.c(cards.compose_open, "2026-Q3")
        legend = self.text(rid).split("\n")[-1]
        self.assertTrue(legend.startswith("Review: go through the missing invoices, one at a time"),
                        legend)


class EmptyQuarterEndCard(_Cards):
    """r2 (Astra S2): a quarter with no payment offers no package, on every end-card branch."""

    def test_nothing_to_check_offers_no_package(self):
        import cards
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertTrue(self.text(rid).startswith("Nothing to check for Q"), self.text(rid))
        self.assertNotIn("Get package", self.labels(rid))
        self.assertNotIn("zip", self.text(rid))


class QuestionsAreTheQuartersOnEveryBranch(_Cards):
    """r3 (Astra S2): a Q3 end card with nothing open never offers a Q4 payment's question."""

    def test_a_q4_replace_question_stays_off_the_q3_card(self):
        import cards, replace
        p = self.pay("Notion", 900, "2026-10-02")
        old = self.doc(issuer="Notion", document_number="NO-1", amount_minor=900)
        self.machine_entry(p, old)
        new = self.doc(issuer="Notion", document_number="NO-2", amount_minor=900)
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?",
                              (self.job_id,))
        with db.tx(self.conn):        # the job's replace answer to a handover (ask_in_tx)
            mid = replace.current(self.conn, p)[0]
            self.conn.execute("INSERT INTO replace_questions(job_id, pid, match_id, new_doc_id,"
                              " state, created_seq) VALUES (?,?,?,?, 'open', ?)",
                              (self.job_id, p, mid, new, db.next_seq(self.conn)))
        self.assertEqual(len(self.c(lambda conn: replace.open_ones(conn))), 1)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        scope = json.loads(self.row_of(rid)["scope_json"])
        self.assertEqual([o for o in scope["order"] if "q" in o], [])


class OtherQuartersStayUnseen(_Cards):
    """r4 (Astra S1): a Q3 card's "Q4 so far" count never marks Q4's proposal seen, so the
    §1 new-state rule still owes it its card."""

    def test_q4s_proposal_is_not_reported_by_the_q3_card(self):
        import cards, views
        q3 = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(q3, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        q4 = self.pay("Notion", 900, "2026-10-02")
        self.propose(q4, issuer="Notion", document_number="NO-1", amount_minor=900,
                     document_date="2026-10-01")          # #72: inside its window
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?",
                              (self.job_id,))
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        views.mark_rendering_delivered(self.conn, rid)
        states = {r[0] for r in self.conn.execute(
            "SELECT pid FROM render_states WHERE render_id=?", (rid,))}
        self.assertIn(q3, states)
        self.assertNotIn(q4, states)
        d = self.c(lambda conn: __import__("work").describe(conn, q4))
        self.assertFalse(self.c(lambda conn: cards.seen_state(conn, q4, cards.item_state(d))))
