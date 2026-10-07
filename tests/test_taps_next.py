"""Simple loop §1 / #1302: every answer commits its keyed decision and returns the next
card; single-use keyboards (nothing relies on an earlier message); Confirm all skips what
was answered, refuses only what changed otherwise, commits the rest; [Never for X] binds the
union of the vendor's pages and refuses a changed set with a fresh first page."""
import json
from tests._base import LoopCase
import db                     # server/ is on sys.path once tests._base is imported


class _Tapping(LoopCase):
    """The tap helpers (no tests of its own)."""

    def scope_of(self, deposit) -> dict:
        rid = deposit["buttons"][0]["call"]["arguments"]["render_id"]
        return json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                            " render_id=?", (rid,)).fetchone()[0])

    def tap(self, deposit, label) -> dict:
        import qa_server, tools  # noqa: F401
        b = next(b for b in deposit["buttons"] if b["label"] == label)
        return qa_server.TOOLS[b["call"]["tool"]]["fn"](dict(b["call"]["arguments"]))

    def end(self):
        import cards
        with db.tx(self.conn):
            return cards.deposit_of(self.conn, cards.compose_end(self.conn, self.job_id,
                                                                  scheduled=False))


class Taps(_Tapping):
    def test_review_walks_card_by_card_and_ends_on_all_answered(self):
        p = self.pay()
        self.propose(p)
        self.pay("Twilio")                                       # one vendor card
        out = self.tap(self.end(), "Review 2")
        self.assertIn("Card 1 of 2", out["next"]["text"])
        out = self.tap(out["next"], "Confirm")
        self.assertIn("Confirmed", out["receipt"])
        self.assertEqual(self.conn.execute("SELECT author FROM match_state WHERE pid=? AND"
                                           " state='matched'", (p,)).fetchone()[0], "operator")
        self.assertIn("Card 2 of 2", out["next"]["text"])
        out = self.tap(out["next"], "Leave missing")
        self.assertIn("all answered", out["next"]["text"])
        self.assertEqual([b["label"] for b in out["next"]["buttons"]], ["Get package"])
        self.assertTrue(out["next"]["revision"].startswith("walk:"))

    def test_leave_for_now_writes_nothing(self):
        p = self.pay()
        self.propose(p)
        rev = self.rev(p)
        out = self.tap(self.tap(self.end(), "Review 1")["next"], "Leave for now")
        self.assertEqual(self.rev(p), rev)
        self.assertIn("still open", out["next"]["text"])          # the open-items card

    def test_a_named_candidate_pairs_it_and_rejects_the_machines_choice(self):
        p = self.pay()
        alt = self.doc(document_number="INV-91", document_date="2026-08-04")
        chosen = self.propose(p, alternatives=[alt], document_number="INV-88",
                              document_date="2026-08-02")
        card = self.tap(self.end(), "Review 1")["next"]
        labels = [b["label"] for b in card["buttons"]]
        self.assertEqual(labels[:2], ["INV-88 · 2 Aug", "INV-91 · 4 Aug"])
        self.assertEqual(labels[2:], ["Wrong", "Leave for now"])
        self.tap(card, "INV-91 · 4 Aug")
        rows = dict(self.conn.execute("SELECT doc_id, state FROM match_state WHERE pid=?",
                                      (p,)).fetchall())
        self.assertEqual((rows[alt], rows[chosen]), ("matched", "rejected"))

    def test_a_candidate_whose_amount_changed_after_display_is_not_committed(self):
        import documents
        p = self.pay()
        alt = self.doc(document_number="INV-91", amount_minor=10000)
        chosen = self.propose(p, alternatives=[alt], document_number="INV-88")
        card = self.tap(self.end(), "Review 1")["next"]
        documents.update_document_metadata(self.conn, alt, amount_minor=90000)
        out = self.tap(card, next(b["label"] for b in card["buttons"]
                                  if b["label"].startswith("INV-91")))
        self.assertIn("changed", out["receipt"])
        rows = dict(self.conn.execute("SELECT doc_id, state FROM match_state WHERE pid=?",
                                      (p,)).fetchall())
        self.assertEqual(rows.get(chosen), "proposed")
        self.assertNotEqual(rows.get(alt), "matched")

    def test_wrong_on_a_joint_proposal_rejects_its_alternatives_too(self):
        import matches
        p = self.pay()
        alt = self.doc(document_number="INV-91")
        self.propose(p, alternatives=[alt], document_number="INV-88")
        card = self.tap(self.end(), "Review 1")["next"]
        self.tap(card, "Wrong")
        with self.assertRaisesRegex(db.Refusal, "operator rejected"):
            matches.propose_match(self.conn, pid=p, doc_id=alt, expected_revision=self.rev(p),
                                  token=self.token, document_date=self.stored_date(alt))

    def test_exempting_page_one_leaves_page_two_answerable(self):
        for i in range(30):
            self.pay("Adobe", 100 + i, "2026-08-%02d" % (i % 28 + 1))
        page1 = self.tap(self.end(), "Review 1")["next"]
        page2 = self.tap(page1, "Next page")["next"]       # page 2 posted before page 1's answer
        # plugin keys are per button: page 1's other button is still its own (Casa clears
        # the keyboard; the test calls the stored call directly)
        self.assertIn("No invoice needed for", self.tap(page1, "No invoice needed for these")
                      ["receipt"])
        out = self.tap(page2, "No invoice needed for these")
        self.assertIn("No invoice needed for", out["receipt"])
        open_ = self.conn.execute("SELECT count(*) FROM projections WHERE status='open'"
                                  ).fetchone()[0]
        self.assertEqual(open_, 0)

    def test_wrong_on_a_legacy_set_rejects_only_the_candidates_the_card_showed(self):
        """Plan round 8 (Astra S1): a legacy set of five machine candidates; the card shows
        four; Wrong rejects those four, and the fifth stays proposable."""
        import lineage, matches
        import reducer as R
        p = self.pay()
        docs = [self.doc(document_number="L-%d" % k, document_date="2026-09-0%d" % k)
                for k in range(1, 6)]
        with db.tx(self.conn):                            # a pre-floor store's set
            row = lineage.live_row(self.conn, lineage.projection(self.conn, p))
            for d in docs:
                mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                        " VALUES (?,?,0)", (p, d)).lastrowid
                lineage.append(self.conn, p, "propose", "auto", match_id=mid, doc_id=d,
                               fp=R.fingerprint(R.facts_of(row), "invoice"))
            lineage.settle(self.conn, p)
        card = self.tap(self.end(), "Review 1")["next"]
        self.assertEqual(len([b for b in card["buttons"] if b["label"].startswith("L-")]), 4)
        self.assertIn("1 more could fit", card["text"])
        self.tap(card, "Wrong")
        unseen = next(d for d in docs if ("L-%d" % (docs.index(d) + 1)) not in
                      " ".join(b["label"] for b in card["buttons"]))
        out = matches.propose_match(self.conn, pid=p, doc_id=unseen,
                                    expected_revision=self.rev(p), token=self.token,
                                    document_date=self.stored_date(unseen))
        self.assertTrue(out["wrote"])                      # never shown: never rejected

    def test_a_pick_key_is_bound_to_its_document(self):
        import keys, qa_server, tools  # noqa: F401
        p = self.pay()
        alt = self.doc(document_number="B")
        self.propose(p, alternatives=[alt], document_number="A")
        card = self.tap(self.end(), "Review 1")["next"]
        b = dict(card["buttons"][0]["call"]["arguments"], doc_id=alt)
        out = qa_server.TOOLS["verdict"]["fn"](b)
        self.assertEqual(out["receipt"], keys.NO_LONGER)

    def test_never_on_the_last_page_refuses_a_changed_union_and_returns_a_fresh_first_page(self):
        for i in range(30):
            self.pay("Adobe", 100 + i, "2026-08-%02d" % (i % 28 + 1))
        page = self.tap(self.end(), "Review 1")["next"]
        while "Next page" in [b["label"] for b in page["buttons"]]:
            page = self.tap(page, "Next page")["next"]
        new = self.pay("Adobe", 999)                             # arrives before the tap
        out = self.tap(page, "Never for Adobe")
        self.assertIn("nothing applied", out["receipt"])
        self.assertIsNone(self.conn.execute("SELECT exp_kind FROM counterparties WHERE"
                                            " name='Adobe'").fetchone())
        self.assertIn("page 1 of", out["next"]["text"])
        r = self.conn.execute("SELECT scope_json FROM renders ORDER BY rowid DESC LIMIT 1"
                              ).fetchone()
        self.assertIn(new, [p for pg in json.loads(r[0])["pages"] for p in pg])

    def test_a_left_missing_payment_is_listed_and_never_still_applies(self):
        import work
        a, b = self.pay("Adobe", 100), self.pay("Adobe", 200)
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, [b], grant=grant))
        page = self.tap(self.end(), "Review 1")["next"]
        self.assertIn("left missing", page["text"])            # b is shown, marked
        out = self.tap(page, "Never for Adobe")
        self.assertIn("never needs an invoice", out["receipt"])
        for p in (a, b):
            self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                               (p,)).fetchone()[0], "no-document")

    def test_never_with_the_union_unchanged_sets_the_rule(self):
        pids = [self.pay("Adobe", 100 + i) for i in range(3)]
        page = self.tap(self.end(), "Review 1")["next"]
        out = self.tap(page, "Never for Adobe")
        self.assertIn("never needs an invoice", out["receipt"])
        for p in pids:
            self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                               (p,)).fetchone()[0], "no-document")

    def test_confirm_all_skips_answered_refuses_changed_and_commits_the_rest(self):
        import matches
        a, b, c = (self.pay(w, 1000 + i) for i, w in enumerate(("A", "B", "C")))
        for p, amt in ((a, 1000), (b, 1001), (c, 1002)):
            self.propose(p, amount_minor=amt, issuer="X%d" % p)
        end = self.end()
        card = self.tap(end, "Review 3")["next"]
        self.tap(card, "Wrong")                                  # a: answered through Review
        matches.propose_match(self.conn, pid=b, doc_id=self.doc(amount_minor=1001),
                              expected_revision=self.rev(b), token=self.token,
                              document_date="2026-09-03")         # b: changed by a later run
        out = self.tap(end, "Confirm all 3")
        self.assertIn("changed since", out["receipt"])
        states = {p: self.conn.execute("SELECT author FROM match_state WHERE pid=? AND state"
                                       "='matched'", (p,)).fetchone() for p in (a, b, c)}
        self.assertIsNone(states[a])
        self.assertIsNone(states[b])
        self.assertEqual(states[c][0], "operator")
        self.assertIn("still open", out["next"]["text"])


