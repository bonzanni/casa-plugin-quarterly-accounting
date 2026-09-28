# tests/test_bounded_answers.py
"""Every list answer fits what the agent can read (issue #3). A real quarter's
sweep continuation answered 62,8xx characters: Claude Code saved it to a file the
agent could not read, and with it the only copy of the rotated pass token. Driven
through qa_server.handle against the real bank-feed, over worst-case items: long
bank texts, 50 long recorded queries each, a long KB hint, documents with long
fields, matches with long runners-up and rationale."""
import json
from unittest import mock

from tests.test_continuation import Flow
import budget  # noqa: E402
import db  # noqa: E402
import kb  # noqa: E402
import lineage  # noqa: E402
import work  # noqa: E402

N = 60
NAME = "Adobe Systems Software Ireland Ltd — Ünïcødé Døcumént Cloud Subscriptions " * 2
REMITTANCE = "Factuur IE-" + "0123456789 abonnement Creative Cloud Pro " * 4


class Bounded(Flow):
    def setUp(self):
        super().setUp()
        bf = self.bf
        bf.fetch([bf.row(f"2026-07-{1 + i % 28:02d}", ref=f"R{i}", amount=1000 + i,
                         counterparty=NAME[:140], remittance=REMITTANCE[:140])
                  for i in range(N)])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        kb.upsert_counterparty(self.conn, "Adobe " + "x" * 400, patterns=[NAME[:140]],
                               source="email", search_hint="from:adobe.com " * 60,
                               document_link="https://example.invalid/" + "y" * 300)
        self.t1 = self.begin()
        self.start(self.t1)
        self.probe_import(self.t1)
        self.sweep(self.t1)
        pids = [d["pid"] for d in work.triage(self.conn)]
        self.assertEqual(len(pids), N)
        for pid in pids:                                # 50 long queries each
            self.call("record_search", pid=pid, pass_token=self.t1,
                      queries=[f"q{j} " + "has:attachment adobe invoice " * 12 for j in range(50)])
        self.pids = pids

    def finish(self):
        tri = self.call("list_quarter_state", triage=True, pass_token=self.t1)
        out = self.call("record_step", pass_token=self.t1, step="sweep", action="finish",
                        remaining_in_cycle=0, triage_remaining=tri["remaining"])
        self.assertTrue(out["finished"], out)

    def test_the_items_are_worst_case(self):
        # without the listing shapes, the same items would not fit one answer
        full = [work.describe(self.conn, pid) for pid in self.pids[:work.TRIAGE_LIMIT]]
        self.assertGreater(budget.size(full), 5 * budget.RESULT_LIMIT)

    def test_the_continuation_fits_and_carries_the_token(self):
        self.finish()
        text = self.text("continue_pass")
        self.assertLessEqual(len(text), budget.RESULT_LIMIT)
        c = json.loads(text)["continue"]
        self.assertNotEqual(c["pass_token"], self.t1)
        self.assertEqual(c["next"], "gmail-round")
        w = c["work"]
        self.assertEqual(len(w["triage"]) + w["remaining"], w["total"])
        self.assertEqual(w["total"], N)
        # worst-case items (every clip at its full length) still fill most of a page;
        # what is left out is `remaining`, counted not searched
        self.assertGreaterEqual(len(w["triage"]), 25)
        self.assertEqual([d["pid"] for d in w["triage"]], self.pids[:len(w["triage"])])
        # the new token works: the Gmail round and the end
        self.call("record_search", pid=w["triage"][0]["pid"], pass_token=c["pass_token"],
                  incomplete=True)
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted",
                        report={"checked": 0, "total": N, "not_searched": N})
        self.assertEqual(end["outcome"], "interrupted")

    def pages(self, **args):
        seen, after, n = [], None, 0
        while True:
            text = self.text("list_quarter_state", **args, **({"after": after} if after else {}))
            self.assertLessEqual(len(text), budget.RESULT_LIMIT, n)
            out = json.loads(text)
            items = out["triage" if args.get("triage") else "items"]
            self.assertTrue(items)
            seen += [d["pid"] for d in items]
            self.assertEqual(out["remaining"] > 0, out["next"] is not None)
            n += 1
            if out["next"] is None:
                return seen, n
            after = out["next"]

    def test_triage_pages_fit_and_visit_every_item_once(self):
        seen, n = self.pages(triage=True, pass_token=self.t1)
        self.assertEqual(seen, self.pids)
        self.assertGreater(n, 1)
        item = json.loads(self.text("list_quarter_state", triage=True))["triage"][0]
        self.assertEqual(item["search"]["query_count"], 50)
        self.assertEqual(len(item["search"]["last_queries"]), 3)
        self.assertIsNotNone(item["row_snapshot"])       # what record_match compares, verbatim
        self.assertEqual(item["row_snapshot"]["counterparty"], NAME[:140])

    def test_an_item_leaving_between_pages_skips_no_other(self):
        first = self.call("list_quarter_state", triage=True)
        # handled in between (as stop_chasing does): the page's first item, and its last —
        # the very item the cursor names
        for gone in (first["triage"][0]["pid"], first["triage"][-1]["pid"]):
            with db.tx(self.conn):
                self.conn.execute("UPDATE projections SET search_state='accepted-missing'"
                                  " WHERE pid=?", (gone,))
                lineage.settle(self.conn, gone)
        second = self.call("list_quarter_state", triage=True, after=first["next"])
        shown = [d["pid"] for d in first["triage"]]
        self.assertEqual(second["triage"][0]["pid"], self.pids[len(shown)])

    def test_quarter_pages_fit_and_visit_every_item_once(self):
        for i, pid in enumerate(self.pids[:10]):       # long runners-up and rationale
            doc = self.file(amount_minor=1000 + i, document_date="2026-07-01",
                            issuer="I" * 500, recipient="R" * 500, document_number="N" * 300)
            d = work.describe(self.conn, pid)
            self.call("propose_match", pid=pid, doc_id=doc, expected_revision=d["revision"],
                      row_snapshot=d["row_snapshot"], pass_token=self.t1,
                      runners_up=["r" * 300] * 20, rationale="because " * 300)
        seen, n = self.pages(quarter="2026-Q3")
        self.assertEqual(sorted(seen), sorted(self.pids))
        self.assertEqual(len(seen), len(set(seen)))
        out = self.call("list_quarter_state", quarter="2026-Q3")
        self.assertEqual(out["total"], N)
        self.assertEqual(sum(out["counts"].values()), N)

    def test_unmatched_documents_fit(self):
        for i in range(N):
            self.file(amount_minor=5000 + i, document_date="2026-07-01", issuer="I" * 2000,
                      counterparty="C" * 2000, recipient="R" * 2000, document_number="N" * 2000)
        text = self.text("list_unmatched_documents", limit=500)
        self.assertLessEqual(len(text), budget.RESULT_LIMIT)
        out = json.loads(text)
        self.assertEqual(len(out["documents"]) + out["remaining"], out["total"])
        self.assertTrue(out["truncated"])

    def test_an_oversized_claim_claims_nothing(self):
        self.finish()
        before = self.digest()
        with mock.patch.object(budget, "RESULT_LIMIT", 200):
            import qa_server
            out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "continue_pass", "arguments": {}}})
        self.assertTrue(out["result"].get("isError"), out)
        self.assertIn("nothing was claimed", out["result"]["content"][0]["text"])
        self.assertEqual(self.digest(), before)         # rolled back: no rotation, no lease
        c = self.claim()["continue"]
        self.assertEqual(c["step"], "sweep")
        self.assertNotEqual(c["pass_token"], self.t1)


class TestPage(__import__("unittest").TestCase):
    def test_a_page_stops_at_the_count_or_the_budget_and_always_progresses(self):
        items = [{"t": "x" * 100}] * 10
        self.assertEqual(budget.page(items, 3), (items[:3], 7))
        shown, rest = budget.page(items, 10, budget=350)
        self.assertEqual((len(shown), rest), (3, 7))
        self.assertLessEqual(budget.size(shown), 350)
        self.assertEqual(budget.page([{"t": "x" * 1000}], 5, budget=10), ([{"t": "x" * 1000}], 0))

    def test_clip(self):
        self.assertEqual(budget.clip("abcdef", 4), "abc…")
        self.assertEqual(budget.clip("abc", 4), "abc")
        self.assertIsNone(budget.clip(None, 4))
