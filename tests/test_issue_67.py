"""0.11.7: issue #67 — a handed-over invoice is read first and matched only against the
payments it could fit (prod 2026-10-08: a desk filing with no reading started a full sweep,
fitted nothing, and the end card said "No payment fits it yet" three times with a Q4
package button). One handover of N documents is ONE run over the union of their payments."""
import datetime as dt

from tests._base import StoreCase, untag
import db                     # server/ is on sys.path once tests._base is imported

NOW = dt.datetime.fromisoformat("2026-10-06T12:00:00+00:00")
SWEEP = ("probes", "snapshot", "erasures", "filing", "mirror")


def kinds(units):
    return [u["unit"] for u in units if u["unit"] != "report"]


class Case(StoreCase):
    def setUp(self):
        super().setUp()
        from tests.sim_job import JobDriver
        self.bind()
        self.drv = JobDriver(self, payments=3)       # Zapier 10.00 Jul, 20.00 Aug, 30.00 Sep
        with self.patch_clock(NOW):
            self.drv.run_job("aaaaaaaa-1")            # the full check: all three missing
        self.pids = [r[0] for r in self.conn.execute(
            "SELECT pid FROM run_work WHERE job_id='aaaaaaaa-1' ORDER BY pid")]
        self.searches = len(self.drv.gmail.searches)
        self.bank_calls = len(self.drv.bank_log)

    def give(self, *docs):
        import asks
        return asks.request_work(self.conn, "handover", "operator", doc_ids=list(docs))

    def go(self, job_id="bbbbbbbb-2", started_by="operator"):
        with self.patch_clock(NOW):
            return self.drv.run_job(job_id, started_by=started_by)

    def card(self, job_id="bbbbbbbb-2"):
        dep = self.drv.posted_end(job_id)
        return untag(dep["text"]), [b["label"] for b in dep["buttons"]]

    def work(self, job_id="bbbbbbbb-2"):
        return [tuple(r) for r in self.conn.execute(
            "SELECT pid, why, outcome FROM run_work WHERE job_id=? ORDER BY pid", (job_id,))]

    def no_sweep(self, units):
        self.assertFalse(set(kinds(units)) & set(SWEEP), kinds(units))
        self.assertEqual(len(self.drv.gmail.searches), self.searches)
        self.assertEqual(len(self.drv.bank_log), self.bank_calls)


class OneUnreadInvoice(Case):
    def test_it_is_read_first_then_only_its_payment_is_worked_and_proposed(self):
        doc = self.drv.file_unread("INV-7", "Zapier", 2000, document_date="2026-08-04")
        self.give(doc)
        units = self.go()
        self.assertEqual(kinds(units), ["reading", "payment", "view", "complete"])
        self.assertEqual(units[0]["docs"] if units[0]["unit"] == "reading" else
                         [u for u in units if u["unit"] == "reading"][0]["docs"], [doc])
        self.no_sweep(units)
        self.assertEqual(self.work(), [(self.pids[1], "handover", "propose")])
        row = self.conn.execute("SELECT amount_minor, currency, read_at FROM documents WHERE"
                                " doc_id=?", (doc,)).fetchone()
        self.assertEqual((row[0], row[1]), (2000, "EUR"))
        self.assertIsNotNone(row[2])
        text, labels = self.card()
        self.assertIn("Zapier INV\\-7 · 4 Aug · EUR 20.00: proposed for Zapier — confirm below.",
                      text)
        self.assertEqual(labels.count("Get package"), 1)        # its payment's quarter, Q3
        self.assertIn("Get package: the Q3 zip", text)

    def test_the_payment_unit_of_a_handover_run_offers_no_search(self):
        doc = self.drv.file_unread("INV-8", "Zapier", 1000, document_date="2026-07-04")
        self.give(doc)
        u = self.drv.to_unit("bbbbbbbb-2", "payment")
        self.assertEqual(u["searches_left"], 0)
        self.assertEqual(u["handed_over"], [doc])
        import work
        with self.assertRaises(db.Refusal) as cm:
            work.record_search(self.conn, token=self.drv.token, pids=[u["pid"]],
                               queries=["zapier invoice"], refs=[])
        self.assertIn("no mail search", str(cm.exception))


