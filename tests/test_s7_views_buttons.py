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
        self.assertEqual(walk, {"view": "item", "pid": fx["pids"][0]})   # no walk to carry
        with FakeBroker() as b:
            posting.show_view(self.conn, **walk)
        item = b.proposal()
        self.assertEqual([x["label"] for x in item["buttons"]],
                         ["Right", "Wrong", "No invoice needed"])       # §4: no walk Next
        out = self.tap(item, "Wrong")
        self.assertIn("Removed the match for", out["receipt"])

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
        # final fix wave T5-a: the key matches (render_id, action, pid), so the key check
        # passes and the rendering's render_items check is what refuses
        import keys, db
        fx = self.sheet_fixture()
        prop = self.sheet()
        rid = prop["buttons"][0]["call"]["arguments"]["render_id"]
        stray, key = fx["pid"] + 999, keys.mint()
        with db.tx(self.conn):
            keys.store_render(self.conn, rid, "right", stray, key)
        spent, real = [], keys.spend_render

        def spy(*a):
            real(*a)
            spent.append(a[1:])
        self.patch(keys, "spend_render", spy)
        import tools, qa_server  # noqa: F401
        out = qa_server.TOOLS["verdict"]["fn"]({"render_id": rid, "action": "right",
                                                "pid": stray, "key": key})
        self.assertEqual(out["receipt"], keys.NO_LONGER)
        self.assertEqual(len(spent), 1)                  # the key check passed
        self.assertIsNone(self.conn.execute("SELECT spent_at FROM render_keys WHERE key=?",
                                            (key,)).fetchone()[0])   # the spend rolled back
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 0)

    def item(self, pid):
        import posting
        with FakeBroker() as b:
            out = posting.show_view(self.conn, view="item", pid=pid)
        return out, b.proposal()

    def test_right_on_an_item_confirms_its_pairing(self):
        """T5-b: taps._apply_one `right`."""
        fx = self.sheet_fixture()
        _, prop = self.item(fx["pid"])
        out = self.tap(prop, "Right")
        self.assertTrue(out["receipt"].startswith("Confirmed "), out)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM log WHERE author='operator' AND kind='pair' AND match_id=?",
            (fx["match_id"],)).fetchone()[0], 1)

    def test_no_invoice_needed_exempts_and_drops_the_guess(self):
        """T5-b: taps._apply_one `no-invoice`."""
        fx = self.sheet_fixture()
        _, prop = self.item(fx["pid"])
        out = self.tap(prop, "No invoice needed")
        self.assertIn("needs no document; removed its match.", out["receipt"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM log WHERE author='operator' AND kind='exempt'").fetchone()[0], 1)
        _, again = self.item(fx["pid"])
        self.assertEqual([x["label"] for x in again["buttons"]], ["What's missing"])

    def candidates_only(self):
        """A payment with two displayed candidates and no current pairing."""
        import db
        self.sheet_fixture()
        self.row(90, counterparty="Two Fits", amount_minor=5000, booking_date="2026-09-10",
                 value_date="2026-09-10")
        pid = self.lineage_for(90)
        self.classify(pid, {"software"})
        self.settle(pid)
        mids = []
        docs = [self.doc(counterparty="Two Fits", issuer=f"Issuer {n}", document_number=n,
                         amount_minor=5000, document_date="2026-09-09")
                for n in ("INV-A", "INV-B")]
        with db.tx(self.conn):
            for doc in docs:
                mid = self.conn.execute(
                    "INSERT INTO matches(pid_created, doc_id, label, runners_up_json,"
                    " created_seq) VALUES (?,?,'clean','[]',?)",
                    (pid, doc, db.next_seq(self.conn))).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state,"
                                  " author, activation) VALUES (?,?,?,'conflicted','auto',0)",
                                  (mid, pid, doc))
                mids.append(mid)
        return pid, mids

    def test_an_item_with_no_pairing_offers_only_no_invoice_needed(self):
        """T5-b: the `none` button set (the `paired` set is pinned above)."""
        import json as _json
        pid, _ = self.candidates_only()
        out, prop = self.item(pid)
        scope = _json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                              " render_id=?", (out["render_id"],)).fetchone()[0])
        self.assertEqual(scope["item_state"], "none")
        self.assertEqual([x["label"] for x in prop["buttons"]], ["No invoice needed"])

    def test_wrong_with_no_current_pairing_sets_every_shown_candidate_aside(self):
        """T5-b: taps._apply_one `wrong` over candidates (reject_all_in_tx)."""
        import db, taps, views, work
        pid, mids = self.candidates_only()
        out, _ = self.item(pid)
        views.mark_rendering_delivered(self.conn, out["render_id"])
        d = work.describe(self.conn, pid)
        self.assertIsNone(d["current"])
        self.assertEqual(sorted(c["match_id"] for c in d["candidates"]), sorted(mids))
        with db.tx(self.conn):
            res, line = taps._apply_one(self.conn, self.grant(), out["render_id"], "wrong", d)
        self.assertEqual(sorted(res["set_aside"]), sorted(mids))
        self.assertTrue(line.startswith("Ruled out both invoices for "), line)
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT match_id FROM log WHERE kind='unpair' AND author='operator'")), sorted(mids))
