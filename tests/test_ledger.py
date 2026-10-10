import json
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import ledger  # noqa: E402
import lineage  # noqa: E402
import version  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()

    def imp(self, rows):
        return ledger.import_ledger_export(self.conn, path=self.export_csv(rows), token=self.token,
                                           ledger_instance=getattr(self, "instance", self.LEDGER),
                                           acq=self.acq)

    def live(self):
        return {r["pid"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM projections WHERE merged_into IS NULL")}


class TestAdmission(Base):
    def test_admits_eligible_rows_both_directions_only(self):
        out = self.imp([
            {"row_id": 1},                                             # eligible DBIT
            {"row_id": 2, "direction": "CRDT", "counterparty": "Client"},  # eligible CRDT
            {"row_id": 3, "booking_date": "2026-06-30", "value_date": "2026-06-30"},  # before watermark
            {"row_id": 4, "account_id": "acc-private"},                # other account
            {"row_id": 5, "state": "vanished"},                        # not active
            {"row_id": 6, "booking_date": "", "value_date": "2026-07-02", "status": "PDNG"},
        ])
        dests = sorted(p["dest_row_id"] for p in self.live().values())
        self.assertEqual(dests, [1, 2, 6])
        self.assertEqual(len(out["admitted"]), 3)

    def test_import_retains_every_state_of_the_bound_account(self):
        self.imp([{"row_id": 1, "state": "superseded", "superseded_by": 2}, {"row_id": 2},
                  {"row_id": 9, "account_id": "acc-private"}])
        states = {r[0]: r[1] for r in self.conn.execute("SELECT row_id, state FROM bank_rows")}
        self.assertEqual(states, {1: "superseded", 2: "active"})

    def test_refused_without_the_account_seen_this_pass(self):
        t = self.pass_(accounts=[])
        with self.assertRaises(db.Refusal) as cm:
            ledger.import_ledger_export(self.conn, path=self.export_csv([{"row_id": 1}]), token=t,
                                        ledger_instance=self.LEDGER, acq=self.acq)
        self.assertIn("was not seen in this pass's list_accounts", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0], 0)

    def test_refused_without_a_pass_token(self):
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=self.export_csv([{"row_id": 1}]), token=None,
                                        ledger_instance=self.LEDGER)


class TestResolution(Base):
    def test_supersession_moves_the_destination_and_keeps_aliases(self):
        self.imp([{"row_id": 1, "status": "PDNG"}])
        (pid,) = self.live()
        self.imp([{"row_id": 1, "status": "PDNG", "state": "superseded", "superseded_by": 2},
                  {"row_id": 2, "first_seen": "2026-07-04T08:00:00Z"}])
        self.assertEqual(self.live()[pid]["dest_row_id"], 2)
        aliases = {r[0] for r in self.conn.execute("SELECT row_id FROM aliases WHERE pid=?", (pid,))}
        self.assertEqual(aliases, {1, 2})
        self.assertEqual(len(self.live()), 1)          # the successor is not admitted twice

    def test_fan_in_merges_into_the_lower_pid_and_folds_the_union(self):
        self.imp([{"row_id": 1}, {"row_id": 2, "first_seen": "2026-07-03T09:00:00Z"}])
        p1, p2 = sorted(self.live())
        with db.tx(self.conn):
            lineage.append(self.conn, p2, "exempt", "operator")
            lineage.settle(self.conn, p2)
        # the export carries the tags (issue #1): bank-feed's supersede re-points them
        # onto the successor, so the merged lineage still reads `software`
        out = self.imp([
            {"row_id": 1, "state": "superseded", "superseded_by": 3},
            {"row_id": 2, "state": "superseded", "superseded_by": 3,
             "first_seen": "2026-07-03T09:00:00Z"},
            {"row_id": 3, "first_seen": "2026-07-05T08:00:00Z", "tags": ["software"],
             "tag_revision": 7}])
        self.assertEqual(out["merged"], [[p1, p2]])
        live = self.live()
        self.assertEqual(list(live), [p1])
        self.assertEqual(live[p1]["status"], "exempt")      # the union was folded
        self.assertEqual(self.conn.execute("SELECT merged_into FROM projections WHERE pid=?",
                                           (p2,)).fetchone()[0], p1)

    def test_a_losers_pairing_survives_the_fan_in_merge(self):
        import reducer
        self.imp([{"row_id": 1}, {"row_id": 2, "first_seen": "2026-07-03T09:00:00Z"}])
        p1, p2 = sorted(self.live())
        self.classify(p1, {"software"})
        self.classify(p2, {"software"})
        d = self.doc()
        with db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, p2))
            mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?, ?, 0)", (p2, d)).lastrowid
            lineage.append(self.conn, p2, "pair", "operator", match_id=mid, doc_id=d,
                           fp=reducer.fingerprint(reducer.facts_of(row), "invoice"))
            lineage.settle(self.conn, p2)
        self.assertEqual(self.live()[p2]["status"], "matched")
        # the export carries the tags (issue #1): bank-feed's supersede re-points them
        # onto the successor, so the merged lineage still reads `software`
        out = self.imp([
            {"row_id": 1, "state": "superseded", "superseded_by": 3},
            {"row_id": 2, "state": "superseded", "superseded_by": 3,
             "first_seen": "2026-07-03T09:00:00Z"},
            {"row_id": 3, "first_seen": "2026-07-05T08:00:00Z", "tags": ["software"],
             "tag_revision": 7}])
        self.assertEqual(out["merged"], [[p1, p2]])
        ms = self.conn.execute("SELECT pid, state FROM match_state WHERE match_id=?",
                               (mid,)).fetchone()
        self.assertEqual((ms["pid"], ms["state"]), (p1, "matched"))
        self.assertEqual(self.live()[p1]["status"], "matched")

    def test_a_superseded_by_cycle_is_a_broken_floor_not_a_hang(self):
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        out = self.imp([{"row_id": 1, "state": "superseded", "superseded_by": 2},
                        {"row_id": 2, "state": "superseded", "superseded_by": 1,
                         "first_seen": "2026-07-04T08:00:00Z"}])
        self.assertEqual(out["broken_floor"], [{"pid": pid, "row_id": 2, "missing": 1}])
        self.assertIsNone(self.live()[pid]["ended"])
        self.assertEqual(len(self.live()), 1)

    def test_a_rebound_ledgers_row_never_merges_into_an_ended_lineage(self):
        # round p4 (Astra S1): after a re-bind, the new ledger allocates the old row id
        import binding
        self.imp([{"row_id": 2}])
        (old,) = self.live()
        self.granted(binding.acknowledge_ledger_reset_in_tx)
        self.token = self.pass_(instance="c" * 32)
        self.instance = "c" * 32
        self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z"}])       # re-bound
        self.token = self.pass_(instance="c" * 32)
        out = self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z"},
                        {"row_id": 2, "first_seen": "2026-09-02T00:00:00Z"}])
        self.assertEqual(out["merged"], [])
        new = [p for p, r in self.live().items() if r["dest_row_id"] == 2 and p != old]
        self.assertEqual(len(new), 1)
        self.assertIsNone(self.live()[new[0]]["ended"])

    def test_a_vanished_destination_ends_the_lineage_at_the_import(self):
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        out = self.imp([{"row_id": 1, "state": "vanished"}])
        self.assertEqual(out["ended_vanished"], [pid])
        self.assertEqual(self.live()[pid]["ended"], "vanished")

    def test_an_absent_destination_is_a_candidate_never_an_end(self):
        self.imp([{"row_id": 1}, {"row_id": 2, "first_seen": "2026-07-03T09:00:00Z"}])
        pid = [p for p, r in self.live().items() if r["dest_row_id"] == 1][0]
        out = self.imp([{"row_id": 2, "first_seen": "2026-07-03T09:00:00Z"}])
        self.assertEqual([c["pid"] for c in out["erase_candidates"]], [pid])
        self.assertIsNone(self.live()[pid]["ended"])

    def test_a_dangling_superseded_by_is_a_broken_floor_not_an_end(self):
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        out = self.imp([{"row_id": 1, "state": "superseded", "superseded_by": 7}])
        self.assertEqual(out["broken_floor"], [{"pid": pid, "row_id": 1, "missing": 7}])
        self.assertIsNone(self.live()[pid]["ended"])
        self.assertEqual(out["erase_candidates"], [])

    def test_a_healed_floor_clears_the_broken_floor_mark(self):
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        self.imp([{"row_id": 1, "state": "superseded", "superseded_by": 7}])
        self.assertIsNotNone(self.live()[pid]["broken_floor"])
        out = self.imp([{"row_id": 1, "state": "superseded", "superseded_by": 7},
                        {"row_id": 7, "first_seen": "2026-07-09T08:00:00Z"}])
        self.assertEqual(out["broken_floor"], [])
        self.assertEqual(self.live()[pid]["dest_row_id"], 7)
        self.assertIsNone(self.live()[pid]["broken_floor"])

    def test_in_place_correction_to_before_the_watermark_is_ineligible_not_dropped(self):
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        self.imp([{"row_id": 1, "booking_date": "2026-06-30"}])
        self.assertEqual(self.live()[pid]["status"], "ineligible")
        self.assertEqual(json.loads(self.live()[pid]["desired_json"]), [])
        self.imp([{"row_id": 1, "booking_date": "2026-07-01"}])
        self.assertEqual(self.live()[pid]["status"], "open")


