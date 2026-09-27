# tests/test_freshness.py
"""Classification freshness (fix E2): a live lineage's classification
observation is FRESH iff the sweep recorded it after the latest successful
import (compared by snapshot id, never by a 1-second timestamp). The sweep
enumerates exactly the non-fresh lineages; a machine match and a package
never act on a non-fresh one. Against the REAL bank-feed, through
qa_server.handle and the rendered get_transaction text where the skill is
exercised."""
import csv
import io
import random
import unittest
import zipfile

from tests import test_e2e
from tests import sim
import db  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import work  # noqa: E402

call = test_e2e.TestPackagingSeesTheClassification.call
read = test_e2e.TestPackagingSeesTheClassification.read


class ToolPass(test_e2e.Base):
    """A pass through the tool layer, in the skill's order."""
    def setUp(self):
        super().setUp()
        from tests.test_tools import _fresh_conn
        _fresh_conn(self)._CONN = self.conn

    def begin(self, trigger):
        bf = self.bf
        token = call("begin_pass", trigger=trigger)["pass_token"]
        accounts = [{"account_id": r["account_id"], "category": r["category"], "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        call("record_probe", pass_token=token, kind="bank_tools", ok=True)
        call("record_probe", pass_token=token, kind="bank_accounts", ok=True,
             data={"accounts": accounts})
        call("record_probe", pass_token=token, kind="bank_sync", ok=True)
        call("record_probe", pass_token=token, kind="ledger", ok=True,
             data=sim.ledger_state(bf.listing()))
        self.assertTrue(call("check_setup")["can_run"])
        imp = call("import_ledger_export", path=bf.export(), pass_token=token,
                   ledger_instance=bf.last_export_instance)
        self.assertEqual(imp["erase_candidates"], [])
        return token

    def observe(self, token, item):
        """The skill's step 5 for ONE item, from the rendered text."""
        bf, row_id = self.bf, item["row_id"]
        for _ in range(4):
            tags, notes, first_seen = read(bf.call("get_transaction", row_id=row_id))
            r = call("record_observation", pid=item["pid"], pass_token=token,
                     observed_tags=tags, observed_notes=notes, observed_first_seen=first_seen)
            ins = r["instructions"]
            if not ins:
                return
            kw = {k: ins[k] for k in ("workflow", "expected_generation", "expected_ledger")}
            if "untag" in ins:
                bf.call("untag_transaction", row_ids=[row_id], tags=ins["untag"], **kw)
            elif "tag" in ins:
                bf.call("tag_transaction", row_ids=[row_id], tags=ins["tag"], **kw)
            else:
                bf.call("add_note", row_ids=[row_id], note=ins["add_note"], author="agent", **kw)

    def sweep(self, token, budget=None):
        """Step 5 until remaining is 0, or until `budget` items were read."""
        n = 0
        while True:
            page = call("list_projections", pass_token=token,
                        limit=25 if budget is None else max(1, min(25, budget - n)))
            for item in page["projections"]:
                self.observe(token, item)
                n += 1
            if page["remaining_in_cycle"] == 0:
                return 0
            if budget is not None and n >= budget:
                return page["remaining_in_cycle"]

    def zip_of(self, quarter="2026-Q3"):
        pkg = call("build_quarterly_package", quarter=quarter)
        z = zipfile.ZipFile(pkg["path"])
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        return pkg, sorted(n for n in z.namelist() if "/" in n), rows, z

    def pid_of(self, row_id):
        return self.conn.execute("SELECT pid FROM aliases WHERE row_id=?", (row_id,)).fetchone()[0]

    def two_rows_matched(self):
        """Adobe 10.00 (software, its invoice matched) and Zapier 20.00, pid order
        Adobe < Zapier."""
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe"),
                  bf.row("2026-07-06", ref="Z1", amount=2000, counterparty="Zapier")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["A1"], "software")
        self.classify(ids["Z1"], "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-05")
        self.assertEqual(len(sim.run_pass(self.conn, bf)["triage"]["matched"]), 1)
        self.assertLess(self.pid_of(ids["A1"]), self.pid_of(ids["Z1"]))
        return ids


class TestAstraInterruptedCycle(ToolPass):
    """Round E2 (Astra): an interrupted cycle reads row 1, not row 2; row 1 is then
    reclassified; the package's sweep used to resume the cursor, read only row 2,
    reach remaining 0 — and ship row 1's stale invoice as MATCHED/invoice."""
    def test_the_package_sweep_rereads_what_an_earlier_cycle_read(self):
        ids = self.two_rows_matched()
        bf = self.bf
        token = self.begin("cron")
        self.assertGreater(self.sweep(token, budget=1), 0)          # row 1 read, row 2 not
        call("end_pass", pass_token=token, outcome="interrupted")
        bf.call("untag_transaction", row_ids=[ids["A1"]], tags=["software"])
        self.classify(ids["A1"], "refund")                           # now wants a credit note
        token = self.begin("package")
        first = call("list_projections", pass_token=token)
        self.assertEqual(sorted(i["row_id"] for i in first["projections"]),
                         sorted(ids.values()))                       # BOTH are due again
        self.assertEqual(self.sweep(token), 0)
        call("end_pass", pass_token=token, outcome="complete")
        _, files, rows, _ = self.zip_of()
        self.assertEqual(files, [])                                  # the invoice does not ship
        self.assertEqual({r["counterparty"]: (r["status"], r["expectation_kind"]) for r in rows},
                         {"Adobe": ("MISSING", "credit-note"), "Zapier": ("MISSING", "invoice")})


class TestTerraPartialSweepThenTriage(ToolPass):
    """Round E2 (Terra): the sweep stops with remaining > 0 and triage then judges from
    an unswept, stale classification: an invoice auto-matched where a credit note is
    required. The machine write refuses a lineage not re-read since the import."""
    def test_an_auto_match_on_a_row_not_reread_is_refused(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["Z1"], "software")
        self.classify(ids["A1"], "software")
        self.first_pass()
        self.assertLess(self.pid_of(ids["Z1"]), self.pid_of(ids["A1"]))
        bf.call("untag_transaction", row_ids=[ids["A1"]], tags=["software"])
        self.classify(ids["A1"], "refund")                           # wants a credit note now
        doc = self.file(amount_minor=1000, document_date="2026-07-06")   # an invoice
        token = self.begin("cron")
        self.assertEqual(self.sweep(token, budget=1), 1)             # Zapier read, Adobe not
        tri = {d["pid"]: d for d in call("list_quarter_state", triage=True)["triage"]}
        stale = tri[self.pid_of(ids["A1"])]
        self.assertEqual(stale["expectation"]["kind"], "invoice")    # the stale picture
        self.assertFalse(stale["fresh"])
        for tool in ("record_match", "propose_match"):
            args = dict(pid=stale["pid"], doc_id=doc, expected_revision=stale["revision"],
                        row_snapshot=stale["row_snapshot"], pass_token=token)
            if tool == "record_match":
                args["author"] = "auto"
            out = _raw(tool, **args)
            self.assertTrue(out.startswith("refused: "), out)
            self.assertIn("not been re-read since the latest bank import", out)
        self.assertIsNone(self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                            (stale["pid"],)).fetchone()[0])
        # once read, the payment wants a credit note and the invoice is refused by kind
        self.assertEqual(self.sweep(token), 0)
        cur = work.describe(self.conn, stale["pid"])
        self.assertTrue(cur["fresh"])
        self.assertEqual(cur["expectation"]["kind"], "credit-note")


