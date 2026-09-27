# tests/test_views.py
import json
import re
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import kb  # noqa: E402
import matches  # noqa: E402
import passes  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p1', 'x', 0, 0, '2026-09-20')")
        self.n = 0

    def add(self, tags=("software",), observed="2026-09-20T10:00:00Z", **row):
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
        return pid

    def render(self, view="status", quarter="2026-Q3", pid=None):
        return views.build_review(self.conn, view=view, quarter=quarter, pid=pid)


class TestCoverage(Base):
    def test_oldest_observation_in_scope_is_the_classification_date(self):
        self.add(observed="2026-09-13T10:00:00Z")
        self.add()
        text = self.render()["text"]
        self.assertIn("Bank checked through 20 Sep · classification through 13 Sep", text)

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
        self.assertIn("No transactions yet.", text)
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
            self.assertEqual("Bank checked through" in text, "classification" in text, v)


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
        self.assertIn("Picked invoice 8841 (17 Sep); 8712 (10 Sep) also fits.", text)
        self.assertIn('"the Zapier one is wrong"', text)

    def test_a_long_list_caps_largest_first_and_counts_the_rest(self):
        for i in range(20):
            self.add(amount_minor=1000 * (i + 1), counterparty=f"Vendor{i:02d}")
        text = self.render()["text"]
        self.assertIn('+12 more — say "all of them"', text)
        self.assertIn("Vendor19", text)
        self.assertNotIn("Vendor00 ", text)
        self.assertLessEqual(views.utf16_len(text), views.TELEGRAM_LIMIT)
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

    def test_missing_not_searched_not_classified_render_differently(self):
        searched = self.add(counterparty="Searched")
        self.add(counterparty="Unsearched")
        self.add(tags=(), counterparty="Unclassified")
        work.record_search(self.conn, pid=searched, token=self.token, queries=["x"])
        text = self.render()["text"]
        block_uns = text[text.index("Unsearched"):]
        self.assertTrue(block_uns.splitlines()[1].startswith("Not searched yet"))
        self.assertNotIn("Not searched yet", text[text.index("Searched · "):text.index("Unsearched")])
        self.assertIn("1 not yet classified", text)

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
        self.add()
        passes.record_probe(self.conn, self.token, "gmail", False, "auth failed")
        self.assertTrue(self.render()["text"].startswith("Review incomplete - Gmail unavailable."))
        passes.end_pass(self.conn, self.token, "interrupted", {"checked": 18, "total": 30})
        text = self.render()["text"]
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
        self.assertEqual(self.render()["text"], self.render()["text"])

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
        for _ in range(2):                                   # two candidates collide
            matches.record_match(self.conn, pid=pid, doc_id=self.doc(), author="auto",
                                 expected_revision=self.rev(pid), token=self.token,
                                 row_snapshot=self.snapshot(pid))
        r = self.render("missing")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        shown = self.conn.execute("SELECT match_revisions_json FROM shown WHERE pid=?",
                                  (pid,)).fetchone()[0]
        self.assertEqual(json.loads(shown), {})
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