class TestDeliveredBankHalf(Base):
    def deliver(self, row_id, facts_row, pid=None):
        import reducer
        with db.tx(self.conn):
            pkg = self.conn.execute(
                "INSERT INTO packages(quarter, filename, path, built_at, partial, digest, size,"
                " caption, manifest_json) VALUES ('2026-Q3', 'books-2026-Q3.zip', '/x', ?, 0,"
                " 'd', 1, 'c', '{}')", (db.now(),)).lastrowid
            self.conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                              " created_at) VALUES (?, 'telegram', '/x', 'delivered', ?)",
                              (pkg, db.now()))
            self.conn.execute("INSERT INTO delivered_rows(package_id, row_id, pid, facts_fp)"
                              " VALUES (?,?,?,?)",
                              (pkg, row_id, pid, db.canonical(reducer.facts_of(facts_row))))

    def alerts(self):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT detail FROM alerts WHERE kind='delivered-changed' ORDER BY alert_id")]

    def test_a_corrected_delivered_row_alerts_once_per_occurrence(self):
        self.imp([{"row_id": 1}])
        self.deliver(1, dict(self.conn.execute("SELECT * FROM bank_rows WHERE row_id=1")
                             .fetchone()))
        self.assertEqual(self.imp([{"row_id": 1}])["delivered_changes"], 0)
        out = self.imp([{"row_id": 1, "amount_minor": 9000}])
        self.assertEqual(out["delivered_changes"], 1)
        self.assertEqual(self.imp([{"row_id": 1, "amount_minor": 9000}])["delivered_changes"], 0)
        out = self.imp([])
        self.assertEqual(out["delivered_changes"], 1)
        self.assertEqual([a["change"] for a in self.alerts()], ["corrected", "erased"])


    def test_after_a_rebind_a_delivered_row_is_erased_not_corrected(self):
        # the new ledger reuses row id 1 for another payment: that is not a correction
        import binding
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        self.deliver(1, dict(self.conn.execute("SELECT * FROM bank_rows WHERE row_id=1")
                             .fetchone()), pid=pid)
        self.granted(binding.acknowledge_ledger_reset_in_tx)
        other = "b" * 32
        self.token = self.pass_(instance=other)
        self.instance = other
        out = self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z",
                         "amount_minor": 9900, "counterparty": "Zapier"}])
        self.assertEqual(out["delivered_changes"], 1)
        self.assertEqual([a["change"] for a in self.alerts()], ["erased"])
        self.token = self.pass_(instance=other)
        out = self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z",
                         "amount_minor": 9900, "counterparty": "Zapier"}])
        self.assertEqual(out["delivered_changes"], 0)


