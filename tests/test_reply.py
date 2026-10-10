# tests/test_reply.py
"""#121: the regex grammar is gone — the desk reads the operator's words and passes explicit
operations by pid. What stays here are the write rules a reading still owns: binding to the
post, stale/not-shown re-show, set-aside all-or-none, effects listing, the size limit,
refusals said in operator words, and the receipts."""
import re
import unittest
from unittest import mock

from tests import _base
from tests._base import StoreCase
import db  # noqa: E402
import matches  # noqa: E402
import reply  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402


def assert_operator_words(text):
    """R4: every text the operator sees speaks operator words."""
    for word in views.FORBIDDEN:
        assert word not in text, (word, text)
    assert re.search(r"#\d|\br\d+\b|\ba [aeiou]", text) is None, text


def apply_now(conn, ops, quoted=None):
    """S7 §8 (Task 6), #121: the desk's operations proposed (propose_reading) and, when a
    reading was posted, its Apply tapped (tests._base.apply_now). The proposal and the
    receipt both speak operator words."""
    out = _base.apply_now(conn, ops, quoted)
    assert_operator_words(out["receipt"])
    assert_operator_words(out["proposal"] or "")
    return out


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        self.n = 0

    def assert_operator_words(self, receipt):
        assert_operator_words(receipt)

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

    def not_on_post(self, ops):
        """#121: a pid not on the bound post is refused to the desk; nothing is read."""
        with self.assertRaises(db.Refusal) as cm:
            apply_now(self.conn, ops)
        self.assertIn("is not on the post", str(cm.exception))

    def readings(self):
        return self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]


