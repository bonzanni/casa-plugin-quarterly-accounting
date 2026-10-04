"""S7 final fix wave: the behavioural pins of final-review.md and the Codex r2 finding —
typed "more"/"all of them" carry the bound rendering's cursor (I-1); "send it again" binds
to the newest delivered rendering that offers a package (I-2, §6.3); a staged send is
posted at most once (Codex r2 S2); store refusals escape names (T3-a); an item page's More
keeps the walk (T5-c); an import does not stale a typed verdict (T6-c, M-2); no document
is staged from the surface (T11-a); a package that could not be posted says so (T11-d)."""
import json
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

    def propose(self, text, quoted=None):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, quoted)
        return out, (b.proposal() if b.deposits else None)

    def tap(self, prop, label):
        import tools, qa_server  # noqa: F401
        call = next(x for x in prop["buttons"] if x["label"] == label)["call"]
        return qa_server.TOOLS[call["tool"]]["fn"](call["arguments"])


class TypedMore(_Q3):
    """I-1: a desk turn is a fresh session; the reading returns the bound rendering's
    `next` as ready show_view arguments."""
    from tests.test_s7_views_buttons import PagedSheet as _P
    big_sheet = _P.big_sheet
    del _P

    def test_all_of_them_returns_the_capped_views_next(self):
        self.big_sheet()
        out = self.show(view="check")
        self.assertIsNotNone(out["next"])
        r, prop = self.propose("all of them", quoted=self.render_text(out["render_id"]))
        self.assertIsNone(prop)
        self.assertEqual(r["instructions"], [{"show_view": out["next"]}])
        self.assertIsNone(arguments_ok(r["instructions"][0]["show_view"]))

    def test_more_returns_the_page_cursor(self):
        self.big_sheet()
        first = self.show(view="check")
        page1 = self.show(**first["next"])
        self.assertIn("after", page1["next"])
        r, _ = self.propose("more")
        (ins,) = r["instructions"]
        self.assertEqual(ins, {"show_view": page1["next"]})
        self.assertTrue(all(type(x) is int for x in ins["show_view"]["after"]))

    def test_more_with_nothing_more_says_so(self):
        self.sheet_fixture()
        r, prop = self.propose("more")
        self.assertIsNone(prop)
        self.assertEqual(r["instructions"], [])
        self.assertIn("nothing more", r["say"])


class SendAgainBinding(StoreCase):
    """I-2 (§6.3): "last saw" is the latest delivered rendering that offers a package."""

    def test_a_later_view_without_offers_does_not_hide_the_offer(self):
        import delivery, posting, views
        pkg = self.delivered_package(first_outcome="uncertain")
        self.assertEqual(delivery.resend_target(self.conn), pkg)
        with FakeBroker():
            out = posting.show_view(self.conn, view="missing")
        views.mark_rendering_delivered(self.conn, out["render_id"])
        self.assertEqual(delivery.uncertain(self.conn)[0][0], pkg)
        self.assertEqual(delivery.resend_target(self.conn), pkg)


class PostOnce(StoreCase):
    """Codex r2 S2: a second post_package on a still-staged send deposits nothing."""

    def post(self, did, tok):
        import tools, qa_server  # noqa: F401
        return qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did,
                                                      "package_token": tok})

    def test_two_posts_one_deposit(self):
        import asks
        asks.request_package(self.conn, "2026-Q3")
        did, tok = self.drive_to_staged(A)
        with FakeBroker() as b:
            first = self.post(did, tok)
            second = self.post(did, tok)
        self.assertRegex(first["package"], r"^casa-cap-")
        self.assertEqual(sum(1 for d in b.deposits if d["slot"] == "package"), 1)
        self.assertEqual(set(second), {"package", "refused"})
        self.assertIsNone(second["package"])
        self.assertIn("already", second["refused"])
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "staged")

    def test_a_posted_send_without_a_receipt_is_still_recovered_uncertain(self):
        import asks, datetime as dt, db, job, steps
        asks.request_package(self.conn, "2026-Q3")
        did, tok = self.drive_to_staged(A)
        with FakeBroker():
            self.post(did, tok)
        lapsed = steps._stamp(db._clock() - dt.timedelta(seconds=steps.LEASE_S + 60))
        with db.tx(self.conn):
            self.conn.execute("UPDATE deliveries SET lease_at=? WHERE delivery_id=?",
                              (lapsed, did))
        job.claim(self.conn, B)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (did,)).fetchone()[0], "uncertain")
        self.assertIn("package-uncertain", [r[0] for r in self.conn.execute(
            "SELECT kind FROM alerts WHERE sent_at IS NULL")])


