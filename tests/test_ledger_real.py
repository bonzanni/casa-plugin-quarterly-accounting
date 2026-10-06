"""Admission and lineage behaviour against bank-feed's REAL export,
reconcile, apply_plan and purge (spec §Testing, rounds 11-13, 41)."""
import unittest

from tests._base import StoreCase
from tests import bankfeed
import ledger  # noqa: E402
import db  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.conn.close)
        self.bf.account()
        self.bind(account=bankfeed.Ledger.ACCOUNT)

    def imp(self):
        t = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                       instance=self.bf.instance())
        path = self.bf.export()
        return ledger.import_ledger_export(self.conn, path=path, token=t,
                                           ledger_instance=self.bf.last_export_instance,
                                           acq=self.acq)


    def live(self):
        return {r["pid"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM projections WHERE merged_into IS NULL")}


class TestReal(Base):
    def test_pending_admitted_on_value_date_follows_its_supersession(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", status="PDNG")])
        self.imp()
        (pid,) = self.live()
        self.bf.fetch([self.bf.row("2026-07-06", ref="R1", status="BOOK")])
        self.imp()
        booked = self.bf.rows(state="active")[0]["row_id"]
        self.assertEqual(self.live()[pid]["dest_row_id"], booked)
        self.assertEqual(len(self.live()), 1)

    def test_june_30_corrected_to_july_1_is_admitted_the_next_pass(self):
        self.bf.fetch([self.bf.row("2026-06-30", ref="R1")])
        self.imp()
        self.assertEqual(self.live(), {})
        self.bf.fetch([self.bf.row("2026-07-01", ref="R1")])
        self.imp()
        self.assertEqual(len(self.live()), 1)

    def test_dbit_corrected_in_place_to_crdt_keeps_its_projection(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.imp()
        (pid,) = self.live()
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", direction="CRDT")])
        self.imp()
        self.assertEqual(list(self.live()), [pid])
        self.assertEqual(self.conn.execute("SELECT direction FROM bank_rows WHERE row_id=?",
                                           (self.live()[pid]["dest_row_id"],)).fetchone()[0], "CRDT")

    def test_tombstoned_row_ends_vanished(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")], cap=bankfeed.CAP_UNKNOWN)
        self.imp()
        (pid,) = self.live()
        self.bf.fetch([], cap=bankfeed.CAP_UNKNOWN)
        out = self.imp()
        self.assertEqual(out["ended_vanished"], [pid])

    def test_purged_row_is_an_erase_candidate_and_ids_are_never_reused(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.imp()
        (pid,) = self.live()
        old_id = self.live()[pid]["dest_row_id"]
        self.bf.purge_before("2026-08-01")
        out = self.imp()
        self.assertEqual([c["pid"] for c in out["erase_candidates"]], [pid])
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.imp()
        new_ids = {r["row_id"] for r in self.bf.rows()}
        self.assertNotIn(old_id, new_ids)

    def test_cut_chain_at_the_floor_ends_nothing(self):
        # round-41 case: pending #1 (30 Jun) superseded by booked #2 (1 Jul),
        # purge before 1 Jul deletes ZERO rows of the chain at bank-feed >= 0.13.0.
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-06-01'")
        self.bf.fetch([self.bf.row("2026-06-30", ref="R1", status="PDNG")])
        self.imp()
        (pid,) = self.live()
        self.bf.fetch([self.bf.row("2026-07-01", ref="R1", status="BOOK")])
        stats = self.bf.purge_before("2026-07-01")
        self.assertEqual(stats["transactions"], 0)
        out = self.imp()
        self.assertEqual((out["erase_candidates"], out["ended_vanished"]), ([], []))
        self.assertEqual(self.live()[pid]["dest_row_id"], self.bf.rows(state="active")[0]["row_id"])

    def test_cut_chain_below_the_floor_shows_why_the_floor_exists(self):
        out = bankfeed.run_below_floor("""
import ingest, apply, store, tempfile, pathlib
d = tempfile.mkdtemp(); c = store.open_db(pathlib.Path(d) / 'f.sqlite')
CAP = {"ref_stable": True, "ref_scope": "account", "observed_n": 200}
def row(date, status):
    return {"account_id": "a", "booking_date": date, "value_date": date, "amount_minor": 100,
            "currency": "EUR", "direction": "DBIT", "counterparty": "X", "remittance": "",
            "provider_ref": "R1", "provider_ref_kind": "entry_reference", "status": status,
            "raw_json": "{}"}
IV = ("2026-01-01", "2026-12-31")
apply.apply_plan(c, "a", ingest.reconcile([], [row("2026-06-30", "PDNG")], IV, CAP))
allr = [dict(r) for r in c.execute("SELECT * FROM transactions")]
apply.apply_plan(c, "a", ingest.reconcile(allr, [row("2026-07-01", "BOOK")], IV, CAP))
apply.purge_before(c, "2026-07-01")
print(sorted((r[0], r[1]) for r in c.execute("SELECT row_id, state FROM transactions")))
""")
        self.assertEqual(out.strip(), "[(2, 'active')]")   # #1 gone, #2 survives: a cut chain


if __name__ == "__main__":
    unittest.main()
