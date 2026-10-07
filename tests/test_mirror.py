"""Simple loop §2.4: the mirror is a diff over every in-scope row — tags against the run's
own export tags, the note against mirror_note — grouped into calls of up to 100 rows by
identical tag lists and identical note texts; distinct invoice notes one call each; no
read-backs; an immediate rerun owes nothing."""
import contextlib
import io
import json
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class Mirror(StoreCase):
    LEDGER = "0123456789abcdef0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))   # mirror: start lines
        self.bind()
        self.token = self.run_claim(instance=self.LEDGER)

    def payment(self, n, who="Adobe", observed=(), amount=10000):
        self.row(n, counterparty=who, amount_minor=amount, booking_date="2026-09-02",
                 value_date="2026-09-02")
        pid = self.lineage_for(n)
        self.classify(pid, {"software"})
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET observed_tags_json=? WHERE pid=?",
                              (json.dumps(sorted(observed)), pid))
        self.settle(pid)
        return pid

    def test_the_note_text_reads_as_plain_words(self):
        import mirror
        pid = self.payment(1)
        self.assertEqual(mirror.note_text(self.conn, pid),
                         "Accounting: invoice missing (quarterly check)")
        d = self.doc(issuer="Adobe", document_number="INV-88", document_date="2026-09-02")
        self.machine_match(pid, d, self.token)
        self.assertEqual(mirror.note_text(self.conn, pid),
                         "Accounting: matched — invoice Adobe INV-88 · 2 Sep 2026 · "
                         "EUR 100.00 (quarterly check)")

    def test_identical_texts_and_tag_lists_group_and_invoice_notes_go_one_by_one(self):
        import mirror
        a, b, c = (self.payment(n) for n in (1, 2, 3))           # all missing, untagged
        m = self.payment(4, observed=["acct::open"])
        self.machine_match(m, self.doc(document_number="INV-9"), self.token)
        calls = mirror.plan(self.conn)
        tools = [(x["tool"], sorted(x["pids"])) for x in calls]
        self.assertIn(("untag_transaction", [m]), tools)
        self.assertIn(("tag_transaction", sorted([a, b, c])), tools)   # acct::open, together
        self.assertIn(("tag_transaction", [m]), tools)                  # acct::matched
        notes = [x for x in calls if x["tool"] == "add_note"]
        self.assertEqual(sorted(len(x["pids"]) for x in notes), [1, 3])
        fence = calls[0]["args"]
        self.assertEqual(fence["expected_ledger"], self.LEDGER)
        self.assertIn("workflow", fence)
        order = [x["tool"] for x in calls]
        self.assertEqual(order, sorted(order, key=["untag_transaction", "tag_transaction",
                                                    "add_note"].index))

    def test_a_pending_row_carries_no_accounting_tag_and_no_note(self):
        import mirror
        self.row(9, counterparty="Figma", amount_minor=1200, status="PDNG",
                 booking_date=None, value_date="2026-09-29")
        pid = self.lineage_for(9)
        self.classify(pid, {"software"})
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET observed_tags_json=? WHERE pid=?",
                              (json.dumps(["acct::open"]), pid))
        self.settle(pid)
        self.assertIsNone(mirror.note_text(self.conn, pid))
        calls = [(c["tool"], c["pids"]) for c in mirror.plan(self.conn)]
        self.assertEqual(calls, [("untag_transaction", [pid])])

    def test_a_group_larger_than_a_call_is_split_at_100_rows(self):
        import mirror
        for n in range(1, 251):
            self.payment(n)
        tags = [x for x in mirror.plan(self.conn) if x["tool"] == "tag_transaction"]
        self.assertEqual([len(x["args"]["row_ids"]) for x in tags], [100, 100, 50])

    def test_an_immediate_rerun_owes_no_call(self):
        import mirror
        for n in (1, 2):
            self.payment(n)
        job = self.job_id                      # the run run_claim() made
        want = mirror.plan(self.conn)
        mirror.start(self.conn, job)
        handed = mirror.hand_calls(self.conn, job, budget=50)
        self.assertEqual(len(handed), len(want))
        self.assertEqual(mirror.hand_calls(self.conn, job, budget=50), handed)  # in flight
        mirror.record(self.conn, self.token, done=[c["n"] for c in handed], failed=[])
        self.assertEqual((mirror.plan(self.conn), mirror.owed(self.conn, job)), ([], 0))

    def test_a_change_after_the_mirror_began_is_mirrored_in_the_same_run(self):
        """Plan round 7 (Astra S2): a payment mirrored missing, then matched mid-run (a
        handover's continuation): the fresh diff owes the corrective calls; the run reaches
        `post` only when it is empty."""
        import mirror
        pid = self.payment(1)
        job = self.job_id
        mirror.start(self.conn, job)
        calls = mirror.hand_calls(self.conn, job, budget=50)
        mirror.record(self.conn, self.token, done=[c["n"] for c in calls], failed=[])
        self.assertEqual(mirror.owed(self.conn, job), 0)
        self.machine_match(pid, self.doc(document_number="INV-1"), self.token)
        self.assertGreater(mirror.owed(self.conn, job), 0)
        fix = mirror.hand_calls(self.conn, job, budget=50)
        self.assertEqual(sorted(c["tool"] for c in fix),
                         ["add_note", "tag_transaction", "untag_transaction"])
        mirror.record(self.conn, self.token, done=[c["n"] for c in fix], failed=[])
        self.assertEqual(mirror.owed(self.conn, job), 0)

    def test_a_failed_write_is_kept_for_the_end_message_and_retried_next_run(self):
        import mirror
        pid = self.payment(1)
        job = self.job_id                      # the run run_claim() made
        mirror.start(self.conn, job)
        calls = mirror.hand_calls(self.conn, job, budget=50)
        mirror.record(self.conn, self.token, done=[],
                      failed=[{"n": c["n"], "error": "refused: stale generation"}
                              for c in calls])          # not retried this run: owed is 0
        self.assertEqual(mirror.owed(self.conn, job), 0)
        self.assertEqual(pending_mirror_lines(self.conn),
                         ["2 bank-ledger updates did not go through — tried again at the "
                          "next check."])                # d1: alerts, one per row and payload
        self.assertEqual(lineage_error(self.conn, pid), "refused: stale generation")
        self.assertTrue(mirror.plan(self.conn))              # still owed: the next run writes it
        self.run_claim(instance=self.LEDGER)
        mirror.start(self.conn, self.job_id)
        self.assertEqual(sorted(c["tool"] for c in
                                mirror.hand_calls(self.conn, self.job_id, budget=50)),
                         ["add_note", "tag_transaction"])


