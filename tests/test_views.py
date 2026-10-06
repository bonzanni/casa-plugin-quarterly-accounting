# tests/test_views.py
import json
import re
import unittest
from unittest import mock

from tests._base import StoreCase, untag
import db  # noqa: E402
import kb  # noqa: E402
import matches  # noqa: E402
import passes  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402


def flat(text):
    return text.replace("\n", " · ")          # the coverage line wraps at " · "


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p1', 'x', 0, 0, '2026-09-20')")
        self.n = 0

    def add(self, tags=("software",), observed="2026-09-20T10:00:00Z", searched=True, **row):
        self.n += 1
        r = dict(row_id=self.n, booking_date=row.pop("booking_date", "2026-09-14"),
                 value_date=row.pop("value_date", None), **row)
        r["value_date"] = r["value_date"] or r["booking_date"]
        self.row(**r)
        pid = self.lineage_for(self.n)
        self.classify(pid, set(tags))
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_at=? WHERE pid=?",
                              (observed, pid))
        self.settle(pid)
        if searched:                # the pass looked; a never-searched item is not `missing`
            work.record_search(self.conn, pid=pid, token=self.token, queries=["x"])
        return pid

    def render(self, view="status", quarter="2026-Q3", pid=None):
        return views.build_review(self.conn, view=view, quarter=quarter, pid=pid)


class TestCoverage(Base):
    def test_oldest_observation_in_scope_is_the_classification_date(self):
        self.add(observed="2026-09-13T10:00:00Z")
        self.add()
        text = flat(self.render()["text"])        # a first review prefixes the line
        self.assertIn("checked through 20 Sep · classification through 13 Sep", text)

    def test_membership_precedes_the_missing_filter(self):
        self.add(tags=("software",), observed="2026-09-22T10:00:00Z")
        self.add(tags=("internal-transfer",), observed="2026-09-13T10:00:00Z")
        text = self.render("missing")["text"]
        self.assertIn("classification through 13 Sep", text)

    def test_an_older_unprinted_lineage_is_a_member(self):
        q2 = self.add(tags=("internal-transfer",), observed="2026-09-13T10:00:00Z",
                      booking_date="2026-07-02")
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
            self.conn.execute("UPDATE bank_rows SET booking_date='2026-05-02',"
                              " value_date='2026-05-02' WHERE row_id=1")
        self.settle(q2)
        self.add()
        r = self.render()
        self.assertIn("classification through 13 Sep", r["text"])
        members = json.loads(self.conn.execute("SELECT membership_json FROM renders WHERE"
                                               " render_id=?", (r["render_id"],)).fetchone()[0])
        self.assertIn(q2, members)

    def test_a_pending_row_is_a_member_by_value_date(self):
        pid = self.add(booking_date=None, value_date="2026-09-15", status="PDNG")
        self.assertIn(pid, views.membership(self.conn, "status", "2026-Q3"))

    def test_never_checked_is_counted_and_does_not_move_the_date(self):
        self.add(observed="2026-09-13T10:00:00Z")
        pid = self.add()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_at=NULL WHERE pid=?", (pid,))
        flat = self.render()["text"].replace("\n", " · ")     # the line wraps at " · "
        self.assertIn("classification through 13 Sep · 1 never checked", flat)

    def test_empty_scope(self):
        text = self.render()["text"]
        self.assertIn("no transactions yet.", text.lower())
        self.assertNotIn("through", text)

    def test_an_ended_lineage_leaves_membership(self):
        old = self.add(observed="2026-09-01T10:00:00Z")
        self.add()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET ended='erased' WHERE pid=?", (old,))
        self.assertIn("classification through 20 Sep", self.render()["text"])

    def test_both_dates_or_neither_in_every_view(self):
        self.add()
        for v in ("status", "missing", "check", "rest", "older", "all", "quarter"):
            text = self.render(v)["text"]
            self.assertEqual("bank checked through" in text.lower(), "classification" in text, v)