class TestEndLineage(Base):
    def test_a_lineage_ends_once_and_records_one_residue(self):
        self.imp([{"row_id": 1}])
        (pid,) = self.live()
        with db.tx(self.conn):
            ledger.end_lineage(self.conn, pid, "vanished")
            ledger.end_lineage(self.conn, pid, "erased")
        self.assertEqual(lineage.projection(self.conn, pid)["ended"], "vanished")
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT reason, detail FROM residue WHERE pid=? AND reason='ended'", (pid,))],
            [("ended", "vanished")])

class TestDeliveredKindHalf(Base):
    deliver = TestDeliveredBankHalf.deliver
    alerts = TestDeliveredBankHalf.alerts

    def kinded(self, pid, row_id, kind):
        with db.tx(self.conn):
            pkg = self.conn.execute("SELECT max(package_id) FROM packages").fetchone()[0]
            self.conn.execute("INSERT INTO delivered_rows(package_id, row_id, pid, facts_fp,"
                              " kind) VALUES (?,?,?,'{}',?)", (pkg, row_id, pid, kind))

    def check(self, pid, **proj):
        with db.tx(self.conn):
            for k, v in proj.items():
                self.conn.execute(f"UPDATE projections SET {k}=? WHERE pid=?", (v, pid))
            return ledger.check_delivered_kind_half(self.conn, pid)

    def test_a_reclassified_row_alerts_once_and_follows_the_lineage_through_a_merge(self):
        self.imp([{"row_id": 1}, {"row_id": 2, "amount_minor": 5000}])
        a, b = sorted(self.live())
        self.deliver(1, dict(self.conn.execute("SELECT * FROM bank_rows WHERE row_id=1")
                             .fetchone()), pid=a)
        self.conn.execute("DELETE FROM delivered_rows")
        self.kinded(a, 1, "invoice")
        self.kinded(b, 2, "invoice")         # delivered under b, which then merged into a
        self.assertEqual(self.check(a, exp_kind="invoice"), 0)
        self.assertEqual(self.check(b, merged_into=a), 0)
        self.assertEqual(self.check(a, exp_kind="receipt"), 2)
        self.assertEqual(self.check(a), 0)                  # once per occurrence
        self.assertEqual({(x["row_id"], x["change"]) for x in self.alerts()},
                         {(1, "reclassified"), (2, "reclassified")})
        self.assertEqual(self.check(a, exp_kind=None), 0)    # unknown is not a change
        self.assertEqual(self.check(a, exp_kind="none", ended="erased"), 0)

    def test_a_lineage_is_alerted_when_its_own_row_is_read(self):
        # review C7 (L3): reading X never alerts Y; Y is alerted when Y is read, so a
        # short-lived change of Y's kind that is undone before Y's read raises nothing
        self.imp([{"row_id": 1}, {"row_id": 2, "amount_minor": 5000}])
        x, y = sorted(self.live())
        self.deliver(1, dict(self.conn.execute("SELECT * FROM bank_rows WHERE row_id=1")
                             .fetchone()), pid=x)
        self.conn.execute("DELETE FROM delivered_rows")
        self.kinded(x, 1, "invoice")
        self.kinded(y, 2, "invoice")
        self.check(x, exp_kind="invoice")
        self.check(y, exp_kind="invoice")
        with db.tx(self.conn):          # Y's kind moves outside Y's own read
            self.conn.execute("UPDATE projections SET exp_kind='receipt' WHERE pid=?", (y,))
        self.assertEqual(self.check(x), 0)                  # X's read: nothing about Y
        with db.tx(self.conn):          # ...and moves back before Y is read
            self.conn.execute("UPDATE projections SET exp_kind='invoice' WHERE pid=?", (y,))
        self.assertEqual(self.check(y), 0)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.check(y, exp_kind="receipt"), 1)   # Y's own read tells it