class TapsMore(_Tapping):
    """Task 8 additions: #1302's shapes, the displayed-only rules, Confirm all's split and
    Never's union binding, each pinned so a mutant of it fails."""

    def test_a_refused_key_answers_a_receipt_and_no_card(self):
        import keys, qa_server, tools  # noqa: F401
        p = self.pay()
        self.propose(p)
        card = self.tap(self.end(), "Review 1")["next"]
        args = dict(next(b for b in card["buttons"] if b["label"] == "Confirm")["call"]
                    ["arguments"])
        self.assertIn("next", qa_server.TOOLS["verdict"]["fn"](dict(args)))
        out = qa_server.TOOLS["verdict"]["fn"](dict(args))          # spent: single use
        self.assertEqual(out, {"receipt": keys.NO_LONGER})

    def test_every_answer_has_a_non_blank_receipt_and_a_card(self):
        p = self.pay()
        self.propose(p)
        self.pay("Twilio")
        out = self.tap(self.end(), "Review 2")
        seen = [out]
        out = self.tap(out["next"], "Leave for now")
        seen.append(out)
        out = self.tap(out["next"], "Leave missing")
        seen.append(out)
        self.assertIn("still open: 1 to confirm · 0 missing", out["next"]["text"])
        seen.append(self.tap(out["next"], "Review 1"))
        self.assertIn("Card 1 of 1", seen[-1]["next"]["text"])
        for o in seen:
            self.assertEqual(set(o), {"receipt", "next"})
            self.assertTrue(o["receipt"].strip())
            self.assertTrue(o["next"]["buttons"])

    def test_a_pick_of_a_document_paired_elsewhere_meanwhile_commits_nothing(self):
        p = self.pay()
        alt = self.doc(document_number="INV-91")
        chosen = self.propose(p, alternatives=[alt], document_number="INV-88")
        card = self.tap(self.end(), "Review 1")["next"]
        other = self.pay("Adobe", 10000, "2026-09-03")
        self.machine_entry(other, alt)              # a merge: the floor refuses this write
        out = self.tap(card, next(b["label"] for b in card["buttons"]
                                  if b["label"].startswith("INV-91")))
        self.assertIn("Nothing was applied", out["receipt"])
        self.assertIn(f"payment #{other}", out["receipt"])
        rows = dict(self.conn.execute("SELECT doc_id, state FROM match_state WHERE pid=?",
                                      (p,)).fetchall())
        self.assertEqual(rows, {chosen: "proposed"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE author='operator'"
                                           ).fetchone()[0], 0)
        self.assertIn("Card 1 of 1", out["next"]["text"])      # the same item, as it is now
        self.assertNotIn("INV-91", " ".join(b["label"] for b in out["next"]["buttons"]))
        self.assertNotIn("INV-91", out["next"]["text"])         # held elsewhere: not offered

    def test_a_pick_rejects_only_the_displayed_candidates(self):
        """Plan round 8: a legacy set of five, four displayed; picking one rejects the other
        three displayed members and never the fifth."""
        import lineage
        import reducer as R
        p = self.pay()
        docs = [self.doc(document_number="L-%d" % k, document_date="2026-09-0%d" % k)
                for k in range(1, 6)]
        with db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, p))
            for d in docs:
                mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                        " VALUES (?,?,0)", (p, d)).lastrowid
                lineage.append(self.conn, p, "propose", "auto", match_id=mid, doc_id=d,
                               fp=R.fingerprint(R.facts_of(row), "invoice"))
            lineage.settle(self.conn, p)
        card = self.tap(self.end(), "Review 1")["next"]
        shown = {d for d, _ in self.scope_of(card)["picks"]}
        self.assertEqual(len(shown), 4)
        pick = sorted(shown)[0]
        label = dict((d, lbl) for d, lbl in self.scope_of(card)["picks"])[pick]
        self.tap(card, label)
        rejected = {r[0] for r in self.conn.execute(
            "SELECT m.doc_id FROM log l JOIN matches m ON m.match_id=l.match_id WHERE"
            " l.pid=? AND l.author='operator' AND l.kind='unpair'", (p,))}
        self.assertEqual(rejected, shown - {pick})
        self.assertEqual(self.conn.execute("SELECT state FROM match_state WHERE pid=? AND"
                                           " doc_id=?", (p, pick)).fetchone()[0], "matched")

    def test_pick_in_tx_refuses_a_candidate_the_card_did_not_display(self):
        import matches
        p = self.pay()
        alt = self.doc(document_number="INV-91")
        self.propose(p, alternatives=[alt], document_number="INV-88")
        card = self.tap(self.end(), "Review 1")["next"]
        rid = card["buttons"][0]["call"]["arguments"]["render_id"]
        mrevs = json.loads(self.conn.execute("SELECT match_revisions_json FROM render_items"
                                             " WHERE render_id=? AND pid=?",
                                             (rid, p)).fetchone()[0])
        with self.assertRaisesRegex(db.Refusal, "not on the card"):
            self.granted(matches.pick_in_tx, pid=p, doc_id=alt, render_id=rid, mrevs=mrevs,
                         alternatives_shown=[])

    def test_confirm_all_skips_a_proposal_confirmed_through_review(self):
        a, b = self.pay("A", 1000), self.pay("B", 1001)
        self.propose(a, amount_minor=1000)
        self.propose(b, amount_minor=1001)
        end = self.end()
        self.tap(self.tap(end, "Review 2")["next"], "Confirm")      # a, through Review
        out = self.tap(end, "Confirm all 2")
        self.assertEqual(out["receipt"],
                         "Confirmed 1 of 2.\n1 proposal already answered — left as answered.")
        for p in (a, b):
            self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE pid=? AND"
                                               " author='operator' AND kind='pair'",
                                               (p,)).fetchone()[0], 1)
        self.assertIn("all answered", out["next"]["text"])

    def _two_pages(self):
        pids = [self.pay("Adobe", 100 + i, "2026-08-%02d" % (i % 28 + 1)) for i in range(30)]
        page1 = self.tap(self.end(), "Review 1")["next"]
        page2 = self.tap(page1, "Next page")["next"]
        self.assertIn("Never for Adobe", [b["label"] for b in page2["buttons"]])
        return pids, page1, page2

    def test_never_applies_when_an_earlier_page_changed_though_the_set_did_not(self):
        """Review round 1 ruling: Never binds by membership — a page-1 payment whose amount
        the bank corrected is still one of the payments the walk displayed."""
        pids, page1, page2 = self._two_pages()
        first = self.scope_of(page1)["pages"][0][0]
        row = self.conn.execute("SELECT dest_row_id FROM projections WHERE pid=?",
                                (first,)).fetchone()[0]
        self.row(row, counterparty="Adobe", amount_minor=7777, booking_date="2026-08-01",
                 value_date="2026-08-01")                     # the bank corrected its amount
        self.settle(first)
        out = self.tap(page2, "Never for Adobe")
        self.assertEqual(out["receipt"], "Adobe never needs an invoice: 30 payments changed.")
        self.assertEqual(self.conn.execute("SELECT exp_kind FROM counterparties WHERE"
                                           " name='Adobe'").fetchone()[0], "none")

    def test_page_one_leave_missing_then_never_applies(self):
        """§1 "one tap per vendor": the operator's own [Leave missing] on page 1 never
        blocks [Never for X] on the last page (review round 1, reproduced)."""
        pids, page1, page2 = self._two_pages()
        self.assertIn("Left missing", self.tap(page1, "Leave missing")["receipt"])
        out = self.tap(page2, "Never for Adobe")
        self.assertEqual(out["receipt"], "Adobe never needs an invoice: 30 payments changed.")
        self.assertEqual({r[0] for r in self.conn.execute(
            "SELECT status FROM projections WHERE pid IN (%s)" % ",".join(map(str, pids)))},
            {"no-document"})

    def test_page_one_exempt_then_never_applies(self):
        pids, page1, page2 = self._two_pages()
        n1 = len(self.scope_of(page1)["pages"][0])
        self.tap(page1, "No invoice needed for these")
        out = self.tap(page2, "Never for Adobe")
        self.assertEqual(out["receipt"], "Adobe never needs an invoice: "
                                         f"{30 - n1} payments changed.")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM projections WHERE status='open'").fetchone()[0], 0)

    def test_a_later_page_whose_payments_were_matched_still_offers_never(self):
        """Task 7 carry, ported for d1 (Never's set is the rehearsed rule): page 2's
        payments matched meanwhile are still payments Never changes (their expectation),
        so page 2 lists them marked, offers no exemption, and Never there binds all 30."""
        import matches
        pids = [self.pay("Adobe", 100 + i, "2026-08-%02d" % (i % 28 + 1)) for i in range(30)]
        page1 = self.tap(self.end(), "Review 1")["next"]
        later = self.scope_of(page1)["pages"][1]
        for p in later:
            matches.record_match(self.conn, pid=p, author="auto",
                                 doc_id=self.doc(amount_minor=self.conn.execute(
                                     "SELECT amount_minor FROM bank_rows b JOIN projections"
                                     " p ON p.dest_row_id=b.row_id WHERE p.pid=?",
                                     (p,)).fetchone()[0]),
                                 expected_revision=self.rev(p), token=self.token)
        out = self.tap(page1, "Next page")
        self.assertEqual(out["receipt"], "Page 2 of 2.")
        lines = out["next"]["text"].splitlines()
        self.assertEqual(sum(ln.endswith("· matched") for ln in lines), len(later))
        self.assertEqual([b["label"] for b in out["next"]["buttons"]], ["Never for Adobe"])
        self.assertEqual(self.scope_of(out["next"])["missing"], [])
        done = self.tap(out["next"], "Never for Adobe")
        self.assertEqual(done["receipt"], "Adobe never needs an invoice: 30 payments changed.")
        self.assertEqual(len(pids), 30)

    def test_a_vendor_with_no_missing_payment_left_has_no_card(self):
        """Task 7 carry: cards.card is None when nothing of the vendor is missing any more;
        the walk goes on (next_after), never a None card."""
        import matches
        a = self.pay("Adobe", 100)
        end = self.end()
        matches.record_match(self.conn, pid=a, author="auto",
                             doc_id=self.doc(amount_minor=100),
                             expected_revision=self.rev(a), token=self.token)
        out = self.tap(end, "Review 1")
        self.assertIn("all answered", out["next"]["text"])

    def test_leave_missing_on_a_changed_page_commits_nothing(self):
        a = self.pay("Adobe", 100)
        page = self.tap(self.end(), "Review 1")["next"]
        self.row(1, counterparty="Adobe", amount_minor=101, booking_date="2026-09-02",
                 value_date="2026-09-02")
        self.settle(a)
        out = self.tap(page, "Leave missing")
        self.assertIn("nothing applied", out["receipt"])
        self.assertNotEqual(self.conn.execute("SELECT search_state FROM projections WHERE"
                                              " pid=?", (a,)).fetchone()[0], "accepted-missing")
        self.assertIn("Card 1 of 1", out["next"]["text"])

    def test_confirm_all_commits_the_rest_when_one_confirm_is_refused(self):
        """Each proposal in its own savepoint: a confirm refused AFTER it wrote (the floor
        at the write) commits nothing for it and leaves the other's confirmation standing.
        (Rev 18.4 §R18.4: an alternative no longer holds, so the refusal is injected.)"""
        import taps
        a, b = self.pay("A", 1000), self.pay("B", 1001)
        self.propose(a, amount_minor=1000)
        self.propose(b, amount_minor=1001)
        real = taps._apply_one

        def refusing(conn, grant, rid, action, d):
            out = real(conn, grant, rid, action, d)
            if d["pid"] == a:
                raise db.Refusal("that document has since been paired with payment #99")
            return out
        self.patch(taps, "_apply_one", refusing)
        out = self.tap(self.end(), "Confirm all 2")
        self.assertTrue(out["receipt"].startswith("Confirmed 1 of 2.\n"), out["receipt"])
        self.assertIn("payment #99", out["receipt"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM log WHERE pid=? AND author="
                                            "'operator'", (a,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT author FROM match_state WHERE pid=?"
                                           " AND state='matched'", (b,)).fetchone()[0],
                         "operator")

    def test_never_on_the_last_page_with_the_set_unchanged_applies_to_every_page(self):
        pids = [self.pay("Adobe", 100 + i, "2026-08-%02d" % (i % 28 + 1)) for i in range(30)]
        page = self.tap(self.end(), "Review 1")["next"]
        while "Next page" in [b["label"] for b in page["buttons"]]:
            page = self.tap(page, "Next page")["next"]
        self.assertGreater(self.scope_of(page)["page"], 1)
        out = self.tap(page, "Never for Adobe")
        self.assertEqual(out["receipt"], "Adobe never needs an invoice: 30 payments changed.")
        self.assertEqual({r[0] for r in self.conn.execute(
            "SELECT status FROM projections WHERE pid IN (%s)" % ",".join(map(str, pids)))},
            {"no-document"})

    def test_a_pick_of_an_alternative_rejects_the_other_displayed_ones(self):
        p = self.pay()
        b = self.doc(document_number="INV-B", document_date="2026-08-04")
        c = self.doc(document_number="INV-C", document_date="2026-08-05")
        a = self.propose(p, alternatives=[b, c], document_number="INV-A",
                         document_date="2026-08-02")
        card = self.tap(self.end(), "Review 1")["next"]
        self.tap(card, "INV-B · 4 Aug")
        rejected = {r[0] for r in self.conn.execute(
            "SELECT m.doc_id FROM log l JOIN matches m ON m.match_id=l.match_id WHERE"
            " l.pid=? AND l.author='operator' AND l.kind='unpair'", (p,))}
        self.assertEqual(rejected, {a, c})
        self.assertEqual(self.conn.execute("SELECT doc_id FROM match_state WHERE pid=? AND"
                                           " state='matched'", (p,)).fetchone()[0], b)

    def test_pick_in_tx_refuses_a_pairing_changed_since_the_card(self):
        import authorship, matches
        p = self.pay()
        alt = self.doc(document_number="INV-91")
        self.propose(p, alternatives=[alt], document_number="INV-88")
        card = self.tap(self.end(), "Review 1")["next"]
        rid = card["buttons"][0]["call"]["arguments"]["render_id"]
        mrevs = {k: v - 1 for k, v in json.loads(self.conn.execute(
            "SELECT match_revisions_json FROM render_items WHERE render_id=? AND pid=?",
            (rid, p)).fetchone()[0]).items()}
        with self.assertRaises(authorship.Stale):
            self.granted(matches.pick_in_tx, pid=p, doc_id=alt, render_id=rid, mrevs=mrevs,
                         alternatives_shown=[alt])
