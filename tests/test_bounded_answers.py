# tests/test_bounded_answers.py
"""Every list answer fits what the agent can read (issue #3). A real quarter's
sweep continuation answered 62,8xx characters: Claude Code saved it to a file the
agent could not read, and with it the only copy of the rotated pass token. Driven
through qa_server.handle against the real bank-feed, over worst-case items: long
bank texts (one remittance of 24,000 characters), 50 long recorded queries each,
a long KB hint and link, documents with long fields and many collisions, pairings
with long runners-up and rationale and repeated labels.

Revision 3 of the design: every listed item is bounded by construction, the facts
a match compares are handed out as a digest, and every paged list is a stateless
cursor over an order that never changes (pids, doc_ids)."""
import json
import unittest

from unittest import mock

from tests.test_ledger import RealLedger
import budget  # noqa: E402
import db  # noqa: E402
import kb  # noqa: E402
import lineage  # noqa: E402
import work  # noqa: E402

N = 60
NAME = "Adobe Systems Software Ireland Ltd — Ünïcødé Døcumént Cloud Subscriptions " * 2
REMITTANCE = "Factuur IE-" + "0123456789 abonnement Creative Cloud Pro " * 4
OTHER = "Other Vendor BV"
HUGE = "X" * 24_000                       # C1 (Astra): one exact bank field over the page


