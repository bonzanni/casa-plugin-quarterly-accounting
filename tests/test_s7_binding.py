# tests/test_s7_binding.py
"""S7 binding: one bound rendering per typed reading (rounds-2026-10-03-s7-diff/binding/
design.md, v3 + the d3 amendment). A reading binds exactly one rendering R — the one its
quote identifies, or, for words with no quote, db.last_delivered — and every resolution
step reads only R's own render_items and scope_json. Each class ports a reviewer's
reproduction (round 4: review_r4.py, review_job_binding.py; d1: review_binding.py; d2:
review_d2.py; d3: review_migration.py) as a stdlib pin, asserting counts, not statuses."""
import ast
import datetime as dt
import inspect
import json
import pathlib
import re
import sqlite3
import unittest
from unittest import mock

from tests._base import StoreCase
from tests.fakebroker import FakeBroker

NOW = dt.datetime(2026, 9, 15, 12, tzinfo=dt.timezone.utc)
ROOT = pathlib.Path(__file__).resolve().parents[1]
LABEL = "\U0001f4ca Finance\n"


def call(name, **args):
    import qa_server
    import tools  # noqa: F401 — registers the tools
    return qa_server.TOOLS[name]["fn"](args)


def tap(prop, label):
    c = next(x["call"] for x in prop["buttons"] if x["label"] == label)
    return call(c["tool"], **c["arguments"])


class _Q3(StoreCase):
    def setUp(self):
        super().setUp()
        cm = self.patch_clock(NOW)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def propose(self, text, quoted=None):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, quoted)
        return out, (b.proposal() if b.deposits else None)

    def readings(self):
        return self.conn.execute("SELECT count(*) FROM readings").fetchone()[0]

    def operator_rows(self, match_id=None):
        if match_id is None:
            return self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                     ).fetchone()[0]
        return self.conn.execute("SELECT count(*) FROM log WHERE author='operator' AND"
                                 " match_id=?", (match_id,)).fetchone()[0]

    def text_of(self, render_id):
        import views
        return views.unesc(self.render_text(render_id))

    def repair(self, pid, number, issuer=None, token=None):
        """The job re-judges `pid`: a new document (same amount and date, `number`) replaces
        its pairing (review_r4.test_quoted_old_pair). The new match row."""
        d = call("list_quarter_state", pid=pid)["item"]
        desc = self.conn.execute("SELECT * FROM projections WHERE pid=?", (pid,)).fetchone()
        import work
        w = work.describe(self.conn, pid)
        doc = self.doc(counterparty=w["counterparty"], issuer=issuer or w["counterparty"],
                       document_number=number, amount_minor=w["amount_minor"],
                       document_date=w["date"])
        tok = token or self._fixture_token
        args = dict(pid=pid, doc_id=doc, author="auto", expected_revision=d["revision"],
                    row_digest=d["row_digest"], document_date=w["date"], labels=["guessed"],
                    pass_token=tok)
        out = call("record_match", **args)
        d = call("list_quarter_state", pid=pid)["item"]
        if d["candidate_ids"]:
            args.update(expected_revision=d["revision"], row_digest=d["row_digest"])
            out = call("record_match", **args)
        assert desc is not None
        return out

    def refused_show(self, **kw):
        """show_view whose deposit Casa refuses (posted_seq stays: §3)."""
        import casa_broker
        import posting
        with mock.patch.object(casa_broker, "deposit",
                               side_effect=casa_broker.DepositFailed("proposal_limit")):
            with self.assertRaises(casa_broker.DepositFailed):
                posting.show_view(self.conn, **kw)
        return dict(self.conn.execute("SELECT * FROM renders ORDER BY rowid DESC LIMIT 1"
                                      ).fetchone())

    def make_legacy(self, *render_ids):
        """What a pre-S7 rendering stored: the same text without the first-line tag."""
        import db
        with db.tx(self.conn):
            for rid in render_ids:
                text = self.render_text(rid)
                first, _, rest = text.partition("\n")
                tag = f" · {rid[1:]}"
                assert first.endswith(tag), (rid, first)
                self.conn.execute("UPDATE renders SET text=? WHERE render_id=?",
                                  (first[:-len(tag)] + ("\n" + rest if _ else ""), rid))


# ---------------------------------------------------------------------------------------
# Red case 1 — r4 Astra S1: no step reads `shown` (the _bind_match fallback is gone)
# ---------------------------------------------------------------------------------------
class OldSheetQuote(_Q3):
    """review_job_binding.py: an OLD sheet's quote after FIRST→SECOND binds the old sheet;
    "all good" and a named verdict judge the pairing that sheet recorded, so nothing commits
    on SECOND, which the operator never saw on it."""

    def _old_and_newer(self):
        f = self.sheet_fixture()
        with FakeBroker() as b:
            old = call("show_view", view="check", quarter="2026-Q3")
            quote = b.proposal()["text"]
            call("mark_rendering_delivered", render_id=old["render_id"])
            second = self.repair(f["pid"], "SECOND")
            newer = call("show_view", view="check", quarter="2026-Q3")
            call("mark_rendering_delivered", render_id=newer["render_id"])
        return f, old, quote, second

    def test_all_good_on_the_old_sheet_commits_nothing(self):
        f, old, quote, second = self._old_and_newer()
        out, prop = self.propose("all good", quoted=quote)
        self.assertIsNone(out["reading"])
        self.assertEqual(self.readings(), 0)
        self.assertIn("changed since", out["say"])
        self.assertEqual([self.operator_rows(f["match_id"]), self.operator_rows(
            second["match_id"])], [0, 0])

    def _named(self, words):
        f, old, quote, second = self._old_and_newer()
        out, _ = self.propose(words, quoted=quote)
        self.assertIsNone(out["reading"])
        self.assertIn("changed since", out["say"])
        self.assertEqual([self.operator_rows(f["match_id"]),
                          self.operator_rows(second["match_id"])], [0, 0])

    def test_the_x_one_is_good_on_the_old_sheet_commits_nothing(self):
        self._named("the Zapier one is good")

    def test_the_x_one_is_wrong_on_the_old_sheet_commits_nothing(self):
        self._named("the Zapier one is wrong")


