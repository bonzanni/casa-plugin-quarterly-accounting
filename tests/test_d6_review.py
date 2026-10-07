"""Diff round d6 (Astra, 26b68ee..90f6cbf), the two findings that led to the server-owned
work queues (operator ruling A), reproduced through the real surface (qa_server.TOOLS, a
real bank-feed) under Casa's 80-call cut:
- Astra S1 "46/60 missing": the vendor search is recorded as soon as it ran, before its
  attachments are filed (the skill's order); the unit's budget ends with most of them
  unfiled. Every found attachment must still be filed and every payment matched.
- Astra S2 "stale erase holder": 30 payments of an earlier run are gone from bank-feed;
  only some of their erasure checks fit the first budget. Every one must still be
  confirmed in the same run, so an erased payment never holds a live payment's invoice."""
from tests._base import StoreCase
from tests.sim_job import JobDriver

CASA_CALLS = 80


def statuses(conn) -> dict:
    return dict(conn.execute("SELECT status, count(*) FROM projections WHERE ended IS NULL"
                             " GROUP BY status").fetchall())


class FoundAttachmentsAreFiled(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_d6_60_found_invoices_recorded_before_filing_all_match(self):
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        drv.casa_cut = CASA_CALLS
        drv.run_job("d6d6d6d6-a1")     # the sim records each search first, as the skill says
        self.assertEqual(drv.cuts, len(drv.batch_calls) - 1)   # no budget: Casa ends a batch
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM documents WHERE source='gmail'").fetchone()[0], 60)
        self.assertEqual(statuses(self.conn), {"matched": 60})
        run = self.conn.execute("SELECT partial, completed_at FROM runs WHERE"
                                " job_id='d6d6d6d6-a1'").fetchone()
        self.assertEqual(run["partial"], 0)
        self.assertIsNotNone(run["completed_at"])


class ErasuresAreAllConfirmed(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_d6_30_erased_payments_are_all_confirmed_and_free_the_live_invoice(self):
        import lineage
        drv = JobDriver(self, payments=31)
        for i in range(31):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        drv.casa_cut = CASA_CALLS
        drv.run_job("d6d6d6d6-b1")
        self.assertEqual(statuses(self.conn), {"matched": 31})
        # bank-feed loses 30 of the rows (erased), and the last of them comes back under a
        # new row id: a new payment whose invoice the erased one still holds
        bf = drv.bf
        rows = bf.rows(state="active")
        gone = rows[1:]
        holder = self.conn.execute("SELECT pid FROM aliases WHERE row_id=?",
                                   (gone[-1]["row_id"],)).fetchone()[0]
        bf.conn.execute("DELETE FROM transactions WHERE row_id IN (%s)"
                        % ",".join(str(r["row_id"]) for r in gone))
        bf.conn.commit()
        back = gone[-1]
        bf.fetch([bf.row(rows[0]["booking_date"], amount=rows[0]["amount_minor"],
                         ref=rows[0]["provider_ref"], counterparty=rows[0]["counterparty"]),
                  bf.row(back["booking_date"], amount=back["amount_minor"],
                         ref=back["provider_ref"] + "-again", counterparty=back["counterparty"])])
        new = [r["row_id"] for r in bf.rows(state="active") if r["row_id"] not in
               {x["row_id"] for x in rows}]
        bf.call("tag_transaction", row_ids=new, tags=["software"])
        drv.run_job("d6d6d6d6-b2")
        self.assertEqual(lineage.projection(self.conn, holder)["ended"], "erased")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                           " ended='erased'").fetchone()[0], 30)
        self.assertEqual(statuses(self.conn), {"matched": 2})
        run = self.conn.execute("SELECT partial FROM runs WHERE job_id='d6d6d6d6-b2'"
                                ).fetchone()
        self.assertEqual(run["partial"], 0)


class ErasureProgressIsReported(StoreCase):
    """d7 Astra S1b: confirming erasures is progress (rev 18.4 §R18.5, progress in ONE
    place): 150 erased payments take several batches, each reports progress, and the run
    completes with every one confirmed."""

    def setUp(self):
        super().setUp()
        self.bind()

    def test_d7_150_erasures_report_progress_every_batch_and_complete(self):
        drv = JobDriver(self, payments=151)
        drv.run_job("d7d7d7d7-b1")
        bf = drv.bf
        rows = bf.rows(state="active")
        gone = rows[1:]
        bf.conn.execute("DELETE FROM transactions WHERE row_id IN (%s)"
                        % ",".join(str(r["row_id"]) for r in gone))
        bf.conn.commit()
        bf.fetch([bf.row(rows[0]["booking_date"], amount=rows[0]["amount_minor"],
                         ref=rows[0]["provider_ref"], counterparty=rows[0]["counterparty"])])
        drv.casa_cut = CASA_CALLS
        drv.run_job("d7d7d7d7-b2")
        self.assertGreater(len(drv.batch_reported), 2)
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                           " ended='erased'").fetchone()[0], 150)
        self.assertIsNotNone(self.conn.execute("SELECT completed_at FROM runs WHERE"
                                               " job_id='d7d7d7d7-b2'").fetchone()[0])
