# tests/test_e2e.py
"""Whole passes against the REAL bank-feed. The specialist's judgment is
replaced by tests/sim.triage (the auto-match bar on filed metadata); every
bank-feed interaction is the real one."""
import csv
import io
import json
import re
import unittest
import zipfile

from tests._base import StoreCase
from tests import bankfeed, sim
import binding  # noqa: E402
import db  # noqa: E402
import documents  # noqa: E402
import lineage  # noqa: E402
import package  # noqa: E402
import passes  # noqa: E402
import work  # noqa: E402
import version  # noqa: E402

PDF = b"%PDF-1.4\n%%EOF\n"


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.close)
        self.bf.account(category="company", label="Zakelijk")
        self.k = 0

    def file(self, **meta):
        self.k += 1
        args = dict(source_path=self.publish(f"d{self.k}.pdf", PDF + str(self.k).encode()),
                    kind="invoice", source="gmail", extraction_author="resident",
                    counterparty="Adobe", issuer="Adobe", currency="EUR",
                    document_number=f"N{self.k}")
        args.update(meta)
        return documents.ingest_document(self.conn, **args)["doc_id"]

    def classify(self, row_id, *tags):
        self.bf.call("tag_transaction", row_ids=[row_id], tags=list(tags))

    def active(self):
        return self.bf.rows(state="active")

    def first_pass(self):
        """The first pass binds the account with today's quarter as its start; the
        fixtures live in 2026-Q3, so move the start earlier (as "start from Q2" would)
        and run the pass that admits them — independent of the date the suite runs on."""
        sim.run_pass(self.conn, self.bf)
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        return sim.run_pass(self.conn, self.bf)

    def owned(self, row_id):
        return sorted(t for t in self.bf.tags(row_id) if t.startswith("acct::"))


