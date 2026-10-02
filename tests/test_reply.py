# tests/test_reply.py
import re
import unittest
from unittest import mock

from tests._base import StoreCase
import db  # noqa: E402
import matches  # noqa: E402
import reply  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        self.n = 0
        real = reply.apply_reply

        def checked(conn, text):                  # every receipt speaks operator words (R4)
            out = real(conn, text)
            self.assert_operator_words(out["receipt"])
            return out
        patcher = mock.patch.object(reply, "apply_reply", checked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_operator_words(self, receipt):
        for word in views.FORBIDDEN:
            self.assertNotIn(word, receipt, word)
        self.assertIsNone(re.search(r"#\d|\br\d+\b|\ba [aeiou]", receipt), receipt)

    def item(self, cp, amount, day, paired=True, labels=("guessed",), tags=("software",)):
        self.n += 1
        self.row(self.n, counterparty=cp, amount_minor=amount, booking_date=day, value_date=day)
        pid = self.lineage_for(self.n)
        self.classify(pid, set(tags))
        self.settle(pid)
        if not paired:
            # Integration fix: a required payment nobody has searched for is "not
            # searched", not "missing", and a status/missing sheet counts it
            # without printing it (Task 16, spec §Weekly pass "four states stay
            # distinct"). These fixtures mean a MISSING line the operator saw.
            self.handed(pid)
            work.record_search(self.conn, pid=pid, token=self.token, queries=[cp])
        if paired:
            d = self.doc(counterparty=cp, issuer=cp, amount_minor=amount, document_date=day)
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid), row_snapshot=self.snapshot(pid),
                                 token=self.token, labels=labels)
        return pid

    def deliver(self, view="status"):
        r = views.build_review(self.conn, view=view, quarter="2026-Q3")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        return r

    def author(self, pid):
        return self.conn.execute("SELECT author FROM match_state WHERE pid=? AND state IN"
                                 " ('matched','proposed')", (pid,)).fetchone()

    def operator_entries(self):
        return self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'").fetchone()[0]


