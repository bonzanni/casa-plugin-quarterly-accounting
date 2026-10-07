"""Q2 re-run R7 (PLAY, 8044f908, 2026-10-07): the run-1 fix told the model to `Read` a
download FIRST — finance's path_scope denies the handoff folder, so every found invoice was
set aside and its payment went missing (AWS, matched in run 1). The sim never enforced
path_scope on Read. Now a document is filed with no reading, read through read_document
(the session copy Read may open), and its reading recorded with update_document_metadata;
a document a cut left unread is handed `unread` and read before it is judged."""
from tests._base import StoreCase
from tests.sim_job import CasaCut, JobDriver


class ReadingUnderPathScope(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def quarter(self, n, job_id, mode):
        drv = JobDriver(self, payments=n)
        for i in range(n):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        drv.read_mode = mode
        drv.casa_cut = 80
        drv.run_job(job_id)
        return drv, dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall())

    def test_r7_reading_a_download_first_is_denied_and_loses_the_invoices(self):
        drv, statuses = self.quarter(6, "c7c7c7c7-01", "handoff")
        self.assertFalse(any(p.startswith(drv.SESSION) for p in drv.reads))
        self.assertNotIn("matched", statuses)                     # every invoice set aside

    def test_filed_then_read_through_read_document_matches_and_never_reads_a_download(self):
        drv, statuses = self.quarter(30, "c7c7c7c7-02", "store")
        self.assertEqual(statuses, {"matched": 30})
        self.assertTrue(drv.reads)
        self.assertTrue(all(p.startswith(drv.SESSION) for p in drv.reads), drv.reads[:3])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE amount_minor"
                                           " IS NULL").fetchone()[0], 0)

    def test_a_cut_between_filing_and_reading_is_read_on_the_redo(self):
        drv = JobDriver(self, payments=2)
        for i in range(2):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        real, cut = drv._read, []

        def cut_first_read(path):
            if not cut:
                cut.append(path)
                raise CasaCut()                  # filed; its reading never recorded
            return real(path)
        drv._read = cut_first_read
        drv.casa_cut = 80
        units = drv.run_job("c7c7c7c7-03")
        self.assertEqual(len(cut), 1)
        self.assertTrue(any(c.get("unread") for u in units if u["unit"] == "payment"
                            for c in u["candidates"]))
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 2})

    def test_the_skill_never_reads_a_download(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parent.parent / "skills/quarterly-job/SKILL.md"
                ).read_text()
        self.assertNotIn("`Read` it FIRST", text)
        self.assertNotIn("`Read` each FIRST", text)
        self.assertIn("A download cannot be `Read`: file it first with no amount, date or\n"
                      "number, then `read_document(doc_id)`, `Read` the path it names", text)
        self.assertIn("`unread`: read it first", text)