class MergedSurvivor(StoreCase):
    """§2 #4/#6: R records the loser of a later lineage merge; the survivor was shown with
    its pairing on another rendering. A verdict on R fails closed (R recorded no row for
    the survivor) — it never borrows the survivor's `shown` record."""

    def setUp(self):
        super().setUp()
        import db
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        self.n = 0
        cm = self.patch_clock(NOW)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    item = __import__("tests.test_reply", fromlist=["Base"]).Base.item

    def test_a_verdict_on_the_losers_rendering_refuses(self):
        import db
        import views
        survivor = self.item("Zapier", 9900, "2026-09-17")
        loser = self.item("Zapier", 9900, "2026-09-16")
        r0 = views.build_review(self.conn, view="item", pid=survivor)
        views.mark_rendering_delivered(self.conn, r0["render_id"])
        r1 = views.build_review(self.conn, view="item", pid=loser)
        views.mark_rendering_delivered(self.conn, r1["render_id"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET merged_into=? WHERE pid=?",
                              (survivor, loser))
        import posting
        with FakeBroker():
            out = posting.propose_reading(self.conn, "the Zapier one is good",
                                          views.unesc(r1["text"]))
        self.assertIsNone(out["reading"])
        self.assertEqual(out["reshow"], [survivor])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 0)


# ---------------------------------------------------------------------------------------
# Red case 2 — r4 Astra S2: refs and broad rules resolve through the bound (posted) R
# ---------------------------------------------------------------------------------------
class PostedOnlyQuote(_Q3):
    """review_r4.py ::test_posted_refs / ::test_posted_broad, inverted: a view posted by a
    tap's stored call and never marked delivered is what the operator replied to."""

    def test_posted_refs(self):
        self.sheet_fixture()
        self._fixture_guess = ("Twin", 9900, "2026-09-17", 0)
        a = self.add_guess()
        self._fixture_guess = ("Twin", 9900, "2026-09-17", 0)
        self.add_guess()
        with FakeBroker() as b:
            call("show_view", view="status", quarter="2026-Q3")
            sheet = tap(b.proposal(), "Anything to check?")
            prop = b.proposal()
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?",
                              (sheet["render_id"],)).fetchone()
        self.assertIsNone(r["delivered_at"])
        ref = next(k for k, v in json.loads(r["scope_json"])["refs"].items() if a[1] in v)
        out, rprop = self.propose(f"ref {ref} is wrong", quoted=prop["text"])
        self.assertIsNotNone(out["reading"])
        self.assertEqual(self.readings(), 1)
        self.assertEqual(self.conn.execute("SELECT render_id FROM readings").fetchone()[0],
                         r["render_id"])
        tap(rprop, "Apply")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           " AND kind='unpair'").fetchone()[0], 1)

    def test_posted_broad(self):
        f = self.sheet_fixture()
        with FakeBroker() as b:
            sh = call("show_view", view="check", quarter="2026-Q3")
            prop = b.proposal()
        out, rprop = self.propose("no invoices ever for Zapier", quoted=prop["text"])
        self.assertIsNotNone(out["reading"])
        self.assertEqual(self.readings(), 1)
        self.assertEqual(self.conn.execute("SELECT render_id FROM readings").fetchone()[0],
                         sh["render_id"])
        tap(rprop, "Apply")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM counterparties WHERE"
                                           " exp_author='operator'").fetchone()[0], 1)
        self.assertEqual(f["payee"], "Zapier")


# ---------------------------------------------------------------------------------------
# Red case 3 — r4 Terra S1: posted_seq is monotone; a refusal never erases a later stamp
# ---------------------------------------------------------------------------------------
class MonotoneStamp(_Q3):
    def test_a_late_refusal_keeps_the_newer_posts_stamp(self):
        import casa_broker
        import posting
        import views
        f = self.sheet_fixture()
        rid = f["render_id"]
        real = casa_broker.deposit
        state = {"n": 0}

        def interleaved(slot, value, **kw):
            state["n"] += 1
            if state["n"] == 1:
                # the second re-post runs (and lands) while the first one's deposit is out
                posting.show_view(self.conn, render_id=rid)
                raise casa_broker.DepositFailed("timeout")
            return real(slot, value, **kw)
        with FakeBroker() as b, mock.patch.object(casa_broker, "deposit", interleaved):
            with self.assertRaises(casa_broker.DepositFailed):
                posting.show_view(self.conn, render_id=rid)
            landed = b.proposal()["text"]
        row = self.conn.execute("SELECT posted_seq FROM renders WHERE render_id=?",
                                (rid,)).fetchone()
        self.assertIsNotNone(row["posted_seq"])
        self.assertEqual(views.bound_rendering(self.conn, landed)["render_id"], rid)


# ---------------------------------------------------------------------------------------
# Red case 4 — not on R: unquoted words naming what only an older sheet printed refuse
# ---------------------------------------------------------------------------------------
class NotOnR(_Q3):
    def _two_then_item(self):
        import views
        f = self.sheet_fixture(guesses=2)
        other = f["pids"][1]
        r = views.build_review(self.conn, view="item", pid=other)
        views.mark_rendering_delivered(self.conn, r["render_id"])
        return f

    def test_a_name_only_on_an_older_sheet(self):
        f = self._two_then_item()
        out, _ = self.propose("the Zapier one is wrong")
        self.assertIsNone(out["reading"])
        self.assertIn("isn't on the last list I sent", out["say"])
        self.assertEqual(self.operator_rows(f["match_id"]), 0)

    def test_a_ref_only_on_an_older_sheet(self):
        f = self._two_then_item()
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (f["render_id"],)).fetchone()[0])
        refs = scope.get("refs") or {}
        if not refs:
            # the fixture prints no ref (no payee collision): give the sheet one
            import db
            refs = {"e40c": [f["pid"]]}
            scope["refs"] = refs
            with db.tx(self.conn):
                self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                                  (db.canonical(scope), f["render_id"]))
        ref = next(iter(refs))
        out, _ = self.propose(f"ref {ref} is wrong")
        self.assertIsNone(out["reading"])
        self.assertEqual(self.operator_rows(), 0)

    def test_quoted_words_naming_what_the_quote_lacks(self):
        f = self._two_then_item()
        item = self.conn.execute("SELECT render_id FROM renders WHERE kind='item'").fetchone()[0]
        out, _ = self.propose("the Zapier one is wrong", quoted=self.text_of(item))
        self.assertIsNone(out["reading"])
        self.assertIn("isn't on the message you replied to", out["say"])
        self.assertEqual(self.operator_rows(f["match_id"]), 0)