class Bounded(RealLedger):
    def setUp(self):
        super().setUp()
        bf = self.bf
        bf.fetch([bf.row(f"2026-07-{1 + i % 28:02d}", ref=f"R{i}", amount=1000 + i,
                         counterparty=OTHER if i % 2 else NAME[:140],
                         remittance=HUGE if i == 7 else REMITTANCE[:140])
                  for i in range(N)])
        for r in self.active():
            self.tag(r["row_id"], "software")
        self.first_pass()
        kb.upsert_counterparty(self.conn, "Adobe " + "x" * 400, patterns=[NAME[:140]],
                               source="email", search_hint="from:adobe.com " * 60,
                               document_link="https://example.invalid/")
        with db.tx(self.conn):          # a link as an earlier version could store it
            self.conn.execute("UPDATE counterparties SET document_link=?",
                              ("https://example.invalid/" + "y" * 30_000,))
        self.t1 = self.begin()
        pids = sorted(d["pid"] for d in work.triage(self.conn))
        self.assertEqual(len(pids), N)
        for pid in pids:                                # 50 long queries each
            self.call("record_search", pid=pid, pass_token=self.t1,
                      queries=[f"q{j} " + "has:attachment adobe invoice " * 12 for j in range(50)])
        self.pids = pids
        self.huge = next(p for p in pids if work.describe(self.conn, p)["amount_minor"] == 1007)
        self.assertEqual(work.describe(self.conn, self.huge)["row_snapshot"]["remittance"], HUGE)

    def test_the_items_are_worst_case(self):
        # without the listing shapes, the same items would not fit one answer
        full = [work.describe(self.conn, pid) for pid in self.pids[:50]]
        self.assertGreater(budget.size(full), 5 * budget.RESULT_LIMIT)
        self.assertGreater(budget.size(work.describe(self.conn, self.huge)),
                           budget.RESULT_LIMIT)

    def page(self, after=None, **args):
        text = self.text("list_quarter_state", **args, **({"after": after} if after else {}))
        self.assertLessEqual(len(text), budget.RESULT_LIMIT)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise AssertionError(text) from None

    def pages(self, between=None, **args):
        """Follow `next` to the end; `between(n)` runs after page n. Returns the pids
        listed, in order, and how many pages it took."""
        seen, after, n = [], None, 0
        while True:
            out = self.page(after, **args)
            items = out["triage" if args.get("triage") else "items"]
            self.assertTrue(items)
            seen += [d["pid"] for d in items]
            self.assertEqual(out["remaining"] > 0, out["next"] is not None)
            n += 1
            if out["next"] is None:
                return seen, n
            if between:
                between(n, seen)
            after = out["next"]

    def test_triage_pages_fit_and_visit_every_item_once(self):
        seen, n = self.pages(triage=True, pass_token=self.t1)
        self.assertEqual(seen, self.pids)
        self.assertGreater(n, 1)
        item = self.page(triage=True)["triage"][0]
        self.assertEqual(item["search"]["query_count"], 50)
        self.assertEqual(len(item["search"]["last_queries"]), 3)
        self.assertEqual(len(item["row_digest"]), 64)
        self.assertNotIn("row_snapshot", item)

    def test_a_page_asked_again_is_the_same_page(self):
        # D2 (Astra, Terra): a lost answer is replayed by asking again, never consumed
        first = self.page(triage=True, pass_token=self.t1)
        again = self.page(first["next"], triage=True, pass_token=self.t1)
        self.assertEqual(again, self.page(first["next"], triage=True, pass_token=self.t1))

    def test_changes_between_pages_skip_nothing(self):
        # C1 (Astra): a set_expectation between pages hid an unvisited payment. Half
        # the payments start optional; after page 1 they turn required (a priority
        # order would now put the unvisited ones before the cursor), and the last
        # payment page 1 listed leaves triage.
        kb.set_expectation(self.conn, scope_type="counterparty", scope=OTHER,
                           kind="invoice", tier="optional", author="specialist", token=self.t1)
        gone = []

        def between(n, seen):
            if n != 1:
                return
            kb.set_expectation(self.conn, scope_type="counterparty", scope=OTHER,
                               kind="invoice", tier="required", author="specialist",
                               token=self.t1)
            with db.tx(self.conn):          # handled in between (as stop_chasing does)
                self.conn.execute("UPDATE projections SET search_state='accepted-missing'"
                                  " WHERE pid=?", (seen[-1],))
                lineage.settle(self.conn, seen[-1])
            gone.append(seen[-1])
        seen, _ = self.pages(between=between, triage=True, pass_token=self.t1)
        self.assertTrue(gone)
        self.assertEqual({work.describe(self.conn, p)["expectation"]["tier"]
                          for p in self.pids if p not in gone}, {"required"})
        self.assertEqual(seen, self.pids)          # page 1 listed `gone` before it left

    def test_the_payment_reference_is_listed(self):
        # C2 (Astra): the reference tells identical payments apart
        item = self.page(pid=self.pids[0])["item"]
        self.assertEqual(item["remittance"], REMITTANCE[:140])
        huge = self.page(pid=self.huge)["item"]["remittance"]
        self.assertEqual((len(huge), huge[-1]), (200, "…"))

    def test_one_item_is_reread_by_pid(self):
        pid = self.pids[3]
        one = self.page(pid=pid)["item"]
        listed = next(d for d in self.page(triage=True)["triage"] if d["pid"] == pid)
        self.assertEqual(one, listed)

    def test_the_huge_field_is_clipped_and_the_digest_still_binds_it(self):
        item = self.page(pid=self.huge)["item"]
        self.assertLessEqual(budget.size(item), budget.PAGE_BUDGET)
        doc = self.file(amount_minor=1007, document_date="2026-07-08")
        out = self.call("record_match", pid=item["pid"], doc_id=doc, author="auto",
                        expected_revision=item["revision"], row_digest=item["row_digest"],
                        pass_token=self.t1, document_date="2026-07-01")
        self.assertEqual(out["state"], "matched")

    def test_quarter_pages_fit_and_visit_every_item_once(self):
        for i, pid in enumerate(self.pids[:10]):       # long runners-up, rationale, labels
            doc = self.file(amount_minor=1000 + i, document_date="2026-07-01",
                            issuer="I" * 500, recipient="R" * 500,
                            document_number="N" * 297 + "%03d" % i)
            d = self.page(pid=pid)["item"]
            self.call("propose_match", pid=pid, doc_id=doc, expected_revision=d["revision"],
                      row_digest=d["row_digest"], pass_token=self.t1,
                      document_date="2026-07-01", labels=["guessed"] * 3000,
                      runners_up=["r" * 300] * 20, rationale="because " * 300)
        self.assertEqual(self.conn.execute("SELECT DISTINCT label FROM matches").fetchall()[0][0],
                         "guessed")                    # stored once from now on
        with db.tx(self.conn):          # labels repeated as an earlier version stored them
            self.conn.execute("UPDATE matches SET label=?", (",".join(["guessed"] * 3000),))
        seen, n = self.pages(quarter="2026-Q3")
        self.assertEqual(seen, self.pids)
        out = self.call("list_quarter_state", quarter="2026-Q3")
        self.assertEqual(out["total"], N)
        self.assertEqual(sum(out["counts"].values()), N)
        cur = self.page(pid=self.pids[0])["item"]["current"]
        self.assertEqual(cur["labels"], ["guessed"])

    def test_unmatched_documents_page_and_fit(self):
        for i in range(N):              # same issuer and number: each collides with all
            self.file(amount_minor=5000 + i, document_date="2026-07-01", issuer="I" * 2000,
                      counterparty="C" * 2000, recipient="R" * 2000, document_number="N" * 2000)
        seen, after = [], None
        while True:
            text = self.text("list_unmatched_documents", limit=500,
                             **({"after": after} if after else {}))
            self.assertLessEqual(len(text), budget.RESULT_LIMIT)
            out = json.loads(text)
            self.assertEqual(out["total"], N)
            seen += [d["doc_id"] for d in out["documents"]]
            if out["next"] is None:
                self.assertEqual(out["remaining"], 0)
                break
            after = out["next"]
        self.assertEqual(len(seen), N)
        self.assertEqual(seen, sorted(set(seen)))
        d = out["documents"][0]
        self.assertEqual((len(d["collisions"]), d["collision_count"]), (5, N - 1))

    def test_an_oversized_item_is_an_error_not_an_answer(self):
        with mock.patch.object(budget, "PAGE_BUDGET", 300):
            out = self.text("list_quarter_state", triage=True)
        self.assertTrue(out.startswith("error: Oversized: payment #"), out)


