"""S7 §7: a view is posted as a proposal whose buttons are stored calls bound to that
rendering; a writing button carries a fresh key minted into the deposit only; verdict
commits only what the tapped rendering listed, all-or-nothing, and a stale tap refuses."""
import json
from tests._base import StoreCase
from tests.fakebroker import FakeBroker, arguments_ok

LABELS = {"All good", "One by one", "More", "Right", "Wrong", "No invoice needed", "Next",
          "What's missing", "Anything to check?", "Apply", "Cancel"} | {
          f"Account {i}" for i in range(1, 6)}


class _Q3(StoreCase):
    """Ruling F3: the sheet fixture's data sits in 2026-Q3 and show_view defaults to
    today's quarter, so every test here runs on a 2026-Q3 clock."""
    def setUp(self):
        super().setUp()
        import datetime as dt
        cm = self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)


class ShowView(_Q3):
    def post(self, **kw):
        import posting
        with FakeBroker() as b:
            out = posting.show_view(self.conn, **kw)
        return out, b.proposal(), b.deposits[0]

    def test_a_check_sheet_with_guesses_has_all_good_one_by_one(self):
        fx = self.sheet_fixture()
        out, prop, body = self.post(view="check")
        self.assertEqual(body["slot"], "view")
        self.assertEqual([b["label"] for b in prop["buttons"]][:2], ["All good", "One by one"])
        ag = prop["buttons"][0]["call"]
        self.assertEqual(ag["tool"], "verdict")
        self.assertEqual(ag["arguments"]["render_id"], out["render_id"])
        self.assertRegex(ag["arguments"]["key"], r"^[0-9a-f]{32}$")
        self.assertEqual(prop["revision"], "view:check:2026-Q3")
        for b in prop["buttons"]:
            self.assertIn(b["label"], LABELS)
            self.assertIsNone(arguments_ok(b["call"]["arguments"]))

    def test_the_key_never_reaches_the_tools_result(self):
        import tools, qa_server  # noqa: F401
        self.sheet_fixture()
        with FakeBroker() as b:
            out = qa_server.TOOLS["show_view"]["fn"]({"view": "check"})
        key = b.proposal()["buttons"][0]["call"]["arguments"]["key"]
        self.assertNotIn(key, json.dumps(out))
        self.assertRegex(out["view"], r"^casa-cap-")

    def test_an_informational_page_offers_whats_missing_and_anything_to_check(self):
        out, prop, _ = self.post(view="status")
        self.assertEqual([b["label"] for b in prop["buttons"]],
                         ["What's missing", "Anything to check?"])

    def test_re_posting_a_stored_rendering_reuses_its_text_and_mints_fresh_keys(self):
        self.sheet_fixture()
        first, p1, _ = self.post(view="check")
        again, p2, _ = self.post(render_id=first["render_id"])
        self.assertEqual(again["render_id"], first["render_id"])
        self.assertEqual(p1["text"], p2["text"])
        self.assertNotEqual(p1["buttons"][0]["call"]["arguments"]["key"],
                            p2["buttons"][0]["call"]["arguments"]["key"])

    def test_a_stored_rendering_over_the_proposal_budget_is_refused_for_buttons(self):
        import db, views
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " text, membership_json) VALUES ('r900','status','{}','x',?,'[]')",
                              ("a" * 4050,))
        self.assertFalse(views.fits_proposal("a" * 4050))
        import tools, qa_server  # noqa: F401
        with FakeBroker():
            out = qa_server.TOOLS["show_view"]["fn"]({"render_id": "r900"})
        self.assertIsNone(out["view"])
        self.assertIn("post_results", out["refused"])

    def test_a_setup_stop_view_posts_with_informational_buttons(self):
        """Plan round 4, Terra S2: an unbound store's status view (the setup lead) has no
        items; show_view still stores and posts it."""
        out, prop, _ = self.post(view="status")          # StoreCase: nothing bound
        self.assertRegex(out["view"], r"^casa-cap-")
        self.assertEqual([b["label"] for b in prop["buttons"]],
                         ["What's missing", "Anything to check?"])

    def test_a_legacy_rendering_with_a_control_character_is_deposited_clean(self):
        import db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " text, membership_json) VALUES ('r901','status','{}','x',?,'[]')",
                              ("ACME\x01 owes",))
        _, prop, _ = self.post(render_id="r901")
        self.assertEqual(prop["text"], "ACME  owes")