# ---------------------------------------------------------------------------------------
# Red cases 5 and 12 — V1: a continued page merges only its explicit predecessor
# ---------------------------------------------------------------------------------------
class ContinuedPages(StoreCase):
    """review_d2.py refused_continuation: Adobe with two candidates, one per page (a
    reduced page budget)."""

    def setUp(self):
        super().setUp()
        import db
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        cm = self.patch_clock(NOW)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        import views
        self.patch(views, "BODY_LIMIT", 200)
        import work
        self.row(1, counterparty="Adobe", amount_minor=5445, booking_date="2026-09-14",
                 value_date="2026-09-14")
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        self.handed(self.pid)
        work.record_search(self.conn, pid=self.pid, token=self.token, queries=["Adobe"])
        self.docs = []
        for i in range(2):
            did = self.doc(counterparty="Adobe", issuer="Adobe", document_number="CANDIDATE%02d"
                           % i + "X" * 40, document_date="2026-09-14", amount_minor=5445)
            self.docs.append(did)
            self.machine_entry(self.pid, did)      # a joint machine set: two candidates
        self.assertEqual(len(work.describe(self.conn, self.pid)["candidates"]), 2)

    def propose(self, text, quoted=None):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, quoted)
        return out, (b.proposal() if b.deposits else None)

    def unpairs(self):
        return self.conn.execute("SELECT count(*) FROM log WHERE author='operator' AND"
                                 " kind='unpair'").fetchone()[0]

    def page1(self):
        with FakeBroker() as b:
            p1 = call("show_view", view="item", pid=self.pid)
            prop = b.proposal()
        call("mark_rendering_delivered", render_id=p1["render_id"])
        self.assertTrue(p1["next"], p1)
        return p1, prop

    def more(self, prop):
        with FakeBroker() as b:
            p2 = tap(prop, "More")
            return p2, b.proposal()

    def test_control_page_two_sets_aside_both(self):
        p1, prop = self.page1()
        more = next(x["call"] for x in prop["buttons"] if x["label"] == "More")
        self.assertEqual(more["arguments"].get("prev"), p1["render_id"])
        p2, prop2 = self.more(prop)
        mrevs = json.loads(self.conn.execute(
            "SELECT match_revisions_json FROM render_items WHERE render_id=? AND pid=?",
            (p2["render_id"], self.pid)).fetchone()[0])
        self.assertEqual(len(mrevs), 2)          # page 1's candidate + its own
        out, rprop = self.propose("the Adobe one is wrong", quoted=prop2["text"])
        self.assertIsNotNone(out["reading"])
        plan = json.loads(self.conn.execute("SELECT plan_json FROM readings").fetchone()[0])
        self.assertEqual([s["op"] for s in plan], ["set_aside"])
        self.assertEqual(len(plan[0]["bound"]), 2)
        tap(rprop, "Apply")
        self.assertEqual(self.unpairs(), 2)

    def test_refused_updated_page_one_never_feeds_page_two(self):
        import documents
        p1, prop = self.page1()
        documents.update_document_metadata(self.conn, self.docs[0], token=self.token,
                                           document_number="NEW-INVOICE" + "Z" * 40)
        import casa_broker
        import posting
        with mock.patch.object(casa_broker, "deposit",
                               side_effect=casa_broker.DepositFailed("refused")):
            with self.assertRaises(casa_broker.DepositFailed):
                posting.show_view(self.conn, view="item", pid=self.pid)
        refused = self.conn.execute("SELECT * FROM renders ORDER BY rowid DESC LIMIT 1"
                                    ).fetchone()
        self.assertIsNotNone(refused["posted_seq"])
        p2, prop2 = self.more(prop)              # the ORIGINAL page's More
        out, _ = self.propose("the Adobe one is wrong", quoted=prop2["text"])
        self.assertIsNone(out["reading"])
        self.assertEqual(self.readings_count(), 0)
        self.assertEqual(self.unpairs(), 0)

    def readings_count(self):
        return self.conn.execute("SELECT count(*) FROM readings").fetchone()[0]

    def test_page_two_without_prev_binds_only_its_own(self):
        import views
        p1, _ = self.page1()
        nxt = {k: v for k, v in p1["next"].items() if k != "prev"}
        p2 = views.build_review(self.conn, **nxt)
        mrevs = json.loads(self.conn.execute(
            "SELECT match_revisions_json FROM render_items WHERE render_id=? AND pid=?",
            (p2["render_id"], self.pid)).fetchone()[0])
        self.assertEqual(len(mrevs), 1)
        views.mark_rendering_delivered(self.conn, p2["render_id"])
        out, _ = self.propose("the Adobe one is wrong", quoted=views.unesc(p2["text"]))
        self.assertIsNone(out["reading"])
        self.assertEqual(self.unpairs(), 0)

    def test_page_one_seen_at_another_revision_merges_nothing(self):
        import documents
        import views
        p1, prop = self.page1()
        documents.update_document_metadata(self.conn, self.docs[1], token=self.token,
                                           document_number="LATER" + "Q" * 40)
        p2, prop2 = self.more(prop)
        mrevs = json.loads(self.conn.execute(
            "SELECT match_revisions_json FROM render_items WHERE render_id=? AND pid=?",
            (p2["render_id"], self.pid)).fetchone()[0])
        self.assertEqual(len(mrevs), 1)
        out, _ = self.propose("the Adobe one is wrong", quoted=prop2["text"])
        self.assertIsNone(out["reading"])
        self.assertEqual(self.unpairs(), 0)
        self.assertTrue(views)