class QuarterAndCopies(Case):
    def test_the_package_is_the_backed_payments_quarter_not_the_newest(self):
        self.drv.add_payments(["2026-10-02"])          # the newest payment is Q4's
        with self.patch_clock(NOW):
            self.drv.run_job("aaaaaaaa-3")
        doc = self.drv.file_unread("INV-15", "Zapier", 2000, document_date="2026-08-04")
        self.give(doc)
        self.go()
        text, labels = self.card()
        self.assertEqual(labels.count("Get package"), 1)
        self.assertIn("Get package: the Q3 zip", text)
        self.assertNotIn("Q4", text)

    def test_a_copy_of_a_held_invoice_is_reported_and_not_worked_again(self):
        a = self.drv.file_unread("OA-7", "Zapier", 2000, document_date="2026-08-04")
        self.give(a)
        self.go("bbbbbbbb-2")                          # proposed for the 20.00 payment
        c = self.drv.file_unread("OA-7", "Zapier", 2000, document_date="2026-08-04")
        self.give(c)
        units = self.go("bbbbbbbb-3")
        self.assertEqual(kinds(units), ["reading", "view", "complete"])
        self.assertEqual(self.work("bbbbbbbb-3"), [])
        text, _ = self.card("bbbbbbbb-3")
        self.assertIn(f"OA\\-7 · 4 Aug · EUR 20.00: already filed as \\#{a}.", text)


class NothingFits(Case):
    def test_it_says_so_with_its_reading_and_offers_no_package(self):
        self.drv.add_payments(["2026-10-02"])          # the newest payment is Q4's
        with self.patch_clock(NOW):
            self.drv.run_job("aaaaaaaa-3")
        before = len(self.drv.gmail.searches)
        doc = self.drv.file_unread("INV-9", "Zapier", 9999, document_date="2026-09-20")
        self.give(doc)
        units = self.go()
        self.assertEqual(kinds(units), ["reading", "view", "complete"])
        self.assertEqual(len(self.drv.gmail.searches), before)
        text, labels = self.card()
        self.assertIn("Zapier INV\\-9 · 20 Sep · EUR 99.99: no payment of EUR 99.99 in the books "
                      "yet", text)
        self.assertNotIn("Get package", labels)
        self.assertNotIn("Q4", text)

    def test_a_document_nobody_could_read_says_so(self):
        doc = self.drv.file_unread("INV-10", "Zapier", 2000)
        self.drv._reading = lambda u, token: None       # the model never records a reading
        self.give(doc)
        units = self.go()
        self.assertEqual(kinds(units), ["reading", "reading", "view", "complete"])
        text, labels = self.card()
        self.assertIn("INV\\-10: could not be read, so it was not matched — send it again "
                      "to retry.", text)
        self.assertNotIn("Get package", labels)


