"""Behavioural pins: typed "more"/"all of them" carry the bound rendering's cursor; "send it again" binds
to the newest delivered rendering that offers a package (I-2); a staged send is
posted at most once; store refusals escape names; an import does not stale a typed verdict; no document
is staged from the surface; a package that could not be posted says so."""
import json
import os
from tests._base import StoreCase
from tests.fakebroker import FakeBroker, arguments_ok

A, B = "aaaaaaaa-1", "bbbbbbbb-2"


class _Q3(StoreCase):
    def setUp(self):
        super().setUp()
        import datetime as dt
        cm = self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def show(self, **kw):
        import posting, views
        with FakeBroker():
            out = posting.show_view(self.conn, **kw)
        views.mark_rendering_delivered(self.conn, out["render_id"])
        return out

    def propose(self, ops, quoted=None):
        import posting
        from tests._base import as_ops
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, as_ops(ops), quoted)
        return out, (b.proposal() if b.deposits else None)

    def more(self, quoted=None):
        """#121: "more" / "all of them" is the bound post's continuation —
        reading_context's `next` (show_view arguments, or None)."""
        import posting
        with FakeBroker() as b:
            ctx = posting.reading_context(self.conn, quoted)
        self.assertEqual(b.deposits, [])
        return ctx["next"]

    def tap(self, prop, label):
        import tools, qa_server  # noqa: F401
        call = next(x for x in prop["buttons"] if x["label"] == label)["call"]
        return qa_server.TOOLS[call["tool"]]["fn"](call["arguments"])


class TypedMore(_Q3):
    """A desk turn is a fresh session; the reading returns the bound rendering's
    `next` as ready show_view arguments. #121: reading_context returns it."""
    from tests.test_views_buttons import PagedSheet as _P
    big_sheet = _P.big_sheet
    del _P

    def test_a_whole_list_posted_leaves_nothing_more(self):
        """#66: show_view posts every page; "all of them" on its card has nothing left."""
        import views
        self.big_sheet()
        out = self.show(view="check")
        self.assertIsNone(out["next"])
        self.assertIsNone(self.more(views.displayed(self.render_text(out["render_id"]))))

    def test_more_past_six_pages_returns_the_cards_cursor(self):
        """#66: a list longer than one post (Casa's six pages) ends its card with "say
        more"; "more" continues from where the sixth page stopped."""
        import views
        self.patch(views, "BODY_LIMIT", 600)
        pids = self.big_sheet()
        first = self.show(view="check")
        self.assertIn("after", first["next"])
        self.assertIn("There are more after these.", self.render_text(first["render_id"]))
        nxt = self.more()
        self.assertEqual(nxt, first["next"])
        self.assertIsNone(arguments_ok(nxt))
        self.assertTrue(all(type(x) is int for x in nxt["after"]))
        second = self.show(**first["next"])
        a = set(views.render_items(self.conn, first["render_id"]))
        b = set(views.render_items(self.conn, second["render_id"]))
        self.assertFalse(a & b)
        self.assertTrue(b)
        self.assertLessEqual(a | b, set(pids))

    def test_more_with_nothing_more_says_so(self):
        # #121: "says so" is the desk's (next is null: there is nothing more)
        self.sheet_fixture()
        self.assertIsNone(self.more())


class SendAgainBinding(StoreCase):
    """I-2: "last saw" is the latest delivered rendering that offers a package."""

    def test_a_later_view_without_offers_does_not_hide_the_offer(self):
        import delivery, posting, views
        pkg = self.sent_package(first_outcome="uncertain")
        self.assertEqual(delivery.resend_target(self.conn), pkg)
        with FakeBroker():
            out = posting.show_view(self.conn, view="missing")
        views.mark_rendering_delivered(self.conn, out["render_id"])
        self.assertEqual(delivery.uncertain(self.conn)[0][0], pkg)
        self.assertEqual(delivery.resend_target(self.conn), pkg)


class PostOnce(StoreCase):
    """A second post_package on a still-staged send deposits nothing."""

    def post(self, did):
        import tools, qa_server  # noqa: F401
        return qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})

    def test_two_posts_one_deposit(self):
        did = self.staged_package()
        with FakeBroker() as b:
            first = self.post(did)
            second = self.post(did)
        self.assertRegex(first["package"], r"^casa-cap-")
        self.assertEqual(sum(1 for d in b.deposits if d["slot"] == "package"), 1)
        self.assertEqual(set(second), {"package", "refused"})
        self.assertIsNone(second["package"])
        self.assertIn("already", second["refused"])
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "staged")

    def test_a_posted_send_without_a_receipt_is_still_recovered_uncertain(self):
        import datetime as dt, db, job, passes
        did = self.staged_package()
        with FakeBroker():
            self.post(did)
        lapsed = (db._clock() - dt.timedelta(seconds=passes.LEASE_S + 60)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.tx(self.conn):
            self.conn.execute("UPDATE deliveries SET lease_at=? WHERE delivery_id=?",
                              (lapsed, did))
        job.claim(self.conn, B)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "uncertain")
        self.assertIn("package-uncertain", [r[0] for r in self.conn.execute(
            "SELECT kind FROM alerts WHERE sent_at IS NULL")])