class TestFixtureQuarter(Base):
    def test_a_quarter_end_to_end(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-01", ref="A1", amount=5445, counterparty="Adobe"),
                  bf.row("2026-07-10", ref="Z1", amount=9900, counterparty="Zapier"),
                  bf.row("2026-07-15", ref="C1", amount=121000, counterparty="Client BV",
                         direction="CRDT"),
                  bf.row("2026-07-20", ref="T1", amount=50000, counterparty="Own savings"),
                  bf.row("2026-07-25", ref="S1", amount=300000, counterparty="Payroll"),
                  bf.row("2026-07-28", ref="U1", amount=700, counterparty="Mystery")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.classify(ids["A1"], "software")
        self.classify(ids["Z1"], "software")
        self.classify(ids["C1"], "income", "consulting")
        self.classify(ids["T1"], "internal-transfer")
        self.classify(ids["S1"], "income", "salary")
        self.first_pass()                                     # binds, admits Q3
        self.file(amount_minor=5445, document_date="2026-06-30")          # cross-quarter
        self.file(kind="sales-invoice", counterparty="Client BV", issuer="Voorbeeld BV",
                  amount_minor=121000, document_date="2026-07-14")
        out = sim.run_pass(self.conn, bf)
        self.assertEqual(len(out["triage"]["matched"]), 2)
        matched = self.conn.execute(
            "SELECT p.dest_row_id FROM projections p JOIN match_state m"
            " ON m.match_id=p.current_match WHERE p.status='matched' AND m.author='auto'"
            " AND p.ended IS NULL AND p.merged_into IS NULL ORDER BY p.dest_row_id").fetchall()
        self.assertEqual([r[0] for r in matched], sorted([ids["A1"], ids["C1"]]))
        self.assertEqual(self.owned(ids["A1"]), ["acct::matched"])
        self.assertEqual(self.owned(ids["C1"]), ["acct::matched"])
        self.assertEqual(self.owned(ids["Z1"]), ["acct::open"])
        self.assertEqual(self.owned(ids["T1"]), ["acct::no-document-expected"])
        self.assertEqual(self.owned(ids["S1"]), [])                        # optional: no tag
        self.assertEqual(self.owned(ids["U1"]), ["acct::open"])            # unclassified
        end = sim.package_pass(self.conn, self.bf)
        pkg = package.build_quarterly_package(self.conn, "2026-Q3", end["package_token"])
        z = zipfile.ZipFile(pkg["path"])
        self.assertEqual(sorted(n for n in z.namelist() if "/" in n),
                         ["invoices/2026-06-30_Adobe_54.45.pdf",
                          "sales-invoices/2026-07-14_Voorbeeld-BV_1210.00.pdf"])
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        self.assertEqual(len(rows), 6)
        st = {r["counterparty"]: r["status"] for r in rows}
        self.assertEqual(st, {"Adobe": "MATCHED", "Zapier": "MISSING", "Client BV": "MATCHED",
                              "Own savings": "NO-DOCUMENT", "Payroll": "OPTIONAL-MISSING",
                              "Mystery": "UNCLASSIFIED"})

    def test_ambiguous_identical_pair_across_two_passes_stays_proposed(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-10", ref="Z1", amount=9900, counterparty="Zapier")])
        self.classify(self.active()[0]["row_id"], "software")
        self.first_pass()
        self.file(counterparty="Zapier", issuer="Zapier", amount_minor=9900,
                  document_date="2026-07-10")
        sim.run_pass(self.conn, bf)
        bf.fetch([bf.row("2026-07-10", ref="Z1", amount=9900, counterparty="Zapier"),
                  bf.row("2026-07-10", ref="Z2", amount=9900, counterparty="Zapier")])
        z2 = next(r["row_id"] for r in self.active() if r["provider_ref"] == "Z2")
        self.classify(z2, "software")
        self.file(counterparty="Zapier", issuer="Zapier", amount_minor=9900,
                  document_date="2026-07-10")
        sim.run_pass(self.conn, bf)
        for r in self.active():
            self.assertEqual(self.owned(r["row_id"]), ["acct::proposed"], r["provider_ref"])


class TestEndsE2E(Base):
    def test_returning_payment_in_the_same_pass_takes_its_document_as_a_machine_pick(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000, counterparty="Adobe")])
        self.classify(self.active()[0]["row_id"], "software")
        self.first_pass()
        doc = self.file(amount_minor=1000, document_date="2026-07-05")
        sim.run_pass(self.conn, bf)
        (old_pid,) = lineage.live_pids(self.conn)
        bf.purge_before("2026-08-01")

        def resync():
            bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000, counterparty="Adobe")])
            self.classify(self.active()[0]["row_id"], "software")
        out = sim.run_pass(self.conn, bf, sync=resync)
        self.assertEqual(len(out["import"]["erase_candidates"]), 1)
        new_pid = self.conn.execute("SELECT pid FROM aliases WHERE row_id=?",
                                    (self.active()[0]["row_id"],)).fetchone()[0]
        cur = work.describe(self.conn, new_pid)["current"]
        self.assertEqual((cur["document"]["doc_id"], cur["author"]), (doc, "auto"))
        self.assertNotEqual(new_pid, old_pid)
        self.assertEqual(lineage.projection(self.conn, old_pid)["ended"], "erased")

    def test_a_purge_between_a_syncs_plan_and_its_apply_is_a_new_lineage(self):
        import apply
        import ingest
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", status="PDNG", amount=1000)])
        self.first_pass()
        sim.run_pass(self.conn, bf)
        (old_pid,) = lineage.live_pids(self.conn)
        stored = bf.rows(account=bankfeed.Ledger.ACCOUNT)
        plan = ingest.reconcile(stored, [bf.row("2026-07-06", ref="R1", amount=1000)],
                                ("2026-01-01", "2026-12-31"), bankfeed.CAP_STABLE)
        bf.purge_before("2026-08-01")
        stats = apply.apply_plan(bf.conn, bankfeed.Ledger.ACCOUNT, plan)
        bf.conn.commit()
        self.assertEqual((stats["inserted"], stats["superseded"]), (1, 0))
        sim.run_pass(self.conn, bf)
        self.assertEqual(lineage.projection(self.conn, old_pid)["ended"], "erased")
        self.assertEqual(len([p for p in lineage.live_pids(self.conn)
                              if not lineage.projection(self.conn, p)["ended"]]), 1)

    def test_a_data_only_erasure_waits_for_the_operator_then_rebinds(self):
        # bank-feed 0.18.0 (component 0.19.0): delete_data_keep_signins erases the data,
        # keeps the account bindings and mints a NEW ledger instance id. From outside that
        # is another ledger, so the store waits for "the bank ledger was reset" (plan §D4).
        # Drive the tool as upstream tests/test_data_only_erasure.py does if it needs more.
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        (old_pid,) = lineage.live_pids(self.conn)
        before = bf.instance()
        bf.call("delete_data_keep_signins")
        self.assertNotEqual(bf.instance(), before)
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])            # re-synced
        out = sim.run_pass(self.conn, bf)
        self.assertFalse(out["gate"]["allowed"])
        self.assertIsNone(lineage.projection(self.conn, old_pid)["ended"])
        self.assertEqual([t for r in self.active() for t in bf.tags(r["row_id"])
                          if t.startswith("acct::")], [])                     # nothing written
        binding.acknowledge_ledger_reset(self.conn)
        out = sim.run_pass(self.conn, bf)
        self.assertTrue(out["gate"]["allowed"])
        self.assertEqual(lineage.projection(self.conn, old_pid)["ended"], "erased")
        self.assertEqual(self.owned(self.active()[0]["row_id"]), ["acct::open"])

    def test_forget_relink_resync_ends_old_lineages_and_keeps_writing(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        sim.run_pass(self.conn, bf)
        (old_pid,) = lineage.live_pids(self.conn)
        bf.call("forget_local_account", account_id=bankfeed.Ledger.ACCOUNT)
        bf.account(category="company", label="Zakelijk")                  # relinked
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])            # re-synced
        out = sim.run_pass(self.conn, bf)
        self.assertTrue(out["gate"]["allowed"])
        self.assertEqual(lineage.projection(self.conn, old_pid)["ended"], "erased")
        self.assertEqual(self.owned(self.active()[0]["row_id"]), ["acct::open"])


