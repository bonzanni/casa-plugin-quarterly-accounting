"""#66 (operator ruling 2026-10-09): a list longer than one message is posted in full, as
plain pages before one separate action card (Casa v0.344.67 `pages`); no More. The card's
"All good" covers every page's items, at the revisions each page recorded. The next-step
buttons say what they show, with their counts, only when non-empty. The card carries Close
(Casa v0.344.64)."""
import json

from tests._base import StoreCase
from tests.fakebroker import FakeBroker, arguments_ok
import views


class _Q3(StoreCase):
    def setUp(self):
        super().setUp()
        import datetime as dt
        cm = self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def post(self, **kw):
        import posting
        with FakeBroker() as b:
            out = posting.show_view(self.conn, **kw)
        self.assertEqual(len(b.deposits), 1)
        return out, b.proposal()

    def plain(self, **kw):
        """#93: a view with nothing to act on: no deposit; `post` names its renderings
        (its pages first), in groups post_results takes."""
        import posting
        with FakeBroker() as b:
            out = posting.show_view(self.conn, **kw)
        self.assertEqual((out["view"], b.deposits), (None, []))
        return out

    def guesses(self, n=40):
        import db, matches
        self.bind()
        token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        pids = []
        for i in range(n):
            who, day = f"Supplier {i:02d} Consulting Services", "2026-08-%02d" % (1 + i % 28)
            self.row(i + 1, counterparty=who, amount_minor=1000 + 37 * i, booking_date=day,
                     value_date=day)
            pid = self.lineage_for(i + 1)
            self.classify(pid, {"software"})
            self.settle(pid)
            doc = self.doc(counterparty=who, issuer=who, amount_minor=1000 + 37 * i,
                           document_date=day)
            matches.record_match(self.conn, pid=pid, doc_id=doc, author="auto",
                                 expected_revision=self.rev(pid),
                                 row_snapshot=self.snapshot(pid), token=token,
                                 labels=("guessed",))
            pids.append(pid)
        return pids

    def labels(self, prop):
        return [b["label"] for b in prop["buttons"]]


class LongList(_Q3):
    def test_a_long_check_list_is_pages_then_a_separate_card(self):
        import views
        pids = self.guesses()
        out, prop = self.post(view="check", quarter="2026-Q3")
        pages = prop.get("pages")
        self.assertIsInstance(pages, list)
        self.assertTrue(2 <= len(pages) <= 6, len(pages))
        for p in pages:
            self.assertTrue(views.fits_proposal(p))
            self.assertNotIn('say "more"', p)
        # every guessed payment is on some page; none on the card's text
        card = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                            (out["render_id"],)).fetchone()[0])
        listed = set()
        for rid in card["list_pages"]:
            listed |= set(views.render_items(self.conn, rid))
        self.assertEqual(listed, set(pids))
        self.assertEqual(set(views.render_items(self.conn, out["render_id"])), set(pids))
        self.assertNotIn("More", self.labels(prop))
        self.assertEqual(self.labels(prop)[:2], ["All good", "One by one"])
        self.assertEqual(self.labels(prop)[-1], "Close")
        self.assertEqual(prop["buttons"][-1], {"label": "Close", "close": True})
        self.assertIn("40", prop["text"])
        self.assertIsNone(out["next"])

    def test_all_good_on_the_card_confirms_every_page(self):
        import qa_server, tools  # noqa: F401
        import views, work
        pids = self.guesses()
        out, prop = self.post(view="check", quarter="2026-Q3")
        ag = prop["buttons"][0]["call"]
        res = qa_server.TOOLS["verdict"]["fn"](dict(ag["arguments"]))
        self.assertEqual(len(res["applied"]), len(pids), res.get("receipt"))
        self.assertFalse(any(views._needs_check(work.describe(self.conn, p)) for p in pids))

    def test_a_change_on_any_page_makes_all_good_apply_nothing(self):
        import qa_server, tools  # noqa: F401
        import db, views, work
        pids = self.guesses()
        out, prop = self.post(view="check", quarter="2026-Q3")
        with db.tx(self.conn):            # the last payment changes after it was shown
            self.conn.execute("UPDATE projections SET revision=revision+1 WHERE pid=?",
                              (pids[-1],))
        res = qa_server.TOOLS["verdict"]["fn"](dict(prop["buttons"][0]["call"]["arguments"]))
        self.assertNotIn("applied", res)
        self.assertIn("Nothing was applied", res["receipt"])
        self.assertTrue(all(views._needs_check(work.describe(self.conn, p)) for p in pids))

    def test_delivering_the_card_marks_its_pages_delivered_first(self):
        import db, views
        self.guesses()
        out, _ = self.post(view="check", quarter="2026-Q3")
        views.mark_rendering_delivered(self.conn, out["render_id"])
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (out["render_id"],)).fetchone()[0])
        for rid in scope["list_pages"]:
            r = self.conn.execute("SELECT delivered_at FROM renders WHERE render_id=?",
                                  (rid,)).fetchone()
            self.assertIsNotNone(r[0])
        self.assertEqual(db.last_delivered(self.conn)["render_id"], out["render_id"])

    def test_a_page_quote_binds_its_own_page(self):
        import views
        self.guesses()
        out, prop = self.post(view="check", quarter="2026-Q3")
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (out["render_id"],)).fetchone()[0])
        for rid, text in zip(scope["list_pages"], prop["pages"]):
            self.assertEqual(views.bound_rendering(self.conn, "📊 Finance\n" + views.displayed(text))
                             ["render_id"], rid)
        self.assertEqual(views.bound_rendering(self.conn, "📊 Finance\n" + views.displayed(prop["text"]))
                         ["render_id"], out["render_id"])

    def test_more_quoting_a_posted_page_posts_nothing_again(self):
        """Every page went out together: a page leads nowhere a typed "more" should go."""
        import posting
        self.guesses()
        out, prop = self.post(view="check", quarter="2026-Q3")
        with FakeBroker() as b:
            r = posting.propose_reading(self.conn, "more", "📊 Finance\n" + views.displayed(prop["pages"][0]))
        self.assertEqual(r.get("instructions") or [], [])
        self.assertEqual(b.deposits, [])

    def test_a_stored_card_reposts_with_its_pages(self):
        self.guesses()
        out, prop = self.post(view="check", quarter="2026-Q3")
        again, prop2 = self.post(render_id=out["render_id"])
        self.assertEqual(prop2["pages"], prop["pages"])
        self.assertEqual(prop2["text"], prop["text"])

    def test_a_long_missing_list_offers_the_check_list_with_its_count(self):
        self.seed_payments([{"counterparty": f"Vendor {i:02d} Holdings International",
                             "amount_minor": 1000 + i} for i in range(120)])
        out = self.plain(view="missing", quarter="2026-Q3")    # nothing to confirm
        ids = [r for g in out["post"] for r in g]
        self.assertGreaterEqual(len(ids), 3)                # its pages, then the list's end
        self.assertEqual(ids[-1], out["render_id"])
        import posting
        with FakeBroker() as b:
            for g in out["post"]:
                posting.post_results(self.conn, g)
        self.assertEqual(len(b.deposits), len(out["post"]))
        self.assertIn("120", b.deposits[-1]["value"])