class EscapedRefusals(StoreCase):
    """T3-a (INV-S7-7): a store refusal that names a counterparty names it escaped."""

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


class ItemMoreKeepsTheWalk(StoreCase):
    """T5-c: an item page's More carries the walk, so page 2 still offers Next."""

    def test_more_on_an_item_page_carries_walk(self):
        import views
        r = {"render_id": "r9", "kind": "item",
             "scope_json": json.dumps({"pid": 1, "item_state": "none",
                                       "next": {"view": "item", "pid": 1, "page": 2,
                                                "after": [3]}})}
        more = next(b for b in views.buttons_for(self.conn, r, walk="r4") if b[0] == "More")
        self.assertEqual(more[2], {"view": "item", "pid": 1, "page": 2, "after": [3],
                                   "walk": "r4"})
        self.assertIsNone(arguments_ok(more[2]))
        plain = next(b for b in views.buttons_for(self.conn, r) if b[0] == "More")
        self.assertNotIn("walk", plain[2])


class ImportDoesNotStaleAReading(_Q3):
    """T6-c (M-2): import bookkeeping and announce flags are not in a step's binding."""

    def test_a_row_high_water_bump_does_not_stale_apply(self):
        import db
        fx = self.sheet_fixture()
        _, prop = self.propose(f"the {fx['payee']} one is wrong")
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET row_high_water=row_high_water+7,"
                              " watermark_announced=1, package_name_announced=1")
        rec = self.tap(prop, "Apply")
        self.assertIn("Unpaired", rec["receipt"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 1)

    def test_a_watermark_change_still_stales_it(self):
        import db
        fx = self.sheet_fixture()
        _, prop = self.propose(f"the {fx['payee']} one is wrong")
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-01-01'")
        self.assertIn("Something changed", self.tap(prop, "Apply")["receipt"])


class NoDocumentStaging(StoreCase):
    """T11-a: stage_for_delivery does not take doc_id on the surface."""

    def test_doc_id_is_refused_at_the_wrapper(self):
        import db, tools, qa_server  # noqa: F401
        self.assertNotIn("doc_id",
                         qa_server.TOOLS["stage_for_delivery"]["schema"]["properties"])
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"doc_id": 1})
        self.assertIn("single document", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)


class NotPostedNotice(StoreCase):
    """T11-d: a built package that could not be posted is told as not sent, without a Casa
    code; an oversized one too."""

    def notice_text(self):
        import alerts
        out = alerts.pending_rendering(self.conn)
        return " ".join(out["text"].split())

    def assert_plain(self, text):
        import views
        self.assertIn("I couldn't send the Q3 2026 package", text)
        self.assertNotIn("build", text)
        self.assertNotIn("bad_filename", text)
        for w in views.FORBIDDEN:
            self.assertNotIn(w, text)

    def test_a_refused_deposit(self):
        import asks, tools, qa_server  # noqa: F401
        asks.request_package(self.conn, "2026-Q3")
        did, tok = self.drive_to_staged(A)
        with FakeBroker() as b:
            b.refuse = "bad_filename"
            qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did, "package_token": tok})
        text = self.notice_text()
        self.assert_plain(text)
        self.assertIn("ask again when you want it", text)

    def test_an_oversized_package(self):
        import asks, package
        asks.request_package(self.conn, "2026-Q3")
        self.patch(package, "MAX_ZIP_BYTES", 10)
        self.drive(A, deliver=True)
        posted = " ".join(" ".join(r[0] for r in self.conn.execute(
            "SELECT text FROM renders WHERE kind='alert'")).split())
        self.assert_plain(posted)
        self.assertIn("20 MB", posted)