class TestSheet(Base):
    def test_sections_missing_first_then_guessed(self):
        kb.upsert_counterparty(self.conn, "Adobe", source="portal",
                               document_link="https://adobe.example/invoices")
        self.add()
        g = self.add(counterparty="Zapier", amount_minor=9900, booking_date="2026-09-17")
        doc = self.doc(counterparty="Zapier", issuer="Zapier", document_number="8841",
                       document_date="2026-09-17", amount_minor=9900)
        matches.record_match(self.conn, pid=g, doc_id=doc, author="auto",
                             expected_revision=self.rev(g), row_snapshot=self.snapshot(g),
                             token=self.token, labels=("guessed",), runners_up=["8712 (10 Sep)"])
        text = self.render()["text"]
        self.assertLess(text.index("MISSING"), text.index("I GUESSED THESE"))
        self.assertIn("Adobe · EUR 100.00 · 14 Sep", text)
        self.assertIn("https://adobe.example/invoices", text)
        self.assertIn("Picked invoice 8841 (17 Sep); 8712 \\(10 Sep\\) also fits.", text)
        self.assertIn('"the Zapier one is wrong"', text)

    def test_a_long_list_caps_largest_first_and_counts_the_rest(self):
        for i in range(20):
            self.add(amount_minor=1000 * (i + 1), counterparty=f"Vendor{i:02d}")
        text = self.render()["text"]
        self.assertIn('+12 more — say "all of them"', text)
        self.assertIn("Vendor19", text)
        self.assertNotIn("Vendor00 ", text)
        self.assertLessEqual(views.utf16_len(text), views.BODY_LIMIT)
        all_text = self.render("all")["text"]
        self.assertIn("Vendor00", all_text)

    def test_phone_width_no_numbering_no_machinery(self):
        for i in range(12):
            self.add(amount_minor=100 + i, counterparty="A very long vendor name that goes on %d" % i)
        for v in views.VIEWS:
            if v == "item":
                continue
            text = self.render(v)["text"]
            for line in text.splitlines():
                if not line.startswith("http"):
                    self.assertLessEqual(len(line), views.WIDTH, (v, line))
                self.assertIsNone(re.match(r"^\s*\d+[.)]\s", line), (v, line))
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text, (v, word))
            self.assertIsNone(re.search(r"\ba [aeiou]", text), (v, text))

    def test_missing_not_searched_not_classified_render_differently(self):
        self.add(counterparty="Searched")
        self.add(counterparty="Unsearched", searched=False)
        self.add(tags=(), counterparty="Unclassified", searched=False)
        text = self.render()["text"]
        self.assertIn("MISSING\nSearched · ", text)
        self.assertNotIn("Unsearched", text)         # never looked for: not `missing`
        self.assertIn("3 transactions, 1 missing a document.", flat(text))
        self.assertIn("2 new payments not checked yet", text)   # an unclassified one is unsearched
        self.assertNotIn("not yet classified", text)

    def test_a_week_spanning_the_boundary_is_one_view(self):
        a = self.add(counterparty="SeptCo", booking_date="2026-09-29")
        b = self.add(counterparty="OctCo", booking_date="2026-10-02")
        for pid in (a, b):
            matches.propose_match(self.conn, pid=pid, doc_id=self.doc(), token=self.token,
                                  expected_revision=self.rev(pid), row_snapshot=self.snapshot(pid))
        text = self.render(quarter="2026-Q4")["text"]
        self.assertIn("SeptCo", text)
        self.assertIn("OctCo", text)
        self.assertIn("Q3 2026", text)

    def test_first_view_carries_the_first_review_and_start_lines_once(self):
        self.add()
        r = self.render()
        self.assertIn("First review", r["text"])
        self.assertIn('say "start from Q2" to go further back', r["text"])
        views.mark_rendering_delivered(self.conn, r["render_id"])
        again = self.render()["text"]
        self.assertNotIn("First review", again)
        self.assertNotIn("start from", again)

    def test_degraded_pass_leads_with_its_condition(self):
        self.add(counterparty="Searched")
        self.add(counterparty="New1", searched=False)
        self.add(counterparty="New2", searched=False)
        passes.record_probe(self.conn, self.token, "gmail", False, "auth failed")
        text = untag(self.render()["text"])
        self.assertTrue(text.startswith(
            "Review incomplete - Gmail unavailable.\n1 invoice already missing.\n"
            "2 new payments not searched.\nNo reply needed; I'll retry next pass.\n"), text)
        self.assertNotIn("New1", text)
        self.assertNotIn("not checked yet", text)    # could not look is not "not reached"
        self.assertIn("3 transactions, 1 missing a document.", flat(text))
        self.end_live_pass("interrupted", {"checked": 18, "total": 30})
        text = untag(self.render()["text"])
        self.assertIn("Review interrupted.\n18 of 30 new payments checked.\n12 not checked yet. Saved.",
                      text)

    def test_not_set_up(self):
        binding_less = self.conn
        with db.tx(binding_less):
            binding_less.execute("DELETE FROM binding")
        text = self.render()["text"]
        self.assertTrue(text.startswith("Not set up yet."))
        self.assertIn("Nothing else to do until then.", text)

    def test_ended_lineage_is_told_once_in_residue(self):
        pid = self.add(counterparty="Adobe", amount_minor=5999, booking_date="2026-07-03")
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM bank_rows WHERE row_id=1")
            import ledger
            ledger.end_lineage(self.conn, pid, "erased")
            import lineage
            lineage.settle(self.conn, pid)
        r = self.render()
        self.assertIn("Adobe · EUR 59.99", r["text"])
        self.assertIn("3 Jul left the bank ledger (erased)", r["text"])
        views.mark_rendering_delivered(self.conn, r["render_id"])
        self.assertNotIn("left the bank ledger", self.render()["text"])

    def test_an_ineligible_lineage_is_enumerated_but_never_asked_about(self):
        self.add()
        pid = self.add(tags=(), counterparty="OtherAcct")
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET account_id='acc-other' WHERE row_id=?",
                              (self.n,))
        self.settle(pid)
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "ineligible")
        self.assertIn(pid, views.membership(self.conn, "status", "2026-Q3"))
        text = self.render()["text"]
        self.assertNotIn("not yet classified", text)
        self.assertNotIn("OtherAcct", text)

    def test_the_wrong_one_example_names_a_printed_guess(self):
        for i in range(12):
            pid = self.add(counterparty=f"G{i:02d}", amount_minor=100 * (i + 1))
            matches.record_match(self.conn, pid=pid, doc_id=self.doc(amount_minor=100 * (i + 1)),
                                 author="auto", expected_revision=self.rev(pid),
                                 row_snapshot=self.snapshot(pid), token=self.token,
                                 labels=("guessed",), runners_up=["x (1 Sep)"])
        text = self.render()["text"]
        example = re.search(r'"the (\S+) one is wrong"', text).group(1)
        self.assertEqual(example, "G11")                 # largest first: the first one printed
        self.assertNotIn("G00", text)

    def test_first_review_survives_an_earlier_item_or_stop_rendering(self):
        pid = self.add()
        views.mark_rendering_delivered(self.conn, self.render("item", pid=pid)["render_id"])
        with db.tx(self.conn):
            saved = dict(self.conn.execute("SELECT * FROM binding").fetchone())
            self.conn.execute("DELETE FROM binding")
        stop = self.render()
        self.assertTrue(stop["text"].startswith("Not set up yet."))
        views.mark_rendering_delivered(self.conn, stop["render_id"])
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO binding(%s) VALUES (%s)" % (
                ",".join(saved), ",".join("?" * len(saved))), tuple(saved.values()))
        text = self.render()["text"]
        self.assertTrue(text.splitlines()[1].startswith("First review · bank checked through"),
                        text)

    def test_many_ended_lineages_stay_within_one_message_and_drain(self):
        import ledger
        import lineage
        pids = [self.add(counterparty=f"Gone{i:03d}", amount_minor=1000 + i) for i in range(150)]
        with db.tx(self.conn):
            for pid in pids:
                ledger.end_lineage(self.conn, pid, "erased")
                lineage.settle(self.conn, pid)
        r = self.render()
        self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
        self.assertIn('+142 more — say "all of them"', r["text"])
        self.assertEqual(r["next"]["view"], "all")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        left = self.conn.execute("SELECT COUNT(*) FROM residue WHERE shown_render IS NULL"
                                 ).fetchone()[0]
        self.assertEqual(left, 142)
        seen, page, after = set(), 1, None
        while True:
            r = views.build_review(self.conn, view="all", quarter="2026-Q3", page=page,
                                   after=after)
            self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
            self.assertNotIn("all of them", r["text"])
            seen |= set(re.findall(r"Gone\d{3}", r["text"]))
            views.mark_rendering_delivered(self.conn, r["render_id"])
            if r["next"] is None:
                break
            page, after = r["next"]["page"], r["next"]["after"]
        self.assertGreater(page, 1)
        self.assertEqual(len(seen), 142)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM residue WHERE shown_render"
                                           " IS NULL").fetchone()[0], 0)

    def test_the_whole_list_pages_and_never_offers_itself_again(self):
        for i in range(200):
            self.add(counterparty=f"Vend{i:03d}", amount_minor=1000 + i)
        capped = self.render("missing")
        self.assertIn('+192 more — say "all of them"', capped["text"])
        self.assertEqual(capped["next"], {"view": "missing", "quarter": "2026-Q3", "page": 1,
                                          "prev": capped["render_id"]})
        for view in ("all", "missing"):
            seen, page, after, n = set(), 1, None, 0
            while True:
                r = views.build_review(self.conn, view=view, quarter="2026-Q3", page=page,
                                       after=after)
                n += 1
                self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
                self.assertNotIn("all of them", r["text"])
                self.assertIn("classification through", flat(r["text"]))
                got = re.findall(r"Vend\d{3}", r["text"])
                self.assertFalse(seen & set(got))
                seen |= set(got)
                self.assertEqual(r["printed"], len(got))
                if r["next"] is None:
                    self.assertNotIn('say "more"', r["text"])
                    break
                self.assertIn('say "more"', r["text"])
                page, after = r["next"]["page"], r["next"]["after"]
            self.assertGreater(n, 1, view)
            self.assertEqual(len(seen), 200, view)
        self.assertIn("That is everything.",
                      views.build_review(self.conn, view="all", quarter="2026-Q3",
                                         page=99, after=[99, 0, 0])["text"])

    def test_an_oversized_item_never_breaks_the_limit(self):
        kb.upsert_counterparty(self.conn, "Adobe", source="portal",
                               document_link="https://adobe.example/")
        with db.tx(self.conn):   # as an earlier version stored it: upsert now refuses it
            self.conn.execute("UPDATE counterparties SET document_link=? WHERE name=?",
                              ("https://adobe.example/" + "x" * 5000, "Adobe"))
        pid = self.add()
        # fix wave D round 2: the unbounded link is clipped with its mark, so the
        # item prints whole and is bound (before, the whole text was cut and bound nothing)
        for r in (self.render(), self.render("item", pid=pid), self.render("all")):
            self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
            self.assertIn("Adobe · EUR 100.00", r["text"])
            self.assertIn("https://adobe.example/xxx", r["text"])
            self.assertIn(views.CLIP_MARK, r["text"])
            self.assertEqual(r["printed"], 1)

    def test_the_fit_is_the_net_under_the_field_clip(self):
        # fix wave D round 2/3: with the link field clip disabled, the final fit alone
        # keeps the text deliverable, and a cut text binds nothing (D3).
        kb.upsert_counterparty(self.conn, "Adobe", source="portal",
                               document_link="https://adobe.example/")
        with db.tx(self.conn):   # as an earlier version stored it: upsert now refuses it
            self.conn.execute("UPDATE counterparties SET document_link=? WHERE name=?",
                              ("https://adobe.example/" + "x" * 5000, "Adobe"))
        pid = self.add()
        with mock.patch.object(views, "LINK_MAX", 10 ** 6):
            for r in (self.render(), self.render("item", pid=pid), self.render("all")):
                self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
                self.assertEqual(r["printed"], 0)

    def test_unprintable_residue_is_marked_with_the_rendering(self):
        pid = self.add()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO residue(pid, reason, detail, created_at)"
                              " VALUES (?, 'merged', '#9', 'x')", (pid,))
            self.conn.execute("INSERT INTO residue(pid, reason, detail, created_at)"
                              " VALUES (NULL, 'ended', 'x', 'x')")
        views.mark_rendering_delivered(self.conn, self.render()["render_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM residue WHERE shown_render"
                                           " IS NULL").fetchone()[0], 0)

    def test_an_item_no_longer_chased_stays_missing_even_if_never_searched(self):
        self.add(counterparty="Found")
        self.add(counterparty="Dropped", searched=False)
        self.granted(work.stop_chasing_in_tx, "2026-Q3")
        text = self.render()["text"]
        self.assertIn("Dropped · EUR 100.00 · 14 Sep\nNo longer chased.", text)
        self.assertIn("2 transactions, 2 missing a document.", flat(text))
        self.assertNotIn("the next pass looks", text)
        self.assertNotIn("Not searched yet", text)

    def test_a_merge_carrying_accepted_missing_keeps_the_survivor_listed(self):
        import ledger
        import lineage
        survivor = self.add(counterparty="Kept", searched=False)
        loser = self.add(counterparty="Kept", searched=False)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='accepted-missing'"
                              " WHERE pid=?", (loser,))
            ledger.merge(self.conn, survivor, loser)
            self.conn.execute("UPDATE projections SET merged_into=? WHERE pid=?",
                              (survivor, loser))
            lineage.settle(self.conn, survivor)
        text = self.render()["text"]
        self.assertIn("MISSING\nKept · ", text)
        self.assertNotIn("the next pass looks", text)

    def test_an_interrupted_pass_says_not_checked_once(self):
        self.add(counterparty="Seen")
        self.add(counterparty="Unreached", searched=False)
        self.end_live_pass("interrupted", {"checked": 1, "total": 2})
        text = untag(self.render()["text"])
        self.assertIn("Review interrupted.\n1 of 2 new payments checked.\n1 not checked yet. Saved.",
                      text)
        self.assertEqual(text.count("not checked yet"), 1)

    def test_a_complete_run_clears_the_interrupted_block(self):
        self.add(counterparty="Seen")
        self.add(counterparty="Unreached", searched=False)
        self.end_live_pass("interrupted", {"checked": 1, "total": 2})
        self.assertIn("Review interrupted.\n1 of 2 new payments checked.",
                      untag(self.render()["text"]))
        self.pass_(trigger="cron")
        self.end_live_pass()
        self.assertNotIn("Review interrupted.", self.render()["text"])

    def _older(self, counterparty, searched):
        pid = self.add(counterparty=counterparty, booking_date="2026-05-04", searched=searched)
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        self.settle(pid)
        return pid

    def test_an_interrupted_catch_up_names_the_older_quarter_it_left(self):
        # issue #4: the start quarter was Q2, the pass was interrupted before
        # searching it; the report named only Q3, "0 missing", and never Q2
        for i in range(3):
            self._older(f"OldNew{i}", searched=False)
        self.add(counterparty="NowSeen", tags=("internal-transfer",))
        self.end_live_pass("interrupted", {"checked": 1, "total": 4})
        text = self.render()["text"]
        self.assertIn('+3 older not searched yet (Q2) — say "show older"', flat(text))
        older = self.render("older")["text"]
        for i in range(3):
            self.assertIn(f"OldNew{i} · ", older)
        self.assertEqual(older.count("Not searched yet."), 3)

    def test_older_missing_and_older_unsearched_are_kept_apart(self):
        self._older("OldSeen", searched=True)
        self._older("OldNew", searched=False)
        self.add(counterparty="NowNew", searched=False)
        text = flat(self.render()["text"])
        self.assertIn('+1 older still missing (Q2) — say "show older" · '
                      '+1 older not searched yet (Q2) — say "show older"', text)
        # the current quarter's line counts the current quarter only: nothing twice
        self.assertIn("1 new payment not checked yet — the next pass looks.", text)
        older = self.render("older")["text"]
        self.assertIn("OldSeen · ", older)
        self.assertIn("OldNew · ", older)
        self.assertNotIn("NowNew", older)

    def test_degraded_counts_agree_with_coverage_and_skip_the_item_view(self):
        old = self.add(counterparty="OldCo", booking_date="2026-05-04")
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        self.settle(old)
        pid = self.add(counterparty="NowCo")
        passes.record_probe(self.conn, self.token, "gmail", False, "auth failed")
        text = self.render()["text"]
        self.assertIn("1 invoice already missing.", text)
        self.assertIn("1 transaction, 1 missing a document.", flat(text))
        self.assertIn("+1 older still missing (Q2)", text)
        item = self.render("item", pid=pid)["text"]
        self.assertTrue(item.startswith("NowCo · "), item)

    def test_a_cut_page_keeps_its_continuation_phrase(self):
        kb.upsert_counterparty(self.conn, "Big", source="portal",
                               document_link="https://big.example/")
        with db.tx(self.conn):   # as an earlier version stored it: upsert now refuses it
            self.conn.execute("UPDATE counterparties SET document_link=? WHERE name=?",
                              ("https://big.example/" + "x" * 5000, "Big"))
        self.add(counterparty="Big", amount_minor=100, booking_date="2026-07-01")
        for i in range(10):
            self.add(counterparty=f"Small{i}", amount_minor=200 + i)
        for i in range(10, 200):
            self.add(counterparty=f"Small{i}", amount_minor=200 + i)
        r = views.build_review(self.conn, view="missing", quarter="2026-Q3", page=1)
        self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
        self.assertIsNotNone(r["next"])
        self.assertTrue(r["text"].endswith('say "more".'), r["text"][-80:])
        r2 = views.build_review(self.conn, view="missing", quarter="2026-Q3", **{
            k: r["next"][k] for k in ("page", "after")})
        self.assertIn("Small199", r2["text"])
        self.assertIsNone(r2["next"])