class EscapedRefusals(StoreCase):
    """A: a store refusal that names a counterparty names it escaped."""

    def test_a_shared_bank_text_refusal_escapes_the_owner(self):
        import db, kb, views
        kb.upsert_counterparty(self.conn, "*Acme*", patterns=["ACME BV"])
        with self.assertRaises(db.Refusal) as cm:
            kb.upsert_counterparty(self.conn, "Other", patterns=["ACME BV"])
        self.assertIn(f"belongs to {views.field('*Acme*')};", str(cm.exception))
        self.assertNotIn("belongs to *Acme*", str(cm.exception))

    def test_an_operator_expectation_refusal_escapes_the_name(self):
        import db, kb, views
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at,"
                              " exp_kind, exp_author) VALUES ('_Acme_', '[]', 'x', 'none',"
                              " 'operator')")
        with self.assertRaises(db.Refusal) as cm:
            kb.set_expectation(self.conn, scope_type="counterparty", scope="_Acme_",
                               kind="none", author="specialist")
        self.assertIn(f"what {views.field('_Acme_')} needs", str(cm.exception))


class ImportDoesNotStaleAReading(_Q3):
    """C (M-2): import bookkeeping and announce flags are not in a step's binding."""

    def test_a_row_high_water_bump_does_not_stale_apply(self):
        import db
        fx = self.sheet_fixture()
        _, prop = self.propose([("reject", fx["pid"])])
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET row_high_water=row_high_water+7,"
                              " watermark_announced=1, package_name_announced=1")
        rec = self.tap(prop, "Apply")
        self.assertIn("Removed the match for", rec["receipt"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 1)

    def test_a_watermark_change_still_stales_it(self):
        import db
        fx = self.sheet_fixture()
        _, prop = self.propose([("reject", fx["pid"])])
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-01-01'")
        self.assertIn("Something changed", self.tap(prop, "Apply")["receipt"])


class NoDocumentStaging(StoreCase):
    """A: stage_for_delivery does not take doc_id on the surface."""

    def test_doc_id_is_refused_at_the_wrapper(self):
        import db, tools, qa_server  # noqa: F401
        self.assertNotIn("doc_id",
                         qa_server.TOOLS["stage_for_delivery"]["schema"]["properties"])
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"doc_id": 1})
        self.assertIn("single document", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)


class NotPostedNotice(StoreCase):
    """D: a built package that could not be posted is told as not sent, without a Casa
    code (the refusal is the post's own answer; no request is left to close)."""

    def test_a_refused_deposit(self):
        import posting, tools, qa_server, views  # noqa: F401
        did = self.staged_package()
        with FakeBroker() as b:
            b.refuse = "bad_filename"
            out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        self.assertEqual((out["package"], out["refused"]), (None, posting.PKG_REFUSED))
        self.assertNotIn("bad_filename", out["refused"])
        for w in views.FORBIDDEN:
            self.assertNotIn(w, out["refused"])
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "failed")
        self.assertEqual(os.listdir(self.outbox), [])           # the staged copy taken back

# --- further pins -------------

class PostedSendLeftThePlugin(StoreCase):
    """A posted send has left the plugin. A new run's claim
    settles it `uncertain` before anything can requeue its ask; no import revokes it; one
    ask gets exactly one file. Port of DuplicateAsk (the real-outbox capture is replaced
    by counting package deposits: one deposit is one claimable file)."""

    def test_one_ask_one_deposit_across_an_interrupted_turn(self):
        import tools, qa_server  # noqa: F401
        did = self.staged_package()
        with FakeBroker() as b:
            qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
            # the turn dies before record_delivery; a new run starts within the lease
            self.drive(B, deliver=True)
        self.assertEqual(sum(1 for d in b.deposits if d["slot"] == "package"), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "uncertain")

    def test_the_late_receipt_still_upgrades_it(self):
        import delivery, job, tools, qa_server  # noqa: F401
        did = self.staged_package()
        with FakeBroker():
            qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        job.claim(self.conn, B)
        out = delivery.record_delivery(self.conn, delivery_id=did, outcome="delivered")
        self.assertEqual(out["status"], "delivered")

    def test_an_import_does_not_revoke_a_posted_send(self):
        import db, delivery, tools, qa_server  # noqa: F401
        did = self.staged_package()
        with FakeBroker():
            qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        with db.tx(self.conn):
            sid = self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows,"
                                    " max_row_id) VALUES (NULL, ?, 0, 0)",
                                    (db.now(),)).lastrowid
            self.assertEqual(delivery.revoke_superseded_first_sends(self.conn, sid), [])
        self.assertEqual(self.conn.execute("SELECT status, revoked_at FROM deliveries WHERE"
                                           " delivery_id=?", (did,)).fetchone()[:],
                         ("staged", None))


