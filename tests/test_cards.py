"""Simple loop §1 (rev 17): the end message (counts, numbered proposals, [Review N]
[Confirm all K] [Get package] last; Confirm all only when every proposal is listed and
fewer than 25), the Review order (proposals, then one item per vendor), paged vendor cards
([Never for X] only on the last page, never on a scheduled walk), the open-items card, the
ready notice, and the scheduled run's new-state rule."""
import json
from tests._base import LoopCase, StoreCase
import db                     # server/ is on sys.path once tests._base is imported



class Cards(LoopCase):
    def c(self, fn, *a, **k):
        """Every cards composer runs inside the caller's transaction."""
        with db.tx(self.conn):
            return fn(self.conn, *a, **k)

    def rendering(self, rid):
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        return r, json.loads(r["scope_json"])

    def labels(self, rid):
        import cards
        r, _ = self.rendering(rid)
        return [b[0] for b in cards.buttons(self.conn, r)]

    def test_the_end_message_lists_proposals_and_ends_with_get_package(self):
        import cards
        p1, p2 = self.pay("OpenRouter", 1899), self.pay("AWS", 4120)
        self.propose(p1, currency="USD", amount_minor=2200, document_number="ABC-123")
        self.propose(p2, amount_minor=4120, document_number="INV-88")
        self.pay("Twilio")                                          # missing
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        r, scope = self.rendering(rid)
        text = r["text"]
        self.assertIn("Q3 checked · 3 payments", text)
        self.assertIn("2 to confirm · 1 missing", text)
        self.assertIn("1. ", text)
        self.assertIn("(other currency)", text)
        self.assertEqual(self.labels(rid), ["Review 3", "Confirm all 2", "Get package"])
        self.assertEqual([("p" in o) for o in scope["order"]], [True, True, False])

    def test_confirm_all_is_left_out_at_25_proposals(self):
        import cards
        for i in range(25):
            p = self.pay("V%02d" % i, 1000 + i)
            self.propose(p, amount_minor=1000 + i, issuer="V%02d" % i)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertNotIn("Confirm all", " ".join(self.labels(rid)))
        self.assertEqual(self.labels(rid)[-1], "Get package")

    def test_nothing_to_ask(self):
        import cards
        p = self.pay()
        self.machine_match(p, self.doc(), self.token)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertIn("all accounted for", self.rendering(rid)[0]["text"])
        self.assertEqual(self.labels(rid), ["Get package"])

    def test_a_vendor_with_more_payments_than_a_card_is_paged(self):
        import cards, views
        for i in range(60):
            self.pay("Adobe", 100 + i, "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1))
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        first = self.c(cards.card, end, 0)
        r, scope = self.rendering(first)
        self.assertGreater(len(scope["pages"]), 1)
        self.assertTrue(views.fits_proposal(r["text"]))
        self.assertIn("Next page", self.labels(first))
        self.assertNotIn("Never for Adobe", self.labels(first))
        last = self.c(cards.card, end, 0, page=len(scope["pages"]))
        self.assertIn("Never for Adobe", self.labels(last))
        self.assertEqual(sorted(p for pg in scope["pages"] for p in pg),
                         sorted(cards.never_set(self.conn, "Adobe")))

    def test_a_scheduled_run_lists_only_new_state_items(self):
        import cards, views
        old = self.pay("Adobe")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        views.mark_rendering_delivered(self.conn, end)
        self.assertIsNone(self.c(cards.compose_end, self.job_id, scheduled=True))   # nothing new
        new = self.pay("Adobe", 777)
        rid = self.c(cards.compose_end, self.job_id, scheduled=True)
        r, scope = self.rendering(rid)
        self.assertIn("1 earlier item still open", r["text"])
        self.assertEqual(scope["order"], [{"v": "Adobe", "pids": [new]}])
        self.assertTrue(r["text"].startswith("Q3 · new: 0 to confirm · 1 missing"))
        page = self.c(cards.card, rid, 0)
        self.assertNotIn("Never for Adobe", self.labels(page))                 # rev 17
        p, pscope = self.rendering(page)
        self.assertEqual([int(x) for x in pscope["bound_lines"]], [new])     # not the old one
        self.assertEqual(json.loads(p["membership_json"]), [new])

    def test_an_end_message_posted_but_never_delivered_is_not_seen(self):
        import cards
        self.pay("Adobe")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        with db.tx(self.conn):
            cards.deposit_of(self.conn, end)              # posted_seq stamped, no receipt
        self.assertIsNotNone(self.c(cards.compose_end, self.job_id, scheduled=True))

    def test_item_states_are_recorded_with_the_rendering(self):
        import cards
        p = self.pay()
        d = self.propose(p)
        m = self.pay("Twilio")                                   # counted, not displayed
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        st = dict(self.conn.execute("SELECT pid, item_state FROM render_states WHERE"
                                    " render_id=?", (rid,)).fetchall())
        self.assertEqual(st, {p: f"proposed:{d}", m: "missing"})
        bound = [r[0] for r in self.conn.execute("SELECT pid FROM render_items WHERE"
                                                 " render_id=?", (rid,))]
        self.assertEqual(bound, [p])                             # only what it displays

    def assert_binds_exactly_what_it_shows(self, rid):
        """Plan round 7: every bound payment's line is in the deposited text, and the
        rendering binds nothing else."""
        r, scope = self.rendering(rid)
        bound = {r2[0] for r2 in self.conn.execute(
            "SELECT pid FROM render_items WHERE render_id=?", (rid,))}
        self.assertEqual(bound, {int(k) for k in scope["bound_lines"]})
        lines = r["text"].split("\n")
        for pid, line in scope["bound_lines"].items():
            self.assertIn(line, lines, pid)
        import views
        self.assertTrue(views.fits_proposal(r["text"]))

    def test_every_card_kind_binds_only_displayed_payments_when_oversized(self):
        import cards, work
        vendor = "Ab*c_d [e] (f) !g #h ~i -j `k` |l| <m> " + "x" * 21     # 60 chars
        pids = [self.pay(vendor, 100 + i, "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1))
                for i in range(30)]
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, pids[1:25], grant=grant))
        for i in range(40):                                      # 40 long proposals
            q = self.pay(vendor[:59] + str(i % 10), 5000 + i)
            self.propose(q, amount_minor=5000 + i, document_number="N" * 40 + str(i))
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assert_binds_exactly_what_it_shows(end)                       # end message
        self.assert_binds_exactly_what_it_shows(
            self.c(cards.compose_open, "2026-Q3"))                           # open-items
        order = self.rendering(end)[1]["order"]
        k = next(i for i, o in enumerate(order) if "v" in o and o["v"] == vendor)
        first = self.c(cards.card, end, k)
        pages = self.rendering(first)[1]["pages"]
        for pg in range(1, len(pages) + 1):                                 # vendor pages
            self.assert_binds_exactly_what_it_shows(self.c(cards.card, end, k, page=pg))
        self.assert_binds_exactly_what_it_shows(self.c(cards.card, end, 0))  # a Review card

    def test_the_ready_notice_and_the_all_answered_card(self):
        import cards
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO quarter_notices(quarter, sig, times) VALUES"
                              " ('2026-Q3', 'an-earlier-completion', 1)")  # completed before
        rid = self.c(cards.compose_ready, ["2026-Q3"])
        self.assertIn("Q3 complete · updated · package ready", self.rendering(rid)[0]["text"])
        self.assertEqual(self.labels(rid), ["Get package"])
        o = self.c(cards.compose_open, "2026-Q3")
        self.assertIn("all answered", self.rendering(o)[0]["text"])

    # ---- beyond the brief -------------------------------------------------------------

    def test_an_oversized_end_message_lists_what_fits_and_drops_confirm_all(self):
        """The end message with more proposal lines than a message holds: the trailing ones
        are dropped behind a closing line, Confirm all is left out, Review still walks every
        proposal, and only the displayed lines are bound."""
        import cards
        vendor = "Ab*c_d [e] (f) !g #h ~i -j `k` |l| <m> " + "x" * 19
        pids = []
        for i in range(30):
            q = self.pay(vendor + "%02d" % i, 5000 + i)
            self.propose(q, amount_minor=5000 + i, document_number="N" * 58 + "%02d" % i)
            pids.append(q)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        r, scope = self.rendering(end)
        self.assert_binds_exactly_what_it_shows(end)
        shown = len(scope["bound_lines"])
        self.assertLess(shown, 24)                   # fewer than Confirm all's bound
        self.assertIn(f"… and {30 - shown} more to confirm — Review shows them.", r["text"])
        self.assertEqual(self.labels(end), ["Review 30", "Get package"])
        self.assertEqual([o["p"] for o in scope["order"]], pids)       # every proposal
        self.assertEqual(sorted(scope["proposed"]),
                         sorted(int(p) for p in scope["bound_lines"]))

    def test_an_oversized_open_items_card_binds_only_its_displayed_lines(self):
        import cards
        vendor = "Ab*c_d [e] (f) !g #h ~i -j `k` |l| <m> " + "x" * 19
        for i in range(30):
            q = self.pay(vendor + "%02d" % i, 5000 + i)
            self.propose(q, amount_minor=5000 + i, document_number="N" * 58 + "%02d" % i)
        rid = self.c(cards.compose_open, "2026-Q3")
        r, scope = self.rendering(rid)
        self.assertTrue(r["text"].startswith("Q3 · still open: 30 to confirm · 0 missing"))
        self.assert_binds_exactly_what_it_shows(rid)
        self.assertLess(len(scope["bound_lines"]), 24)
        self.assertEqual(self.labels(rid), ["Review 30", "Get package"])

    def test_an_oversized_vendor_page_is_sized_from_its_real_lines(self):
        """Round 7 (Astra S1): a punctuated 60-character vendor name, 24 "left missing"
        marks and a long portal link — every page fits, every bound line is whole, and
        the pages' union is the vendor's never-set."""
        import cards, kb, work
        vendor = "Ab*c_d [e] (f) !g #h ~i -j `k` |l| <m> " + "x" * 21
        kb.upsert_counterparty(self.conn, vendor, document_link="https://portal.example/"
                               + "a_b-c" * 35)
        pids = [self.pay(vendor, 100 + i, "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1))
                for i in range(48)]
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, pids[1:25], grant=grant))
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        first = self.c(cards.card, end, 0)
        pages = self.rendering(first)[1]["pages"]
        self.assertGreaterEqual(len(pages), 2)
        self.assertEqual(sorted(p for pg in pages for p in pg),
                         cards.never_set(self.conn, vendor))
        rids = [first] + [self.c(cards.card, end, 0, page=pg)
                          for pg in range(2, len(pages) + 1)]
        for pg, rid in enumerate(rids, 1):
            self.assert_binds_exactly_what_it_shows(rid)
            r, scope = self.rendering(rid)
            self.assertEqual(sorted(int(p) for p in scope["bound_lines"]),
                             sorted(pages[pg - 1]))
        self.assertIn("· left missing", self.rendering(first)[0]["text"])
        self.assertIn("portal.example", self.rendering(first)[0]["text"])
        last_scope = self.rendering(rids[-1])[1]
        self.assertEqual(last_scope["prior"], rids[:-1])        # Never binds the union
        import views
        self.assertEqual(self.labels(rids[-1])[1], views.clip("Never for " + vendor, 32))
        self.assertEqual(self.labels(rids[-1]), ["No invoice needed for these",
                                                 views.clip("Never for " + vendor, 32),
                                                 "Leave missing"])
        self.assertEqual(self.labels(first), ["No invoice needed for these", "Leave missing",
                                              "Next page"])

    WIDE = "*_" * 30          # 60 characters, every one escaped: the widest field there is

    def test_a_vendor_page_fills_to_the_limit_before_its_line_cap(self):
        """Pages sized from their real lines: with the widest vendor name and every payment
        left missing, a page holds fewer than PAGE_LINES payments — and still fits whole."""
        import cards, work
        pids = [self.pay(self.WIDE, 100000000 + i, "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1))
                for i in range(30)]
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, pids, grant=grant))
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertEqual(self.rendering(end)[1]["order"], [])    # all answered: no item
        with db.tx(self.conn):
            rid = cards._store(self.conn, "end", ["x"], {"quarter": "2026-Q3", "order": [
                {"v": self.WIDE, "pids": pids}]}, {}, {})
        first = self.c(cards.card, rid, 0)
        pages = self.rendering(first)[1]["pages"]
        self.assertLess(len(pages[0]), cards.PAGE_LINES)
        self.assertEqual(sorted(p for pg in pages for p in pg), sorted(pids))
        for pg in range(1, len(pages) + 1):
            self.assert_binds_exactly_what_it_shows(self.c(cards.card, rid, 0, page=pg))

    def test_a_later_page_that_grew_since_page_one_binds_only_what_it_shows(self):
        """Pages are frozen on page 1. If page 2's payments were left missing meanwhile,
        their lines grew: page 2 binds the lines it displays whole and no others (so the
        union differs from the vendor's set, and Never refuses with a fresh first page)."""
        import cards, work
        pids = [self.pay(self.WIDE, 100000000 + i, "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1))
                for i in range(50)]
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        first = self.c(cards.card, end, 0)
        pages = self.rendering(first)[1]["pages"]
        self.assertEqual(len(pages[1]), cards.PAGE_LINES)
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, pages[1], grant=grant))
        second = self.c(cards.card, end, 0, page=2)
        self.assert_binds_exactly_what_it_shows(second)
        bound = [int(p) for p in self.rendering(second)[1]["bound_lines"]]
        self.assertLess(len(bound), len(pages[1]))
        self.assertEqual(sorted(bound), sorted(pages[1][:len(bound)]))      # its first lines
        self.assertEqual(sorted(pids), cards.never_set(self.conn, self.WIDE))

    def test_a_later_page_lists_only_payments_still_missing(self):
        """Review round 1: page 1 froze 50 payments over two pages; a page-2 payment is
        machine-matched meanwhile. Page 2 neither prints nor binds it (an exemption there
        would hit a matched payment); what the walk displayed is then exactly what Never
        changes now."""
        import cards
        pids = [self.pay("Adobe", 100 + i, "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1))
                for i in range(50)]
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        first = self.c(cards.card, end, 0)
        pages = self.rendering(first)[1]["pages"]
        gone = pages[1][3]
        amount = self.conn.execute("SELECT amount_minor FROM bank_rows b JOIN projections p"
                                   " ON p.dest_row_id=b.row_id WHERE p.pid=?",
                                   (gone,)).fetchone()[0]
        self.machine_match(gone, self.doc(amount_minor=amount), self.token)
        self.assertNotIn(gone, cards.never_set(self.conn, "Adobe"))
        second = self.c(cards.card, end, 0, page=2)
        r, scope = self.rendering(second)
        self.assert_binds_exactly_what_it_shows(second)
        bound = sorted(int(p) for p in scope["bound_lines"])
        self.assertEqual(bound, sorted(p for p in pages[1] if p != gone))
        self.assertEqual(json.loads(r["membership_json"]), bound)
        self.assertEqual(scope["pages"], pages)                  # still page 1's frozen pages
        union = set(pages[0]) | set(bound)                       # what the walk displayed
        self.assertEqual(sorted(union), cards.never_set(self.conn, "Adobe"))
        self.assertEqual(sorted(union | {gone}), sorted(pids))

    def test_a_closing_open_items_card_counts_as_seen_once_posted(self):
        """Review round 1 ruling: Wrong during a walk turns a proposal into a missing item;
        the closing open-items card (the tap's `next`) counts it and is posted, never
        delivered; the next scheduled run posts nothing for it."""
        import cards, matches, views
        p = self.pay()
        self.propose(p)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        views.mark_rendering_delivered(self.conn, end)
        card = self.c(cards.card, end, 0)
        self.c(cards.deposit_of, card)
        mid = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (p,)).fetchone()[0]
        with db.tx(self.conn):
            matches.reject_in_tx(self.conn, grant=self.grant(), match_id=mid,
                                 expected_revision=self.rev(match_id=mid), render_id=card)
            closing = cards.next_after(self.conn, end, 0)
            cards.deposit_of(self.conn, closing)
        r, _ = self.rendering(closing)
        self.assertEqual(r["kind"], "open-items")
        self.assertIn("still open: 0 to confirm · 1 missing", r["text"])
        self.assertIsNone(r["delivered_at"])
        self.assertTrue(cards.seen_state(self.conn, p, "missing"))
        self.assertIsNone(self.c(cards.compose_end, self.job_id, scheduled=True))

    def test_an_end_line_binds_only_the_pairing_it_names(self):
        """Review round 1: a joint set's summary line names no pairing, and binds none."""
        import cards
        p, mids = self.legacy_set(2)
        q = self.pay("AWS", 4120)
        self.propose(q, amount_minor=4120)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        mrevs = {r[0]: json.loads(r[1]) for r in self.conn.execute(
            "SELECT pid, match_revisions_json FROM render_items WHERE render_id=?", (end,))}
        cur = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (q,)).fetchone()[0]
        self.assertEqual(mrevs, {p: {}, q: {str(cur): self.rev(match_id=cur)}})

    def test_the_measure_counts_the_worst_case_tag(self):
        import cards, views
        line = "a" * (views.BODY_LIMIT - len(cards.TAG_WORST))
        self.assertTrue(cards._fits([line]))
        self.assertFalse(cards._fits([line + "a"]))
        self.assertTrue(views.fits_proposal(line + "a"))     # fits without the tag

    def test_confirm_all_is_left_out_when_any_proposal_line_is_dropped(self):
        import cards
        for i in range(22):                                   # fewer than 25, too long
            q = self.pay(self.WIDE[:58] + "%02d" % i, 5000 + i)
            self.propose(q, amount_minor=5000 + i, document_number="N" * 58 + "%02d" % i)
        extra = ["Bank not synced since 28 Sep · bank-feed needs attention",
                 "Gmail could not be searched on the last 3 checks — it needs attention.",
                 "3 bank notes could not be written; the next check writes them again.",
                 "Missing · search incomplete: 4 payments — the next check goes on."]
        end = self.c(cards.compose_end, self.job_id, scheduled=False, extra=extra)
        r, scope = self.rendering(end)
        self.assertLess(len(scope["bound_lines"]), 22)
        self.assertEqual(self.labels(end), ["Review 22", "Get package"])
        self.assertEqual(scope["confirm_all"], 0)
        self.assertEqual(r["text"].split("\n")[-4:], extra)       # the failure lines stay
        self.assert_binds_exactly_what_it_shows(end)

    def legacy_set(self, n):
        """A payment holding a legacy joint machine set of `n` candidates (D3)."""
        p = self.pay()
        mids = [self.machine_entry(p, self.doc(document_number="LEG-%d" % i,
                                               document_date="2026-09-%02d" % (i + 1)))
                for i in range(n)]
        return p, mids

    def test_an_oversized_review_card_binds_only_the_candidates_it_displays(self):
        """Plan round 8 (Astra S1): a legacy set of six; the card offers four by name,
        counts the rest, and its render_items holds only the four shown."""
        import cards
        p, mids = self.legacy_set(6)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertIn("6 invoices fit", self.rendering(end)[0]["text"])
        self.assertEqual(self.labels(end), ["Review 1", "Get package"])   # no chosen one
        rid = self.c(cards.card, end, 0)
        r, scope = self.rendering(rid)
        self.assert_binds_exactly_what_it_shows(rid)
        self.assertIn("Card 1 of 1 · to confirm", r["text"])
        self.assertIn("2 more could fit — say \"candidates for", r["text"])
        it = self.conn.execute("SELECT match_revisions_json FROM render_items WHERE"
                               " render_id=?", (rid,)).fetchone()[0]
        shown = sorted(int(m) for m in json.loads(it))
        self.assertEqual(len(shown), 4)
        self.assertTrue(set(shown) <= set(mids))
        self.assertEqual(self.labels(rid), ["LEG-0 · 1 Sep", "LEG-1 · 2 Sep",
                                            "LEG-2 · 3 Sep", "LEG-3 · 4 Sep", "Wrong",
                                            "Leave for now"])
        st = self.conn.execute("SELECT item_state FROM render_states WHERE render_id=?",
                               (rid,)).fetchone()[0]
        self.assertTrue(st.startswith("proposed:joint:"))

    def test_a_proposal_with_alternatives_offers_named_candidates(self):
        import cards, matches
        p = self.pay("AWS", 4120, "2026-08-02")
        alt = self.doc(document_number="INV-91", document_date="2026-08-04",
                       amount_minor=4120)
        chosen = self.doc(document_number="INV-88", document_date="2026-08-02",
                          amount_minor=4120)
        matches.propose_match(self.conn, pid=p, doc_id=chosen, expected_revision=self.rev(p),
                              token=self.token, document_date="2026-08-02",
                              alternatives=[alt])
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertIn("2 invoices fit; chose INV\\-88 (2 Aug)", self.rendering(end)[0]["text"])
        self.assertEqual(self.labels(end), ["Review 1", "Confirm all 1", "Get package"])
        rid = self.c(cards.card, end, 0)
        r, scope = self.rendering(rid)
        self.assertEqual(scope["alternatives"], [alt])
        self.assertEqual(self.labels(rid), ["INV-88 · 2 Aug", "INV-91 · 4 Aug", "Wrong",
                                            "Leave for now"])
        dep = self.c(cards.deposit_of, rid)
        picks = [b["call"]["arguments"] for b in dep["buttons"]
                 if b["call"]["arguments"].get("action") == "pick"]
        self.assertEqual([a["doc_id"] for a in picks], [chosen, alt])
        stored = dict(self.conn.execute("SELECT key, doc_id FROM render_keys WHERE"
                                        " render_id=? AND action='pick'", (rid,)).fetchall())
        self.assertEqual({a["key"]: a["doc_id"] for a in picks}, stored)

    def test_a_single_proposal_card_has_confirm_wrong_leave(self):
        import cards
        p = self.pay()
        self.propose(p, document_number="INV-1")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        rid = self.c(cards.card, end, 0)
        r, scope = self.rendering(rid)
        self.assertEqual(self.labels(rid), ["Confirm", "Wrong", "Leave for now"])
        self.assertEqual(scope["alternatives"], [])
        self.assertEqual(scope["review_of"], end)
        self.assertEqual(scope["pos"], 0)
        self.assertEqual(json.loads(r["membership_json"]), [p])

    def test_the_review_order_and_next_after(self):
        """§1: proposals in line order, then one item per vendor with unanswered missing
        payments; next_after skips what is answered and ends on the open-items card."""
        import cards, work
        a = self.pay("Zeta", 300)
        b = self.pay("adobe", 200)
        self.propose(a, amount_minor=300)
        self.propose(b, amount_minor=200)
        t1, t2 = self.pay("Twilio", 50), self.pay("Twilio", 60, "2026-07-02")
        left = self.pay("Basecamp", 70)
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, [left], grant=grant))
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        order = self.rendering(end)[1]["order"]
        self.assertEqual(order, [{"p": b}, {"p": a}, {"v": "Twilio", "pids": [t2, t1]}])
        self.assertEqual(self.rendering(self.c(cards.next_after, end, -1))[1]["pid"], b)
        self.granted(lambda c, grant: work.leave_missing_in_tx(c, [t1, t2], grant=grant))
        nxt = self.c(cards.next_after, end, 1)             # Twilio answered meanwhile
        r, scope = self.rendering(nxt)
        self.assertEqual(r["kind"], "open-items")
        self.assertIn("Q3 · still open: 2 to confirm · 0 missing", r["text"])

    def test_counts_partition_and_earlier_quarter_line(self):
        import cards
        m = self.pay("Matched", 100)
        self.machine_match(m, self.doc(amount_minor=100), self.token)
        self.pay("Pend", 200)
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET status='PDNG' WHERE row_id=?", (self.n,))
        self.pay("Open", 300, "2026-07-01")
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        old = self.pay("Old", 400, "2026-05-04")                 # Q2, still open
        st = self.c(cards.state)
        self.assertEqual({k: len(v) for k, v in st["by_bucket"].items()},
                         {"matched": 1, "proposed": 0, "missing": 2, "not_needed": 0,
                          "pending": 1})
        self.assertEqual(dict(st["counts"]["2026-Q2"]), {"missing": 1})
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        lines = self.rendering(rid)[0]["text"].split("\n")
        self.assertTrue(lines[0].startswith("Q3 checked · 3 payments"))
        self.assertEqual(lines[1], "1 matched · 0 need no invoice · 0 to confirm · 1 missing"
                                   " · 1 pending")
        self.assertEqual(lines[2], "Q2 · still open: 0 to confirm · 1 missing")
        self.assertEqual(self.rendering(rid)[1]["order"],
                         [{"v": "Old", "pids": [old]}, {"v": "Open", "pids": [old - 1]}])

    def test_a_handover_end_message_shows_only_what_it_changed(self):
        import cards
        p = self.pay("Adobe", 10000)
        filed = self.doc(amount_minor=10000)
        self.machine_match(p, filed, self.token)
        q = self.pay("AWS", 4120)
        prop = self.propose(q, amount_minor=4120, document_number="INV-88")
        lone = self.doc(amount_minor=1)
        self.pay("Twilio")                                       # missing, not shown
        rid = self.c(cards.compose_end, self.job_id, scheduled=False,
                     handover_docs=[filed, prop, lone])
        r, scope = self.rendering(rid)
        lines = r["text"].split("\n")
        self.assertTrue(lines[0].startswith("Filed. Paired with Adobe · 2 Sep · EUR 100.00"))
        self.assertIn("Filed. No payment fits it yet — it's matched when one does.", lines)
        self.assertIn("To confirm:", lines)
        self.assertNotIn("Twilio", r["text"])
        self.assertEqual(scope["order"], [{"p": q}])
        self.assertEqual(self.labels(rid), ["Review 1", "Confirm all 1", "Get package"])

    def test_owed_notices_join_an_end_message_or_become_the_message(self):
        import cards, loop
        self.pay("Adobe")
        rid = self.c(cards.compose_end, self.job_id, scheduled=False, ready=["2026-Q2"],
                     extra=["Bank not synced since 28 Sep · bank-feed needs attention"])
        r, scope = self.rendering(rid)
        self.assertEqual(r["kind"], "end")
        self.assertIn('Q2 complete · package ready — say "send the Q2 package"', r["text"])
        self.assertTrue(r["text"].endswith("bank-feed needs attention"))
        self.assertEqual(scope["ready_quarters"], ["2026-Q2"])
        self.assertEqual(scope["ready_sigs"], {"2026-Q2": loop.completion_sig(self.conn,
                                                                              "2026-Q2")})
        self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
            c, [self.only_pid()], grant=grant))
        rid = self.c(cards.compose_end, self.job_id, scheduled=False, ready=["2026-Q3"])
        r, scope = self.rendering(rid)
        self.assertEqual(r["kind"], "ready")
        self.assertIn("Q3 complete · 1 of 1 accounted for · package ready", r["text"])
        self.assertIsNone(self.c(cards.compose_end, self.job_id, scheduled=True))
        rid = self.c(cards.compose_end, self.job_id, scheduled=True, extra=["Gmail is down"])
        self.assertIn("Gmail is down", self.rendering(rid)[0]["text"])

    def test_delivery_records_the_notice_and_an_undelivered_one_stays_owed(self):
        import cards, loop, views
        self.pay("Adobe")
        rid = self.c(cards.compose_ready, ["2026-Q3"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM quarter_notices").fetchone())
        views.mark_rendering_delivered(self.conn, rid)
        n = self.conn.execute("SELECT * FROM quarter_notices WHERE quarter='2026-Q3'"
                              ).fetchone()
        self.assertEqual((n["sig"], n["times"], n["render_id"]),
                         (loop.completion_sig(self.conn, "2026-Q3"), 1, rid))
        rid2 = self.c(cards.compose_ready, ["2026-Q3"])
        self.assertIn("updated", self.rendering(rid2)[0]["text"])
        views.mark_rendering_delivered(self.conn, rid2)
        self.assertEqual(self.conn.execute("SELECT times FROM quarter_notices").fetchone()[0], 2)

    def test_seen_state_counts_posted_tap_cards_only_when_delivered_for_run_messages(self):
        import cards
        p = self.pay()
        d = self.propose(p)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertFalse(cards.seen_state(self.conn, p, f"proposed:{d}"))
        card = self.c(cards.card, end, 0)
        self.assertFalse(cards.seen_state(self.conn, p, f"proposed:{d}"))     # not posted
        self.c(cards.deposit_of, card)
        self.assertTrue(cards.seen_state(self.conn, p, f"proposed:{d}"))      # posted tap card
        self.assertFalse(cards.seen_state(self.conn, p, "missing"))
        self.assertIsNone(self.c(cards.compose_end, self.job_id, scheduled=True))

    def test_every_body_is_free_of_machinery_words(self):
        import cards, views
        p = self.pay("OpenRouter", 1899)
        self.propose(p, currency="USD", amount_minor=2200)
        self.pay("Twilio")
        q, _ = self.legacy_set(2)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        rids = [end, self.c(cards.compose_open, "2026-Q3"),
                self.c(cards.compose_ready, ["2026-Q3"])]
        rids += [self.c(cards.card, end, i) for i in range(3)]
        for rid in rids:
            text = self.rendering(rid)[0]["text"]
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text, rid)
            for b in self.labels(rid):
                self.assertLessEqual(len(b), 32)

    def test_labels_never_read_as_a_link(self):
        """casa:result_broker.py _text_ok: a label is printable, at most 32 characters, with
        no "://" and no "www." — else Casa refuses the whole card."""
        import cards
        self.pay("WWW.EXAMPLE.COM\u200b")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        label = self.labels(self.c(cards.card, end, 0))[1]
        self.assertTrue(label.startswith("Never for WWW EXAMPLE.COM"), label)
        for x in ("Never for https://x.y", "Never for www.a.b"):
            got = cards._label(x)
            self.assertTrue(got.isprintable() and len(got) <= 32)
            self.assertNotIn("://", got.lower())
            self.assertNotIn("www.", got.lower())

    def test_show_view_reposts_a_card_and_composes_open(self):
        import cards, posting
        from tests.fakebroker import FakeBroker
        self.pay("Twilio")
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        with FakeBroker() as b:
            posting.show_view(self.conn, render_id=end)
            posting.show_view(self.conn, view="open")
        first, second = b.proposal(0), b.proposal(1)
        self.assertEqual([x["label"] for x in first["buttons"]], ["Review 1", "Get package"])
        self.assertEqual(first["revision"], "walk:" + end)
        self.assertIn("Q3 · still open: 0 to confirm · 1 missing", second["text"])
        self.assertEqual(second["buttons"][-1]["call"],
                         {"tool": "get_package", "arguments": {"quarter": "2026-Q3"}})
        self.assertIsNotNone(self.conn.execute("SELECT posted_seq FROM renders WHERE"
                                               " render_id=?", (end,)).fetchone()[0])
        with self.assertRaises(db.Refusal):
            posting.show_view(self.conn, view="open", page=2)    # the open items: one card

    def test_store_refuses_to_bind_a_line_that_does_not_fit(self):
        import cards
        p = self.pay()
        with db.tx(self.conn):
            with self.assertRaises(cards.Undisplayed):
                cards._store(self.conn, "end", ["head"] + ["x" * 3000, "y" * 3000],
                             {"quarter": "2026-Q3", "order": []}, {p: 2}, {})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM renders WHERE kind='end'"
                                            ).fetchone())