# ---------------------------------------------------------------------------------------
# Red case 6 — R5 + the d3 amendment: legacy telegram package sends may have gone out
# ---------------------------------------------------------------------------------------
class LegacyReceipt(StoreCase):
    """review_r4.py::test_legacy_receipt, inverted, and review_migration.py (d3)."""

    def _v10(self, status="staged", channel="telegram"):
        from tests.schema_history import build_v10_store
        import db
        import tools
        path = self.tmp / f"v10-{status}-{channel}.sqlite"
        build_v10_store(path, staged_email=True)
        c = sqlite3.connect(path)
        c.execute("UPDATE deliveries SET channel=?, created_at='2026-09-01T00:00:00Z',"
                  " lease_at='2026-09-01T00:00:00Z'", (channel,))
        if status == "uncertain":
            # what v0.9.0's recover_staged left: settled uncertain, its staged copy taken back
            c.execute("UPDATE deliveries SET status='uncertain', settled_at="
                      "'2026-09-02T00:00:00Z', withdrawn_at='2026-09-02T00:00:00Z'")
        c.execute("UPDATE counters SET value=1 WHERE name='pass_generation'")
        c.commit()
        c.close()
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        old = tools._CONN
        tools._CONN = conn
        self.addCleanup(setattr, tools, "_CONN", old)
        return conn

    def delivered(self, c):
        return c.execute("SELECT count(*) FROM deliveries WHERE status='delivered'"
                         ).fetchone()[0]

    def test_a_migrated_staged_send_takes_its_late_receipt(self):
        c = self._v10()
        self.assertEqual(c.execute("SELECT count(*) FROM deliveries WHERE posted_at IS NULL"
                                   ).fetchone()[0], 0)
        call("record_delivery", delivery_id=1, outcome="delivered", message_id="telegram-42")
        self.assertEqual(self.delivered(c), 1)

    def test_recovered_after_the_migration_then_a_late_receipt(self):
        c = self._v10()
        call("job_next", job_id="aaaaaaaa-1")
        self.assertEqual(c.execute("SELECT status FROM deliveries").fetchone()[0], "uncertain")
        call("record_delivery", delivery_id=1, outcome="delivered", message_id="telegram-42")
        self.assertEqual(self.delivered(c), 1)

    def test_recovered_before_the_migration_then_a_late_receipt(self):
        """d3 Astra S2: v0.9.0 recovered the send `uncertain` before the upgrade."""
        c = self._v10(status="uncertain")
        self.assertIsNotNone(c.execute("SELECT posted_at FROM deliveries").fetchone()[0])
        call("record_delivery", delivery_id=1, outcome="delivered", message_id="telegram-42")
        self.assertEqual(self.delivered(c), 1)

    def test_email_sends_are_not_backfilled(self):
        c = self._v10(channel="email")
        self.assertEqual(c.execute("SELECT count(*) FROM deliveries WHERE posted_at IS NOT"
                                   " NULL").fetchone()[0], 0)


class _Long(StoreCase):
    """Fifteen vendors with long document numbers (review_d2.py quote_cycle)."""
    N = 15

    def setUp(self):
        super().setUp()
        import db
        import documents
        import matches
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        cm = self.patch_clock(NOW)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.pids = []
        for i in range(self.N):
            self.row(i + 1, counterparty="Vendor%02d" % i, amount_minor=10000 + i,
                     booking_date="2026-09-14", value_date="2026-09-14")
            pid = self.lineage_for(i + 1)
            self.classify(pid, {"software"})
            self.settle(pid)
            d = self.doc(counterparty="Vendor%02d" % i, issuer="Vendor%02d" % i,
                         amount_minor=10000 + i, document_date="2026-09-14")
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid),
                                 row_snapshot=self.snapshot(pid), token=self.token,
                                 labels=("guessed",))
            self.pids.append(pid)
        self.docs = [r[0] for r in self.conn.execute("SELECT doc_id FROM documents ORDER BY"
                                                      " doc_id")]
        for i, did in enumerate(self.docs):
            documents.update_document_metadata(self.conn, did, token=self.token,
                                               document_number="INV%02d" % i
                                               + "A" * (30 if i == 0 else 45))

    def deliver(self, view="all", **kw):
        import views
        r = views.build_review(self.conn, view=view, quarter="2026-Q3", **kw)
        views.mark_rendering_delivered(self.conn, r["render_id"])
        return r

    def propose(self, text, quoted=None):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, quoted)
        return out, (b.proposal() if b.deposits else None)

    def quote(self, render_id):
        import views
        return (LABEL + views.unesc(self.render_text(render_id)))[:2000]


class RawTruncation(_Long):
    """Red case 13 (V2, Astra d2 S2): two `all` views first differing near Casa's raw
    2,000-character cut: each quote binds exactly its own rendering; no ambiguity, no
    recovery chain."""

    def test_each_quote_binds_its_own_rendering(self):
        import documents
        import views
        self.deliver()
        a = self.deliver()
        documents.update_document_metadata(self.conn, self.docs[-1], token=self.token,
                                           document_number="INV14B" + "A" * 44)
        for _ in range(4):
            b = self.deliver()
            self.assertEqual(views.bound_rendering(self.conn, self.quote(b["render_id"]))
                             ["render_id"], b["render_id"])
            out, _ = self.propose("show older", quoted=self.quote(b["render_id"]))
            self.assertNotIn("more than one version", out.get("say") or "")
        self.assertEqual(views.bound_rendering(self.conn, self.quote(a["render_id"]))
                         ["render_id"], a["render_id"])


class LegacyAmbiguity(_Long):
    """Red case 7 (d1 Terra S1): two seen legacy renderings equal over 2,000 normalised
    characters, differing later in a pairing — refused, never the newest."""
    N = 22

    def test_differing_facts_refuse_with_a_tagged_recovery(self):
        import db
        import views
        self.deliver(page=1)                 # the first review's announcements, once
        a = self.deliver(page=1)
        text_a = views._bnorm(self.text_of(a["render_id"]))
        last = max(self.pids, key=lambda p: text_a.find("Vendor%02d" % (p - self.pids[0])))
        mid = self.conn.execute("SELECT match_id FROM match_state WHERE pid=? AND state IN"
                                " ('matched','proposed')", (last,)).fetchone()[0]
        self.repair_late(last, mid)
        b = self.deliver(page=1)
        self.make_legacy_both(a["render_id"], b["render_id"])
        na, nb = (views._bnorm(self.text_of(r)) for r in (a["render_id"], b["render_id"]))
        self.assertNotEqual(na, nb)
        diff = next(i for i, (x, y) in enumerate(zip(na, nb)) if x != y)
        self.assertGreater(diff, 2000)
        out, _ = self.propose("the Vendor00 one is good", quoted=self.quote(b["render_id"]))
        self.assertIsNone(out["reading"])
        self.assertIn("more than one version", out["say"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 0)
        rec = [i for i in out["instructions"] if isinstance(i, dict) and "show_view" in i]
        self.assertEqual(len(rec), 1)
        with FakeBroker() as br:
            fresh = call("show_view", **rec[0]["show_view"])
            ftext = br.proposal()["text"]
        self.assertTrue(views.unesc(ftext).split("\n")[0].endswith(f" · {fresh['render_id'][1:]}"))
        self.assertEqual(views.bound_rendering(self.conn, LABEL + ftext)["render_id"],
                         fresh["render_id"])
        self.assertTrue(db)

    def repair_late(self, pid, mid):
        import documents
        doc = self.conn.execute("SELECT doc_id FROM match_state WHERE match_id=?",
                                (mid,)).fetchone()[0]
        documents.update_document_metadata(self.conn, doc, token=self.token,
                                           document_number="LATE" + "C" * 44)

    def text_of(self, rid):
        import views
        return views.unesc(self.render_text(rid))

    make_legacy_both = _Q3.make_legacy

    def test_two_s7_renderings_of_one_unchanged_view_differ(self):
        self.deliver(page=1)                 # the first review's announcements, once
        a = self.deliver(page=1)
        b = self.deliver(page=1)
        self.assertNotEqual(self.render_text(a["render_id"]), self.render_text(b["render_id"]))


class SameFactsLegacy(_Q3):
    """Red case 11: identical text and identical binding facts on two legacy rows — one
    binds (the lowest render id), and the plan is the same whichever it is."""

    def test_same_facts_bind_the_lowest(self):
        import db
        import views
        f = self.sheet_fixture()
        src = self.conn.execute("SELECT * FROM renders WHERE render_id=?",
                                (f["render_id"],)).fetchone()
        self.make_legacy(f["render_id"])
        with db.tx(self.conn):
            rid2 = f"r{db.next_seq(self.conn)}"
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " delivered_at, text, membership_json, delivered_seq) VALUES"
                              " (?,?,?,?,?,?,?,?)",
                              (rid2, src["kind"], src["scope_json"], db.now(), db.now(),
                               self.render_text(f["render_id"]), src["membership_json"],
                               db.next_seq(self.conn)))
            for it in self.conn.execute("SELECT * FROM render_items WHERE render_id=?",
                                        (f["render_id"],)).fetchall():
                self.conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                                  " match_revisions_json) VALUES (?,?,?,?)",
                                  (rid2, it["pid"], it["projection_revision"],
                                   it["match_revisions_json"]))
        quote = self.text_of(f["render_id"])
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], f["render_id"])
        out, _ = self.propose("all good", quoted=quote)
        self.assertIsNotNone(out["reading"])
        self.assertEqual(self.conn.execute("SELECT render_id FROM readings").fetchone()[0],
                         f["render_id"])