class TestGrammar(Base):
    def test_a_negative_verdict_unpairs_only_what_it_names(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, "the Zapier one is wrong")
        self.assertIn("Unpaired Zapier · EUR 99.00 · 17 Sep.", out["receipt"])
        self.assertIsNone(self.author(z))
        self.assertEqual(self.author(v)[0], "auto")
        self.assertEqual(self.operator_entries(), 1)

    def test_a_no_ref_line_names_its_invoice(self):
        pid = self.item("Adobe", 5445, "2026-09-14", labels=("no-ref",))
        text = self.deliver()["text"]
        self.assertIn("Paired with invoice", text)
        del pid

    def test_all_good_confirms_only_what_was_shown(self):
        a = self.item("Adobe", 5445, "2026-09-14")
        self.deliver()
        late = self.item("Figma", 1815, "2026-09-15")          # created after the sheet was sent
        reply.apply_reply(self.conn, "all good")
        self.assertEqual(self.author(a)[0], "operator")
        self.assertEqual(self.author(late)[0], "auto")

    def test_the_guesses_named_together_are_the_sheet_reply(self):
        # issue #11: the operator's own words, from two live runs
        for text in ("Those six guesses are all right, confirm them.",
                     "All six proposals are right — confirm them"):
            with self.subTest(text=text):
                self.setUp()
                pids = [self.item(f"Vendor{i}", 1000 + i, f"2026-09-{10 + i:02d}")
                        for i in range(6)]
                self.deliver()
                out = reply.apply_reply(self.conn, text)
                self.assertEqual(len(out["applied"]), 6, out["receipt"])
                self.assertEqual([self.author(p)[0] for p in pids], ["operator"] * 6)
                self.assertNotIn("didn't understand", out["receipt"])

    def test_a_count_that_is_not_the_sheets_confirms_nothing(self):
        pids = [self.item(f"Vendor{i}", 1000 + i, f"2026-09-{10 + i:02d}") for i in range(3)]
        self.deliver()
        # R1 Astra: a count in the trailing "confirm …" is checked as well
        for text in ("those five guesses are right", "confirm all 4", "both guesses are good",
                     "all the guesses look right, confirm both pairings",
                     "these three guesses are right, confirm all four"):
            with self.subTest(text=text):
                out = reply.apply_reply(self.conn, text)
                self.assertEqual(out["applied"], [])
                self.assertIn("that sheet has 3 pairings waiting for your approval, not ",
                              out["receipt"])
                self.assertEqual([self.author(p)[0] for p in pids], ["auto"] * 3)
        out = reply.apply_reply(self.conn, "confirm all three")
        self.assertEqual(len(out["applied"]), 3)

    def test_a_pronoun_or_a_bare_the_guesses_confirms_no_more_than_was_named(self):
        # R2 Astra: "them" may mean the ones just named — never the whole sheet
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe", "Figma"))]
        self.deliver()
        for text in ("Zapier and Vercel are right. Confirm them.",
                     "Zapier and Vercel are right. They're all good.",
                     "Zapier and Vercel are right. The guesses are right, confirm them.",
                     "Zapier and Vercel are right. Confirm them all."):
            with self.subTest(text=text):
                reply.apply_reply(self.conn, text)
                self.assertEqual([self.author(p)[0] for p in pids[2:]], ["auto", "auto"])

    def test_an_exception_on_its_own_line_approves_no_sheet(self):
        # R3 Astra: the line break must not cut the exception loose from the approval
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe"))]
        self.deliver()
        for text in ("Confirm all three,\nexcept the Zapier one.", "All good,\nexcept the Zapier one",
                     "All three guesses are right.\nBut not the Zapier one.",
                     # R4 Astra: any clause the grammar does not understand may qualify it
                     "Confirm all three,\nwith the exception of the Zapier one.",
                     "Confirm all three,\nexcluding the Zapier one.",
                     "Confirm all three.\n— except the Zapier one",
                     "All good.\nZapier not so sure.", "All good. 1 wrong."):
            with self.subTest(text=text):
                out = reply.apply_reply(self.conn, text)
                self.assertEqual(out["applied"], [])
                self.assertEqual([self.author(p)[0] for p in pids], ["auto"] * 3)
                self.assertIn("something else in the same message", out["receipt"])
                self.assertEqual(self.operator_entries(), 0)

    def test_an_approval_beside_understood_clauses_still_applies(self):
        # the form the receipts recommend: the approval and the correction, two sentences
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, "All good. The Zapier one is wrong.")
        # R5: the sheet is approved last, for what no other clause named
        self.assertIsNone(self.author(z))
        self.assertEqual(self.author(v)[0], "operator")
        self.assertNotIn("something else in the same message", out["receipt"])

    def test_a_correction_beside_a_collective_is_never_confirmed(self):
        # R5 Terra: the collective used to confirm Zapier before its correction ran.
        # R8: a collective beside a verdict applies nothing (it may refer back to it)
        for text in ("All three guesses are right. The Zapier one is wrong.",
                     "The Zapier one is wrong. All three guesses are right."):
            with self.subTest(text=text):
                self.setUp()
                z = self.item("Zapier", 9900, "2026-09-17")
                others = [self.item("Vercel", 1210, "2026-09-18"),
                          self.item("Adobe", 5445, "2026-09-14")]
                self.deliver()
                out = reply.apply_reply(self.conn, text)
                self.assertIsNone(self.author(z), out["receipt"])
                self.assertEqual([self.author(p)[0] for p in others], ["auto"] * 2)
                self.assertIn("something else in the same message", out["receipt"])

    def test_a_collective_after_named_approvals_confirms_only_those(self):
        # R8 Astra: "those two guesses" refers back to the two just named
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe", "Figma"))]
        self.deliver()
        for text in ("Zapier and Vercel are right. Those two guesses are all right, confirm them.",
                     "Zapier and Vercel are right. All those guesses are right, confirm them."):
            with self.subTest(text=text):
                reply.apply_reply(self.conn, text)
                self.assertEqual([self.author(p)[0] for p in pids[2:]], ["auto", "auto"])

    def test_an_unresolved_clause_leaves_the_sheet_unapproved(self):
        a1 = self.item("Adobe", 5445, "2026-09-14")
        a2 = self.item("Adobe", 2999, "2026-09-03")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, "All good. The Adobe one is wrong.")   # which Adobe?
        self.assertEqual([self.author(p)[0] for p in (a1, a2, v)], ["auto"] * 3)
        self.assertEqual(self.operator_entries(), 0)
        self.assertIn("something else in the same message", out["receipt"])

    def test_a_question_beside_a_sheet_wide_approval_leaves_the_sheet_unapproved(self):
        # R6 Astra: a question may carry an exclusion
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe"))]
        self.deliver()
        for text in ("Confirm all three. Can you leave the Zapier one unconfirmed?",
                     "All good. Is Vercel right?"):
            with self.subTest(text=text):
                out = reply.apply_reply(self.conn, text)
                self.assertEqual([self.author(p)[0] for p in pids], ["auto"] * 3)
                self.assertIn("something else in the same message", out["receipt"])

    def test_two_sheet_wide_clauses_are_one_approval(self):
        # R7 Astra: a count one clause states bounds every other sheet-wide clause
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe"))]
        self.deliver()
        for text in ("Those two guesses are all right. Confirm all those guesses.",
                     "Confirm all those guesses. Those two guesses are all right.",
                     "All good. Both guesses are right."):
            with self.subTest(text=text):
                out = reply.apply_reply(self.conn, text)
                self.assertEqual([self.author(p)[0] for p in pids], ["auto"] * 3)
                self.assertIn("that sheet has 3 pairings waiting", out["receipt"])
        out = reply.apply_reply(self.conn, "All good. All three guesses are right.")
        self.assertEqual([self.author(p)[0] for p in pids], ["operator"] * 3)

    def test_a_search_request_beside_all_good_takes_nothing_from_it(self):
        # R6 Astra S2: only a verdict on a pairing takes it out of the sheet-wide approval
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe"))]
        self.deliver()
        reply.apply_reply(self.conn, "All good. Have another look at Zapier.")
        self.assertEqual([self.author(p)[0] for p in pids], ["operator"] * 3)

    def test_a_qualifier_that_names_no_payment_leaves_the_sheet_unapproved(self):
        # R5 Astra: "Only Vercel is right" parses as a confirm whose target resolves to
        # nothing — it is unresolved, so the sheet-wide approval applies nothing
        pids = [self.item(n, 1000 + i, f"2026-09-{10 + i:02d}")
                for i, n in enumerate(("Zapier", "Vercel", "Adobe"))]
        self.deliver()
        for text in ("Confirm all three. Only Vercel is right.",
                     "Only Vercel is right. Confirm all three."):
            with self.subTest(text=text):
                reply.apply_reply(self.conn, text)
                self.assertEqual([self.author(p)[0] for p in pids], ["auto"] * 3)
                self.assertEqual(self.operator_entries(), 0)

    def test_a_collective_confirmation_binds_like_all_good(self):
        a = self.item("Adobe", 5445, "2026-09-14")
        self.deliver()
        late = self.item("Figma", 1815, "2026-09-15")          # created after the sheet was sent
        reply.apply_reply(self.conn, "all the guesses are right, confirm them")
        self.assertEqual(self.author(a)[0], "operator")
        self.assertEqual(self.author(late)[0], "auto")

    def test_identity_is_not_an_exemption(self):
        pid = self.item("BCK*XYZ", 18000, "2026-09-16", paired=False)
        self.deliver()
        out = reply.apply_reply(self.conn, "the BCK*XYZ one is my accountant")
        self.assertIn("BCK*XYZ: my accountant; still missing a document.", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='exempt'")
                         .fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "open")

    def test_a_target_list_without_a_verb_applies_nothing(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, "Zapier and Vercel")
        self.assertEqual(out["applied"], [])
        self.assertIn('"Zapier and Vercel are wrong"', out["receipt"])

    def test_ambiguous_bulk_applies_nothing(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "all good except the Zapier")
        self.assertEqual((out["applied"], self.operator_entries()), ([], 0))
        self.assertIn('"all good"', out["receipt"])

    def test_two_matches_ask_with_dates_and_amounts(self):
        self.item("Adobe", 5445, "2026-09-14")
        self.item("Adobe", 2999, "2026-09-03")
        self.deliver()
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("EUR 54.45 · 14 Sep", out["receipt"])
        self.assertIn("EUR 29.99 · 3 Sep", out["receipt"])
        out = reply.apply_reply(self.conn, "the Adobe 54.45 one is wrong")
        self.assertEqual(len(out["applied"]), 1)

    def test_no_match_is_said_and_never_redirected(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "the Zapiér one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("Nothing open matches", out["receipt"])

    def test_an_item_never_shown_is_reshown_and_nothing_applies(self):
        pid = self.item("Zapier", 9900, "2026-09-17")
        out = reply.apply_reply(self.conn, "the Zapier one is wrong")
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.author(pid)[0], "auto")

    def test_a_pass_that_moved_one_item_refuses_it_and_applies_the_rest(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        matches.relabel_match(self.conn, match_id=self.conn.execute(
            "SELECT current_match FROM projections WHERE pid=?", (z,)).fetchone()[0],
            labels=("guessed", "no-ref"), token=self.token)      # the running pass moved it
        out = reply.apply_reply(self.conn, "the Zapier one is wrong; the Vercel one is wrong")
        self.assertEqual(out["reshow"], [z])
        self.assertIsNone(self.author(v))
        self.assertEqual(self.author(z)[0], "auto")

    def test_candidates_not_displayed_are_reshown_not_rejected(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        for _ in range(2):
            matches.record_match(self.conn, pid=pid, doc_id=self.doc(), author="auto",
                                 expected_revision=self.rev(pid), token=self.token,
                                 row_snapshot=self.snapshot(pid))
        self.deliver(view="missing")
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertEqual(out["reshow"], [pid])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM match_state WHERE"
                                           " state='rejected'").fetchone()[0], 0)

    def test_a_question_is_never_a_correction(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "is the Zapier one right?")
        self.assertTrue(out["not_a_reply"])
        self.assertEqual(self.author(z)[0], "auto")

    def test_the_receipt_comes_from_the_commit(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        with mock.patch.object(matches, "reject_match", side_effect=db.Busy("locked")):
            out = reply.apply_reply(self.conn, "the Zapier one is wrong")
        self.assertNotIn("Unpaired", out["receipt"])
        self.assertIn("not applied", out["receipt"])

    def test_exemption_by_amount_says_what_it_dropped(self):
        pid = self.item("Adobe", 18000, "2026-09-16")
        self.deliver()
        out = reply.apply_reply(self.conn, "the 180.00 one needs no invoice")
        self.assertIn("needs no document", out["receipt"])
        self.assertIn("dropped", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "exempt")

    def test_no_invoices_ever_is_a_counterparty_expectation(self):
        pid = self.item("Adobe", 18000, "2026-09-16", paired=False)
        self.deliver()
        reply.apply_reply(self.conn, "no invoices ever for Adobe")
        row = self.conn.execute("SELECT exp_kind, exp_row FROM projections WHERE pid=?",
                                (pid,)).fetchone()
        self.assertEqual(tuple(row), ("none", 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='exempt'")
                         .fetchone()[0], 0)

    def test_more_asks_for_the_next_page(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver(view="all")
        out = reply.apply_reply(self.conn, "more")
        self.assertEqual((out["instructions"], out["applied"]), (["more"], []))

    def test_instructions_are_returned_not_performed(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "Zapier is wrong; rebuild it")
        self.assertIn("rebuild 2026-Q3", out["instructions"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0], 0)

    def test_identity_on_an_unseen_or_changed_item_applies_nothing(self):
        self.deliver()
        pid = self.item("BCK*XYZ", 18000, "2026-09-16", paired=False)    # never shown
        out = reply.apply_reply(self.conn, "the BCK*XYZ one is my accountant")
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 0)
        self.deliver()
        self.row(self.n, counterparty="BCK*XYZ", amount_minor=17000, booking_date="2026-09-16",
                 value_date="2026-09-16")                                # changed since shown
        self.settle(pid)
        out = reply.apply_reply(self.conn, "the BCK*XYZ one is my accountant")
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 0)

    def test_a_vendor_wide_rule_waits_for_every_payment_it_changes_to_be_seen(self):
        # round p6 (Terra S1)
        self.deliver()
        pid = self.item("Adobe", 5445, "2026-09-14")                     # paired, unseen
        out = reply.apply_reply(self.conn, "no invoices ever for Adobe")
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties WHERE"
                                           " exp_kind IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.author(pid)[0], "auto")                   # the pairing survives
        self.deliver()
        out = reply.apply_reply(self.conn, "no invoices ever for Adobe")
        self.assertEqual(len(out["applied"]), 1)

    def test_an_identity_waits_for_every_payment_it_changes(self):
        # round p7 (Astra S1): the identity reaches an unseen payment with the same bank text
        import kb
        kb.upsert_counterparty(self.conn, "my accountant")
        kb.set_expectation(self.conn, scope_type="counterparty", scope="my accountant",
                           kind="none", author="specialist")
        shown_pid = self.item("BCK*XYZ", 18000, "2026-09-16", paired=False)
        self.deliver()
        hidden = self.item("BCK*XYZ", 25000, "2026-09-18")              # paired, never shown
        out = reply.apply_reply(self.conn, "the BCK*XYZ 180.00 one is my accountant")
        self.assertEqual(out["applied"], [])
        self.assertIn(hidden, out["reshow"])
        self.assertEqual(self.author(hidden)[0], "auto")
        del shown_pid

    def test_a_broad_rule_rebuilds_the_quarter_it_changed(self):
        pid = self.item("Adobe", 5445, "2026-05-14", paired=False)
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        self.settle(pid)
        r = views.build_review(self.conn, view="missing", quarter="2026-Q2")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        out = reply.apply_reply(self.conn, "no invoices ever for Adobe; rebuild it")
        self.assertEqual(out["instructions"], ["rebuild 2026-Q2"])

    def test_rebuild_is_decided_after_the_whole_reply(self):
        self.deliver()
        self.item("Zapier", 9900, "2026-09-17")                            # unseen
        for text in ("rebuild it; Zapier is wrong", "all good except the Zapier; rebuild it"):
            out = reply.apply_reply(self.conn, text)
            self.assertEqual(out["instructions"], [], text)
            self.assertIn("Not rebuilding yet", out["receipt"], text)

    def test_an_unresolved_correction_blocks_its_rebuild(self):
        self.deliver()
        self.item("Zapier", 9900, "2026-09-17")                            # unseen
        out = reply.apply_reply(self.conn, "Zapier is wrong; rebuild it")
        self.assertEqual(out["instructions"], [])
        self.assertIn("Not rebuilding yet", out["receipt"])

    def test_a_bare_number_is_not_a_line_reference(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "4 good")
        self.assertEqual(out["applied"], [])
        self.assertIn("no numbered lines", out["receipt"])

    def test_a_reply_after_the_quarter_shipped_offers_a_rebuild(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        with db.tx(self.conn):
            pk = self.conn.execute("INSERT INTO packages(quarter, filename, path, built_at, partial,"
                                   " digest, size, caption, manifest_json) VALUES ('2026-Q3',"
                                   " 'books-2026-Q3-2026-10-14.zip', '/x', '2026-10-14T10:00:00Z',"
                                   " 0, 'd', 1, 'c', '{}')").lastrowid
            self.conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                              " created_at, settled_at) VALUES (?, 'telegram', '/x', 'delivered',"
                              " 'x', '2026-10-14T10:00:00Z')", (pk,))
        out = reply.apply_reply(self.conn, "the Zapier one is wrong")
        self.assertIn('say "rebuild it"', out["receipt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0], 1)
        del z


class TestPreflightRulings(Base):
    def test_a_refused_setting_rides_in_the_same_receipt(self):
        # R2: "start from Q3" is refused (the start can only move earlier) after an
        # earlier clause committed; the operator still gets one receipt with both
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "the Zapier one is wrong; start from Q3")
        self.assertIsNone(self.author(z))
        self.assertIn("Unpaired Zapier · EUR 99.00 · 17 Sep.", out["receipt"])
        self.assertIn("Not changing where the books start", out["receipt"])
        self.assertIn("it can only move earlier", out["receipt"])
        self.assertEqual(len(out["applied"]), 1)

    def test_every_setting_clause_is_guarded(self):
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM binding")
        out = reply.apply_reply(self.conn, "stop chasing Q2; call the zips acme; the bank"
                                           " ledger was reset; start from Q1")
        self.assertEqual(len(out["applied"]), 1)              # stop chasing: nothing to stop
        self.assertEqual(out["receipt"].count("no account is bound yet"), 3)

    def test_a_rule_with_no_sheet_sent_says_so_in_operator_words(self):
        self.item("Adobe", 18000, "2026-09-16", paired=False)
        out = reply.apply_reply(self.conn, "no invoices ever for Adobe")
        self.assertEqual(out["applied"], [])
        self.assertIn("Not applied", out["receipt"])
        self.assertNotIn("render", out["receipt"])

    def test_store_refusals_are_translated(self):
        self.item("Zapier", 9900, "2026-09-17")
        pid = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        for msg, said in (("this payment needs a receipt, and this is a invoice",
                           "this is an invoice"),
                          (f"that document has since been paired with payment #{pid}",
                           "paired with another payment (Vercel · EUR 12.10 · 18 Sep)")):
            with mock.patch.object(matches, "confirm_match", side_effect=db.Refusal(msg)):
                out = reply.apply_reply(self.conn, "the Zapier one is good")
            self.assertIn(said, out["receipt"])
            self.assertIn("not applied", out["receipt"])

    def test_a_question_beside_a_correction_changes_only_the_correction(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, "the Vercel one is wrong. is the Zapier one right?")
        self.assertFalse(out["not_a_reply"])
        self.assertIsNone(self.author(v))
        self.assertEqual(self.author(z)[0], "auto")
        self.assertIn("a question", out["receipt"])

    def test_all_good_answers_only_a_sheet_sent_last(self):
        a = self.item("Adobe", 5445, "2026-09-14")
        self.deliver()
        r = views.build_review(self.conn, view="item", quarter="2026-Q3", pid=a)
        views.mark_rendering_delivered(self.conn, r["render_id"])
        out = reply.apply_reply(self.conn, "all good")
        self.assertEqual(out["applied"], [])
        self.assertEqual(self.author(a)[0], "auto")
        self.assertIn("\"all good\"", out["receipt"])



class TestFixRound1(Base):
    def candidates(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        docs = []
        for _ in range(2):
            docs.append(self.doc())
            matches.record_match(self.conn, pid=pid, doc_id=docs[-1], author="auto",
                                 expected_revision=self.rev(pid), token=self.token,
                                 row_snapshot=self.snapshot(pid))
        self.deliver(view="check")
        return pid, docs

    def states(self):
        return [r[0] for r in self.conn.execute("SELECT state FROM match_state ORDER BY match_id")]

    def test_candidates_are_set_aside_all_or_none(self):
        import documents
        pid, docs = self.candidates()
        documents.update_document_metadata(self.conn, docs[0], document_date="2026-09-01")
        before = self.states()
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.states(), before)
        self.assertNotIn("rejected", self.states())
        self.assertIn("changed since you saw it", out["receipt"])
        self.assertNotIn("Set aside", out["receipt"])

    def test_unchanged_candidates_are_all_set_aside(self):
        pid, _ = self.candidates()
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertEqual(self.states(), ["rejected", "rejected"])
        self.assertIn("Set aside both candidates for Adobe", out["receipt"])
        self.assertEqual(len(out["applied"]), 1)
        del pid

    def test_a_numbered_refusal_keeps_its_noun(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        with mock.patch.object(matches, "reject_match",
                               side_effect=db.Refusal("there is no pairing #12")):
            out = reply.apply_reply(self.conn, "the Zapier one is wrong")
        self.assertIn("there is no such pairing", out["receipt"])
        self.assertEqual(reply._say(self.conn, db.Refusal("see document #4 first")),
                         "see that document first")

    def test_machine_kinds_become_words(self):
        said = reply._say(self.conn, db.Refusal(
            "this payment needs a credit-note, and this is a other"))
        self.assertEqual(said, "this payment needs a credit note, and this is a document")

    def test_a_rule_for_an_unknown_vendor_creates_nothing(self):
        self.item("Adobe", 18000, "2026-09-16", paired=False)
        self.deliver()
        out = reply.apply_reply(self.conn, "no invoices ever for Adbe")
        self.assertEqual(out["applied"], [])
        self.assertIn("Nothing open matches", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 0)

    def test_a_no_op_correction_blocks_its_rebuild(self):
        self.item("Zapier", 9900, "2026-09-17", paired=False)
        self.deliver()
        for text in ("Zapier is wrong; rebuild it", "Zapier is good; rebuild it"):
            out = reply.apply_reply(self.conn, text)
            self.assertEqual(out["instructions"], [], text)
            self.assertIn("Not rebuilding yet", out["receipt"], text)

    def test_the_escape_word_binds_anywhere(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        out = reply.apply_reply(self.conn, "the Zapier one is wrong, accounting")
        self.assertIsNone(self.author(z))
        self.assertIn("Unpaired Zapier", out["receipt"])
        self.assertNotIn("didn't understand", out["receipt"])

    def test_bulk_except_cites_the_vendor_written(self):
        self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, "all good except the Vercel")
        self.assertIn('"the Vercel one is wrong"', out["receipt"])
        self.assertNotIn("Zapier", out["receipt"])



class TestFixRound2(Base):
    def test_the_escape_word_is_a_marker_not_clause_content(self):
        abc = self.item("ABC Accounting Services", 9900, "2026-09-17")
        z = self.item("Zapier", 1210, "2026-09-18")
        v = self.item("Vercel", 2000, "2026-09-19")
        self.deliver()
        out = reply.apply_reply(self.conn, "the ABC Accounting Services one is wrong")
        self.assertIsNone(self.author(abc))
        self.assertIn("Unpaired ABC Accounting Services", out["receipt"])
        out = reply.apply_reply(self.conn, "the Zapier one is wrong, accounting")
        self.assertIsNone(self.author(z))
        out = reply.apply_reply(self.conn, "accounting: the Vercel one is wrong")
        self.assertIsNone(self.author(v))
        self.assertNotIn("didn't understand", out["receipt"])

    def test_a_zip_name_may_say_accounting(self):
        import binding
        out = reply.apply_reply(self.conn, "call the zips accounting.zip")
        self.assertEqual(len(out["applied"]), 1, out["receipt"])
        self.assertEqual(self.conn.execute("SELECT package_name FROM binding").fetchone()[0],
                         binding.slug("accounting.zip"))



class TestFixRound3(Base):
    def both_apply(self, text):
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = reply.apply_reply(self.conn, text)
        self.assertEqual(len(out["applied"]), 2, out["receipt"])
        self.assertEqual(self.author(z)[0], "operator")
        self.assertIsNone(self.author(v))

    def test_a_period_without_a_space_still_ends_a_sentence(self):
        self.both_apply("Zapier is fine.Vercel is wrong")

    def test_a_period_and_a_space_ends_a_sentence(self):
        self.both_apply("Zapier is fine. Vercel is wrong")

    def test_numbers_and_file_names_stay_inside_their_clause(self):
        self.assertEqual(reply._clauses("call the zips accounting.zip"),
                         ["call the zips accounting.zip"])
        self.assertEqual(reply._clauses("the Adobe 99.00 one is wrong.the 1.234,56 one is"
                                         " good. the 14.09 one needs no invoice"),
                         ["the adobe 99.00 one is wrong", "the 1.234,56 one is good",
                          "the 14.09 one needs no invoice"])
        self.assertEqual(reply._clauses("send invoice.pdf again.Adobe is wrong"),
                         ["send invoice.pdf again", "adobe is wrong"])
        self.assertEqual(reply._clauses("Zapier is fine.Adobe is wrong"),
                         ["zapier is fine", "adobe is wrong"])



class TestFixWaveD(Base):
    def test_all_good_binds_to_the_sheet_delivered_last_within_one_second(self):
        # Astra S1: delivered_at has one-second resolution; B then A delivered in the
        # same second must bind "all good" to A (delivery order), never to B (creation order).
        a = self.item("Adobe", 5445, "2026-09-14")
        sheet_a = views.build_review(self.conn, view="status", quarter="2026-Q3")
        f = self.item("Figma", 1815, "2026-09-15")
        sheet_b = views.build_review(self.conn, view="status", quarter="2026-Q3")
        self.assertEqual(sorted(views.render_items(self.conn, sheet_b["render_id"])), [a, f])
        with mock.patch.object(db, "now", lambda: "2026-09-27T10:00:00Z"):
            views.mark_rendering_delivered(self.conn, sheet_b["render_id"])
            views.mark_rendering_delivered(self.conn, sheet_a["render_id"])
        out = reply.apply_reply(self.conn, "all good")
        self.assertEqual(self.operator_entries(), 1)
        self.assertEqual(self.author(a)[0], "operator")
        self.assertEqual(self.author(f)[0], "auto")
        self.assertNotIn("Figma", out["receipt"])

    def test_a_rule_binds_to_the_rendering_delivered_last(self):
        views.build_review(self.conn, view="status", quarter="2026-Q3")
        first = views.build_review(self.conn, view="status", quarter="2026-Q3")["render_id"]
        second = views.build_review(self.conn, view="missing", quarter="2026-Q3")["render_id"]
        with mock.patch.object(db, "now", lambda: "2026-09-27T10:00:00Z"):
            views.mark_rendering_delivered(self.conn, second)
            views.mark_rendering_delivered(self.conn, first)
        self.assertEqual(db.last_delivered(self.conn)["render_id"], first)


class TestReceiptPages(Base):
    """fix wave D (Astra S2): 100 rejections committed, then a 4,199-unit
    receipt nobody could be sent. The receipt is paged at whole lines; every
    committed effect and every exception is still named, in order."""
    def setUp(self):
        super().setUp()
        self.pids = [self.item("Vendor %03d Holding" % i, 10000 + i, "2026-09-%02d" % (1 + i % 28))
                     for i in range(1, 101)]
        self.show(*self.pids)

    def test_a_hundred_rejections_give_pages_within_the_limit(self):
        text = " ".join("Vendor %03d Holding is wrong." % i for i in range(1, 101))
        self.assertLess(views.utf16_len(text), views.TELEGRAM_LIMIT)
        out = reply.apply_reply(self.conn, text)
        self.assertEqual(len(out["applied"]), 100)
        self.assertEqual(self.operator_entries(), 100)
        pages = out["receipt_pages"]
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(views.utf16_len(page), views.TELEGRAM_LIMIT)
            self.assert_operator_words(page)
        self.assertEqual(out["receipt"], pages[0])
        lines = "\n".join(pages).splitlines()
        self.assertEqual(len(lines), 100)
        for i in range(1, 101):
            self.assertEqual(sum(1 for ln in lines
                                 if ln.startswith("Unpaired Vendor %03d Holding" % i)), 1, i)

    def test_one_overlong_line_is_split_and_nothing_is_lost(self):
        long = "Which one? " + "; ".join("Vendor %03d Holding · EUR 100.%02d · %d Sep" % (i, i % 100, i % 28 + 1)
                                           for i in range(1, 200)) + " — say it with the amount or the date."
        pages = reply._pages([long, "Confirmed Adobe."])
        for page in pages:
            self.assertLessEqual(views.utf16_len(page), views.TELEGRAM_LIMIT)
        self.assertEqual("".join(p.replace("\n", "") for p in pages).replace(" ", ""),
                         (long + "Confirmed Adobe.").replace(" ", ""))

    def test_a_short_receipt_is_one_page(self):
        out = reply.apply_reply(self.conn, "Vendor 001 Holding is wrong.")
        self.assertEqual(out["receipt_pages"], [out["receipt"]])

    def test_a_non_reply_has_no_pages(self):
        out = reply.apply_reply(self.conn, "is this about Adobe?")
        self.assertEqual((out["receipt"], out["receipt_pages"]), ("", []))


class TestReceiptSplit(Base):
    def test_a_which_one_listing_splits_within_the_limit(self):
        # fix wave D round 2 (Astra S2): the splitter appended ";" to a full
        # piece and produced a 4097-unit page.
        adobe = [self.item("Adobe", 1000 if i == 0 else 10000, "2026-09-01", paired=False)
                 for i in range(147)]
        figma = self.item("Figma", 1815, "2026-09-15")
        self.show(*adobe, figma)
        out = reply.apply_reply(self.conn, "Adobe is wrong; Figma is wrong")
        for page in out["receipt_pages"]:
            self.assertLessEqual(views.utf16_len(page), views.TELEGRAM_LIMIT)
        self.assertIsNone(self.author(figma))
        ask = out["asks"][0]
        self.assertGreater(views.utf16_len(ask), views.TELEGRAM_LIMIT)
        joined = "\n".join(out["receipt_pages"])
        self.assertEqual(joined.replace("\n", " "), " ".join(
            ln for ln in [ask, "Unpaired Figma · EUR 18.15 · 15 Sep."]))


class TestFieldClip(Base):
    """fix wave D round 3: a whole line clipped at 600 units stayed bound although
    what identified it (amount, date, a second candidate) was cut away. Only
    free-text FIELDS are clipped; identifying fields always print."""
    def test_two_payees_with_one_long_prefix_each_show_amount_and_date(self):
        prefix = "Consolidated Holding Services " * 24              # ~720 characters
        a = self.item(prefix + "Alpha", 10101, "2026-09-03")
        b = self.item(prefix + "Beta", 20202, "2026-09-04")
        r = self.deliver()
        flat = " ".join(r["text"].split())
        self.assertIn("EUR 101.01 · 3 Sep", flat)
        self.assertIn("EUR 202.02 · 4 Sep", flat)
        self.assertIn(views.CLIP_MARK, r["text"])                 # the payee field was clipped
        self.assertLess(views.utf16_len(r["text"]), 1500)
        self.assertEqual(sorted(views.render_items(self.conn, r["render_id"])), sorted([a, b]))
        reply.apply_reply(self.conn, "all good")
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("operator", "operator"))

    def test_a_long_invoice_number_never_hides_the_next_candidate(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        for number in ("N" * 590, "HIDDEN-B"):
            d = self.doc(document_number=number)
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid), token=self.token,
                                 row_snapshot=self.snapshot(pid))
        r = self.deliver(view="check")
        self.assertIn("HIDDEN-B", r["text"])
        self.assertIn(views.CLIP_MARK, r["text"])
        self.assertLess(views.utf16_len(r["text"]), 1000)
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertIn("Set aside both candidates", out["receipt"])


class TestIdentity(Base):
    """fix wave D round 4: every entity a rendering binds is uniquely identified
    by text visibly in it, and every item is bindable in a reachable view."""
    def pair(self, pid, number, date="2026-09-02", **kw):
        d = self.doc(document_number=number, document_date=date)
        matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                             expected_revision=self.rev(pid), token=self.token,
                             row_snapshot=self.snapshot(pid), **kw)
        return d

    def more_candidates(self, pid, numbers, dates_):
        """Candidates beyond the two record_match makes, written as the store
        holds them (match + conflicted match_state), for a large candidate set."""
        tmpl = self.conn.execute("SELECT * FROM match_state WHERE pid=? AND state='conflicted'"
                                 " ORDER BY match_id LIMIT 1", (pid,)).fetchone()
        docs = [self.doc(document_number=n, document_date=d) for n, d in zip(numbers, dates_)]
        with db.tx(self.conn):
            for doc in docs:
                mid = self.conn.execute(
                    "INSERT INTO matches(pid_created, doc_id, label, runners_up_json,"
                    " created_seq) VALUES (?,?,'clean','[]',?)",
                    (pid, doc, db.next_seq(self.conn))).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                                  " activation, fp, revision, digest) VALUES"
                                  " (?,?,?,'conflicted','auto',?,?,0,?)",
                                  (mid, pid, doc, tmpl["activation"], tmpl["fp"], tmpl["digest"]))

    def bound(self, rid, pid):
        import json
        row = self.conn.execute("SELECT match_revisions_json FROM render_items WHERE"
                                " render_id=? AND pid=?", (rid, pid)).fetchone()
        return None if row is None else {int(k) for k in json.loads(row[0])}

    def test_numbers_alike_in_their_first_sixty_characters_render_apart(self):
        # Astra S1 (a): 65 x "A" + CORRECT / WRONG rendered identically
        x = self.item("Adobe", 5445, "2026-09-14", paired=False)
        y = self.item("Adobe", 6000, "2026-09-15", paired=False)
        self.pair(x, "A" * 65 + "CORRECT", labels=("guessed",))
        self.pair(y, "A" * 65 + "WRONG", labels=("guessed",))
        r = self.deliver()
        a, b = views.field("A" * 65 + "CORRECT"), views.field("A" * 65 + "WRONG")
        self.assertNotEqual(a, b)
        self.assertIn(a, " ".join(r["text"].split()))
        self.assertIn(b, " ".join(r["text"].split()))

    def test_two_candidates_alike_in_their_first_sixty_characters_render_apart(self):
        # Astra S1 (b): both rendered identically, and "Adobe is wrong" rejected both
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        m = [self.pair(pid, "A" * 65 + tail) for tail in ("CORRECT", "WRONG")]
        r = self.deliver(view="check")
        idents = [views.ident(views.work.describe(self.conn, pid)["candidates"][i]["document"])
                  for i in range(2)]
        self.assertNotEqual(idents[0], idents[1])
        for i in idents:
            self.assertIn(i, " ".join(r["text"].split()))
        self.assertEqual(len(self.bound(r["render_id"], pid)), 2)
        del m

    def test_same_number_across_issuers_binds_both_and_applies(self):
        # round 5 (Astra S2): number SAME, date 2 Sep, two issuers — the backstop bound
        # neither and "Adobe is wrong" re-showed forever. Distinct by construction now.
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        for issuer in ("Adobe", "Adobe Ireland"):
            d = self.doc(document_number="SAME", issuer=issuer, document_date="2026-09-02")
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid), token=self.token,
                                 row_snapshot=self.snapshot(pid))
        r = self.deliver(view="check")
        flat = " ".join(r["text"].split())
        self.assertIn("invoice SAME \u00b7Adobe (2 Sep)", flat)
        self.assertIn("invoice SAME \u00b7Adobe Ireland (2 Sep)", flat)
        self.assertEqual(len(self.bound(r["render_id"], pid)), 2)
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertIn("Set aside both candidates", out["receipt"])
        self.assertEqual(out["reshow"], [])

    def test_same_number_same_issuer_is_told_apart_by_the_content_hash(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        docs = []
        for body in ("one", "two"):
            docs.append(self.doc(document_number="SAME", issuer="Adobe",
                                 document_date="2026-09-02", source_ref=body))
        with db.tx(self.conn):          # the store holds such a pair (a duplicate not yet resolved)
            for doc in docs:
                mid = self.conn.execute(
                    "INSERT INTO matches(pid_created, doc_id, label, runners_up_json,"
                    " created_seq) VALUES (?,?,'clean','[]',?)",
                    (pid, doc, db.next_seq(self.conn))).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                                  " activation) VALUES (?,?,?,'conflicted','auto',0)",
                                  (mid, pid, doc))
        r = self.deliver(view="check")
        shas = [self.conn.execute("SELECT sha256 FROM documents WHERE doc_id=?", (d,))
                .fetchone()[0] for d in docs]
        flat = " ".join(r["text"].split())
        self.assertEqual(flat.count("invoice SAME \u00b7Adobe\u00b7"), 2, flat)
        self.assertEqual(len(self.bound(r["render_id"], pid)), 2)
        del shas

    def test_identical_payments_print_apart_and_both_bind(self):
        a = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        r = self.deliver()
        self.assertEqual(sorted(views.render_items(self.conn, r["render_id"])), sorted([a, b]))
        self.assertEqual(" ".join(r["text"].split()).count("Adobe · EUR 54.45 · 14 Sep · ref "), 2)
        reply.apply_reply(self.conn, "all good")
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("operator", "operator"))

    def test_identical_payments_are_named_by_their_ref(self):
        a = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        r = self.deliver()
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("or the ref", out["receipt"])
        ref = views.lineage_ref(b)[:4]
        self.assertIn("ref " + ref, " ".join(r["text"].split()))
        self.assertIn("ref " + ref, out["receipt"])
        out = reply.apply_reply(self.conn, f"the Adobe ref {ref} one is wrong")
        self.assertIsNone(self.author(b))
        self.assertEqual(self.author(a)[0], "auto")

    def conflicted(self, pid, specs, shas=None):
        docs = [self.doc(document_number=n, issuer=i, document_date="2026-09-02",
                         source_ref="%s|%s" % (n, i), **({"sha256": shas[k]} if shas else {}))
                for k, (n, i) in enumerate(specs)]
        with db.tx(self.conn):
            for doc in docs:
                mid = self.conn.execute(
                    "INSERT INTO matches(pid_created, doc_id, label, runners_up_json,"
                    " created_seq) VALUES (?,?,'clean','[]',?)",
                    (pid, doc, db.next_seq(self.conn))).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                                  " activation) VALUES (?,?,?,'conflicted','auto',0)",
                                  (mid, pid, doc))
        return docs

    def test_a_payee_literally_named_like_a_ref_is_not_a_ref(self):
        # round 6 (Astra S1): "Adobe ref e40c" was parsed as pid 1's ref
        a = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        forged = "Adobe ref " + views.lineage_ref(a)[:4]
        b = self.item(forged, 5445, "2026-09-14", labels=("guessed",))
        r = self.deliver()
        self.assertNotIn(" · ref ", r["text"])                 # nothing needed a generated ref
        out = reply.apply_reply(self.conn, f"the {forged} one is wrong")
        self.assertIsNone(self.author(b))                        # the payment it names
        self.assertEqual(self.author(a)[0], "auto")              # untouched
        del out

    def test_a_literal_name_and_a_delivered_ref_together_ask(self):
        a = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        twin = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        r = self.deliver()
        ref = views.lineage_ref(a)[:4]
        self.assertIn("ref " + ref, " ".join(r["text"].split()))
        c = self.item("Adobe ref " + ref, 7000, "2026-09-16", labels=("guessed",))
        self.deliver()
        out = reply.apply_reply(self.conn, f"the Adobe ref {ref} one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("Which one?", out["receipt"])
        self.assertEqual((self.author(a)[0], self.author(c)[0], self.author(twin)[0]),
                         ("auto", "auto", "auto"))

    def test_a_literal_number_cannot_forge_a_generated_identity(self):
        # round 6 (Astra S2): SAME, SAME and literally "SAME from Adobe ·7692"
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        docs = self.conflicted(pid, [("SAME", "Adobe"), ("SAME", "Adobe"),
                                     ("SAME from Adobe \u00b77692", "Adobe"),
                                     ("SAME \u00b7Adobe", "Adobe"), ("SAME", "Adobe \u00b7x")],
                               shas=["7692" + "1" * 60, "1234" + "2" * 60, "3" * 64,
                                     "4" * 64, "5" * 64])
        r = self.deliver(view="check")
        self.assertLessEqual(views.utf16_len(r["text"]), views.TELEGRAM_LIMIT)
        it = views.build_review(self.conn, view="item", pid=pid)
        flat = " ".join(it["text"].split())
        # a literal never prints the reserved mark: generated text is unforgeable
        self.assertIn("invoice SAME from Adobe \u20227692", flat)
        self.assertIn("invoice SAME \u2022Adobe", flat)
        self.assertNotIn("SAME from Adobe \u00b77692", flat)
        page, after, bound = None, None, set()
        while True:
            kw = {"page": page, "after": after} if page else {}
            it = views.build_review(self.conn, view="item", pid=pid, **kw)
            bound |= self.bound(it["render_id"], pid)
            views.mark_rendering_delivered(self.conn, it["render_id"])
            if it["next"] is None:
                break
            page, after = it["next"]["page"], it["next"]["after"]
        self.assertEqual(len(bound), len(docs))
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertIn("Set aside 5 candidates", out["receipt"])

    def test_a_name_that_displays_like_another_asks(self):
        # round 7 (Astra S1): "A·B" displays as "A•B"; "the A•B one is wrong" unpaired the
        # literal "A•B" while the payment shown as "A•B" stayed — no question asked
        a = self.item("A\u00b7B", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("A\u2022B", 7000, "2026-09-16", labels=("guessed",))
        r = self.deliver()
        self.assertEqual(" ".join(r["text"].split()).count("A\u2022B \u00b7 EUR"), 2)
        out = reply.apply_reply(self.conn, "the A\u2022B one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("Which one?", out["receipt"])
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("auto", "auto"))
        out = reply.apply_reply(self.conn, "the A\u2022B 54.45 one is wrong")
        self.assertIsNone(self.author(a))                       # the amount tells them apart
        self.assertEqual(self.author(b)[0], "auto")

    def alias_after(self, between):
        # round 8 (Astra + Terra S1): an alert or an unrelated item view delivered after the
        # sheet dropped the alias, and the reply unpaired the EUR 70.00 payment silently
        import passes
        a = self.item("A\u00b7B", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("A\u2022B", 7000, "2026-09-16", labels=("guessed",))
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        if between == "alert":
            t = self.pass_()
            passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
            speak = passes.end_pass(self.conn, t, "complete", {})["speak"]
            views.mark_rendering_delivered(self.conn, speak["render_id"])
            self.token = self.pass_()
        else:
            it = views.build_review(self.conn, view="item", pid=z)
            views.mark_rendering_delivered(self.conn, it["render_id"])
        out = reply.apply_reply(self.conn, "the A\u2022B one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("Which one?", out["receipt"])
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("auto", "auto"))

    def test_a_display_alias_survives_a_delivered_alert(self):
        self.alias_after("alert")

    def test_a_display_alias_survives_an_unrelated_item_view(self):
        self.alias_after("item")

    def at_pid(self, pid):
        """The next lineage gets this pid (payments far apart share a ref prefix)."""
        with db.tx(self.conn):
            self.conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='projections'",
                              (pid - 1,))

    def test_a_ref_printed_on_two_payments_asks(self):
        # round 9 (Astra S1): payments 73 (Alpha) and 223 (Beta) both printed "ref 7291";
        # the scope kept 7291 -> 223 only and "ref 7291 is wrong" unpaired Beta silently
        self.item("Seed", 100, "2026-09-01")                    # creates the sequence row
        self.at_pid(73)
        alpha = self.item("Alpha", 5445, "2026-09-14", labels=("guessed",))
        self.item("Alpha", 5445, "2026-09-14", labels=("guessed",))
        self.at_pid(223)
        beta = self.item("Beta", 7000, "2026-09-16", labels=("guessed",))
        self.item("Beta", 7000, "2026-09-16", labels=("guessed",))
        self.assertEqual((alpha, beta), (73, 223))
        r = self.deliver(view="check")
        self.assertEqual(" ".join(r["text"].split()).count("ref 7291"), 2)
        out = reply.apply_reply(self.conn, "ref 7291 is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("Which one?", out["receipt"])
        self.assertEqual((self.author(alpha)[0], self.author(beta)[0]), ("auto", "auto"))
        reply.apply_reply(self.conn, "the Alpha ref 7291 one is wrong")
        self.assertIsNone(self.author(alpha))
        self.assertEqual(self.author(beta)[0], "auto")

    def test_a_name_that_displays_like_another_asks_with_equal_facts(self):
        a = self.item("A\u00b7B", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("A\u2022B", 5445, "2026-09-14", labels=("guessed",))
        r = self.deliver()
        self.assertEqual(" ".join(r["text"].split()).count(" \u00b7 ref "), 2)
        out = reply.apply_reply(self.conn, "the A\u2022B one is wrong")
        self.assertEqual(out["applied"], [])
        self.assertIn("or the ref", out["receipt"])
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("auto", "auto"))

    def test_a_rule_for_a_name_that_displays_like_another_asks(self):
        self.item("A\u00b7B", 5445, "2026-09-14", labels=("guessed",))
        self.item("A\u2022B", 7000, "2026-09-16", labels=("guessed",))
        self.deliver()
        out = reply.apply_reply(self.conn, "no invoices ever for A\u2022B")
        self.assertEqual(out["applied"], [])
        self.assertIn("Which one?", out["receipt"])

    def test_a_four_hex_digest_collision_is_lengthened(self):
        # round 5 (Astra S1): "A"*65+"149" and +"257" share the digest 0844
        n1, n2 = "A" * 65 + "149", "A" * 65 + "257"
        self.assertEqual(views.field(n1), views.field(n2))       # the collision, outside a view
        x = self.item("Adobe", 5445, "2026-09-14", paired=False)
        y = self.item("Adobe", 6000, "2026-09-15", paired=False)
        self.pair(x, n1, labels=("guessed",))
        self.pair(y, n2, labels=("guessed",))
        r = self.deliver()
        flat = " ".join(r["text"].split())
        self.assertIn("\u00b70844d4d4", flat)
        self.assertIn("\u00b708449d0c", flat)
        self.assertEqual(sorted(views.render_items(self.conn, r["render_id"])), sorted([x, y]))

    def test_a_hundred_runners_up_leave_the_item_bindable_everywhere(self):
        # Astra + Terra: 80-120 runners-up made the item unbindable in every view
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        mid = self.pair(pid, "INV-1", labels=("guessed",),
                        runners_up=tuple("runner-up invoice %03d from Adobe" % i
                                         for i in range(100)))
        del mid
        for view, kw in (("status", {}), ("check", {}), ("all", {}), ("item", {"pid": pid})):
            r = views.build_review(self.conn, view=view, quarter="2026-Q3", **kw)
            self.assertLessEqual(views.utf16_len(r["text"]), views.TELEGRAM_LIMIT)
            self.assertIn("and 97 others", r["text"], view)
            self.assertEqual(views.render_items(self.conn, r["render_id"]), [pid], view)
            self.assertEqual(len(self.bound(r["render_id"], pid)), 1, view)

    def test_many_candidates_page_through_the_item_view_then_apply(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        for i in range(2):
            self.pair(pid, "CANDIDATE-%02d-" % i + "X" * 40, date="2026-09-%02d" % (1 + i))
        self.more_candidates(pid, ["CANDIDATE-%02d-" % i + "X" * 40 for i in range(2, 60)],
                             ["2026-09-%02d" % (1 + i % 28) for i in range(2, 60)])
        r = self.deliver(view="check")
        self.assertEqual(len(self.bound(r["render_id"], pid)), views.CANDIDATES_MAX)
        self.assertIn('57 more could fit — say "candidates for 54.45 14 Sep".', r["text"])
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")      # not all shown
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        out = reply.apply_reply(self.conn, "candidates for 54.45 14 Sep")
        self.assertEqual(out["instructions"], ["show item %d" % pid])
        seen, page, after = set(), None, None
        for _ in range(20):
            kw = {"page": page, "after": after} if page else {}
            it = views.build_review(self.conn, view="item", pid=pid, **kw)
            self.assertLessEqual(views.utf16_len(it["text"]), views.TELEGRAM_LIMIT)
            seen |= self.bound(it["render_id"], pid)
            views.mark_rendering_delivered(self.conn, it["render_id"])
            if it["next"] is None:
                break
            self.assertTrue(it["text"].endswith('say "more".'))
            page, after = it["next"]["page"], it["next"]["after"]
        self.assertGreater(page or 1, 1)
        self.assertEqual(len(seen), 60)
        out = reply.apply_reply(self.conn, "the Adobe one is wrong")
        self.assertIn("Set aside 60 candidates", out["receipt"])


class TestIntegration(Base):
    def test_a_counted_but_unprinted_payment_is_reshown_not_changed(self):
        # a never-searched payment is counted ("not searched"), not printed: the
        # operator never saw its line, so a correction naming it applies nothing
        self.n += 1
        self.row(self.n, counterparty="BCK*XYZ", amount_minor=18000,
                 booking_date="2026-09-16", value_date="2026-09-16")
        pid = self.lineage_for(self.n)
        self.classify(pid, {"software"})
        self.settle(pid)
        self.deliver()
        out = reply.apply_reply(self.conn, "the BCK*XYZ one is my accountant")
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