class Helpers(StoreCase):
    """Task 8 / Task 10 helpers built here (ruling P1)."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()

    def pay(self, n, who="Adobe", amount=10000, day="2026-09-02"):
        self.row(n, counterparty=who, amount_minor=amount, booking_date=day, value_date=day)
        pid = self.lineage_for(n)
        self.classify(pid, {"software"})
        self.settle(pid)
        return pid

    def test_keys_bind_the_document(self):
        import keys
        key = keys.mint()
        with db.tx(self.conn):
            keys.store_render(self.conn, "r1", "pick", 5, key, doc_id=7)
        for doc in (None, 8):
            with self.assertRaises(db.Refusal):
                with db.tx(self.conn):
                    keys.spend_render(self.conn, key, "r1", "pick", 5, doc_id=doc)
        with db.tx(self.conn):
            keys.spend_render(self.conn, key, "r1", "pick", 5, doc_id=7)
        self.assertIsNotNone(self.conn.execute("SELECT spent_at FROM render_keys WHERE key=?",
                                               (key,)).fetchone()[0])
        plain = keys.mint()
        with db.tx(self.conn):
            keys.store_render(self.conn, "r1", "right", 5, plain)
            keys.spend_render(self.conn, plain, "r1", "right", 5)

    def test_keyed_stores_the_document_of_a_three_part_key_spec(self):
        import posting
        with db.tx(self.conn):
            out = posting._keyed(self.conn, "r9", [
                ("A", "verdict", {"render_id": "r9", "action": "pick"}, ("pick", 3, 11)),
                ("B", "verdict", {"render_id": "r9", "action": "wrong"}, ("wrong", 3, None)),
                ("C", "get_package", {"quarter": "2026-Q3"}, None)])
        rows = {r["action"]: (r["pid"], r["doc_id"], r["key"]) for r in self.conn.execute(
            "SELECT * FROM render_keys WHERE render_id='r9'")}
        self.assertEqual({k: v[:2] for k, v in rows.items()},
                         {"pick": (3, 11), "wrong": (3, None)})
        self.assertEqual(out[0][2]["key"], rows["pick"][2])
        self.assertNotIn("key", out[2][2])

    def test_leave_missing_needs_a_grant_and_settles_each_payment(self):
        import work
        a, b = self.pay(9001), self.pay(9002, amount=200)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                work.leave_missing_in_tx(self.conn, [a], grant=None)
        rev = self.rev(a)
        self.assertEqual(self.granted(work.leave_missing_in_tx, [a, b]), [a, b])
        st = [r[0] for r in self.conn.execute("SELECT search_state FROM projections WHERE"
                                              " pid IN (?,?)", (a, b))]
        self.assertEqual(st, ["accepted-missing"] * 2)
        self.assertGreater(self.rev(a), rev)          # settled: the revision moved

    def test_completion_sig_is_per_quarter_and_moves_with_a_decision(self):
        import loop, matches
        p = self.pay(9001)
        self.pay(9002, day="2026-05-02")            # before the watermark: out of scope
        q2 = loop.completion_sig(self.conn, "2026-Q2")
        self.assertEqual(q2, "[]")
        s0 = loop.completion_sig(self.conn, "2026-Q3")
        self.assertEqual(json.loads(s0)[0][:2], [p, "open"])
        self.assertEqual(loop.completion_sig(self.conn, "2026-Q3"), s0)
        d = self.doc()
        mid = self.machine_match(p, d, self.token)["match_id"]
        s1 = loop.completion_sig(self.conn, "2026-Q3")
        self.assertNotEqual(s1, s0)
        # Wrong: status, search state and pairing are back to what s0 recorded; the latest
        # decision is not (plan round 4)
        rid = self.show(p)
        with db.tx(self.conn):
            matches.reject_in_tx(self.conn, grant=self.grant(), match_id=mid,
                                 expected_revision=self.rev(match_id=mid), render_id=rid)
        s2 = loop.completion_sig(self.conn, "2026-Q3")
        self.assertEqual(json.loads(s2)[0][:4], json.loads(s0)[0][:4])
        self.assertNotEqual(s2, s0)