class PagedSheet(_Q3):
    """Review r1, finding 1 (§7.6): a paged sheet's More button stores its cursor; the
    cursor is ints only, never a bank-feed date, and the next page starts where the
    previous one stopped."""
    def big_sheet(self, n=40):
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

    def test_the_more_cursor_is_ints_and_page_two_continues_page_one(self):
        import posting, views
        pids = self.big_sheet()
        args, seen, cursors = {"view": "check"}, [], []
        for _ in range(20):
            with FakeBroker() as b:
                out = posting.show_view(self.conn, **args)
            more = [x for x in b.proposal()["buttons"] if x["label"] == "More"]
            if args.get("page"):
                seen.append(views.render_items(self.conn, out["render_id"]))
            if not more:
                break
            args = more[0]["call"]["arguments"]
            self.assertIsNone(arguments_ok(args))
            if "after" in args:
                self.assertTrue(all(type(x) is int for x in args["after"]), args)
                cursors.append(args["after"])
        self.assertGreaterEqual(len(seen), 2)         # the sheet really paged
        self.assertTrue(cursors)
        flat = [p for page in seen for p in page]
        self.assertEqual(len(flat), len(set(flat)))   # no payment on two pages
        self.assertEqual(set(flat), set(pids))        # and none skipped between them


class Verdict(_Q3):
    def tap(self, prop, label):
        import tools, qa_server  # noqa: F401
        b = next(x for x in prop["buttons"] if x["label"] == label)
        return qa_server.TOOLS[b["call"]["tool"]]["fn"](b["call"]["arguments"])

    def sheet(self, **kw):
        import posting
        with FakeBroker() as b:
            posting.show_view(self.conn, view="check", **kw)
        return b.proposal()

    def test_all_good_confirms_exactly_the_sheets_guesses(self):
        fx = self.sheet_fixture(guesses=2)
        out = self.tap(self.sheet(), "All good")
        self.assertIn("Confirmed", out["receipt"])
        self.assertEqual(len(out["applied"]), 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           " AND kind='pair'").fetchone()[0], 2)

    def test_all_good_after_one_item_changed_commits_nothing(self):
        """Review Focus 1: the job re-judged one payment after the sheet was posted."""
        fx = self.sheet_fixture(guesses=2)
        prop = self.sheet()
        self.rejudge(fx["pids"][1])          # a machine relabel: bumps that match's revision
        out = self.tap(prop, "All good")
        self.assertTrue(out["receipt"].startswith("Nothing was applied: this sheet is out of date"))
        self.assertIn("1 payment changed", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 0)

    def test_a_key_is_spent_by_its_first_use_and_a_forged_key_refuses(self):
        fx = self.sheet_fixture()
        prop = self.sheet()
        self.tap(prop, "All good")
        again = self.tap(prop, "All good")
        self.assertEqual(again, {"receipt": __import__("keys").NO_LONGER})
        import tools, qa_server  # noqa: F401
        args = dict(prop["buttons"][0]["call"]["arguments"], key="f" * 32)
        self.assertEqual(qa_server.TOOLS["verdict"]["fn"](args)["receipt"],
                         __import__("keys").NO_LONGER)

    def test_item_walk_right_wrong_no_invoice(self):
        import posting
        fx = self.sheet_fixture(guesses=2)
        prop = self.sheet()
        walk = prop["buttons"][1]["call"]["arguments"]
        with FakeBroker() as b:
            posting.show_view(self.conn, **walk)
        item = b.proposal()
        self.assertEqual([x["label"] for x in item["buttons"]],
                         ["Right", "Wrong", "No invoice needed", "Next"])
        out = self.tap(item, "Wrong")
        self.assertIn("Unpaired", out["receipt"])
        nxt = next(x for x in item["buttons"] if x["label"] == "Next")["call"]["arguments"]
        self.assertEqual(nxt["pid"], fx["pids"][1])

    def test_after_right_the_item_is_paired_not_proposed(self):
        """T5b / spec 7.3: a verdict acts on PROPOSED pairings; an operator-confirmed pairing
        is not one, though its match label row stays "guessed"."""
        import posting
        fx = self.sheet_fixture(guesses=1)

        def item():
            with FakeBroker() as b:
                out = posting.show_view(self.conn, view="item", pid=fx["pid"])
            scope = json.loads(self.conn.execute(
                "SELECT scope_json FROM renders WHERE render_id=?",
                (out["render_id"],)).fetchone()[0])
            return b.proposal(), scope

        prop, scope = item()
        self.assertEqual(scope["item_state"], "proposed")
        self.assertIn("among several that fit", prop["text"])
        self.tap(prop, "Right")
        prop, scope = item()
        self.assertEqual(scope["item_state"], "paired")
        self.assertEqual(scope["proposed"], [])
        self.assertEqual([x["label"] for x in prop["buttons"]], ["Wrong", "No invoice needed"])
        self.assertNotIn("among several that fit", prop["text"])
        self.assertNotIn("also fits", prop["text"])
        with FakeBroker() as b:
            posting.show_view(self.conn, view="check")
        self.assertNotIn("All good", [x["label"] for x in b.proposal()["buttons"]])

    def test_a_verdict_on_a_pid_the_rendering_did_not_list_refuses(self):
        import keys, db
        fx = self.sheet_fixture()
        prop = self.sheet()
        args = dict(prop["buttons"][0]["call"]["arguments"])
        import tools, qa_server  # noqa: F401
        out = qa_server.TOOLS["verdict"]["fn"](dict(args, action="right", pid=fx["pid"] + 999))
        self.assertEqual(out["receipt"], keys.NO_LONGER)