class TestConstruction(unittest.TestCase):
    """The largest item the clips allow is well under one page: a page never has to
    refuse an item a reachable store holds (issue #3, revision 2)."""
    def test_the_largest_listed_item_fits_a_page(self):
        for long in ("Ü" * 100_000, "\x01" * 100_000):     # a control character renders as 6
            self._largest(long)

    def _largest(self, long):
        doc = {"doc_id": 1, "kind": long, "issuer": long, "number": long, "date": long,
               "amount_minor": 1, "currency": long, "recipient": long, "sha256": long}
        match = {"match_id": 1, "revision": 1, "state": long, "author": long,
                 "labels": ["guessed", "no-ref", "partial-search", "recipient?"] * 100,
                 "runners_up": [long] * 50, "rationale": long, "document": doc}
        d = {"pid": 1, "revision": 1, "status": long, "ended": long,
             "reasons": ["unclassified", "conflicted", "kind-mismatch", long],
             "date": long, "quarter": long, "amount_minor": 1, "currency": long,
             "direction": long, "pending": False, "counterparty": long,
             "bank_counterparty": long,
             "expectation": {"kind": long, "tier": long, "row": 11}, "last_known_kind": long,
             "current": match, "candidates": [match] * 50, "search_state": long,
             "search": {"queries": [long] * 50, "exhausted": True, "incomplete": True,
                        "last_searched_at": long},
             "identity_question": True, "link": long, "search_hint": long, "window_days": 10,
             "portal": False, "class_observed_at": long, "unprojectable": long, "fresh": True,
             "broken_floor": long, "row_snapshot": {"counterparty": long, "remittance": long}}
        self.assertLessEqual(budget.size(work.listed(d)), budget.PAGE_BUDGET * 3 // 4)


class TestPage(unittest.TestCase):
    def test_a_page_stops_at_the_count_or_the_budget(self):
        items = [{"t": "x" * 100}] * 10
        self.assertEqual(budget.page(items, 3), (items[:3], 7))
        shown, rest = budget.page(items, 10, budget=350)
        self.assertEqual((len(shown), rest), (3, 7))
        self.assertLessEqual(budget.size(shown), 350)
        with self.assertRaises(budget.Oversized):
            budget.page([{"t": "x" * 1000}], 5, budget=10)

    def test_clip_and_bounded(self):
        self.assertEqual(budget.clip("abcdef", 4), "abc…")
        self.assertEqual(budget.clip("abc", 4), "abc")
        self.assertIsNone(budget.clip(None, 4))
        self.assertEqual(budget.clip("\x01" * 10, 13), "\x01\x01…")   # 6 + 6 + 1 rendered
        self.assertEqual(budget.bounded({"a": ["xxxxx", {"b": "yyyyy"}], "c": "zzzzz", "n": 5},
                                        3, longer={"c": 4}),
                         {"a": ["xx…", {"b": "yy…"}], "c": "zzz…", "n": 5})