class TestGrammar(Base):
    def test_a_negative_verdict_unpairs_only_what_it_names(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        out = apply_now(self.conn, [("reject", z)])
        self.assertIn("Removed the match for Zapier · EUR 99.00 · 17 Sep.", out["receipt"])
        self.assertIsNone(self.author(z))
        self.assertEqual(self.author(v)[0], "auto")
        self.assertEqual(self.operator_entries(), 1)

    def test_a_no_ref_line_names_its_invoice(self):
        pid = self.item("Adobe", 5445, "2026-09-14", labels=("no-ref",))
        text = self.deliver()["text"]
        self.assertIn("Matched to invoice", text)
        del pid

    def test_all_good_confirms_only_what_was_shown(self):
        # #121: ops — a payment created after the sheet was sent is refused to the desk
        a = self.item("Adobe", 5445, "2026-09-14")
        self.deliver()
        late = self.item("Figma", 1815, "2026-09-15")          # created after the sheet was sent
        self.not_on_post([("confirm", a), ("confirm", late)])
        self.assertEqual(self.author(a)[0], "auto")
        apply_now(self.conn, [("confirm", a)])
        self.assertEqual(self.author(a)[0], "operator")
        self.assertEqual(self.author(late)[0], "auto")

    def test_identity_is_not_an_exemption(self):
        pid = self.item("BCK*XYZ", 18000, "2026-09-16", paired=False)
        self.deliver()
        out = apply_now(self.conn, [("identity", pid, "my accountant")])
        self.assertIn("BCK\\*XYZ: my accountant; still missing a document.", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='exempt'")
                         .fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "open")

    def test_an_item_never_shown_is_refused_and_nothing_applies(self):
        # binding R3: words resolve only on the one bound rendering; nothing names it
        # (#121: a pid not on the post is refused to the desk, nothing read)
        pid = self.item("Zapier", 9900, "2026-09-17")
        self.not_on_post([("reject", pid)])
        self.assertEqual(self.readings(), 0)
        self.assertEqual(self.author(pid)[0], "auto")

    def test_a_pass_that_moved_one_item_refuses_it_and_applies_the_rest(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        v = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        matches.relabel_match(self.conn, match_id=self.conn.execute(
            "SELECT current_match FROM projections WHERE pid=?", (z,)).fetchone()[0],
            labels=("guessed", "no-ref"), token=self.token)      # the running pass moved it
        out = apply_now(self.conn, [("reject", z), ("reject", v)])
        self.assertEqual(out["reshow"], [z])
        self.assertIsNone(self.author(v))
        self.assertEqual(self.author(z)[0], "auto")

    def test_the_receipt_comes_from_the_commit(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        with mock.patch.object(matches, "reject_in_tx", side_effect=db.Busy("locked")):
            out = apply_now(self.conn, [("reject", z)])
        self.assertNotIn("Removed the match", out["receipt"])
        self.assertIn("not applied", out["receipt"])

    def test_exemption_by_amount_says_what_it_dropped(self):
        pid = self.item("Adobe", 18000, "2026-09-16")
        self.deliver()
        out = apply_now(self.conn, [("no_document", pid)])
        self.assertIn("needs no document", out["receipt"])
        self.assertIn("removed its match", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "exempt")

    def test_no_invoices_ever_is_a_counterparty_expectation(self):
        pid = self.item("Adobe", 18000, "2026-09-16", paired=False)
        self.deliver()
        apply_now(self.conn, [("never", pid)])
        row = self.conn.execute("SELECT exp_kind, exp_row FROM projections WHERE pid=?",
                                (pid,)).fetchone()
        self.assertEqual(tuple(row), ("none", 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='exempt'")
                         .fetchone()[0], 0)

    def test_more_asks_for_the_next_page(self):
        # final fix wave I-1: "more" is the bound rendering's `next` as show_view arguments;
        # a one-page list has none (#121: the desk reads it from reading_context)
        import posting
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver(view="all")
        self.assertIsNone(posting.reading_context(self.conn)["next"])

    def test_identity_on_an_unseen_item_applies_nothing_and_a_changed_one_lists_it(self):
        self.deliver()
        pid = self.item("BCK*XYZ", 18000, "2026-09-16", paired=False)    # never shown
        # binding R2/R3: no provenance on the bound rendering — refused, nothing re-shown
        # (#121: refused to the desk)
        self.not_on_post([("identity", pid, "my accountant")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 0)
        self.deliver()
        self.row(self.n, counterparty="BCK*XYZ", amount_minor=17000, booking_date="2026-09-16",
                 value_date="2026-09-16")                                # changed since shown
        self.settle(pid)
        out = apply_now(self.conn, [("identity", pid, "my accountant")])
        # R2: provenance is the named payment on R; the payment it changes is listed in the
        # proposal as it is now (170.00), and Apply's replay-and-compare guards that set
        self.assertEqual((len(out["applied"]), out["reshow"]), (1, []))
        self.assertIn("170.00", out["proposal"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 1)

    def test_a_vendor_wide_rule_needs_the_vendor_on_the_bound_rendering(self):
        # round p6 (Terra S1); binding R2: provenance, not a re-show of every payment
        self.deliver()
        pid = self.item("Adobe", 5445, "2026-09-14")                     # paired, unseen
        self.not_on_post([("never", pid)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties WHERE"
                                           " exp_kind IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.author(pid)[0], "auto")                   # the pairing survives
        self.deliver()
        out = apply_now(self.conn, [("never", pid)])
        self.assertEqual(len(out["applied"]), 1)

    def test_an_identity_lists_every_payment_it_changes(self):
        # round p7 (Astra S1): the identity reaches an unseen payment with the same bank text.
        # Binding R2 (d1 Astra S2): it binds by provenance and lists that payment in the
        # proposal — the operator sees it before Apply; no re-show, no livelock
        import kb
        kb.upsert_counterparty(self.conn, "my accountant")
        kb.set_expectation(self.conn, scope_type="counterparty", scope="my accountant",
                           kind="none", author="specialist")
        shown = self.item("BCK*XYZ", 18000, "2026-09-16", paired=False)
        self.deliver()
        hidden = self.item("BCK*XYZ", 25000, "2026-09-18")              # paired, never shown
        out = apply_now(self.conn, [("identity", shown, "my accountant")])
        self.assertEqual((len(out["applied"]), out["reshow"]), (1, []))
        self.assertIn("It changes 2 payments", out["proposal"])
        self.assertIn("250.00", out["proposal"])
        self.assertIn(str(hidden), out["applied"][0]["read"])

    def test_a_broad_rule_rebuilds_the_quarter_it_changed(self):
        pid = self.item("Adobe", 5445, "2026-05-14", paired=False)
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        self.settle(pid)
        r = views.build_review(self.conn, view="missing", quarter="2026-Q2")
        views.mark_rendering_delivered(self.conn, r["render_id"])
        # S7 §8: the reading's touched quarter is the one the rule changes (the quarter a
        # delivered package's "no longer matches" line names); a rebuild waits for the Apply
        # (#121: rebuild is get_package, never a reading operation)
        ops = reply.check_ops(_base.as_ops([("never", pid)]))
        with db.tx(self.conn):
            read = reply.reading_in_tx(self.conn, ops)
        self.assertEqual(read["quarters"], ["2026-Q2"])
        out = apply_now(self.conn, [("never", pid)])
        self.assertEqual(len(out["applied"]), 1)
        self.assertEqual(self.conn.execute("SELECT exp_kind FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "none")

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
        out = apply_now(self.conn, [("reject", z)])
        # #121: the receipt offers the rebuild without teaching a phrase
        self.assertIn("no longer matches — I can build a fresh one.", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0], 1)


class TestPreflightRulings(Base):
    def test_a_refused_setting_rides_in_the_same_receipt(self):
        # R2: a setting refused after an earlier clause committed: the operator still gets
        # one receipt with both (r4 Astra S2: re-aimed at a setting that survives 0.11.2 —
        # the zip name — refused by the store, so the guard in _Run.setting is what runs)
        import binding
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()

        def refuse(conn, name, *, grant):
            raise db.Refusal("the zip name cannot change right now")
        self.patch(binding, "set_package_name_in_tx", refuse)
        out = apply_now(self.conn, [("reject", z), ("zip_name", "acme")])
        self.assertIsNone(self.author(z))
        self.assertIn("Removed the match for Zapier · EUR 99.00 · 17 Sep.", out["receipt"])
        self.assertIn("The zip name: not applied", out["receipt"])
        self.assertEqual(len(out["applied"]), 1)

    def test_every_setting_clause_is_guarded(self):
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM binding")
        # 0.11.2 (#55): "start from …" is no longer a setting clause
        out = apply_now(self.conn, [("stop_chasing", "2026-Q2"), ("zip_name", "acme"),
                                    ("ledger_reset",)])
        self.assertEqual(len(out["applied"]), 1)              # stop chasing: nothing to stop
        self.assertEqual(out["receipt"].count("no account is bound yet"), 2)

    def test_a_rule_with_no_sheet_sent_says_so_in_operator_words(self):
        # #121: with no post the words could be about, the desk is refused; nothing is read
        pid = self.item("Adobe", 18000, "2026-09-16", paired=False)
        self.not_on_post([("never", pid)])
        self.assertEqual(self.readings(), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties WHERE"
                                           " exp_kind IS NOT NULL").fetchone()[0], 0)

    def test_store_refusals_are_translated(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        pid = self.item("Vercel", 1210, "2026-09-18")
        self.deliver()
        for msg, said in (("this payment needs a receipt, and this is a invoice",
                           "this is an invoice"),
                          (f"that document has since been matched to payment #{pid}",
                           "matched to another payment (Vercel · EUR 12.10 · 18 Sep)")):
            with mock.patch.object(matches, "confirm_in_tx", side_effect=db.Refusal(msg)):
                out = apply_now(self.conn, [("confirm", z)])
            self.assertIn(said, out["receipt"])
            self.assertIn("not applied", out["receipt"])

    def test_a_typed_right_on_an_already_confirmed_item_is_a_no_op(self):
        """T5b: a confirmed pairing is no proposal (its label row stays "guessed"), so a
        second "right" says it was already fine and writes nothing."""
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        apply_now(self.conn, [("confirm", z)])
        self.assertEqual(self.author(z)[0], "operator")
        n = self.conn.execute("SELECT count(*) FROM log WHERE pid=?", (z,)).fetchone()[0]
        out = apply_now(self.conn, [("confirm", z)])
        self.assertIn("already fine", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE pid=?",
                                           (z,)).fetchone()[0], n)


class TestFixRound1(Base):
    def candidates(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        docs = []
        for _ in range(2):
            docs.append(self.doc())
            self.machine_entry(pid, docs[-1])      # a joint machine set (pre-floor shape)
        self.deliver(view="check")
        return pid, docs

    def states(self):
        return [r[0] for r in self.conn.execute("SELECT state FROM match_state ORDER BY match_id")]

    def test_candidates_are_set_aside_all_or_none(self):
        import documents
        pid, docs = self.candidates()
        documents.update_document_metadata(self.conn, docs[0], document_date="2026-09-01")
        before = self.states()
        out = apply_now(self.conn, [("reject", pid)])
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        self.assertEqual(self.states(), before)
        self.assertNotIn("rejected", self.states())
        self.assertIn("changed since you saw it", out["receipt"])
        self.assertNotIn("Ruled out", out["receipt"])

    def test_unchanged_candidates_are_all_set_aside(self):
        pid, _ = self.candidates()
        out = apply_now(self.conn, [("reject", pid)])
        self.assertEqual(self.states(), ["rejected", "rejected"])
        self.assertIn("Ruled out both invoices for Adobe", out["receipt"])
        self.assertEqual(len(out["applied"]), 1)

    def test_a_numbered_refusal_keeps_its_noun(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        with mock.patch.object(matches, "reject_in_tx",
                               side_effect=db.Refusal("there is no pairing #12")):
            out = apply_now(self.conn, [("reject", z)])
        self.assertIn("there is no such pairing", out["receipt"])
        self.assertEqual(reply._say(self.conn, db.Refusal("see document #4 first")),
                         "see that document first")

    def test_machine_kinds_become_words(self):
        said = reply._say(self.conn, db.Refusal(
            "this payment needs a credit-note, and this is a other"))
        self.assertEqual(said, "this payment needs a credit note, and this is a document")


class TestFixWaveD(Base):
    def test_all_good_binds_to_the_sheet_delivered_last_within_one_second(self):
        # Astra S1: delivered_at has one-second resolution; B then A delivered in the
        # same second must bind the reading to A (delivery order), never to B (creation
        # order). #121: ops — Figma, only on B, is refused to the desk
        a = self.item("Adobe", 5445, "2026-09-14")
        sheet_a = views.build_review(self.conn, view="status", quarter="2026-Q3")
        f = self.item("Figma", 1815, "2026-09-15")
        sheet_b = views.build_review(self.conn, view="status", quarter="2026-Q3")
        self.assertEqual(sorted(views.render_items(self.conn, sheet_b["render_id"])), [a, f])
        with mock.patch.object(db, "now", lambda: "2026-09-27T10:00:00Z"):
            views.mark_rendering_delivered(self.conn, sheet_b["render_id"])
            views.mark_rendering_delivered(self.conn, sheet_a["render_id"])
        self.not_on_post([("confirm", f)])
        out = apply_now(self.conn, [("confirm", a)])
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
    receipt nobody could be sent. S7 §8: a reading is one proposal and its Apply
    one receipt, so a reading too long to show whole is refused before anything is
    read, and every text it does produce fits one message."""
    def setUp(self):
        super().setUp()
        self.pids = [self.item("Vendor %03d Holding" % i, 10000 + i, "2026-09-%02d" % (1 + i % 28))
                     for i in range(1, 101)]
        self.show(*self.pids)

    def test_a_hundred_rejections_are_refused_whole_and_sixty_apply_whole(self):
        import posting
        with self.assertRaises(db.Refusal) as cm:
            apply_now(self.conn, [("reject", p) for p in self.pids])
        self.assertEqual(str(cm.exception), posting.READING_TOO_LONG)
        self.assertEqual(self.operator_entries(), 0)
        self.assertEqual(self.readings(), 0)
        n = 60
        out = apply_now(self.conn, [("reject", p) for p in self.pids[:n]])
        self.assertEqual(len(out["applied"]), n)
        self.assertEqual(self.operator_entries(), n)
        for t in (out["proposal"], out["receipt"]):
            self.assertLessEqual(views.utf16_len(t), views.BODY_LIMIT)
        lines = out["receipt"].splitlines()
        self.assertEqual(len(lines), n)
        for i in range(1, n + 1):
            self.assertEqual(sum(1 for ln in lines
                                 if ln.startswith("Removed the match for Vendor %03d Holding" % i)), 1, i)
        with self.assertRaises(db.Refusal):                      # past the sanity bound
            posting.propose_reading(self.conn, [{"op": "reject", "pid": 1}] * (reply.OPS_MAX + 1))

    # S7 §8: test_one_overlong_line_is_split_and_nothing_is_lost is deleted with
    # reply._pages — a reading's answer is one proposal, one receipt or one `say`.

    def test_a_short_receipt_is_one_message(self):
        out = apply_now(self.conn, [("reject", self.pids[0])])
        self.assertTrue(out["receipt"].startswith("Removed the match for Vendor 001 Holding"))
        self.assertEqual(len(out["receipt"].splitlines()), 1)


class TestReceiptSplit(Base):
    def test_a_proposal_too_long_to_show_is_refused_whole(self):
        # fix wave D round 2 (Astra S2): the splitter appended ";" to a full
        # piece and produced a 4097-unit page. S7 §8: one that cannot be shown whole is
        # refused and nothing is read. #121: ops — a vendor-wide rule lists every payment
        # it moves (147 here), which cannot fit one proposal
        import posting
        adobe = [self.item("Adobe", 1000 if i == 0 else 10000, "2026-09-01", paired=False)
                 for i in range(147)]
        figma = self.item("Figma", 1815, "2026-09-15")
        self.show(*adobe, figma)
        with self.assertRaises(db.Refusal) as cm:
            apply_now(self.conn, [("never", adobe[0]), ("reject", figma)])
        self.assertEqual(str(cm.exception), posting.READING_TOO_LONG)
        self.assertEqual(self.author(figma)[0], "auto")
        self.assertEqual(self.readings(), 0)
        out = apply_now(self.conn, [("reject", figma)])
        self.assertEqual(out["receipt"], "Removed the match for Figma · EUR 18.15 · 15 Sep.")
        self.assertIsNone(self.author(figma))


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
        apply_now(self.conn, [("confirm", a), ("confirm", b)])
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("operator", "operator"))

    def test_a_long_invoice_number_never_hides_the_next_candidate(self):
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        for number in ("N" * 590, "HIDDEN-B"):
            self.machine_entry(pid, self.doc(document_number=number))     # a joint set
        r = self.deliver(view="check")
        self.assertIn("HIDDEN\\-B", r["text"])
        self.assertIn(views.CLIP_MARK, r["text"])
        self.assertLess(views.utf16_len(r["text"]), 1000)
        out = apply_now(self.conn, [("reject", pid)])
        self.assertIn("Ruled out both invoices", out["receipt"])


class TestIdentity(Base):
    """fix wave D round 4: every entity a rendering binds is uniquely identified
    by text visibly in it, and every item is bindable in a reachable view."""
    def pair(self, pid, number, date="2026-09-02", **kw):
        """A machine match through the floor (the document carries the payment's amount);
        a further one for the same payment joins a joint machine set, which only a
        pre-floor store holds (the floor replaces a payment's own pairing)."""
        d = self.doc(document_number=number, document_date=date,
                     amount_minor=self.snapshot(pid)["amount_minor"])
        if self.conn.execute("SELECT 1 FROM match_state WHERE pid=? AND author='auto' AND"
                             " state IN ('matched','proposed','conflicted')",
                             (pid,)).fetchone() is None:
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid), token=self.token,
                                 row_snapshot=self.snapshot(pid), **kw)
        else:
            self.machine_entry(pid, d, **kw)
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
            self.machine_entry(pid, self.doc(document_number="SAME", issuer=issuer,
                                             document_date="2026-09-02"))   # a joint set
        r = self.deliver(view="check")
        flat = " ".join(r["text"].split())
        self.assertIn("invoice SAME ·Adobe (2 Sep)", flat)
        self.assertIn("invoice SAME ·Adobe Ireland (2 Sep)", flat)
        self.assertEqual(len(self.bound(r["render_id"], pid)), 2)
        out = apply_now(self.conn, [("reject", pid)])
        self.assertIn("Ruled out both invoices", out["receipt"])
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
        self.assertEqual(flat.count("invoice SAME ·Adobe·"), 2, flat)
        self.assertEqual(len(self.bound(r["render_id"], pid)), 2)
        del shas

    def test_identical_payments_print_apart_and_both_bind(self):
        a = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("Adobe", 5445, "2026-09-14", labels=("guessed",))
        r = self.deliver()
        self.assertEqual(sorted(views.render_items(self.conn, r["render_id"])), sorted([a, b]))
        self.assertEqual(" ".join(r["text"].split()).count("Adobe · EUR 54.45 · 14 Sep · ref "), 2)
        apply_now(self.conn, [("confirm", a), ("confirm", b)])
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("operator", "operator"))

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

    def test_a_literal_number_cannot_forge_a_generated_identity(self):
        # round 6 (Astra S2): SAME, SAME and literally "SAME from Adobe ·7692"
        pid = self.item("Adobe", 5445, "2026-09-14", paired=False)
        docs = self.conflicted(pid, [("SAME", "Adobe"), ("SAME", "Adobe"),
                                     ("SAME from Adobe ·7692", "Adobe"),
                                     ("SAME ·Adobe", "Adobe"), ("SAME", "Adobe ·x")],
                               shas=["7692" + "1" * 60, "1234" + "2" * 60, "3" * 64,
                                     "4" * 64, "5" * 64])
        r = self.deliver(view="check")
        self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
        it = views.build_review(self.conn, view="item", pid=pid)
        flat = " ".join(it["text"].split())
        # a literal never prints the reserved mark: generated text is unforgeable
        self.assertIn("invoice SAME from Adobe •7692", flat)
        self.assertIn("invoice SAME •Adobe", flat)
        self.assertNotIn("SAME from Adobe ·7692", flat)
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
        out = apply_now(self.conn, [("reject", pid)])
        self.assertIn("Ruled out 5 invoices", out["receipt"])

    def alias_after(self, between):
        # round 8 (Astra + Terra S1): an alert or an unrelated item view delivered after the
        # sheet dropped the alias, and the reply unpaired the EUR 70.00 payment silently
        import passes
        a = self.item("A·B", 5445, "2026-09-14", labels=("guessed",))
        b = self.item("A•B", 7000, "2026-09-16", labels=("guessed",))
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver()
        if between == "alert":
            t = self.pass_()
            passes.record_probe(self.conn, t, "gmail", False, "invalid_grant")
            speak = self.end_and_speak()
            views.mark_rendering_delivered(self.conn, speak["render_id"])
            self.token = self.pass_()
        else:
            it = views.build_review(self.conn, view="item", pid=z)
            views.mark_rendering_delivered(self.conn, it["render_id"])
        # binding R3: unquoted words bind what came last, which shows neither payment
        # (#121: ops — refused to the desk)
        self.not_on_post([("reject", b)])
        self.assertEqual((self.author(a)[0], self.author(b)[0]), ("auto", "auto"))
        # quoting the sheet binds it: the payment the desk names there is the one changed
        sheet = self.conn.execute("SELECT text FROM renders WHERE kind='status' ORDER BY"
                                  " rowid DESC LIMIT 1").fetchone()[0]
        out = apply_now(self.conn, [("reject", b)], quoted=views.displayed(sheet))
        self.assertEqual(len(out["applied"]), 1)
        self.assertEqual((self.author(a)[0], self.author(b)), ("auto", None))

    def test_a_display_alias_survives_a_delivered_alert(self):
        self.alias_after("alert")

    def test_a_display_alias_survives_an_unrelated_item_view(self):
        self.alias_after("item")

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
        self.assertIn("·0844d4d4", flat)
        self.assertIn("·08449d0c", flat)
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
            self.assertLessEqual(views.utf16_len(r["text"]), views.BODY_LIMIT)
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
        self.assertIn("57 more could fit — ask me to show them.", r["text"])
        out = apply_now(self.conn, [("reject", pid)])            # not all shown
        self.assertEqual((out["applied"], out["reshow"]), ([], [pid]))
        # #121: "show them" is the desk's show_view(view="item", pid), paged below
        seen, kw = set(), {"view": "item", "pid": pid}
        page = None
        for _ in range(20):
            it = views.build_review(self.conn, **kw)
            self.assertLessEqual(views.utf16_len(it["text"]), views.BODY_LIMIT)
            seen |= self.bound(it["render_id"], pid)
            views.mark_rendering_delivered(self.conn, it["render_id"])
            if it["next"] is None:
                break
            self.assertTrue(it["text"].endswith(views.MORE_LINE))
            # binding V1: More's call names this page as `prev`, so the next page adds its
            # candidates — the last page binds all 60
            kw, page = dict(it["next"]), it["next"]["page"]
        self.assertGreater(page or 1, 1)
        self.assertEqual(len(seen), 60)
        out = apply_now(self.conn, [("reject", pid)])
        self.assertIn("Ruled out 60 invoices", out["receipt"])


class TestIntegration(Base):
    def test_a_counted_but_unprinted_payment_is_refused_not_changed(self):
        # a never-searched payment is counted ("not searched"), not printed: the
        # operator never saw its line, so a correction naming it applies nothing (binding
        # R3: it is not on the bound rendering; #121: refused to the desk)
        self.n += 1
        self.row(self.n, counterparty="BCK*XYZ", amount_minor=18000,
                 booking_date="2026-09-16", value_date="2026-09-16")
        pid = self.lineage_for(self.n)
        self.classify(pid, {"software"})
        self.settle(pid)
        self.deliver()
        self.not_on_post([("identity", pid, "my accountant")])
        self.assertEqual(self.readings(), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