class TestPassStops(Base):
    def test_an_import_refusal_ends_the_pass_stopped(self):
        # row #N now names a different transaction than the one the store holds
        # (first_seen differs): the import refuses and poisons the pass's writes;
        # the pass must END (stopped), not stay live holding the marker.
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        rid = self.active()[0]["row_id"]
        tags_before = bf.tags(rid)
        bf.conn.execute("UPDATE transactions SET first_seen='2026-09-21T08:00:00Z'"
                        " WHERE row_id=?", (rid,))
        bf.conn.commit()
        snapshots = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        out = sim.run_pass(self.conn, bf)
        self.assertIsNone(out["import"])
        self.assertIn("different transaction", out["refused"])
        self.assertEqual(out["end"]["outcome"], "stopped")
        self.assertIsNone(passes.current_pass(self.conn))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                         snapshots)
        self.assertEqual(bf.tags(rid), tags_before)
        self.assertEqual(passes.begin_pass(self.conn, "cron")["status"], "started")


    def test_a_sweep_refusal_ends_the_pass_stopped(self):
        # the row changes under the pass AFTER the import proved the ledger: the
        # sweep's record_observation refuses (first_seen mismatch, poisoned); the
        # pass must END (stopped) so no live marker answers "A check is running".
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        rid = self.active()[0]["row_id"]
        tags_before = bf.tags(rid)
        export = bf.export

        def export_then_change():
            path = export()
            bf.conn.execute("UPDATE transactions SET first_seen='2026-09-21T08:00:00Z'"
                            " WHERE row_id=?", (rid,))
            bf.conn.commit()
            return path
        bf.export = export_then_change
        out = sim.run_pass(self.conn, bf)
        self.assertIsNotNone(out["import"])                   # the import itself succeeded
        self.assertIn("stop the pass", out["refused"])
        self.assertEqual(out["end"]["outcome"], "stopped")
        self.assertIsNone(passes.current_pass(self.conn))
        self.assertEqual(bf.tags(rid), tags_before)
        self.assertEqual(passes.begin_pass(self.conn, "cron")["status"], "started")

    def test_a_failed_sync_is_probed_as_failed_and_does_not_advance_bank_through(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        with db.tx(self.conn):
            self.conn.execute("UPDATE snapshots SET bank_through='2026-09-01'")

        def failing():
            raise RuntimeError("bank unreachable")
        out = sim.run_pass(self.conn, bf, sync=failing)
        probe = self.conn.execute("SELECT ok, detail FROM probes WHERE kind='bank_sync'"
                                  ).fetchone()
        self.assertEqual((probe[0], "bank unreachable" in probe[1]), (0, True))
        self.assertEqual(self.conn.execute(
            "SELECT bank_through FROM snapshots WHERE snapshot_id=?",
            (out["import"]["snapshot"],)).fetchone()[0], "2026-09-01")


class TestRestoreAndReset(Base):
    def _backups(self):
        text = self.bf.listing()
        return [line.split()[0] for line in text.splitlines()
                if line.startswith("  ") and ("install:" in line or "weekly" in line)]

    def test_restore_stops_the_pass_reset_cleans_and_the_next_write_mints_again(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        sim.run_pass(self.conn, bf)
        rid = self.active()[0]["row_id"]
        self.assertEqual(self.owned(rid), ["acct::open"])
        install = bf.registered()[version.WORKFLOW]
        bf.call("restore_backup", backup_id=install)
        out = sim.run_pass(self.conn, bf)
        self.assertFalse(out["gate"]["allowed"])
        self.assertIn("reset", out["gate"]["reason"].lower())
        binding.reset_store(self.conn)
        self.assertEqual([t for t in bf.tags(rid) if t.startswith("acct::")], [])
        self.assertFalse([n for n in bf.notes(rid) if n.startswith("Accounting revision")])
        out = sim.run_pass(self.conn, bf)
        self.assertTrue(out["gate"]["allowed"])
        self.assertIn(version.WORKFLOW, bf.registered())
        self.assertNotEqual(bf.registered()[version.WORKFLOW], install)

    def test_a_restored_weekly_backup_keeps_the_registration_and_names_the_install_backup(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        sim.run_pass(self.conn, bf)
        install = bf.registered()[version.WORKFLOW]
        bf.call("backup", reason="weekly")
        weekly = [b for b in self._backups() if b != install][-1]
        bf.call("restore_backup", backup_id=weekly)
        binding.reset_store(self.conn)
        tags_before = {r["row_id"]: bf.tags(r["row_id"]) for r in self.active()}
        out = sim.run_pass(self.conn, bf)
        self.assertFalse(out["gate"]["allowed"])
        self.assertIn(f"restore backup {install}", out["gate"]["reason"])
        self.assertEqual({r["row_id"]: bf.tags(r["row_id"]) for r in self.active()}, tags_before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0], 0)
        out = sim.run_pass(self.conn, bf)                   # and the next pass refuses again
        self.assertFalse(out["gate"]["allowed"])

    def test_restoring_purges_pre_erasure_backup_stops_the_pass_for_reset_store(self):
        # spec §Testing / "Undoing a purge is a restore": the REAL purge tool takes the
        # pre-erasure backup its reply names; restoring it advances the generation and
        # the pass stops for reset_store.
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000),
                  bf.row("2026-08-05", ref="R2", amount=2000)])
        self.first_pass()
        rows_before = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.assertEqual([self.owned(r) for r in rows_before.values()],
                         [["acct::open"], ["acct::open"]])
        reply = bf.call("purge", before_date="2026-08-01", user_work="keep")
        m = re.search(r"restore_backup backup_id=(\S+?)\s", reply)
        self.assertIsNotNone(m, reply)
        pre_erasure = m.group(1)
        self.assertEqual(len(self.active()), 1)
        out = sim.run_pass(self.conn, bf)
        self.assertTrue(out["gate"]["allowed"])
        self.assertEqual(len(out["import"]["erase_candidates"]), 1)
        ended = self.conn.execute("SELECT COUNT(*) FROM projections WHERE ended='erased'"
                                  ).fetchone()[0]
        self.assertEqual(ended, 1)
        gen = bf.generation()
        restored = bf.call("restore_backup", backup_id=pre_erasure)
        self.assertEqual(len(self.active()), 2, restored)
        self.assertEqual(bf.generation(), gen + 1)
        tags = {r["row_id"]: bf.tags(r["row_id"]) for r in self.active()}
        n_proj = self.conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0]
        out = sim.run_pass(self.conn, bf)
        self.assertFalse(out["gate"]["allowed"])
        self.assertIn("reset_store", out["gate"]["reason"])
        self.assertEqual(out["end"]["outcome"], "stopped")
        self.assertNotIn("refused", out)          # stopped at check_setup, before any export
        self.assertEqual({r["row_id"]: bf.tags(r["row_id"]) for r in self.active()}, tags)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0],
                         n_proj)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projections WHERE"
                                           " ended='erased'").fetchone()[0], 1)

    def test_a_restore_after_the_generation_was_read_rejects_the_write(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R1", amount=1000)])
        self.first_pass()
        sim.run_pass(self.conn, bf)
        install = bf.registered()[version.WORKFLOW]
        token = passes.begin_pass(self.conn, "cron")["pass_token"]
        sim.probe(self.conn, bf, token)
        gen = passes.bank_write_gate(self.conn)["expected_generation"]
        bf.call("restore_backup", backup_id=install)
        rid = self.active()[0]["row_id"]
        bf.call("tag_transaction", row_ids=[rid], tags=["acct::matched"], workflow=version.WORKFLOW,
                expected_generation=gen)
        self.assertNotIn("acct::matched", bf.tags(rid))


class ToolFlow(Base):
    """The skill's flows driven through the tool layer (qa_server.handle) against the
    real bank-feed and its RENDERED get_transaction text. No tests of its own."""
    def setUp(self):
        super().setUp()
        from tests.test_tools import _fresh_conn
        _fresh_conn(self)._CONN = self.conn

    @staticmethod
    def call(name, **args):
        import json
        import qa_server
        out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
        text = out["result"]["content"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise AssertionError(f"{name} answered {text!r}") from None

    @staticmethod
    def read(text):
        """What the skill's sweep step transcribes from get_transaction's text."""
        tags, notes, in_notes = [], [], False
        for line in text.splitlines():
            if line.startswith("Tags: ") and line != "Tags: none":
                tags += line[len("Tags: "):].split(", ")
            elif line.startswith("Other workflows' tags (not classifications): "):
                tags += line.split(": ", 1)[1].split(", ")
            elif line.startswith("Notes"):
                in_notes = line != "Notes: none"
            elif in_notes and line.startswith("  ["):
                notes.append(line[2:])
        first_seen = re.search(r"first seen (\S+), last seen", text).group(1)
        return tags, notes, first_seen

    def sweep(self, token, budget=None, **scope):
        """SKILL.md's sweep (the specialist's pass, step 5), through the tools: until
        remaining_in_cycle is 0, or `budget` rows were read (the room ran out).
        Returns what remains. `scope` is list_projections' quarter=, if any."""
        bf = self.bf
        n = 0
        while True:
            page = self.call("list_projections", pass_token=token,
                             **({"limit": budget - n} if budget is not None else {}), **scope)
            for item in page["projections"]:
                n += 1
                row_id = item["row_id"]
                for _ in range(4):
                    text = bf.call("get_transaction", row_id=row_id)
                    if text.startswith("no transaction #"):
                        self.call("record_observation", pid=item["pid"], pass_token=token,
                                  snapshot_id=page["snapshot_id"],
                                  not_found=True)
                        break
                    tags, notes, first_seen = self.read(text)
                    r = self.call("record_observation", pid=item["pid"], pass_token=token,
                                  snapshot_id=page["snapshot_id"],
                                  observed_tags=tags, observed_notes=notes,
                                  observed_first_seen=first_seen)
                    ins = r["instructions"]
                    if not ins:
                        break
                    kw = {k: ins[k] for k in ("workflow", "expected_generation", "expected_ledger")}
                    if "untag" in ins:
                        bf.call("untag_transaction", row_ids=[row_id], tags=ins["untag"], **kw)
                    elif "tag" in ins:
                        bf.call("tag_transaction", row_ids=[row_id], tags=ins["tag"], **kw)
                    else:
                        bf.call("add_note", row_ids=[row_id], note=ins["add_note"],
                                author="agent", **kw)
            if page["remaining_in_cycle"] == 0 or (budget is not None and n >= budget) \
                    or page["time_up"]:
                return page["remaining_in_cycle"]

    def snapshot_pass(self, trigger="package"):
        """Ellen's begin_pass and the snapshot step, then the specialist's probes (step 1)
        and snapshot (step 3)."""
        bf = self.bf
        token = self.call("begin_pass", trigger=trigger)["pass_token"]
        if trigger == "package":
            self.call("record_step", pass_token=token, step="snapshot", action="start",
                      quarter="2026-Q3", channel="telegram")
        accounts = [{"account_id": r["account_id"], "category": r["category"], "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        self.call("record_probe", pass_token=token, kind="bank_tools", ok=True)
        self.call("record_probe", pass_token=token, kind="bank_accounts", ok=True,
                  data={"accounts": accounts})
        self.call("record_probe", pass_token=token, kind="bank_sync", ok=True)
        self.call("record_probe", pass_token=token, kind="ledger", ok=True,
                  data=sim.ledger_state(bf.listing()))
        self.assertTrue(self.call("check_setup")["can_run"])
        imp = self.call("import_ledger_export", path=bf.export(), pass_token=token,
                        ledger_instance=bf.last_export_instance)
        self.assertEqual(imp["erase_candidates"], [])
        return token

    def package(self, sweep):
        """SKILL.md's Packaging, step 1 (the specialist's package snapshot), then step 2."""
        token = self.snapshot_pass()
        if sweep:
            self.sweep(token)
        end = self.call("end_pass", pass_token=token, outcome="complete")
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=end["package_token"])
        z = zipfile.ZipFile(pkg["path"])
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        return sorted(n for n in z.namelist() if "/" in n), rows



class TestPackagingSeesTheClassification(ToolFlow):
    """Round E1 (Astra S1): the CSV import carries no classification tags; only the
    sweep's per-row read refreshes the classification the expectation and the kind
    guard use. Packaging, driven through the tool layer against the real bank-feed
    and its RENDERED get_transaction text, must sweep between import and build."""
    def matched_then_reclassified(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe")])
        rid = self.active()[0]["row_id"]
        self.classify(rid, "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-05")
        self.assertEqual(len(sim.run_pass(self.conn, bf)["triage"]["matched"]), 1)
        # the classification changes: a refund wants a credit note, not the invoice
        bf.call("untag_transaction", row_ids=[rid], tags=["software"])
        self.classify(rid, "refund")

    def test_the_package_sees_the_new_classification(self):
        self.matched_then_reclassified()
        files, rows = self.package(sweep=True)
        self.assertEqual(files, [])                               # the invoice does not ship
        self.assertEqual([(r["status"], r["expectation_kind"]) for r in rows],
                         [("MISSING", "credit-note")])

    def test_without_the_sweep_the_invoice_is_withheld(self):
        # the reproduction: Packaging without its sweep step shipped a stale picture
        # (MATCHED, invoices/). Since fix E2 the build itself withholds a row not re-read
        # since the import: unclassified, its document under unresolved/.
        self.matched_then_reclassified()
        files, rows = self.package(sweep=False)
        self.assertEqual(files, ["unresolved/2026-07-05_Adobe_10.00.pdf"])
        self.assertEqual([(r["status"], r["expectation_kind"]) for r in rows],
                         [("UNCLASSIFIED", "")])


class TestWaveF(ToolFlow):
    """Fix wave F, end to end through the tools against the real bank-feed."""
    def two_matched(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe"),
                  bf.row("2026-07-06", ref="B1", amount=2000, counterparty="Adobe")])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        self.file(amount_minor=1000, document_date="2026-07-05")
        self.file(amount_minor=2000, document_date="2026-07-06")
        self.assertEqual(len(sim.run_pass(self.conn, bf)["triage"]["matched"]), 2)

    def build(self, end, quarter="2026-Q3"):
        pkg = self.call("build_quarterly_package", quarter=quarter,
                        package_token=end["package_token"])
        pkg["package_token"] = end["package_token"]
        return pkg

    def deliver(self, pkg):
        staged = self.call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                           package_token=pkg["package_token"])
        return self.call("record_delivery", delivery_id=staged["delivery_id"],
                         outcome="delivered", package_token=pkg["package_token"])

    def test_a_row_shipped_unread_raises_no_false_changed_alert(self):
        # (1) the package pass's sweep has room for one of the two payments: the other
        # ships unclassified (not re-read since the import). The next pass reads it and
        # nothing changed — the accountant's copy is not "categorised differently".
        self.two_matched()
        token = self.snapshot_pass()
        self.assertEqual(self.sweep(token, budget=1), 1)
        pkg = self.build(self.call("end_pass", pass_token=token, outcome="interrupted"))
        z = zipfile.ZipFile(pkg["path"])
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        self.assertEqual(sorted(r["status"] for r in rows), ["MATCHED", "UNCLASSIFIED"])
        notes = z.read("notes.md").decode()
        unread = notes.split("## Not re-read since the last bank check")[1].split("\n## ")[0]
        self.assertIn("unresolved/", unread)                 # its set-aside invoice, named there
        unresolved = notes.split("## Unresolved candidates")[1].split("\n## ")[0]
        self.assertEqual(unresolved.strip(), "- none")        # it is no unresolved candidate
        self.deliver(pkg)
        end = sim.run_pass(self.conn, self.bf)["end"]
        self.assertIsNone(end["speak"], end)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_a_reclassified_row_shipped_unread_is_still_alerted(self):
        # the kind check stays: against the last known kind, a real change is reported
        self.two_matched()
        token = self.snapshot_pass()
        self.sweep(token, budget=1)
        end = self.call("end_pass", pass_token=token, outcome="interrupted")
        stale = [r[0] for r in self.conn.execute(
            "SELECT pid FROM projections WHERE class_observed_snapshot <"
            " (SELECT max(snapshot_id) FROM snapshots)")]
        self.assertEqual(len(stale), 1)
        pkg = self.build(end)
        self.deliver(pkg)
        rid = sim.lineage_row(self.conn, stale[0])
        self.bf.call("untag_transaction", row_ids=[rid], tags=["software"])
        self.classify(rid, "refund")
        sim.run_pass(self.conn, self.bf)
        changes = [json.loads(a[0])["change"] for a in self.conn.execute(
            "SELECT detail FROM alerts WHERE kind='delivered-changed'")]
        self.assertEqual(changes, ["reclassified"])

    def test_the_package_pass_sweeps_only_its_quarter(self):
        # (2a) a package needs only its own rows fresh
        bf = self.bf
        bf.fetch([bf.row("2026-05-05", ref="Q2", amount=500, counterparty="Adobe"),
                  bf.row("2026-07-05", ref="Q3", amount=1000, counterparty="Adobe")])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        token = self.snapshot_pass()
        page = self.call("list_projections", pass_token=token, quarter="Q3")
        q3 = [p for p in work.quarter_pids(self.conn, "2026-Q3")]
        self.assertEqual([i["pid"] for i in page["projections"]], q3)
        cursor = "SELECT cycle_started_at, last_cycle_completed_at FROM cursor"
        before = tuple(self.conn.execute(cursor).fetchone())
        self.assertEqual(self.sweep(token, quarter="2026-Q3"), 0)
        empty = self.call("list_projections", pass_token=token, quarter="2026-Q3")
        self.assertEqual((empty["projections"], empty["remaining_in_cycle"]), ([], 0))
        # the other quarter's payments are still owed a read: the cycle is not complete
        self.assertEqual(tuple(self.conn.execute(cursor).fetchone()), before)
        self.assertIsNotNone(before[0])
        rest = self.call("list_projections", pass_token=token)
        self.assertEqual([i["pid"] for i in rest["projections"]],
                         work.quarter_pids(self.conn, "2026-Q2"))
        end = self.call("end_pass", pass_token=token, outcome="complete", report={})
        report = json.loads(self.conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                                              (end["ended"],)).fetchone()[0])
        # (2d) the pass records its throughput
        self.assertEqual((report["swept_this_pass"], report["remaining_in_cycle"]), (1, 1))
        self.assertEqual(self.call("check_setup")["last_pass"]["report"]["swept_this_pass"], 1)
        pkg = self.build(end)
        self.assertNotIn("not re-read", pkg["caption"])

    def test_give_me_q3(self):
        # (3) the skill's literal wording: "give me Q3" -> build_quarterly_package(quarter="Q3")
        from unittest import mock
        import datetime as _dt
        self.two_matched()
        token = self.snapshot_pass()
        self.sweep(token, quarter="Q3")
        end = self.call("end_pass", pass_token=token, outcome="complete")
        autumn = _dt.datetime(2026, 10, 5, 9, 0, tzinfo=_dt.timezone.utc)
        with mock.patch.object(db, "_clock", lambda: autumn):
            pkg = self.call("build_quarterly_package", quarter="Q3",
                            package_token=end["package_token"])
        pkg["package_token"] = end["package_token"]
        self.assertTrue(pkg["filename"].endswith("-2026-Q3-2026-10-05.zip"), pkg["filename"])
        self.assertEqual(self.deliver(pkg)["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