class TestInstance(Base):
    """Ledger identity is bank-feed's instance id (#69; plan §D4)."""
    OTHER = "b" * 32

    def test_a_fresh_store_binds_to_the_exports_instance(self):
        import binding
        self.imp([{"row_id": 1}])
        self.assertEqual(binding.get(self.conn)["ledger_instance"], self.LEDGER)

    def test_another_instance_imports_and_ends_nothing(self):
        # rounds p1-p4: a different ledger carrying the same account and none of our rows
        self.imp([{"row_id": 1}])
        self.token = self.pass_(instance=self.OTHER)
        self.instance = self.OTHER
        with self.assertRaises(db.Refusal):
            self.imp([{"row_id": 7, "first_seen": "2026-08-01T00:00:00Z"}])
        self.assertIsNone(list(self.live().values())[0]["ended"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_an_export_from_another_instance_than_the_probe_is_refused(self):
        self.imp([{"row_id": 1}])
        self.token = self.pass_()
        self.instance = self.OTHER          # list_backups said LEDGER; the export says OTHER
        with self.assertRaises(db.Refusal):
            self.imp([])
        self.assertIsNone(list(self.live().values())[0]["ended"])

    def test_everything_purged_on_the_same_instance_is_candidates(self):
        self.imp([{"row_id": 40}])
        out = self.imp([])
        self.assertEqual([c["row_id"] for c in out["erase_candidates"]], [40])

    def test_the_refusal_persists_while_the_other_instance_does(self):
        import passes
        self.imp([{"row_id": 1}])
        for _ in range(2):
            self.pass_(instance=self.OTHER)
            self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        self.pass_()
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])

    def test_the_operators_word_rebinds_the_store(self):
        import binding
        self.imp([{"row_id": 1}])
        (old,) = self.live()
        self.granted(binding.acknowledge_ledger_reset_in_tx)
        self.token = self.pass_(instance=self.OTHER)
        self.instance = self.OTHER
        out = self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z",
                         "counterparty": "Zapier", "amount_minor": 9900,
                         "booking_date": "2026-09-02", "value_date": "2026-09-02"},
                        {"row_id": 50, "first_seen": "2026-09-01T00:00:00Z"}])
        self.assertEqual(self.live()[old]["ended"], "erased")
        self.assertEqual(len(out["admitted"]), 2)
        # the NEW ledger's row 1 is not it: an erased lineage reads no row, keeps its facts
        proj = lineage.projection(self.conn, old)
        self.assertIsNone(lineage.live_row(self.conn, proj))
        saved = json.loads(proj["last_facts_json"])
        self.assertEqual((saved["counterparty"], saved["amount_minor"]), ("Adobe", 10000))
        b = binding.get(self.conn)
        self.assertEqual((b["ledger_reset_ack"], b["ledger_instance"]), (0, self.OTHER))
        self.assertEqual({r[0] for r in self.conn.execute("SELECT row_id FROM aliases")}, {1, 50})

    def test_the_acknowledgement_is_consumed_by_any_successful_import(self):
        # round p3 (Astra S1): an acknowledgement that met the same ledger stayed armed
        import binding
        import passes
        self.imp([{"row_id": 1}])
        self.granted(binding.acknowledge_ledger_reset_in_tx)
        self.token = self.pass_()
        self.imp([{"row_id": 1}])                         # the same instance
        self.assertEqual(binding.get(self.conn)["ledger_reset_ack"], 0)
        self.pass_(instance=self.OTHER)
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])

    def test_nothing_is_imported_while_the_gate_refuses(self):
        self.token = self.pass_(generation=1, registered={version.WORKFLOW: "b-1"})
        with self.assertRaises(db.Refusal):
            self.imp([{"row_id": 1}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0], 0)
        # the refusal the import met sticks: it is not rolled back with the import (D11)
        prior = self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
        self.assertEqual(json.loads(prior[0])["kind"], "dirty-ledger")
        gate = self.conn.execute("SELECT gate_json FROM passes WHERE generation=?",
                                 (self.token,)).fetchone()[0]
        self.assertFalse(json.loads(gate)["allowed"])

    def test_an_other_instance_refusal_met_by_the_import_is_recorded(self):
        self.imp([{"row_id": 1}])
        self.token = self.pass_(instance=self.OTHER)
        self.instance = self.OTHER
        with self.assertRaises(db.Refusal):
            self.imp([{"row_id": 7}])
        prior = self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'").fetchone()
        self.assertEqual(json.loads(prior[0])["kind"], "other-ledger")

    def test_a_ledger_that_changed_mid_pass_stops_the_passs_bank_writes(self):
        import passes
        self.imp([{"row_id": 1}])
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])
        with self.assertRaises(db.Refusal):     # same pass: row #1 is now another transaction
            self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z"}])
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        self.assertIsNone(passes.current_pass(self.conn)["snapshot_id"])
        self.assertFalse(self.conn.in_transaction)

    def test_a_ledger_switched_mid_pass_under_a_matching_probe_stops_the_pass(self):
        import passes
        self.imp([{"row_id": 1}])
        self.assertTrue(passes.bank_write_gate(self.conn)["allowed"])
        # still in this pass: list_backups is re-read and shows another instance,
        # and the export is read from it too
        passes.record_probe(self.conn, self.token, "ledger", True,
                            data={"generation": 0, "registered": {}, "instance": self.OTHER})
        self.instance = self.OTHER
        with self.assertRaises(db.Refusal):
            self.imp([{"row_id": 7}])
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])
        self.assertIsNone(passes.current_pass(self.conn)["snapshot_id"])
        self.assertIsNone(list(self.live().values())[0]["ended"])

    def test_an_export_from_another_instance_than_the_probe_stops_the_pass(self):
        import passes
        self.imp([{"row_id": 1}])
        self.instance = self.OTHER
        with self.assertRaises(db.Refusal):
            self.imp([{"row_id": 1}])
        self.assertFalse(passes.bank_write_gate(self.conn)["allowed"])

    def test_a_reused_row_id_is_refused_and_ends_nothing(self):
        self.imp([{"row_id": 1}])
        with self.assertRaises(db.Refusal):
            self.imp([{"row_id": 1, "first_seen": "2026-09-01T00:00:00Z"}])
        self.assertIsNone(list(self.live().values())[0]["ended"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)


# --- against the REAL bank-feed (moved from tests/test_freshness.py, simple loop Task 11):
# classification freshness at the import, the custody lock, and the first send / resend
# against a newer import. A pass is pass_ (the run's claim) with bank-feed's own probes,
# and its import runs under the run's acquisition (self.acq).
PDF = b"%PDF-1.4\n%%EOF\n"


def _raw(name, **args):
    """A tool called the way the model calls it (qa_server.handle): its answer's text."""
    import qa_server
    import tools  # noqa: F401  -- registers every tool
    out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    return out["result"]["content"][0]["text"]


def _call(name, **args):
    text = _raw(name, **args)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AssertionError(f"{name} answered {text!r}") from None


class RealLedger(StoreCase):
    def setUp(self):
        super().setUp()
        from tests import bankfeed
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.close)
        self.bf.account(category="company", label="Zakelijk")
        self.k = 0

    @staticmethod
    def text(name, **args):
        return _raw(name, **args)

    @staticmethod
    def call(name, **args):
        return _call(name, **args)

    def file(self, **meta):
        import documents
        self.k += 1
        args = dict(source_path=self.publish(f"d{self.k}.pdf", PDF + str(self.k).encode()),
                    kind="invoice", source="gmail", extraction_author="desk",
                    counterparty="Adobe", issuer="Adobe", currency="EUR",
                    document_number=f"N{self.k}")
        args.update(meta)
        return documents.ingest_document(self.conn, **args)["doc_id"]

    def tag(self, row_id, *tags):
        self.bf.call("tag_transaction", row_ids=[row_id], tags=list(tags))

    def retag(self, row_id, old, new):
        self.bf.call("untag_transaction", row_ids=[row_id], tags=[old])
        self.tag(row_id, new)

    def active(self):
        return self.bf.rows(state="active")

    def pid_of(self, row_id):
        return self.conn.execute("SELECT pid FROM aliases WHERE row_id=?", (row_id,)).fetchone()[0]

    def begin(self, do_import=True, candidates=0):
        """A run's pass (pass_): its probes as bank-feed answers them, then — unless
        `do_import` is false — the import of a fresh export under the run's acquisition,
        and each erase candidate bank-feed no longer has confirmed (record_not_found)."""
        import binding
        from tests.sim_job import ledger_state
        st = ledger_state(self.bf.listing())
        accounts = [{"account_id": r["account_id"], "category": r["category"],
                     "label": r["name"]}
                    for r in self.bf.conn.execute("SELECT account_id, category, name FROM"
                                                  " accounts")]
        token = self.pass_(generation=st["generation"], registered=st["registered"],
                           instance=st["instance"], accounts=accounts)
        self.assertTrue(binding.check_setup(self.conn)["can_run"])
        if do_import:
            imp = self.imp(token)
            self.assertEqual(len(imp["erase_candidates"]), candidates)
            import loop
            for c in imp["erase_candidates"]:          # the snapshot unit's confirmation
                if self.bf.call("get_transaction", row_id=c["row_id"]).startswith(
                        "no transaction #"):
                    loop.record_not_found(self.conn, token, c["pid"], imp["snapshot"])
        return token

    def imp(self, token):
        return ledger.import_ledger_export(self.conn, path=self.bf.export(), token=token,
                                           ledger_instance=self.bf.last_export_instance,
                                           acq=self.acq)

    def first_pass(self):
        """The first pass binds the account with today's quarter as its start; the
        fixtures live in 2026-Q3, so the start moves earlier (as "start from Q2" would)
        and a second pass admits them."""
        self.begin()
        self.end_live_pass()
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        self.begin()
        self.end_live_pass()

    def zip_of(self, quarter="2026-Q3"):
        import csv
        import io
        import package
        import zipfile
        pkg = package.build_quarterly_package(self.conn, quarter)
        z = zipfile.ZipFile(pkg["path"])
        rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        return pkg, sorted(n for n in z.namelist() if "/" in n), rows, z

    def two_rows_matched(self):
        """Adobe 10.00 (software, its invoice machine-matched) and Zapier 20.00, pid order
        Adobe < Zapier."""
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe"),
                  bf.row("2026-07-06", ref="Z1", amount=2000, counterparty="Zapier")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.tag(ids["A1"], "software")
        self.tag(ids["Z1"], "software")
        self.first_pass()
        doc = self.file(amount_minor=1000, document_date="2026-07-05")
        token = self.begin()
        self.machine_match(self.pid_of(ids["A1"]), doc, token)
        self.end_live_pass()
        self.assertLess(self.pid_of(ids["A1"]), self.pid_of(ids["Z1"]))
        return ids

    def stage(self, **kw):
        import delivery
        return delivery.stage_for_delivery(self.conn, channel="telegram", **kw)

    def posted(self, delivery_id):
        """r3 #2: a delivered outcome needs the post post_package marks."""
        with db.tx(self.conn):
            self.conn.execute("UPDATE deliveries SET posted_at=coalesce(posted_at, ?) WHERE"
                              " delivery_id=?", (db.now(), delivery_id))