class SeveralInvoices(Case):
    def test_one_run_reads_each_and_works_the_union_with_copies_reported(self):
        a = self.drv.file_unread("OA-1", "Zapier", 1000, document_date="2026-07-04")
        b = self.drv.file_unread("OA-3", "Zapier", 3000, document_date="2026-09-04")
        c = self.drv.file_unread("OA-1", "Zapier", 1000, document_date="2026-07-04")
        c_bytes = self.conn.execute("SELECT sha256 FROM documents WHERE doc_id=?", (c,)).fetchone()
        self.assertIsNotNone(c_bytes)
        self.assertNotIn(c, (a, b))                     # other bytes, the same invoice
        self.give(a, b, c)
        units = self.go()
        self.assertEqual(kinds(units), ["reading", "payment", "payment", "view", "complete"])
        self.assertEqual(sorted([u for u in units if u["unit"] == "reading"][0]["docs"]),
                         sorted([a, b, c]))
        self.no_sweep(units)
        self.assertEqual(self.work(), [(self.pids[0], "handover", "propose"),
                                       (self.pids[2], "handover", "propose")])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        text, _labels = self.card()
        self.assertIn(f"Zapier OA\\-1 · 4 Jul · EUR 10.00: already filed as \\#{a}.", text)
        self.assertEqual(text.count(": proposed for Zapier"), 2)

    def test_a_reissue_with_another_recipient_is_no_copy(self):
        import documents
        a = self.drv.file_unread("OA-1", "Zapier", 1000, document_date="2026-07-04")
        b = self.drv.file_unread("OA-1", "Zapier", 1000, document_date="2026-07-04")
        for d, who in ((a, "N. Bonzanni"), (b, "Lesina BV")):
            documents.update_document_metadata(self.conn, d, recipient=who,
                                               **self.drv.printed[d])
        self.assertIsNone(documents.duplicate_of(self.conn, b))
        documents.update_document_metadata(self.conn, b, recipient="N. Bonzanni")
        self.assertEqual(documents.duplicate_of(self.conn, b), a)


class Reissue(Case):
    def test_a_reissue_of_a_held_invoice_asks_to_replace_it(self):
        """BRAIN 2026-10-09 (OpenAI reissued its invoices to the company): same number, the
        recipient read differently — no copy; on the payment holding the first the operator is asked."""
        a = self.drv.file_unread("OR-1", "Zapier", 2000, document_date="2026-08-05")
        self.drv.printed[a]["recipient"] = "N. Bonzanni"
        self.give(a)
        self.go("bbbbbbbb-2")
        pid = self.pids[1]            # it holds OR-1 (proposed): a held document
        b = self.drv.file_unread("OR-1", "Zapier", 2000, document_date="2026-08-05")
        self.drv.printed[b]["recipient"] = "Lesina BV"
        self.give(b)
        units = self.go("bbbbbbbb-3")
        self.no_sweep(units)
        import documents
        self.assertIsNone(documents.duplicate_of(self.conn, b))
        (u,) = [x for x in units if x["unit"] == "payment"]
        self.assertEqual((u["pid"], u["handed_over"]), (pid, [b]))
        self.assertIsNotNone(u["holds"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM replace_questions WHERE"
                                           " new_doc_id=? AND state='open'", (b,)).fetchone()[0],
                         1)
        text, _ = self.card("bbbbbbbb-3")
        self.assertIn("OR\\-1 · 5 Aug · EUR 20.00: its payment already has a document — Review "
                      "asks which to keep.", text)


class DeskFiling(Case):
    def test_a_caption_reading_at_the_desk_still_gets_the_checks_reading(self):
        """r3 (Astra S2): the desk filed a caption's currency — still read by the check."""
        import documents
        path = self.publish("cap.pdf", b"%PDF-1.4 caption invoice\n", producer="telegram")
        doc = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="manual-telegram", extraction_author="desk",
                                        currency="EUR")["doc_id"]
        self.drv.printed[doc] = {"issuer": "Zapier", "amount_minor": 2000, "currency": "EUR",
                                 "document_date": "2026-08-04", "document_number": "CAP-1"}
        self.give(doc)
        units = self.go()
        self.assertEqual(kinds(units), ["reading", "payment", "view", "complete"])
        self.assertEqual(self.work(), [(self.pids[1], "handover", "propose")])


