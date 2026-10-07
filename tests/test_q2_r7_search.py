"""Q2 re-run R7 (PLAY, 8044f908, 2026-10-07): three invoices run 1 found were decided
missing. One was emailed before the search window (run 1 found it by the remittance's
reference); one sat behind a page of the vendor's shipping notices (found by the order
number); one came back three times but its PDF shows only by listing the message's
attachments (run 1 had a learned hint and listed them). The skill now searches the
remittance's reference or order number with no dates after a learned hint, and lists the
attachments of a message naming the payment before deciding `missing`. Shifted into the
sim's quarter (Q3 2026), same shapes."""
from tests._base import StoreCase
from tests.sim_job import JobDriver

REGISTRY, SHOP = "Registry", "Shop"


class R7Searches(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def quarter(self, mode, job_id):
        drv = JobDriver(self, payments=0)
        g = drv.gmail
        g.cap = 20                                       # Gmail's page
        # (a) paid 1 Jul; the invoice was emailed 24 Jun, before the window, naming the reference
        a = drv.pay_once(REGISTRY, 8000, "2026-07-01", remittance="260189773 To Registry PU")
        g.invoice(REGISTRY, 8000, "EUR", "2026-06-24", "KVK-1", mentions=["260189773"])
        # (b) paid 28 Jul, order ME280726000457: the invoice rides on a "shipped" email whose
        # PDF only list_attachments shows, behind 25 later shipping notices (a page is 20)
        b = drv.pay_once(SHOP, 69580, "2026-07-28", remittance="ME280726000457 Shop B.V.")
        g.invoice(SHOP, 69580, "EUR", "2026-07-28", "M003188553", sender="notify@shop.example",
                  mentions=["ME280726000457"], listed=True)
        g.notices(SHOP, [f"2026-08-{d:02d}" for d in range(6, 31)], sender="notify@shop.example")
        # (c) paid 4 Aug, order ME040826000902: one "shipped" email, PDF listed only
        c = drv.pay_once(SHOP, 27796, "2026-08-04", remittance="ME040826000902 Shop B.V.")
        g.invoice(SHOP, 27796, "EUR", "2026-08-05", "M003193800", sender="notify@shop.example",
                  mentions=["ME040826000902"], listed=True)
        drv.search_mode = mode
        drv.casa_cut = 80
        drv.run_job(job_id)
        status = {no: self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                        (drv.pid_of(no),)).fetchone()[0] for no in (a, b, c)}
        return drv, status

    def test_r7_vendor_searches_alone_decide_all_three_missing(self):
        drv, status = self.quarter("dated", "c7c7c7c7-11")
        self.assertEqual(sorted(status.values()), ["open"] * 3)        # decided missing
        self.assertTrue(all("after:" in q for _, _, q in drv.search_log))

    def test_the_reference_first_and_listed_attachments_match_all_three(self):
        drv, status = self.quarter("reference", "c7c7c7c7-12")
        self.assertEqual(sorted(status.values()), ["matched"] * 3)
        undated = [q for v, k, q in drv.search_log if "after:" not in q]
        self.assertIn("260189773", undated)
        self.assertIn("ME280726000457", undated)
        # the pre-window invoice matched with its own printed date
        doc = self.conn.execute("SELECT document_date FROM documents WHERE document_number"
                                "='KVK-1'").fetchone()
        self.assertEqual(doc[0], "2026-06-24")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM documents").fetchone()[0], 3)       # nothing filed twice

    def test_a_learned_hint_still_goes_first(self):
        import kb
        kb.upsert_counterparty(self.conn, SHOP, hint_sender="notify@shop.example")
        drv, status = self.quarter("reference", "c7c7c7c7-13")
        shop = [(k, q) for v, k, q in drv.search_log if v == SHOP]
        self.assertEqual(shop[0][0], "hinted")
        self.assertEqual(sorted(status.values()), ["matched"] * 3)
