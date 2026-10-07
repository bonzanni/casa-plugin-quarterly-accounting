"""Diff round d5 (Astra, 26b68ee..dccbaea), reproduced through the real surface under
Casa's 80-call cut. Astra S1 (ruled: generalize — the 2nd instance of "which attachment
refs are already filed"): ONE server-side membership for every attachment ref, own mail
and a vendor's alike — of the refs a search found, only the exact ones no ingest of ANY run
filed (work.filed) join the run's queue and are answered as `files`; no per-run list."""
from tests._base import StoreCase
from tests.sim_job import JobDriver

CASA_CALLS = 80


class OneMembershipAcrossRuns(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def delivered(self, jid):
        return self.conn.execute("SELECT count(*) FROM renders r JOIN runs u ON"
                                 " u.end_render_id=r.render_id WHERE u.job_id=? AND"
                                 " r.delivered_at IS NOT NULL", (jid,)).fetchone()[0]

    def test_astras_two_runs_the_old_invoices_first(self):
        """Run 1: 40 Zapier payments and invoices matched. Then 43 more of each; the
        mailbox lists the 40 old invoices first. Run 2 files only the new ones and
        completes."""
        drv = JobDriver(self, payments=40)
        for i in range(40):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"INV-{i + 1}")
        drv.casa_cut = CASA_CALLS
        drv.run_job("d5d5d5d5-a1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                           " status='matched'").fetchone()[0], 40)
        drv.add_payments([drv.DATES[i % 3] for i in range(40, 83)])
        for i in range(40, 83):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"INV-{i + 1}")
        before, cuts = len(drv.ingested), drv.cuts
        old = {m["ref"] for m in drv.gmail.messages[:40]}
        drv.run_job("d5d5d5d5-a2")
        again = drv.ingested[before:]
        self.assertFalse(old & set(again))                      # no old one again
        # each new one filed; one a cut parted from its reading is filed again (h1)
        self.assertEqual(len(set(again)), 43)
        self.assertEqual(drv.cuts - cuts, len(drv.batch_calls) - 1)   # no budget: Casa ends a batch
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 83)
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 83})
        run = self.conn.execute("SELECT * FROM runs WHERE job_id='d5d5d5d5-a2'").fetchone()
        self.assertIsNotNone(run["completed_at"])
        self.assertFalse(run["partial"])
        self.assertEqual(self.delivered("d5d5d5d5-a2"), 1)          # one end message
        again = drv.run_job("d5d5d5d5-a3", started_by="scheduled")
        self.assertEqual(sum(len(u["calls"]) for u in again if u["unit"] == "mirror"), 0)

    def test_a_ref_filed_in_an_earlier_run_is_not_offered_again(self):
        """Own mail and a vendor's message alike: the next run's probe answers only the
        refs no ingest of any run filed."""
        drv = JobDriver(self, payments=2)
        own = drv.gmail.own(1000, day=drv.DATES[0], number="OWN-1")
        drv.gmail.invoice("Zapier", 2000, "EUR", drv.DATES[1], "INV-2")
        drv.run_job("d5d5d5d5-b1")
        vendor_ref = drv.gmail.messages[0]["ref"]
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT ref FROM operator_refs")), sorted([own, vendor_ref]))
        drv.to_unit("d5d5d5d5-b2", "filing")
        out = drv._tool("record_probe", dict(pass_token=drv.token, kind="gmail", ok=True,
                                             data={"refs": ["new-msg:att-1", own, vendor_ref]}))
        self.assertEqual((out["files"], out["files_total"]), (["new-msg:att-1"], 1))

    def test_a_legacy_bare_message_id_counts_for_every_attachment_of_it(self):
        """A vendor document filed before refs named the attachment holds the bare message
        id: it counts as filed for every attachment of that message (the vendor step then
        filed every plausible invoice of a message under that one id); a message it does
        not name is offered."""
        import db
        drv = JobDriver(self, payments=1)
        drv.to_unit("d5d5d5d5-c1", "filing")
        doc = drv.file_document(vendor="Zapier", amount_minor=1000)
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET source_ref='legacy-msg' WHERE doc_id=?",
                              (doc,))
            self.conn.execute("DELETE FROM operator_refs")
        out = drv._tool("record_probe", dict(pass_token=drv.token, kind="gmail", ok=True,
                                             data={"refs": ["legacy-msg:att-1",
                                                            "legacy-msg:att-2",
                                                            "fresh-msg:att-1"]}))
        self.assertEqual(out["files"], ["fresh-msg:att-1"])

    def test_one_vendor_email_with_two_invoices_files_and_matches_both(self):
        drv = JobDriver(self, payments=2)                   # Zapier EUR 10.00 and 20.00
        first = drv.gmail.invoice("Zapier", 1000, "EUR", drv.DATES[0], "INV-A")
        second = drv.gmail.invoice("Zapier", 2000, "EUR", drv.DATES[1], "INV-B",
                                   message=drv.gmail.messages[0]["id"])
        self.assertEqual(first.split(":")[0], second.split(":")[0])   # one email
        drv.casa_cut = CASA_CALLS
        drv.run_job("d5d5d5d5-d1")
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT source_ref FROM documents")), sorted([first, second]))
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 2})
