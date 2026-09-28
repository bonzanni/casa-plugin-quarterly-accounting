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
                                           ledger_instance=getattr(self, "instance", self.LEDGER))

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
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=self.export_csv([{"row_id": 1}]), token=t,
                                        ledger_instance=self.LEDGER)
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
        out = self.imp([
            {"row_id": 1, "state": "superseded", "superseded_by": 3},
            {"row_id": 2, "state": "superseded", "superseded_by": 3,
             "first_seen": "2026-07-03T09:00:00Z"},
            {"row_id": 3, "first_seen": "2026-07-05T08:00:00Z"}])
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
        out = self.imp([
            {"row_id": 1, "state": "superseded", "superseded_by": 3},
            {"row_id": 2, "state": "superseded", "superseded_by": 3,
             "first_seen": "2026-07-03T09:00:00Z"},
            {"row_id": 3, "first_seen": "2026-07-05T08:00:00Z"}])
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
        binding.acknowledge_ledger_reset(self.conn)
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
        binding.acknowledge_ledger_reset(self.conn)
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
        binding.acknowledge_ledger_reset(self.conn)
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
        binding.acknowledge_ledger_reset(self.conn)
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


if __name__ == "__main__":
    unittest.main()