class IdenticalSheets(_Q3):
    """Red case 8 (d1 Astra S1, review_binding.py identical_render): A delivered, B (the
    same printed text, a different document) refused with posted_seq set."""

    def _a_and_b(self):
        f = self.sheet_fixture()
        doc = self.conn.execute("SELECT * FROM documents WHERE doc_id=?",
                                (f["doc_id"],)).fetchone()
        new = self.repair(f["pid"], doc["document_number"], issuer="Other issuer")
        b = self.refused_show(view="check", quarter="2026-Q3")
        self.assertIsNotNone(b["posted_seq"])
        return f, new, b

    def test_tagged_a_binds_alone_and_refuses_changed_since(self):
        import views
        f, new, b = self._a_and_b()
        body = lambda rid: self.text_of(rid).split("\n", 1)[1]       # noqa: E731
        self.assertEqual(body(f["render_id"]), body(b["render_id"]))  # identical sheets
        quote = self.text_of(f["render_id"])
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], f["render_id"])
        out, _ = self.propose("all good", quoted=quote)
        self.assertIsNone(out["reading"])
        self.assertIn("changed since", out["say"])
        self.assertEqual([self.operator_rows(f["match_id"]),
                          self.operator_rows(new["match_id"])], [0, 0])

    def test_legacy_a_and_b_refuse_then_the_recovery_binds(self):
        import views
        f, new, b = self._a_and_b()
        self.make_legacy(f["render_id"], b["render_id"])
        quote = self.text_of(f["render_id"])
        out, _ = self.propose("all good", quoted=quote)
        self.assertIsNone(out["reading"])
        self.assertIn("more than one version", out["say"])
        self.assertEqual([self.operator_rows(f["match_id"]),
                          self.operator_rows(new["match_id"])], [0, 0])
        rec = next(i["show_view"] for i in out["instructions"]
                   if isinstance(i, dict) and "show_view" in i)
        with FakeBroker() as br:
            fresh = call("show_view", **rec)
            text = br.proposal()["text"]
        self.assertEqual(views.bound_rendering(self.conn, text)["render_id"],
                         fresh["render_id"])


class UnmatchedQuote(_Q3):
    """Red case 9 (d1 Astra S1, review_binding.py missed_quote): no row limit, and an
    explicit quote never falls back to db.last_delivered."""

    def test_an_old_quote_behind_200_deliveries_binds_and_refuses(self):
        import views
        f = self.sheet_fixture()
        old = self.text_of(f["render_id"])
        new = self.repair(f["pid"], "SECOND")
        for _ in range(200):
            r = views.build_review(self.conn, view="check", quarter="2026-Q3")
            views.mark_rendering_delivered(self.conn, r["render_id"])
        self.assertEqual(views.bound_rendering(self.conn, old)["render_id"], f["render_id"])
        out, _ = self.propose("all good", quoted=old)
        self.assertIsNone(out["reading"])
        self.assertIn("changed since", out["say"])
        self.assertEqual([self.operator_rows(f["match_id"]),
                          self.operator_rows(new["match_id"])], [0, 0])

    def test_a_quote_matching_nothing_refuses(self):
        f = self.sheet_fixture()
        out, _ = self.propose("all good", quoted=LABEL + "a message I never sent")
        self.assertIsNone(out["reading"])
        self.assertEqual(self.readings(), 0)
        self.assertIn("I can't find the message you replied to", out["say"])
        self.assertIn({"show_view": {"view": "status"}}, out["instructions"])
        self.assertEqual(self.operator_rows(f["match_id"]), 0)


class LegacyQuarterFacts(_Q3):
    """Red case 15 (V3, Terra d2 S2): two legacy pages, identical text, no `next`, equal
    names and items, different quarter (or item pid) — refused, and typed "more" never
    opens the other one."""

    def _two(self, scopes, kind="status"):
        import db
        rids = []
        with db.tx(self.conn):
            for scope in scopes:
                rid = f"r{db.next_seq(self.conn)}"
                self.conn.execute("INSERT INTO renders(render_id, kind, scope_json,"
                                  " created_at, delivered_at, text, membership_json,"
                                  " delivered_seq) VALUES (?,?,?,?,?,?, '[]', ?)",
                                  (rid, kind, db.canonical(scope), db.now(), db.now(),
                                   "Your books\nNothing is missing.", db.next_seq(self.conn)))
                rids.append(rid)
        return rids

    def test_different_quarters_refuse(self):
        self.bind()
        self._two([{"quarter": "2026-Q2"}, {"quarter": "2026-Q3"}])
        out, _ = self.propose("more", quoted="Your books\nNothing is missing.")
        self.assertIsNone(out["reading"])
        self.assertIn("more than one version", out["say"])
        self.assertEqual(out["instructions"], [{"show_view": {"view": "status"}}])

    def test_different_item_pids_refuse(self):
        self.bind()
        self._two([{"quarter": "2026-Q3", "pid": 1}, {"quarter": "2026-Q3", "pid": 2}],
                  kind="item")
        out, _ = self.propose("more", quoted="Your books\nNothing is missing.")
        self.assertIn("more than one version", out["say"])
        self.assertEqual(out["instructions"], [{"show_view": {"view": "status"}}])

    def test_reply_reads_exactly_the_v3_fields(self):
        """V3: every grammar-read scope field is a binding fact. A new read joins the list."""
        import views
        tree = ast.parse((ROOT / "server" / "reply.py").read_text("utf-8"))
        reads = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "scope" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                reads.add(node.args[0].value)
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                    and node.value.id == "scope" and isinstance(node.slice, ast.Constant):
                reads.add(node.slice.value)
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) \
                    and any(isinstance(c, ast.Name) and c.id == "scope"
                            for c in node.comparators):
                reads.add(node.left.value)
        self.assertEqual(reads, set(views.FACT_FIELDS))
        self.assertEqual(set(views.FACT_FIELDS), {"names", "refs", "proposed", "offers",
                                                  "next", "walk", "quarter", "pid"})