class TestRenderLog(Base):
    def test_rendering_is_not_showing(self):
        pid = self.add()
        r = self.render()
        self.assertIsNone(self.conn.execute("SELECT * FROM shown WHERE pid=?", (pid,)).fetchone())
        views.mark_rendering_delivered(self.conn, r["render_id"])
        s = self.conn.execute("SELECT * FROM shown WHERE pid=?", (pid,)).fetchone()
        self.assertEqual((s["render_id"], s["projection_revision"]), (r["render_id"], self.rev(pid)))

    def test_a_failed_send_leaves_the_pointer_where_it_was(self):
        pid = self.add()
        first = self.render()
        views.mark_rendering_delivered(self.conn, first["render_id"])
        self.render()                                   # built, never delivered
        s = self.conn.execute("SELECT render_id FROM shown WHERE pid=?", (pid,)).fetchone()
        self.assertEqual(s[0], first["render_id"])

    def test_same_store_same_bytes(self):
        self.add()
        # binding V2: two renderings of one unchanged store differ only by their tag
        a, b = self.render(), self.render()
        self.assertNotEqual(a["text"], b["text"])
        self.assertEqual(untag(a["text"]), untag(b["text"]))

    def test_composition_holds_the_write_lock(self):
        import sqlite3
        self.add()
        other = sqlite3.connect(str(self.data / db.DB_NAME), timeout=0.1, isolation_level=None)
        self.addCleanup(other.close)
        real = views._compose
        seen = []

        def composing(*a, **kw):
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                seen.append("wrote")
            except sqlite3.OperationalError:
                seen.append("locked")
            return real(*a, **kw)
        from unittest import mock
        with mock.patch.object(views, "_compose", composing):
            self.render()
        self.assertEqual(set(seen), {"locked"})

    def test_a_view_binds_only_the_pairings_it_displays(self):
        pid = self.add()
        for _ in range(2):                                   # a joint machine set
            self.machine_entry(pid, self.doc())
        r = self.render("missing")         # D3: the joint set is a proposal, not "missing"
        views.mark_rendering_delivered(self.conn, r["render_id"])
        self.assertIsNone(self.conn.execute("SELECT match_revisions_json FROM shown WHERE pid=?",
                                            (pid,)).fetchone())
        r = self.render("check")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        shown = self.conn.execute("SELECT match_revisions_json FROM shown WHERE pid=?",
                                  (pid,)).fetchone()[0]
        self.assertEqual(len(json.loads(shown)), 2)

    def test_a_merged_lineage_renders_as_its_survivor(self):
        import ledger
        survivor = self.add(counterparty="Survivor")
        loser = self.add(counterparty="Loser")
        with db.tx(self.conn):
            ledger.merge(self.conn, survivor, loser)
            self.conn.execute("UPDATE projections SET merged_into=? WHERE pid=?",
                              (survivor, loser))
            import lineage
            lineage.settle(self.conn, survivor)
        self.assertEqual(views.membership(self.conn, "status", "2026-Q3"), [survivor])
        r = self.render("item", pid=loser)
        self.assertEqual(views.render_items(self.conn, r["render_id"]), [survivor])
        self.assertNotIn(loser, views.render_items(self.conn, self.render()["render_id"]))


if __name__ == "__main__":
    unittest.main()