class TestTheImportObservesTheClassification(RealLedger):
    """Round E2 (Terra) and issue #1: the import itself observes every exported row's
    classification — with no read of the row, the work list sees a reclassification."""
    def test_a_reclassification_is_known_at_the_import_without_a_read(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.tag(ids["Z1"], "software")
        self.tag(ids["A1"], "software")
        self.first_pass()
        self.retag(ids["A1"], "software", "refund")                  # wants a credit note now
        doc = self.file(amount_minor=1000, document_date="2026-07-06")   # an invoice
        token = self.begin()                                         # the import; no read
        tri = {d["pid"]: d for d in _call("list_quarter_state", triage=True)["triage"]}
        cur = tri[self.pid_of(ids["A1"])]
        self.assertTrue(cur["fresh"])
        self.assertEqual(cur["expectation"]["kind"], "credit-note")
        # design rev 17 §2: the kind gate is deleted — the invoice is paired, and the
        # pairing is fingerprinted against the live kind the import made known
        out = json.loads(_raw("record_match", pid=cur["pid"], doc_id=doc, author="auto",
                              expected_revision=cur["revision"], row_digest=cur["row_digest"],
                              pass_token=token, document_date="2026-07-01"))
        self.assertEqual(out["state"], "matched")
        fp = self.conn.execute("SELECT fp FROM log WHERE match_id=? AND kind='pair'",
                               (out["match_id"],)).fetchone()[0]
        self.assertEqual(json.loads(fp)["kind"], "credit-note")

    def test_the_machine_write_still_refuses_a_lineage_not_observed_at_the_import(self):
        # defense in depth: the guard stays for a lineage the latest import did not
        # observe (mutation check: without it this match would be recorded)
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="A1", amount=1000, counterparty="Adobe")])
        rid = self.active()[0]["row_id"]
        self.tag(rid, "software")
        self.first_pass()
        doc = self.file(amount_minor=1000, document_date="2026-07-05")
        token = self.begin()
        d = next(x for x in _call("list_quarter_state", triage=True)["triage"]
                 if x["pid"] == self.pid_of(rid))
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_snapshot=NULL WHERE pid=?",
                              (d["pid"],))
        out = _raw("record_match", pid=d["pid"], doc_id=doc, expected_revision=d["revision"],
                   row_digest=d["row_digest"], pass_token=token, author="auto",
                   document_date="2026-07-01")
        self.assertIn("was not in the latest bank import", out)