# ---------------------------------------------------------------------------------------
# Red case 10 — d1 Astra S2: broad rules bind by provenance; no re-show livelock
# ---------------------------------------------------------------------------------------
class BroadProvenance(StoreCase):
    def setUp(self):
        super().setUp()
        from tests.test_reply import Base
        self.base = Base
        import db
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        self.n = 0
        cm = self.patch_clock(NOW)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    item = __import__("tests.test_reply", fromlist=["Base"]).Base.item

    def item_view(self, pid):
        import views
        r = views.build_review(self.conn, view="item", pid=pid)
        views.mark_rendering_delivered(self.conn, r["render_id"])
        return r

    def propose(self, text, quoted=None):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, quoted)
        return out, (b.proposal() if b.deposits else None)

    def test_class_rule_lists_its_effect_and_applies_once(self):
        pids = [self.item("Bank %d" % i, 1000 + i, "2026-09-17", paired=False,
                          tags=("fees",)) for i in range(2)]
        self.item_view(pids[0])
        out, prop = self.propose("statements don't matter")
        self.assertIsNotNone(out["reading"])
        self.assertEqual(out["reshow"], [])
        self.assertIn("Bank 0", prop["text"])
        self.assertIn("Bank 1", prop["text"])
        tap(prop, "Apply")
        # one Apply: the class's chain overrides once each (statements: fees, interest, tax)
        import reply
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chain_overrides WHERE"
                                           " scope='fees'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chain_overrides").fetchone()[0],
                         len(reply.CLASS_SCOPES["statements"]))

    def test_class_rule_without_provenance_applies_nothing(self):
        self.item("Bank 0", 1000, "2026-09-17", paired=False, tags=("fees",))
        sw = self.item("Zapier", 9900, "2026-09-17", paired=False)
        self.item_view(sw)
        out, _ = self.propose("statements don't matter")
        self.assertIsNone(out["reading"])
        self.assertEqual(out["reshow"], [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chain_overrides"
                                           ).fetchone()[0], 0)

    def test_never_rule_lists_its_effect(self):
        pids = [self.item("Adobe", 1000 + i, "2026-09-1%d" % (i + 6), paired=False)
                for i in range(2)]
        self.item_view(pids[0])
        out, prop = self.propose("no invoices ever for Adobe")
        self.assertIsNotNone(out["reading"])
        self.assertEqual(out["reshow"], [])
        self.assertIn("10.01", prop["text"])
        tap(prop, "Apply")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM counterparties WHERE"
                                           " exp_author='operator'").fetchone()[0], 1)

    def test_never_rule_without_provenance_applies_nothing(self):
        self.item("Adobe", 1000, "2026-09-16", paired=False)
        sw = self.item("Zapier", 9900, "2026-09-17", paired=False)
        self.item_view(sw)
        out, _ = self.propose("no invoices ever for Adobe")
        self.assertIsNone(out["reading"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM counterparties WHERE"
                                           " exp_author='operator'").fetchone()[0], 0)

    def test_identity_lists_its_effect(self):
        pids = [self.item("ACME BV 123", 1000 + i, "2026-09-1%d" % (i + 6), paired=False)
                for i in range(2)]
        self.item_view(pids[0])
        out, prop = self.propose("the acme bv 123 one is my landlord")
        self.assertIsNotNone(out["reading"], out)
        self.assertEqual(out["reshow"], [])
        self.assertIn("10.01", prop["text"])
        tap(prop, "Apply")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM counterparties WHERE"
                                           " name='my landlord'").fetchone()[0], 1)


# ---------------------------------------------------------------------------------------
# Red case 14 — V2 tag mechanics
# ---------------------------------------------------------------------------------------
class Tags(_Q3):
    def test_every_view_kind_ends_its_first_line_with_its_sequence(self):
        import views
        f = self.sheet_fixture()
        for view in views.VIEWS:
            kw = {"pid": f["pid"]} if view == "item" else {"quarter": "2026-Q3"}
            r = views.build_review(self.conn, view=view, **kw)
            first = r["text"].split("\n")[0]
            with self.subTest(view=view):
                self.assertTrue(first.endswith(f" · {r['render_id'][1:]}"), first)
                for word in views.FORBIDDEN:
                    self.assertNotIn(word, first)
                self.assertEqual(views.esc(views.unesc(first)), first)
                self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)

    def test_the_fit_never_cuts_the_tag(self):
        import views
        self.sheet_fixture()
        self.patch(views, "BODY_LIMIT", 60)
        r = views.build_review(self.conn, view="status", quarter="2026-Q3")
        self.assertTrue(r["text"].split("\n")[0].endswith(f" · {r['render_id'][1:]}"),
                        r["text"])
        self.assertLessEqual(views.utf16_len(r["text"]), 60)

    def test_a_repost_keeps_the_tag(self):
        f = self.sheet_fixture()
        with FakeBroker() as b:
            call("show_view", render_id=f["render_id"])
            text = b.proposal()["text"]
        self.assertTrue(text.split("\n")[0].endswith(f" · {f['render_id'][1:]}"), text)