class Scope(Case):
    def test_a_scheduled_run_that_finds_a_queued_handover_still_checks_everything(self):
        doc = self.drv.file_unread("INV-11", "Zapier", 2000, document_date="2026-08-04")
        self.give(doc)
        units = self.go(started_by="scheduled")
        self.assertIn("probes", kinds(units))
        self.assertLess(kinds(units).index("reading"), kinds(units).index("probes"))

    def test_a_check_asked_while_a_handover_runs_widens_the_run(self):
        import asks
        doc = self.drv.file_unread("INV-12", "Zapier", 2000, document_date="2026-08-04")
        self.give(doc)
        with self.patch_clock(NOW):
            self.drv.to_unit("bbbbbbbb-2", "payment")
            self.drv.do(self.drv.last, self.drv.token)          # the handed one, proposed
            asks.request_work(self.conn, "check", "operator")
            units = self.drv._loop("bbbbbbbb-2")
        self.assertIn("probes", kinds(units))
        self.assertIn("filing", kinds(units))
        self.assertGreater(len(self.drv.gmail.searches), self.searches)
        listed = {r[0] for r in self.work()}
        self.assertEqual(listed, set(self.pids))         # the full list after the widening

    def test_a_reissue_handed_with_a_check_mid_run_is_still_offered(self):
        """r1 (Astra S2): a late reissue and a check taken together — the widening rebuild
        must not mark the reissue fitted without handing its payment out again."""
        import asks
        a = self.drv.file_unread("R1-1", "Zapier", 2000, document_date="2026-08-04")
        self.drv.printed[a]["recipient"] = "Person"
        self.give(a)
        with self.patch_clock(NOW):
            u = self.drv.to_unit("bbbbbbbb-2", "payment")
            self.drv.do(u, self.drv.token)
            b = self.drv.file_unread("R1-1", "Zapier", 2000, document_date="2026-08-04")
            self.drv.printed[b]["recipient"] = "Company"
            self.give(b)
            asks.request_work(self.conn, "check", "operator")
            units = self.drv._loop("bbbbbbbb-2")
        offered = [u for u in units if u["unit"] == "payment" and b in u["handed_over"]]
        self.assertEqual(len(offered), 1)

    def test_a_widened_run_hands_out_a_payment_the_new_import_changed(self):
        """r2 (Astra S2): proposed in the narrow list, then the bank corrects the amount and
        a check joins — the full check hands the changed payment out again."""
        import asks
        no = self.drv.pay_once("Acme", 4000, "2026-08-04")
        self.go("aaaaaaaa-4")
        pid = self.drv.pid_of(no)
        d = self.drv.file_unread("R2-A", "Acme", 4000, document_date="2026-08-04")
        self.give(d)
        with self.patch_clock(NOW):
            u = self.drv.to_unit("bbbbbbbb-2", "payment")
            self.assertEqual(u["pid"], pid)
            self.drv.do(u, self.drv.token)
            self.drv._spec[no]["amount"] = 4500
            self.drv._fetch_spec()
            asks.request_work(self.conn, "check", "operator")
            units = self.drv._loop("bbbbbbbb-2")
        again = [u for u in units if u["unit"] == "payment" and u["pid"] == pid]
        self.assertEqual([(u["amount_minor"], u["why"]) for u in again], [(4500, "changed")])

    def test_a_store_never_imported_runs_the_full_check(self):
        import job
        from tests.sim_job import JobDriver
        StoreCase.setUp(self)
        self.bind()
        drv = JobDriver(self, payments=1)
        doc = drv.file_unread("INV-13", "Zapier", 1000)
        self.give(doc)
        with self.patch_clock(NOW):
            units = drv.run_job("cccccccc-1")
        self.assertIn("probes", kinds(units))
        self.assertIsNotNone(job)

    def test_a_claim_without_a_starter_line_adds_no_check(self):
        import job
        doc = self.drv.file_unread("INV-14", "Zapier", 2000)
        self.give(doc)
        with self.patch_clock(NOW):
            job.claim(self.conn, "dddddddd-1", started_by=None)
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT kind FROM work_requests WHERE pass_id=(SELECT pass_id FROM runs WHERE"
            " job_id='dddddddd-1')")], ["handover"])


class Desk(Case):
    def test_the_ask_says_how_many_it_checks(self):
        a = self.drv.file_unread("D-1", "Zapier", 1000)
        b = self.drv.file_unread("D-2", "Zapier", 2000)
        self.assertEqual(self.give(a)["line"],
                         "Filed. Checking it against the payments — I'll post what I find.")
        self.assertEqual(self.give(a, b)["line"],
                         "Filed 2 documents. Checking them against the payments — I'll post "
                         "what I find.")