class TestAnImportCannotLandInsideABuild(RealLedger):
    def test_an_import_cannot_land_inside_a_build_in_flight(self):
        # round E6: the import takes the custody lock the build holds from its freeze
        # through its registration, so N+1 cannot land in between; it waits (here:
        # refuses Busy at a short bound) and lands after the build registered under N
        import multiprocessing
        from tests import _procs
        ids = self.two_rows_matched()
        self.retag(ids["A1"], "software", "refund")
        token = self.begin()                         # its import is N; the build freezes N
        ctx = multiprocessing.get_context("spawn")
        rendered, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        proc = ctx.Process(target=_procs.build_paused, args=("2026-Q3", rendered, resume, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(rendered.wait(60), "the build never rendered")
        n = lineage.latest_import(self.conn)
        saved, db.LOCK_BOUND_S = db.LOCK_BOUND_S, 0.5
        try:
            out_text = _raw("import_ledger_export", path=self.bf.export(), pass_token=token,
                            ledger_instance=self.bf.last_export_instance, acq=self.acq)
        finally:
            db.LOCK_BOUND_S = saved
        self.assertTrue(out_text.startswith("refused: "), out_text)
        self.assertEqual(lineage.latest_import(self.conn), n)       # nothing imported
        resume.set()
        proc.join(60)
        got = out.get(timeout=5)
        self.assertEqual(got[0], "ok", got)                          # registered under N
        self.imp(token)                                              # N+1 lands after it
        self.assertEqual(lineage.latest_import(self.conn), n + 1)
        self.assertEqual(_raw("stage_for_delivery", channel="telegram",
                              package_id=got[1]["package_id"]),
                         "refused: the bank was re-read since this package was built — "
                         "build it again")
        self.end_live_pass()
        _, files, rows, _ = self.zip_of()
        self.assertEqual(files, ["invoices/2026-07-05_Adobe_10.00.pdf"])   # §2: no kind gate
        self.assertEqual({r["counterparty"]: r["expectation_kind"] for r in rows},
                         {"Adobe": "credit-note", "Zapier": "invoice"})


class TestFirstSendChecksTheBuildSnapshot(RealLedger):
    """Round E4 (Terra, Astra): a package built under snapshot N, then a real
    reclassification and import N+1 — staging the still-unsent package carried the
    superseded invoice to the outbox. Its FIRST send is refused at the delivery-log
    write; a resend of a file already sent is that exact file."""
    def built_then_superseded(self, before_import=None):
        ids = self.two_rows_matched()
        pkg, files, rows, _ = self.zip_of()
        self.assertEqual(files, ["invoices/2026-07-05_Adobe_10.00.pdf"])     # MATCHED/invoice
        if before_import:
            before_import(pkg)
        self.retag(ids["A1"], "software", "refund")
        self.begin()                                                   # import N+1
        self.end_live_pass()
        return pkg

    def outbox_files(self):
        import os
        return sorted(os.listdir(self.outbox))

    def handoff_files(self):
        return sorted(str(p.relative_to(self.handoff)) for p in self.handoff.rglob("*")
                      if p.is_file() and "quarterly-accounting" in p.parts)

    def test_the_first_send_of_a_superseded_package_is_refused(self):
        import os
        pkg = self.built_then_superseded()
        handoff_before = self.handoff_files()
        out = _raw("stage_for_delivery", channel="telegram", package_id=pkg["package_id"])
        self.assertEqual(out, "refused: the bank was re-read since this package was built — "
                              "build it again")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.outbox_files(), [])
        self.assertEqual(self.handoff_files(), handoff_before)
        # built again after the import, it ships the truth and stages
        pkg2, files, rows, _ = self.zip_of()
        self.assertEqual(files, ["invoices/2026-07-05_Adobe_10.00.pdf"])   # §2: no kind gate
        staged = self.stage(package_id=pkg2["package_id"])
        self.assertEqual(self.outbox_files(), [os.path.basename(staged["path"])])

    def test_a_resend_of_a_package_already_sent_still_works_after_a_newer_import(self):
        import os
        import pathlib
        import delivery
        import views

        def send_uncertain(pkg):
            d = self.stage(package_id=pkg["package_id"])
            delivery.record_delivery(self.conn, delivery_id=d["delivery_id"],
                                     outcome="uncertain")
            for f in os.listdir(self.outbox):              # Casa consumed the outbox copy
                os.unlink(self.outbox / f)
        pkg = self.built_then_superseded(before_import=send_uncertain)
        r = _call("build_review", view="status", quarter="2026-Q3")
        self.assertIn(views.field(pkg["filename"]), r["text"])                     # offered again
        _call("mark_rendering_delivered", render_id=r["render_id"])
        # S7 §6.3/§8, #121: "send it again" is stage_for_delivery(resend=true), not a reading
        staged = _call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(staged["filename"], pkg["filename"])
        self.assertEqual(pathlib.Path(staged["path"]).read_bytes(),
                         pathlib.Path(pkg["path"]).read_bytes())


class TestImportRevokesAnUnsentFirstSend(RealLedger):
    """Round E5 (Terra S1): a first send staged under snapshot N stayed sendable after
    import N+1. The import revokes it in its own commit: the staged bytes go, and
    record_delivery refuses it. A resend of a file already sent is never revoked."""
    def built(self):
        self.two_rows_matched()
        pkg, files, _, _ = self.zip_of()
        self.assertEqual(files, ["invoices/2026-07-05_Adobe_10.00.pdf"])
        return pkg

    def test_a_first_send_staged_before_an_import_is_revoked(self):
        import os
        import package
        pkg = self.built()
        tg = self.stage(package_id=pkg["package_id"])
        pkg2 = package.build_quarterly_package(self.conn, "2026-Q3")     # a second copy
        other = self.stage(package_id=pkg2["package_id"])
        self.assertEqual(sorted(os.listdir(self.outbox)),
                         sorted(os.path.basename(d["path"]) for d in (tg, other)))
        self.begin()                                                # import N+1
        self.end_live_pass()
        self.assertEqual(os.listdir(self.outbox), [])                # nothing left to send
        for d in (tg, other):
            self.posted(d["delivery_id"])
            out = _raw("record_delivery", delivery_id=d["delivery_id"], outcome="delivered",
                       message_id="m-1")
            self.assertEqual(out, "refused: the bank was re-read before this was sent — "
                                  "nothing was recorded")
        rows = self.conn.execute("SELECT status, revoked_at IS NOT NULL FROM deliveries"
                                 " ORDER BY delivery_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("failed", 1), ("failed", 1)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM delivered_rows").fetchone()[0],
                         0)

    def test_a_resend_of_a_file_already_sent_is_not_revoked(self):
        import os
        import delivery
        pkg = self.built()
        d = self.stage(package_id=pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=d["delivery_id"], outcome="uncertain")
        for f in os.listdir(self.outbox):                  # Casa consumed the outbox copy
            os.unlink(self.outbox / f)
        r = _call("build_review", view="status", quarter="2026-Q3")
        _call("mark_rendering_delivered", render_id=r["render_id"])
        # #121: "send it again" is stage_for_delivery(resend=true), not a reading
        again = _call("stage_for_delivery", channel="telegram", resend=True)
        self.begin()                                                # import N+1
        self.end_live_pass()
        self.assertEqual(os.listdir(self.outbox), [os.path.basename(again["path"])])
        self.posted(again["delivery_id"])
        self.assertEqual(_call("record_delivery", delivery_id=again["delivery_id"],
                               outcome="delivered")["status"], "delivered")