# ---------------------------------------------------------------------------------------
# R4 — quoted "send it again" binds the bound rendering's own offers
# ---------------------------------------------------------------------------------------
class QuotedResend(_Q3):
    def test_a_quote_offering_nothing_says_so(self):
        f = self.sheet_fixture()
        out, _ = self.propose("send it again", quoted=self.text_of(f["render_id"]))
        self.assertNotIn("resend", out["instructions"])
        self.assertIn("that message offers no package to send again", out["say"])

    def test_resend_target_reads_the_named_rendering(self):
        import db
        import delivery
        f = self.sheet_fixture()
        with self.assertRaises(db.Refusal) as cm:
            delivery.resend_target(self.conn, f["render_id"])
        self.assertIn("that message offers no package to send again", str(cm.exception))

    def test_a_quote_offering_a_package_names_it(self):
        import db
        f = self.sheet_fixture()
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (f["render_id"],)).fetchone()[0])
        scope["offers"] = [7]
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                              (db.canonical(scope), f["render_id"]))
        out, _ = self.propose("send it again", quoted=self.text_of(f["render_id"]))
        self.assertIn({"stage_for_delivery": {"resend": True, "render_id": f["render_id"]}},
                      out["instructions"])


# ---------------------------------------------------------------------------------------
# r5 (Astra S2) — a rendering that LACKS a field a clause reads refuses that clause
# ---------------------------------------------------------------------------------------
class LegacyFields(_Q3):
    """Round 5 ruling: a reading bound to a view rendering whose scope lacks a grammar-read
    field (views.FACT_FIELDS) refuses the clause that reads it in plain words, with a fresh
    show_view of that rendering's view and quarter (pid for an item) as the recovery. An
    explicitly present empty value stays empty. Generalises r3 #4's legacy `next`."""

    def _legacy(self, rid, keep=("quarter", "pid", "names", "refs")):
        """What v0.9.0 (a404990) stored for a view: no first-line tag; quarter and pid, and
        names/refs only when non-empty — no proposed, next, offers or walk."""
        import db
        self.make_legacy(rid)
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        old = {k: v for k, v in scope.items() if k in keep and (k in ("quarter", "pid") or v)}
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                              (db.canonical(old), rid))

    def _migrated(self):
        """The store copied into the v10 schema and opened (10 -> 11), as
        review_migrated_sheet.py does."""
        import db
        import tools
        from tests.schema_history import DDL_V10
        path = self.tmp / "schema10.sqlite"
        c = sqlite3.connect(path)
        c.executescript(DDL_V10)
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                          " AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            names = ",".join(cols)
            rows = self.conn.execute(f"SELECT {names} FROM {table}").fetchall()
            c.execute(f"DELETE FROM {table}")
            c.executemany(f"INSERT INTO {table} ({names}) VALUES"
                          f" ({','.join('?' for _ in cols)})", [tuple(r) for r in rows])
        c.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
        c.commit()
        c.close()
        migrated = db.open_store(path)
        self.addCleanup(migrated.close)
        old = tools._CONN
        tools._CONN = migrated
        self.addCleanup(setattr, tools, "_CONN", old)
        return migrated

    def test_all_good_on_a_migrated_v090_sheet_refuses_with_a_recovery(self):
        """Astra r5 S2 (review_migrated_sheet.py): the sheet has no `proposed`."""
        import views
        f = self.sheet_fixture()
        self._legacy(f["render_id"])
        quote = self.text_of(f["render_id"])
        m = self._migrated()
        with FakeBroker() as b:
            out = call("propose_reading", text="all good", quoted=quote)
            self.assertEqual(len(b.deposits), 0)
            self.assertEqual(m.execute("SELECT count(*) FROM readings").fetchone()[0], 0)
            self.assertEqual(m.execute("SELECT count(*) FROM log WHERE author='operator'"
                                       ).fetchone()[0], 0)
            rec = [i["show_view"] for i in out["instructions"]
                   if isinstance(i, dict) and "show_view" in i]
            self.assertEqual(rec, [{"view": "check", "quarter": "2026-Q3"}], out)
            self.assertIn(views.LACKS, out["say"])
            call("show_view", **rec[0])                       # the recovery, then its quote
            out = call("propose_reading", text="all good",
                       quoted=views.unesc(b.proposal()["text"]))
            self.assertIsNotNone(out["reading"])
            tap(b.proposal(), "Apply")
        self.assertEqual(m.execute("SELECT count(*) FROM readings").fetchone()[0], 1)
        self.assertEqual(m.execute("SELECT count(*) FROM log WHERE author='operator'"
                                   ).fetchone()[0], 1)

    def test_a_named_verdict_on_a_rendering_without_names(self):
        import db
        import views
        f = self.sheet_fixture()
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (f["render_id"],)).fetchone()[0])
        del scope["names"]
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                              (db.canonical(scope), f["render_id"]))
        out, _ = self.propose("the Zapier one is wrong", quoted=self.text_of(f["render_id"]))
        self.assertIsNone(out["reading"])
        self.assertIn(views.LACKS, out["say"])
        self.assertIn({"show_view": {"view": "check", "quarter": "2026-Q3"}},
                      out["instructions"])
        self.assertEqual(self.operator_rows(), 0)

    def test_an_item_rendering_without_refs_recovers_its_item(self):
        import views
        f = self.sheet_fixture()
        r = views.build_review(self.conn, view="item", pid=f["pid"])
        views.mark_rendering_delivered(self.conn, r["render_id"])
        self._legacy(r["render_id"])
        out, _ = self.propose("the Zapier one is wrong", quoted=self.text_of(r["render_id"]))
        self.assertIsNone(out["reading"])
        self.assertIn({"show_view": {"view": "item", "quarter": "2026-Q3", "pid": f["pid"]}},
                      out["instructions"])
        self.assertEqual(self.operator_rows(), 0)

    def test_a_present_empty_value_stays_empty(self):
        import db
        f = self.sheet_fixture()
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (f["render_id"],)).fetchone()[0])
        self.assertIn("names", scope)
        self.assertIn("refs", scope)                  # composed explicitly, even when empty
        self.assertIn("offers", scope)
        scope["names"] = {}
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                              (db.canonical(scope), f["render_id"]))
        out, prop = self.propose("the Zapier one is wrong", quoted=self.text_of(f["render_id"]))
        self.assertIsNotNone(out["reading"])          # stored names still resolve it
        self.assertEqual(scope["refs"], {})

    def test_every_view_composes_every_grammar_field(self):
        import views
        f = self.sheet_fixture()
        for view in views.VIEWS:
            kw = {"pid": f["pid"]} if view == "item" else {"quarter": "2026-Q3"}
            r = views.build_review(self.conn, view=view, **kw)
            scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                                 " render_id=?", (r["render_id"],)).fetchone()[0])
            with self.subTest(view=view):
                self.assertEqual(set(views.FACT_FIELDS) - set(scope), set())