class OtherCards(_Q3):
    def test_a_vendor_cards_own_pages_are_not_list_pages(self):
        """#66 r1 (Astra S2): a vendor card's scope `pages` (pid groups) is its own; only a
        list card's `list_pages` are delivered with it."""
        import db, views
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " text, membership_json) VALUES ('r950','vendor-page',?,'x','v',"
                              "'[]')", (json.dumps({"page": 1, "pages": [[1, 2], [3]]}),))
        out = views.mark_rendering_delivered(self.conn, "r950")
        self.assertIn("delivered_at", out)


class ShortList(_Q3):
    def test_a_one_message_list_is_its_own_card_with_no_pages(self):
        self.sheet_fixture(guesses=2)
        out, prop = self.post(view="check", quarter="2026-Q3")
        self.assertNotIn("pages", prop)
        self.assertEqual(self.labels(prop), ["All good", "One by one", "Close"])

    def test_show_buttons_name_what_they_show_with_counts_only_when_non_empty(self):
        self.sheet_fixture(guesses=3)
        out, prop = self.post(view="status", quarter="2026-Q3")
        self.assertEqual(self.labels(prop), ["Show matches to confirm (3)", "Close"])
        call = prop["buttons"][0]["call"]
        self.assertEqual(call["arguments"], {"view": "check", "quarter": "2026-Q3"})
        for b in prop["buttons"]:
            self.assertLessEqual(len(b["label"]), 32)
            if "call" in b:
                self.assertIsNone(arguments_ok(b["call"]["arguments"]))

    def test_a_missing_count_appears_beside_the_check_count(self):
        self.seed_payments([{"counterparty": "Adobe"}, {"counterparty": "Zapier"}])
        out, prop = self.post(view="status", quarter="2026-Q3")
        self.assertEqual(self.labels(prop), ["Show missing invoices (2)", "Close"])
        # #93 (d1, Astra S2): the missing list has nothing to act on — the button is a tap
        # whose answer is the list itself, plain
        call = prop["buttons"][0]["call"]
        self.assertEqual((call["tool"], call["arguments"]["action"]), ("verdict", "show-missing"))
        import qa_server, tools  # noqa: F401
        ans = qa_server.TOOLS["verdict"]["fn"](dict(call["arguments"]))
        self.assertNotIn("next", ans)
        self.assertIn("Adobe", ans["receipt"])
        self.assertIn("Zapier", ans["receipt"])
        self.plain(view="missing", quarter="2026-Q3")

    def test_an_empty_store_is_a_plain_message(self):
        self.plain(view="status")