class DeliveredNeedsAPost(StoreCase):
    """A package send never posted cannot be recorded delivered."""

    def test_delivered_without_a_post_is_refused(self):
        import db, delivery
        did = self.staged_package()
        with self.assertRaises(db.Refusal) as cm:
            delivery.record_delivery(self.conn, delivery_id=did, outcome="delivered")
        self.assertIn("never posted", str(cm.exception))
        self.assertIn("uncertain", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "staged")
        out = delivery.record_delivery(self.conn, delivery_id=did, outcome="uncertain")
        self.assertEqual(out["status"], "uncertain")


class LegacyMore(_Q3):
    """A page delivered by v0.9.0 code stored no `next` key; "more" on it
    returns a fresh paged view of the same view and quarter, never "nothing more". Port of
    LegacyMore (the v0.9.0 store is replaced by a delivered page with its `next` key
    removed, which is what that code stored)."""
    from tests.test_views_buttons import PagedSheet as _P
    big_sheet = _P.big_sheet
    del _P

    def test_more_on_a_legacy_page(self):
        import db
        import views
        self.big_sheet()
        page1 = views.build_review(self.conn, view="check", page=1)     # a page as v0.9 sent it
        views.mark_rendering_delivered(self.conn, page1["render_id"])
        self.assertIsNotNone(page1["next"])
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (page1["render_id"],)).fetchone()[0])
        del scope["next"]
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                              (db.canonical(scope), page1["render_id"]))
        self.assertEqual(self.more(), {"view": "check", "quarter": "2026-Q3", "page": 1})

    def test_an_explicit_null_next_still_says_nothing_more(self):
        self.big_sheet(n=2)
        page1 = self.show(view="check", page=1)
        self.assertIsNone(page1["next"])
        self.assertIsNone(self.more())


class PlainRefusalWords(_Q3):
    """Plain refusal words."""

    def test_already_posted_and_refused_words_name_no_tool_and_no_code(self):
        import posting
        self.assertEqual(posting.ALREADY_POSTED,
                         "That package was already sent once — nothing was posted again.")
        self.assertNotIn("{code}", posting.PKG_REFUSED)
        self.assertNotIn("record_delivery", posting.PKG_REFUSED)


class QuoteBindsAPostedPage(_Q3):
    """A view posted by a navigation tap (the More button's
    stored show_view, a turn with no room for mark_rendering_delivered) is stamped posted;
    an explicit quote binds it. """
    from tests.test_views_buttons import PagedSheet as _P
    big_sheet = _P.big_sheet
    del _P

    def test_quoting_page_two_binds_page_two(self):
        """#66: a list posted by a tap (no mark follows) — its pages are stamped posted with
        its card, so a quote of page two binds page two."""
        import tools, qa_server, views  # noqa: F401
        self.big_sheet()
        call = lambda name, **a: qa_server.TOOLS[name]["fn"](a)
        with FakeBroker() as b:
            first = call("show_view", view="missing")
            call("mark_rendering_delivered", render_id=first["render_id"])
            second = call("show_view", view="check")              # the tap: no mark follows
            prop = b.proposal()
        page2 = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (second["render_id"],)
                                             ).fetchone()[0])["list_pages"][1]
        self.assertEqual(views.bound_rendering(self.conn, views.displayed(prop["pages"][1]))["render_id"], page2)
        self.assertEqual(views.bound_rendering(self.conn, None)["render_id"],
                         first["render_id"])                   # the unquoted fallback

    def test_a_refused_deposit_keeps_the_stamp(self):
        # posted_seq is monotone — "a deposit was attempted"; a
        # refusal never restores it, so a late refusal cannot erase a later post's stamp
        import posting
        self.sheet_fixture()
        with FakeBroker() as b:
            b.refuse = "bad_value"
            with self.assertRaises(Exception):
                posting.show_view(self.conn, view="check")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE posted_seq IS"
                                           " NOT NULL").fetchone()[0], 1)