# ---------------------------------------------------------------------------------------
# r6 (Astra S2) — Casa's label is stripped from the quote only, never from a stored body
# ---------------------------------------------------------------------------------------
class LabelOnlyOnTheQuote(_Q3):
    """review_label_control.py: a payee named "📊 Analytics" heads its item view; the stored
    body's first line (its heading, with the tag) is data, not Casa's label. The quote is
    Casa's label line + the deposited text, unescaped (what tg_richtext renders)."""

    def test_a_payee_that_looks_like_the_label_binds(self):
        import views
        f = self.sheet_fixture(payee="\U0001f4ca Analytics")
        with FakeBroker() as b:
            s = call("show_view", view="item", pid=f["pid"])
            quote = LABEL + views.unesc(b.proposal()["text"])
            call("mark_rendering_delivered", render_id=s["render_id"])
            self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"],
                             s["render_id"])
            out = call("propose_reading", text="the \U0001f4ca Analytics one is wrong",
                       quoted=quote)
            self.assertIsNotNone(out["reading"], out)
            self.assertEqual(self.readings(), 1)
            tap(b.proposal(), "Apply")
        self.assertEqual(self.operator_rows(), 1)

    def test_the_label_is_dropped_only_from_the_quote(self):
        import views
        body = "\U0001f4ca Analytics \u00b7 EUR 99.00 \u00b7 17 Sep \u00b7 12\nmore"
        self.assertEqual(views._qnorm(LABEL + body), views._bnorm(body))
        self.assertTrue(views._bnorm(body).startswith("\U0001f4ca Analytics"))


# ---------------------------------------------------------------------------------------
# r7 (Astra S2) — Casa's clip marker on a long quote is undone, like its label
# ---------------------------------------------------------------------------------------
CASA_QUOTE_CHARS = 2000      # casa specialist_desk.py:48 DESK_QUOTE_CHARS (bcebd66b)
CASA_CLIP = "[\u2026]"       # casa specialist_desk.py:52 CLIP


def casa_clip(text, limit=CASA_QUOTE_CHARS):
    """A copy of casa specialist_desk.py:78-84 clip(): what Casa puts in the desk context
    as the quoted post (specialist_desk.py:692)."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:max(limit - len(CASA_CLIP), 0)] + CASA_CLIP


class ClippedQuote(_Long):
    """review_casa_quote.py: "all good" quoting a page over 2,000 characters — Casa sends
    its first 1,997 characters and "[…]". It binds that page; 15 confirmations."""

    def test_a_clipped_quote_binds_its_page(self):
        import views
        with FakeBroker() as b:
            r = call("show_view", view="all", quarter="2026-Q3", page=1)
            call("mark_rendering_delivered", render_id=r["render_id"])
            raw = LABEL + views.unesc(b.proposal()["text"])
            quote = casa_clip(raw)
            self.assertGreater(len(raw), CASA_QUOTE_CHARS)
            self.assertTrue(quote.endswith(CASA_CLIP))
            self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"],
                             r["render_id"])
            out = call("propose_reading", text="all good", quoted=quote)
            self.assertIsNotNone(out["reading"], out)
            self.assertEqual(self.conn.execute("SELECT render_id FROM readings").fetchone()[0],
                             r["render_id"])
            tap(b.proposal(), "Apply")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 15)

    def test_a_legacy_body_binds_as_it_was_deposited(self):
        """Found by the r7 gate check (legacy:ctrl): a pre-S7 body's control characters are
        spaces in what was posted (views.deposit_safe, §7.6), so in Casa's quote too."""
        import db
        import views
        text = "Accounting \u00b7 Q3 2026\nACME\x01Corp owes 12.00\x7f and\x1bmore"
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " delivered_at, text, membership_json, delivered_seq) VALUES"
                              " ('r9000', 'status', '{}', 'x', 'x', ?, '[]', ?)",
                              (text, db.next_seq(self.conn)))
        quote = casa_clip(LABEL + views.unesc(views.deposit_safe(text)))
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], "r9000")

    def test_only_casa_s_own_clip_is_undone(self):
        import views
        body = "x" * 3000
        self.assertEqual(views._qnorm(casa_clip(body)), "x" * (CASA_QUOTE_CHARS - 3))
        short = "a sheet that ends with [\u2026]"          # under the cap: text, kept
        self.assertEqual(views._qnorm(short), short)


# ---------------------------------------------------------------------------------------
# Red case 17 — grep pins; §1's one "seen" predicate
# ---------------------------------------------------------------------------------------
class GrepPins(unittest.TestCase):
    def test_no_step_reads_shown(self):
        for f in ("reply.py", "authorship.py", "kb.py", "matches.py", "taps.py",
                  "delivery.py", "posting.py"):
            src = (ROOT / "server" / f).read_text("utf-8")
            with self.subTest(file=f):
                self.assertIsNone(re.search(r"FROM\s+shown\b|JOIN\s+shown\b", src))
        for f in (ROOT / "server").glob("*.py"):
            self.assertNotIn('bind="shown"', f.read_text("utf-8"), f.name)

    def test_bound_rendering_has_no_row_limit(self):
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        import views
        self.assertNotIn("LIMIT", inspect.getsource(views.bound_rendering))

    def test_one_seen_predicate(self):
        import db
        self.assertTrue(db.seen_render({"delivered_at": "x", "posted_seq": None}))
        self.assertTrue(db.seen_render({"delivered_at": None, "posted_seq": 4}))
        self.assertFalse(db.seen_render({"delivered_at": None, "posted_seq": None}))


class KbProvenance(_Q3):
    """§2 #11: an operator rule's provenance is a SEEN rendering (delivered or posted)."""

    def test_posted_only_rendering_is_provenance(self):
        import authority
        import db
        import kb
        f = self.sheet_fixture()
        with FakeBroker():
            sh = call("show_view", view="check", quarter="2026-Q3")
        with db.tx(self.conn), authority.rehearsal(self.conn) as g:
            kb.set_expectation_in_tx(self.conn, scope_type="counterparty", scope="Zapier",
                                     kind="none", author="operator", render_id=sh["render_id"],
                                     grant=g)
        rid = self.insert_render("check", "never shown")
        with db.tx(self.conn), authority.rehearsal(self.conn) as g:
            with self.assertRaises(db.Refusal):
                kb.set_expectation_in_tx(self.conn, scope_type="counterparty", scope="Zapier",
                                         kind="none", author="operator", render_id=rid,
                                         grant=g)
        self.assertTrue(f)


if __name__ == "__main__":
    unittest.main()