class TestWithdrawalUnderTheCustodyLock(RealLedger):
    """Round E6 (Terra, Astra S1): the import withdraws the staged bytes before it
    commits the revocation, under the custody lock taken before its transaction."""
    built = TestImportRevokesAnUnsentFirstSend.built

    def staged(self):
        import os
        pkg = self.built()
        d = self.stage(package_id=pkg["package_id"])
        self.assertEqual(os.listdir(self.outbox), [os.path.basename(d["path"])])
        return pkg, d

    def state(self, delivery_id):
        # what another session sees: committed rows (a plain reader: open_store would
        # wait for the write lock the paused import holds)
        import sqlite3
        c = sqlite3.connect(str(db.data_dir() / db.DB_NAME))
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT status, revoked_at FROM deliveries WHERE delivery_id=?",
                          (delivery_id,)).fetchone()
            return r["status"], r["revoked_at"] is not None
        finally:
            c.close()

    def spawn(self, target, *args):
        import multiprocessing
        ctx = multiprocessing.get_context("spawn")
        ev, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        proc = ctx.Process(target=target, args=(*args, ev, resume, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(ev.wait(60), f"{target.__name__} never reached its pause")
        return proc, resume, out

    def short_bound(self):
        saved = db.LOCK_BOUND_S
        db.LOCK_BOUND_S = 0.5
        self.addCleanup(setattr, db, "LOCK_BOUND_S", saved)

    def test_the_bytes_are_gone_before_the_revocation_commits(self):
        import os
        from tests import _procs
        _, d = self.staged()
        token = self.begin(do_import=False)
        proc, resume, out = self.spawn(_procs.import_paused, self.bf.export(), token,
                                       self.bf.last_export_instance, self.acq)
        # paused between withdrawal and commit: nothing sendable, nothing revoked yet
        self.assertEqual(os.listdir(self.outbox), [])
        self.assertEqual(self.state(d["delivery_id"]), ("staged", False))
        resume.set()
        proc.join(60)
        self.assertEqual(out.get(timeout=5), ("ok", [d["delivery_id"]]))
        self.assertEqual(self.state(d["delivery_id"]), ("failed", True))
        self.assertEqual(os.listdir(self.outbox), [])

    def test_an_import_refuses_whole_while_the_custody_lock_is_held(self):
        import os
        import pathlib
        from tests import _procs
        pkg, d = self.staged()
        token = self.begin(do_import=False)
        n = lineage.latest_import(self.conn)
        proc, release, out = self.spawn(_procs.hold_custody)
        self.short_bound()
        text = _raw("import_ledger_export", path=self.bf.export(), pass_token=token,
                    ledger_instance=self.bf.last_export_instance, acq=self.acq)
        self.assertTrue(text.startswith("refused: "), text)
        self.assertEqual(lineage.latest_import(self.conn), n)       # nothing imported
        self.assertEqual(self.state(d["delivery_id"]), ("staged", False))
        self.assertEqual(pathlib.Path(d["path"]).read_bytes(),
                         pathlib.Path(pkg["path"]).read_bytes())    # intact, still consistent
        release.set()
        proc.join(60)
        imp = self.imp(token)
        self.assertEqual(imp["revoked_deliveries"], [d["delivery_id"]])
        self.assertEqual(os.listdir(self.outbox), [])

    @unittest.skipIf(hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0,
                     "root ignores the mode")
    def test_a_failed_withdrawal_refuses_the_whole_import(self):
        # round E7 (Terra, Astra S1): a withdrawal that fails must not leave the
        # superseded package sendable under a committed newer snapshot
        import os
        import pathlib
        pkg = self.built()
        d = self.stage(package_id=pkg["package_id"])
        staged = pathlib.Path(d["path"])
        locked = staged.parent          # the outbox
        os.chmod(locked, 0o500)                      # the withdrawal will fail
        self.addCleanup(lambda p=locked: p.exists() and os.chmod(p, 0o770))
        token = self.begin(do_import=False)
        n = lineage.latest_import(self.conn)
        text = _raw("import_ledger_export", path=self.bf.export(), pass_token=token,
                    ledger_instance=self.bf.last_export_instance, acq=self.acq)
        self.assertTrue(text.startswith("refused: could not withdraw a staged package "
                                        "— nothing was imported"), text)
        self.assertEqual(lineage.latest_import(self.conn), n)   # snapshot unchanged
        self.assertEqual(self.state(d["delivery_id"]), ("staged", False))  # not revoked
        self.assertEqual(staged.read_bytes(), pathlib.Path(pkg["path"]).read_bytes())
        os.chmod(locked, 0o770)                      # recovered: the retry commits
        imp = self.imp(token)
        self.assertEqual(imp["snapshot"], n + 1)
        self.assertEqual(imp["revoked_deliveries"], [d["delivery_id"]])
        self.assertFalse(staged.exists())
        self.assertEqual(self.state(d["delivery_id"]), ("failed", True))

    def test_imports_and_builds_in_two_processes_never_deadlock(self):
        import multiprocessing
        import time
        from tests import _procs
        self.built()
        token = self.begin()
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        proc = ctx.Process(target=_procs.build_repeatedly, args=("2026-Q3", 6, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        snaps, deadline = [], time.monotonic() + 120
        while (proc.is_alive() or len(snaps) < 2) and time.monotonic() < deadline:
            snaps.append(self.imp(token)["snapshot"])
        proc.join(120)
        self.assertFalse(proc.is_alive(), "the builds never finished")
        got = out.get(timeout=5)
        self.assertEqual(got[0], "ok", got)
        # every build ran whole between two imports (the lock serializes them), so none
        # was superseded before registering, and none waited past the bound
        self.assertEqual(got[1], ["ok"] * 6)
        self.assertEqual(snaps, sorted(set(snaps)))


class TestPackageWithANonFreshMember(RealLedger):
    """A build never ships a non-fresh row's documents as MATCHED: the row ships as
    UNCLASSIFIED, its documents under unresolved/, and the caption counts it (spec
    §Error handling: packaging ships rather than blocking). Since issue #1 every row
    the export carries is observed at the import, so this is defense in depth: the
    lineage is made unobserved by hand (mutation check of the build's guard)."""
    def test_a_matched_row_not_observed_at_the_import_does_not_ship_its_invoice(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="Z1", amount=2000, counterparty="Zapier"),
                  bf.row("2026-07-06", ref="A1", amount=1000, counterparty="Adobe")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.tag(ids["Z1"], "software")
        self.tag(ids["A1"], "software")
        self.first_pass()
        doc = self.file(amount_minor=1000, document_date="2026-07-06")
        token = self.begin()
        self.machine_match(self.pid_of(ids["A1"]), doc, token)
        self.assertEqual(lineage.projection(self.conn, self.pid_of(ids["A1"]))["status"],
                         "matched")
        self.end_live_pass()
        self.begin()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_snapshot=NULL WHERE pid=?",
                              (self.pid_of(ids["A1"]),))
        self.end_live_pass()
        pkg, files, rows, z = self.zip_of()
        self.assertEqual(files, ["unresolved/2026-07-06_Adobe_10.00.pdf"])
        st = {r["counterparty"]: (r["status"], r["expectation_kind"], r["document"]) for r in rows}
        self.assertEqual(st["Adobe"], ("UNCLASSIFIED", "", ""))
        self.assertEqual(st["Zapier"], ("MISSING", "invoice", ""))
        # simple loop §1: one line; the unread row is not documented (UNCLASSIFIED: open)
        self.assertTrue(pkg["caption"].endswith(" · 0 of 2 documented · 2 open"),
                        pkg["caption"])
        self.assertIn("## Not seen in the last bank check", z.read("notes.md").decode())


class TestFreshnessProperty(RealLedger):
    """Issue #1's invariant, over random reclassifications and a purge (fixed seed): after
    ANY import, every lineage whose row the export carries is fresh with the kind
    bank-feed's live tags derive, and the package ships that kind's truth; a lineage whose
    row the export dropped is not fresh, never machine-matched, and ships UNCLASSIFIED with
    no document."""
    KIND = {"software": "invoice", "refund": "credit-note"}

    def test_every_exported_row_is_fresh_with_its_live_kind_after_any_import(self):
        import random
        import matches
        import work
        bf = self.bf
        refs = ["R%d" % i for i in range(6)]
        bf.fetch([bf.row("2026-07-%02d" % (i + 1), ref=r, amount=1000 + i,
                         counterparty="Vendor%d" % i) for i, r in enumerate(refs)])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        live = {}
        for ref, rid in ids.items():
            self.tag(rid, "software")
            live[ref] = "software"
        self.first_pass()
        docs = {r: self.file(counterparty="Vendor%d" % i, issuer="Vendor%d" % i,
                             amount_minor=1000 + i, document_date="2026-07-%02d" % (i + 1))
                for i, r in enumerate(refs)}
        rng = random.Random(20260928)
        purged = set()
        matched = 0
        for trial in range(5):
            for ref in rng.sample(sorted(set(refs) - purged), 2):     # reclassify two
                new = "refund" if live[ref] == "software" else "software"
                self.retag(ids[ref], live[ref], new)
                live[ref] = new
            if trial == 2:                                             # drop the earliest row
                bf.purge_before("2026-07-02")
                purged.add("R0")
            token = self.begin(candidates=1 if trial == 2 else 0)
            for d in work.list_quarter_state(self.conn, "2026-Q3")["items"]:
                p = lineage.projection(self.conn, d["pid"])
                ref = next(k for k, v in ids.items() if v == p["dest_row_id"])
                self.assertEqual(d["fresh"], ref not in purged, (trial, ref))
                if ref in purged:
                    continue
                self.assertEqual(d["expectation"]["kind"], self.KIND[live[ref]], (trial, ref))
                if d["status"] != "open":
                    continue
                # design rev 17 §2: no kind gate — a fresh open payment takes its invoice
                # whatever its live kind (the kind assertion above is the freshness proof)
                matches.record_match(self.conn, pid=d["pid"], doc_id=docs[ref], author="auto",
                                     expected_revision=d["revision"],
                                     row_digest=d["row_digest"], token=token)
                matched += 1
            self.end_live_pass()
            _, files, rows, _ = self.zip_of()
            for r in rows:
                ref = next(k for k in refs if r["counterparty"] == "Vendor%s" % k[1:])
                if ref in purged:
                    self.assertEqual((r["status"], r["document"]), ("UNCLASSIFIED", ""))
                else:
                    self.assertEqual(r["expectation_kind"], self.KIND[live[ref]], (trial, ref))
                    self.assertEqual(r["status"], "MATCHED" if r["document"] else "MISSING",
                                     (trial, ref))        # §2: a kept pairing ships, whatever the kind
        self.assertEqual(matched, len(refs))                  # every payment took its invoice


if __name__ == "__main__":
    unittest.main()


class TestPackageUnsettledClassification(RealLedger):
    """#105 d1 (Terra): an open payment whose tags conflict ships UNCLASSIFIED, as the cards
    say "not classified yet" — never MISSING."""
    def test_a_conflicting_payment_ships_unclassified(self):
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="W1", amount=148000, counterparty="Loon"),
                  bf.row("2026-07-06", ref="Z1", amount=2000, counterparty="Zapier")])
        ids = {r["provider_ref"]: r["row_id"] for r in self.active()}
        self.tag(ids["W1"], "payroll")
        self.tag(ids["W1"], "taxes")
        self.tag(ids["Z1"], "software")
        self.first_pass()
        pkg, files, rows, z = self.zip_of()
        st = {r["counterparty"]: r["status"] for r in rows}
        self.assertEqual(st["Loon"], "UNCLASSIFIED")
        self.assertEqual(st["Zapier"], "MISSING")
