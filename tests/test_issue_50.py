# tests/test_issue_50.py
"""Issue #50 (operator ruling A, the narrow reading): record_search carries `emails` — the
vendor emails a search on the payment's OWN reference or order number returned, each
`listed` or not. `missing` is refused while a reported email is unlisted (queued, or given up
with a payment a handover reopened); the refusal names it. Model-reported: a reminder that
catches a skipped step on an honest report, never an omitted email. Q2 re-run #3: Megekko 1
was decided missing with "Bestelling is verzonden" (the PDF's email) never listed."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported

JOB = "aaaa5000-1"


def items(conn, job_id, **where) -> list:
    sql = "SELECT unit, kind, key, state FROM run_items WHERE job_id=?"
    args = [job_id]
    for k, v in where.items():
        sql += f" AND {k}=?"
        args.append(v)
    return [tuple(r) for r in conn.execute(sql + " ORDER BY seq", args)]


class UnlistedEmail(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=1)
        self.u = self.drv.to_unit(JOB, "payment")
        self.pid = self.u["pid"]

    def search(self, emails, queries=("ME280426000457",), refs=()):
        return self.drv._tool("record_search", {
            "pass_token": self.drv.token, "pids": [self.pid], "search": "payment",
            "queries": list(queries), "refs": list(refs), "emails": emails})

    def missing(self):
        return self.drv._tool("decide", {"pass_token": self.drv.token, "entries": [{
            "pid": self.pid, "outcome": "missing", "reason": "no invoice email",
            "expected_revision": self.u["revision"]}]})

    def test_missing_is_refused_while_a_reported_email_is_unlisted(self):
        """The Megekko shape: the order-number search returned the shipped email; the
        model listed another one and decided missing."""
        self.search([{"id": "19dd52ffe4f84727", "listed": False},
                     {"id": "19dd82a96185e4eb", "listed": True}])
        out = self.missing()
        self.assertEqual(out["applied"], 0)
        refused = out["results"][0]["refused"]
        self.assertIn("19dd52ffe4f84727", refused)
        self.assertNotIn("19dd82a96185e4eb", refused)
        self.assertIn("list_attachments", refused)
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work").fetchone()[0], None)

    def test_a_listing_report_lets_missing_through(self):
        self.search([{"id": "m-ship", "listed": False}])
        self.search([{"id": "m-ship", "listed": True}], queries=())
        self.assertEqual(self.missing()["applied"], 1)
        self.assertEqual(items(self.conn, JOB, kind="email"),
                         [(f"payment:{self.pid}", "email", "m-ship", "done")])

    def test_a_listing_that_found_an_attachment_closes_the_email_and_owes_the_file(self):
        """A ref of the message is a listing; the found attachment is owed as before."""
        self.search([{"id": "m-ship", "listed": False}])
        self.search([], queries=(), refs=["m-ship:att-1"])
        self.assertEqual(items(self.conn, JOB, kind="email")[0][3], "done")
        with self.assertRaisesRegex(db.Refusal, "not yet filed"):
            self.missing()                                      # the ref is owed (§R18.2)
        self.drv._tool("set_aside", {"pass_token": self.drv.token,
                                     "items": [{"ref": "m-ship:att-1"}],
                                     "reason": "shipping notice"})
        self.assertEqual(self.missing()["applied"], 1)

    def test_an_email_listed_at_its_search_owes_nothing(self):
        self.search([{"id": "m-ship", "listed": True}])
        self.assertEqual(items(self.conn, JOB, kind="email"), [])
        self.assertEqual(self.missing()["applied"], 1)

    def test_a_plain_search_reports_no_emails(self):
        self.search([], queries=("Zapier after:2026/06/01",))
        self.assertEqual(self.missing()["applied"], 1)

    def test_a_match_or_proposal_is_never_blocked_by_an_unlisted_email(self):
        """The floor checks `missing` alone: a document that fits is decided as before."""
        self.search([{"id": "m-ship", "listed": False}])
        doc = self.drv.file_document(vendor="Zapier", amount_minor=1)
        out = self.drv._tool("decide", {"pass_token": self.drv.token, "entries": [{
            "pid": self.pid, "outcome": "propose", "doc_id": doc,
            "document_date": "2026-07-09", "expected_revision": self.u["revision"]}]})
        self.assertEqual(out["applied"], 1, out)

    def test_a_listing_report_is_not_a_search(self):
        """d1: a payment that used its searches can still report the listing (no stall)."""
        import loop
        for n in range(loop.SEARCHES_MAX):
            self.search([{"id": f"m{n}", "listed": False}], queries=(f"REF-{n}",))
        self.assertEqual(self.conn.execute("SELECT searches FROM run_work").fetchone()[0],
                         loop.SEARCHES_MAX)
        # the listing found an attachment: refs given, yet no search counted nor refused
        out = self.search([{"id": f"m{n}", "listed": True} for n in range(loop.SEARCHES_MAX)],
                          queries=(), refs=["m0:att-1"])
        self.assertEqual(out["files"], ["m0:att-1"])
        self.assertEqual(self.conn.execute("SELECT searches FROM run_work").fetchone()[0],
                         loop.SEARCHES_MAX)
        self.drv._tool("set_aside", {"pass_token": self.drv.token,
                                     "items": [{"ref": "m0:att-1"}], "reason": "a delivery note"})
        self.assertEqual(self.missing()["applied"], 1)

    def test_the_hand_out_carries_the_unlisted_emails(self):
        """A cut between the report and the decide loses nothing: job_next hands them."""
        self.search([{"id": "m-ship", "listed": False}, {"id": "m-ok", "listed": True}])
        u = self.drv.next()
        self.assertEqual((u["unit"], u["pid"]), ("payment", self.pid))
        self.assertEqual(u["emails_to_list"], ["m-ship"])

    def test_a_given_up_email_still_refuses_missing_once_the_payment_is_reopened(self):
        """d1 Astra S1, d2 Terra S2: given up with its payment, reopened by a handover —
        missing is refused until the listing is reported, and the report clears it."""
        self.search([{"id": "m-ship", "listed": False}])
        with db.tx(self.conn):                  # the payment and what it owes, given up …
            self.conn.execute("UPDATE run_items SET state='given_up', reason='not reached'"
                              " WHERE kind='email'")
            self.conn.execute("UPDATE run_work SET attempts=0")   # … then reopened
        self.assertEqual(self.missing()["applied"], 0)
        self.search([{"id": "m-ship", "listed": True}], queries=())
        self.assertEqual(items(self.conn, JOB, kind="email")[0][3], "done")
        self.assertEqual(self.missing()["applied"], 1)

    def test_a_given_up_email_is_no_give_up_of_its_own(self):
        """d2 Astra S2: its payment's undecided outcome reports it; the email kind never
        marks the run interrupted nor every missing "search incomplete"."""
        import queues
        self.search([{"id": "m-ship", "listed": False}])
        with db.tx(self.conn):
            self.conn.execute("UPDATE run_items SET state='given_up' WHERE kind='email'")
        self.assertEqual(queues.given_up(self.conn, JOB), {})
        self.assertFalse(queues.gave_up_upstream(self.conn, JOB))

    def test_emails_are_a_list_of_id_and_listed(self):
        for bad in ([{"id": "m"}], [{"id": "", "listed": False}], ["m"], {"id": "m"},
                    [{"id": "m", "listed": "no"}]):
            with self.assertRaises(db.Refusal, msg=bad):
                self.search(bad)

    def test_an_email_once_listed_stays_listed(self):
        """A later search returning it again owes no second listing (queues.enqueue keeps
        a key in any state)."""
        self.search([{"id": "m-ship", "listed": False}])
        self.search([{"id": "m-ship", "listed": True}], queries=())
        self.search([{"id": "m-ship", "listed": False}], queries=("ME280426000457 bis",))
        self.assertEqual(items(self.conn, JOB, kind="email")[0][3], "done")
        self.assertEqual(self.missing()["applied"], 1)


class Schema15(StoreCase):
    def test_14_to_15_keeps_run_items_and_admits_the_email_kind(self):
        """The rebuild that widens run_items.kind keeps a live run's rows (d1 question)."""
        import db as D
        old = D.RUN_ITEMS_DDL.replace("'erase', 'search', 'ref', 'email'",
                                      "'erase', 'search', 'ref'")
        self.assertNotEqual(old, D.RUN_ITEMS_DDL)
        with D.tx(self.conn):
            self.conn.execute("DROP TABLE run_items")
            self.conn.execute(old)
            self.conn.execute("INSERT INTO run_items(job_id, unit, kind, key, state, attempts,"
                              " hand_seq, seq) VALUES ('j', 'payment:1', 'ref', 'm:a', 'queued',"
                              " 1, 7, 8)")
            self.conn.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
        D.migrate(self.conn)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key="
                                           "'schema_version'").fetchone()[0], "15")
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT job_id, unit, kind, key, state, attempts, hand_seq, seq FROM run_items")],
            [("j", "payment:1", "ref", "m:a", "queued", 1, 7, 8)])
        with D.tx(self.conn):
            self.conn.execute("INSERT INTO run_items(job_id, unit, kind, key, state, seq)"
                              " VALUES ('j', 'payment:1', 'email', 'm', 'queued', 9)")