class TestPassWithAnUnfinishedSweep(ToolPass):
    """SKILL.md step 6 (fix E2): triage runs only when the sweep reported 0; a sweep
    cut short ends the pass `interrupted` with nothing judged."""
    def test_no_triage_after_a_sweep_cut_short(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-06")
        out = sim.run_pass(self.conn, bf, sweep_budget=1)
        self.assertEqual((out["triage"], out["remaining"]), (None, 1))
        self.assertEqual(self.conn.execute("SELECT outcome FROM passes WHERE pass_id=?",
                                           (out["end"]["ended"],)).fetchone()[0], "interrupted")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM match_state").fetchone()[0], 0)
        out = sim.run_pass(self.conn, bf, sweep_budget=2)            # room for both: judged
        self.assertEqual(len(out["triage"]["matched"]), 1)


def _raw(name, **args):
    import qa_server
    out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    return out["result"]["content"][0]["text"]


class TestPackageWithANonFreshMember(ToolPass):
    """A build never ships a non-fresh row's documents as MATCHED: the row ships as
    UNCLASSIFIED, its documents under unresolved/, and the caption counts it (spec
    §Error handling: packaging ships rather than blocking)."""
    def test_the_matched_row_not_reread_does_not_ship_its_invoice(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["Z1"], "software")
        self.classify(ids["A1"], "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-06")
        self.assertEqual(len(sim.run_pass(self.conn, bf)["triage"]["matched"]), 1)
        bf.call("untag_transaction", row_ids=[ids["A1"]], tags=["software"])
        self.classify(ids["A1"], "refund")
        token = self.begin("package")
        self.assertEqual(self.sweep(token, budget=1), 1)             # Zapier read, Adobe not
        call("end_pass", pass_token=token, outcome="interrupted")
        pkg, files, rows, z = self.zip_of()
        self.assertEqual(files, ["unresolved/2026-07-06_Adobe_10.00.pdf"])
        st = {r["counterparty"]: (r["status"], r["expectation_kind"], r["document"]) for r in rows}
        self.assertEqual(st["Adobe"], ("UNCLASSIFIED", "", ""))
        self.assertEqual(st["Zapier"], ("MISSING", "invoice", ""))
        self.assertIn("1 not re-read since the last bank check", pkg["caption"])
        notes = z.read("notes.md").decode()
        self.assertIn("## Not re-read since the last bank check", notes)
        # after a complete sweep the same build ships the truth
        token = self.begin("package")
        self.assertEqual(self.sweep(token), 0)
        call("end_pass", pass_token=token, outcome="complete")
        pkg, files, rows, _ = self.zip_of()
        self.assertEqual(files, [])
        st = {r["counterparty"]: (r["status"], r["expectation_kind"]) for r in rows}
        self.assertEqual(st["Adobe"], ("MISSING", "credit-note"))
        self.assertNotIn("not re-read", pkg["caption"])


class TestFreshnessProperty(ToolPass):
    """After any import, a machine match or a build over a non-fresh lineage never
    succeeds; over a fresh one it does. Random subsets of rows read, fixed seeds."""
    def test_no_machine_match_and_no_shipped_document_on_a_non_fresh_lineage(self):
        bf = self.bf
        refs = ["R%d" % i for i in range(6)]
        bf.fetch([bf.row("2026-07-%02d" % (i + 1), ref=r, amount=1000 + i,
                         counterparty="Vendor%d" % i) for i, r in enumerate(refs)])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        for rid in ids.values():
            self.classify(rid, "software")
        self.first_pass()
        docs = {r: self.file(counterparty="Vendor%d" % i, issuer="Vendor%d" % i,
                             amount_minor=1000 + i, document_date="2026-07-%02d" % (i + 1))
                for i, r in enumerate(refs)}
        rng = random.Random(20260927)
        for trial in range(4):
            token = self.begin("cron")
            budget = rng.randint(0, len(refs))
            if budget:
                self.sweep(token, budget=budget)
            for d in work.list_quarter_state(self.conn, "2026-Q3")["items"]:
                fresh = lineage.is_fresh(self.conn, lineage.projection(self.conn, d["pid"]))
                self.assertEqual(d["fresh"], fresh)
                ref = next(k for k, v in ids.items() if v == lineage.projection(
                    self.conn, d["pid"])["dest_row_id"])
                if d["status"] != "open":
                    continue
                try:
                    matches.record_match(self.conn, pid=d["pid"], doc_id=docs[ref], author="auto",
                                         expected_revision=d["revision"],
                                         row_snapshot=d["row_snapshot"], token=token)
                    ok = True
                except db.Refusal as exc:
                    ok = False
                    self.assertIn("not been re-read", str(exc))
                self.assertEqual(ok, fresh, (trial, ref))
            call("end_pass", pass_token=token, outcome="interrupted")
            _, files, rows, _ = self.zip_of()
            for d in work.list_quarter_state(self.conn, "2026-Q3")["items"]:
                r = next(x for x in rows if x["counterparty"] == d["counterparty"])
                if not d["fresh"]:
                    self.assertEqual((r["status"], r["document"]), ("UNCLASSIFIED", ""))
            # the next pass imports again: every lineage is non-fresh after it
            token = self.begin("cron")
            self.assertFalse(any(lineage.is_fresh(self.conn, lineage.projection(self.conn, p))
                                 for p in lineage.live_pids(self.conn)))
            call("end_pass", pass_token=token, outcome="interrupted")


if __name__ == "__main__":
    unittest.main()