def lineage_error(conn, pid):
    return conn.execute("SELECT last_error FROM projections WHERE pid=?", (pid,)).fetchone()[0]


def pending_mirror_lines(conn) -> list:
    """The refused mirror writes' line as the run's message would carry it (d1: alerts)."""
    import alerts
    with db.tx(conn):
        lines = alerts.pending_lines(conn)[0]          # wrapped: one sentence, joined back
    return [" ".join(lines)] if lines else []


class NoteTexts(Mirror):
    """§2.4's table, one status at a time (the amount is the document's, in its own
    currency; no revision counter, no fingerprint, no "Supersedes")."""

    def write(self, kind, pid, doc_id, **kw):
        import matches
        fn = matches.record_match if kind == "pair" else matches.propose_match
        extra = {"author": "auto"} if kind == "pair" else {}
        return fn(self.conn, pid=pid, doc_id=doc_id, expected_revision=self.rev(pid),
                  token=self.token, document_date="2026-09-18", **extra, **kw)

    def test_a_proposal_in_another_currency(self):
        import mirror
        pid = self.payment(1, who="OpenRouter")
        d = self.doc(issuer="OpenRouter", document_number="ABC-123", currency="USD",
                     amount_minor=2200)
        self.write("propose", pid, d)
        self.assertEqual(mirror.note_text(self.conn, pid),
                         "Accounting: proposed — invoice OpenRouter ABC-123 · 18 Sep 2026 · "
                         "USD 22.00, awaiting confirmation (quarterly check)")

    def test_a_proposal_with_alternatives_counts_them(self):
        import mirror
        pid = self.payment(1, who="AWS")
        d = self.doc(issuer="AWS", document_number="INV-88")
        self.write("propose", pid, d, alternatives=[self.doc(issuer="AWS")])
        self.assertEqual(mirror.note_text(self.conn, pid),
                         "Accounting: proposed — invoice AWS INV-88 · 18 Sep 2026 · "
                         "EUR 100.00 (or 1 other invoice), awaiting confirmation "
                         "(quarterly check)")
        p2 = self.payment(2, who="AWS")
        self.write("propose", p2, self.doc(issuer="AWS", document_number="INV-89"),
                   alternatives=[self.doc(issuer="AWS"), self.doc(issuer="AWS")])
        self.assertIn("(or 2 other invoices), awaiting", mirror.note_text(self.conn, p2))

    def test_a_joint_machine_set_says_how_many_fit(self):
        import mirror
        pid = self.payment(1)
        self.machine_entry(pid, self.doc())
        self.machine_entry(pid, self.doc())
        self.assertEqual(mirror.note_text(self.conn, pid),
                         "Accounting: proposed — 2 invoices fit, awaiting confirmation "
                         "(quarterly check)")

    def test_a_document_with_no_number_names_kind_issuer_date_and_amount(self):
        import mirror
        pid = self.payment(1, who="Bakker", amount=4550)
        self.write("pair", pid, self.doc(kind="receipt", issuer="Bakker", document_number=None,
                                         amount_minor=4550))
        self.assertEqual(mirror.note_text(self.conn, pid),
                         "Accounting: matched — receipt Bakker · 18 Sep 2026 · EUR 45.50 "
                         "(quarterly check)")

    def test_no_document_needed_and_exempt_share_one_text(self):
        import matches
        import mirror
        transfer = self.payment(1)
        self.classify(transfer, {"internal-transfer"})
        self.settle(transfer)
        exempt = self.payment(2)
        rid = self.show(exempt)
        self.granted(matches.set_exemption_in_tx, pid=exempt, exempt=True,
                     expected_revision=self.rev(exempt), render_id=rid)
        for pid in (transfer, exempt):
            self.assertEqual(mirror.note_text(self.conn, pid),
                             "Accounting: no invoice needed (quarterly check)")
        notes = [c for c in mirror.plan(self.conn) if c["tool"] == "add_note"]
        self.assertEqual([sorted(c["pids"]) for c in notes], [sorted([transfer, exempt])])

    def test_an_ineligible_row_has_no_note_and_loses_its_accounting_tags(self):
        """Fix round 1: a row the export still carries that left scope owes the removal of
        every acct:: tag it carries (as 26b68ee's sweep untagged it), and no note."""
        import mirror
        pid = self.payment(1, observed=["acct::open", "acct::matched", "software"])
        self.row(1, counterparty="Adobe", booking_date="2026-06-02", value_date="2026-06-02")
        self.settle(pid)                                # before the watermark: ineligible
        self.assertIsNone(mirror.note_text(self.conn, pid))
        self.assertEqual([(c["tool"], c["args"]["tags"], c["pids"])
                          for c in mirror.plan(self.conn)],
                         [("untag_transaction", ["acct::matched", "acct::open"], [pid])])

    def test_an_ended_row_the_export_carries_loses_its_accounting_tags(self):
        import mirror
        pid = self.payment(1, observed=["acct::open"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET ended='vanished' WHERE pid=?", (pid,))
        self.assertEqual([(c["tool"], c["args"]["tags"]) for c in mirror.plan(self.conn)],
                         [("untag_transaction", ["acct::open"])])

    def test_a_row_the_export_did_not_carry_owes_nothing(self):
        import mirror
        self.payment(1, observed=["acct::open"])
        self.row(1, counterparty="Adobe", booking_date="2026-06-02", value_date="2026-06-02")
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES ('p', 'x', 0, 0)")      # a later import, without it
        self.assertEqual(mirror.plan(self.conn), [])

    def test_a_long_note_is_cut_to_bank_feeds_limit(self):
        import mirror
        pid = self.payment(1)
        self.write("pair", pid, self.doc(issuer="A" * 2000, document_number="N" * 2000))
        text = mirror.note_text(self.conn, pid)
        self.assertLessEqual(len(text), mirror.NOTE_MAX)
        self.assertTrue(text.startswith("Accounting: matched — invoice "))


class HandOut(Mirror):
    """D9 and plan round 7: in-flight calls first, then a fresh diff that never repeats a
    row's write already in flight, and never retries a refused one within the run."""

    def test_a_row_joining_a_group_in_flight_gets_its_own_call(self):
        import mirror
        self.payment(1)
        self.payment(2)
        mirror.start(self.conn, self.job_id)
        first = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.payment(3)                           # owes the same tags and the same note
        again = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual(again[:len(first)], first)
        new = again[len(first):]
        self.assertEqual([(x["tool"], x["args"]["row_ids"]) for x in new],
                         [("tag_transaction", [3]), ("add_note", [3])])
        self.assertEqual(mirror.owed(self.conn, self.job_id), len(again))
        mirror.record(self.conn, self.token, done=[x["n"] for x in again], failed=[])
        self.assertEqual(mirror.owed(self.conn, self.job_id), 0)

    def test_a_refused_row_is_not_retried_when_its_group_changes(self):
        import mirror
        self.payment(1)
        mirror.start(self.conn, self.job_id)
        calls = mirror.hand_calls(self.conn, self.job_id, budget=50)
        mirror.record(self.conn, self.token, done=[],
                      failed=[{"n": c["n"], "error": "no"} for c in calls])
        self.payment(2)                           # the same texts: a group with row 1 again
        fresh = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual(sorted((x["tool"], x["args"]["row_ids"]) for x in fresh),
                         [("add_note", [2]), ("tag_transaction", [2])])

    def test_the_budget_bounds_a_hand_out(self):
        import mirror
        self.payment(1)
        self.payment(2, observed=["acct::proposed"])
        mirror.start(self.conn, self.job_id)
        one = mirror.hand_calls(self.conn, self.job_id, budget=1)
        self.assertEqual([c["n"] for c in one], [1])
        self.assertEqual(mirror.hand_calls(self.conn, self.job_id, budget=1), one)
        self.assertEqual(len(mirror.hand_calls(self.conn, self.job_id, budget=10)),
                         len(mirror.plan(self.conn)))
        self.assertEqual(mirror.hand_calls(self.conn, self.job_id, budget=0), [])

    def test_done_is_applied_in_hand_out_order(self):
        """Two notes for one row in flight (its outcome changed in between): the later
        one is the row's note, whatever order the report lists them in; the fresh diff
        then owes what the bank holds beyond the store, and converges."""
        import mirror
        pid = self.payment(1)
        mirror.start(self.conn, self.job_id)
        first = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual(len(first), 2)
        self.machine_match(pid, self.doc(document_number="INV-7"), self.token)
        both = mirror.hand_calls(self.conn, self.job_id, budget=50)
        mirror.record(self.conn, self.token, done=sorted((c["n"] for c in both), reverse=True),
                      failed=[])
        self.assertEqual(self.conn.execute("SELECT mirror_note FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0],
                         mirror.note_text(self.conn, pid))
        # the acct::open tag handed before the match also landed: the fresh diff owes
        # removing it, and only that
        last = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual([(c["tool"], c["args"]["tags"]) for c in last],
                         [("untag_transaction", ["acct::open"])])
        mirror.record(self.conn, self.token, done=[c["n"] for c in last], failed=[])
        self.assertEqual(mirror.owed(self.conn, self.job_id), 0)

    def test_an_older_note_reported_in_a_later_record_does_not_win(self):
        """Fix round 1: two notes for one row in flight, acknowledged in reverse order by
        two separate record_mirror calls: the newer note stays the row's mirror_note."""
        import mirror
        pid = self.payment(1)
        mirror.start(self.conn, self.job_id)
        first = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.machine_match(pid, self.doc(document_number="INV-7"), self.token)
        both = mirror.hand_calls(self.conn, self.job_id, budget=50)
        later = [c["n"] for c in both if c not in first]
        mirror.record(self.conn, self.token, done=later, failed=[])
        mirror.record(self.conn, self.token, done=[c["n"] for c in first], failed=[])
        note, tags = self.conn.execute("SELECT mirror_note, observed_tags_json FROM"
                                       " projections WHERE pid=?", (pid,)).fetchone()
        self.assertEqual(note, mirror.note_text(self.conn, pid))
        self.assertTrue(note.startswith("Accounting: matched"))
        self.assertEqual(json.loads(tags), ["acct::matched"])   # the older tag ack skipped

    def test_a_later_note_does_not_hide_an_earlier_tag_acknowledgement(self):
        import mirror
        pid = self.payment(1)
        mirror.start(self.conn, self.job_id)
        tag, note = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual((tag["tool"], note["tool"]), ("tag_transaction", "add_note"))
        mirror.record(self.conn, self.token, done=[note["n"]], failed=[])
        mirror.record(self.conn, self.token, done=[tag["n"]], failed=[])
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT observed_tags_json FROM projections WHERE pid=?", (pid,)).fetchone()[0]),
            ["acct::open"])
        self.assertEqual(mirror.owed(self.conn, self.job_id), 0)

    def test_start_logs_once_and_stamps_the_run(self):
        import mirror
        self.payment(1)
        self.payment(2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mirror.start(self.conn, self.job_id)
            mirror.start(self.conn, self.job_id)
        self.assertEqual(err.getvalue(), f"mirror: start job={self.job_id} rows=2\n")
        self.assertIsNotNone(self.conn.execute("SELECT mirror_at FROM runs WHERE job_id=?",
                                               (self.job_id,)).fetchone()[0])

    def test_record_is_fenced_and_ignores_unknown_numbers(self):
        import mirror
        self.payment(1)
        mirror.start(self.conn, self.job_id)
        calls = mirror.hand_calls(self.conn, self.job_id, budget=50)
        with self.assertRaises(db.Refusal):
            mirror.record(self.conn, self.token + 99, done=[c["n"] for c in calls], failed=[])
        out = mirror.record(self.conn, self.token, done=[99] + [c["n"] for c in calls],
                            failed=[{"n": 98, "error": "x"}])
        self.assertEqual(out, {"recorded": len(calls), "owed": 0})
        self.assertEqual(mirror.record(self.conn, self.token, done=[c["n"] for c in calls],
                                       failed=[]), {"recorded": 0, "owed": 0})

    def test_the_tool_records_a_mirror_unit(self):
        import mirror
        import qa_server
        import tools  # noqa: F401
        self.payment(1)
        mirror.start(self.conn, self.job_id)
        calls = mirror.hand_calls(self.conn, self.job_id, budget=50)
        out = qa_server.TOOLS["record_mirror"]["fn"](
            {"pass_token": self.token, "done": [calls[0]["n"]],
             "failed": [{"n": calls[1]["n"], "error": "refused"}]})
        self.assertEqual(out, {"recorded": 2, "owed": 0})
        self.assertEqual(pending_mirror_lines(self.conn),
                         ["1 bank-ledger update did not go through — tried again at the "
                          "next check."])


class RealBankFeed(StoreCase):
    """The grouped calls run against the REAL bank-feed (tests/bankfeed.py), then an
    immediate rerun's diff — against a fresh export — is empty."""

    def setUp(self):
        super().setUp()
        from tests import bankfeed
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.close)
        self.bf.account()
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.bind(account=bankfeed.Ledger.ACCOUNT)

    def run_and_import(self):
        """A run, its bank read (the acquisition its import is bound to) and the import of
        a fresh export, as the cursor hands them out."""
        import ledger
        import loop
        import passes
        token = self.run_claim(instance=self.bf.instance(), generation=self.bf.generation())
        with db.tx(self.conn):
            acq = loop.hand_acquisition(self.conn, token, self.pass_id)
        passes.record_probe(self.conn, token, "bank_sync", True, acq=acq)
        ledger.import_ledger_export(self.conn, path=self.bf.export(), token=token,
                                    ledger_instance=self.bf.last_export_instance, acq=acq)
        return token

    def test_the_grouped_calls_land_and_a_rerun_owes_nothing(self):
        import lineage
        import mirror
        import version
        self.bf.fetch([self.bf.row("2026-08-03" if n == 0 else "2026-09-02",
                                   amount=10000 + n, ref=f"R{n}", counterparty=who)
                       for n, who in enumerate(("Adobe", "Figma", "Zapier", "Notion"))])
        ids = [r["row_id"] for r in self.bf.rows()]
        self.bf.call("tag_transaction", row_ids=ids, tags=["software"])     # the classifier
        self.bf.call("tag_transaction", row_ids=[ids[3]], tags=["acct::proposed"],
                     workflow=version.WORKFLOW,
                     expected_generation=self.bf.generation())             # an older mirror
        token = self.run_and_import()
        pids = {lineage.projection(self.conn, p)["dest_row_id"]: p
                for p in lineage.live_pids(self.conn)}
        d = self.doc(issuer="Adobe", document_number="INV-88", amount_minor=10000,
                     document_date="2026-09-02")
        self.machine_match(pids[ids[0]], d, token)
        mirror.start(self.conn, self.job_id)
        calls = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual(sorted((c["tool"], len(c["args"]["row_ids"])) for c in calls),
                         [("add_note", 1), ("add_note", 3), ("tag_transaction", 1),
                          ("tag_transaction", 3), ("untag_transaction", 1)])
        for c in calls:
            out = self.bf.call(c["tool"], **c["args"])
            self.assertNotIn("changed nothing", out, out)
        mirror.record(self.conn, token, done=[c["n"] for c in calls], failed=[])
        self.assertEqual(mirror.owed(self.conn, self.job_id), 0)
        self.assertEqual(self.bf.tags(ids[0]), ["acct::matched", "software"])
        for rid in ids[1:]:
            self.assertEqual(self.bf.tags(rid), ["acct::open", "software"])
            self.assertEqual(self.bf.notes(rid), ["Accounting: invoice missing (quarterly check)"])
        self.assertEqual(self.bf.notes(ids[0]),
                         ["Accounting: matched — invoice Adobe INV-88 · 2 Sep 2026 · "
                          "EUR 100.00 (quarterly check)"])
        # an immediate rerun: a new run, a fresh export; nothing is owed, nothing is handed
        self.run_and_import()
        self.assertEqual(mirror.plan(self.conn), [])
        mirror.start(self.conn, self.job_id)
        self.assertEqual(mirror.hand_calls(self.conn, self.job_id, budget=50), [])
        self.assertEqual(mirror.owed(self.conn, self.job_id), 0)
        # fix round 1: the watermark moves past the matched row (still in the export): it
        # is untagged and gets no new note; the others are untouched
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-08-15'")
        token = self.run_and_import()
        mirror.start(self.conn, self.job_id)
        calls = mirror.hand_calls(self.conn, self.job_id, budget=50)
        self.assertEqual([(c["tool"], c["args"]["row_ids"], c["args"]["tags"]) for c in calls],
                         [("untag_transaction", [ids[0]], ["acct::matched"])])
        for c in calls:
            self.assertNotIn("changed nothing", self.bf.call(c["tool"], **c["args"]))
        mirror.record(self.conn, token, done=[c["n"] for c in calls], failed=[])
        self.assertEqual(self.bf.tags(ids[0]), ["software"])
        self.assertEqual(len(self.bf.notes(ids[0])), 1)          # no new note
        self.run_and_import()
        self.assertEqual(mirror.plan(self.conn), [])
