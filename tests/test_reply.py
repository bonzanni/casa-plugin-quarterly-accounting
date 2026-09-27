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
