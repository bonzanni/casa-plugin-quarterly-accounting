"""Diff round d1 (Astra + Terra, 26b68ee..d7729e3), the three accepted findings, each
reproduced through the real surface:
- Terra S1 (ruled: generalize): [Never for X]'s affected set is learned by REHEARSAL — the
  counterparty rule applied in a savepoint and rolled back — and that exact set is what the
  vendor card lists and what Never binds (a pending and a proposed payment included);
- Astra S1: once a run's end message is composed, the run takes no more handovers; they
  stay queued for the next run (the continuation), which posts their proposal;
- Astra S2: a failed mirror write is an alert, once per occurrence (pid + payload), in the
  run's one message — a scheduled run posts it with no new item, and never again."""
import json
from tests._base import StoreCase
from tests.sim_job import JobDriver
from tests.test_taps_next import _Tapping
import db                     # server/ is on sys.path once tests._base is imported


class NeverIsRehearsed(_Tapping):
    def pending(self, who="Adobe", amount=200):
        """A pending (PDNG) payment of `who`."""
        pid = self.pay(who, amount)
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET status='PDNG' WHERE row_id=?", (self.n,))
        self.settle(pid)
        return pid

    def status(self, pid):
        return self.conn.execute("SELECT status, exp_kind FROM projections WHERE pid=?",
                                 (pid,)).fetchone()

    def page_of(self, deposit):
        rid = deposit["buttons"][0]["call"]["arguments"]["render_id"]
        return rid, sorted(r[0] for r in self.conn.execute(
            "SELECT pid FROM render_items WHERE render_id=?", (rid,)))

    def test_terras_sequence_the_pending_payment_is_listed_and_bound(self):
        """Terra d1 S1, exactly: one booked missing Adobe payment and one PDNG Adobe
        payment; the vendor card; Never. PLAY 0.11.2: the card lists the missing one and
        states the pending one in its `also` line; Never changes exactly those two."""
        booked = self.pay("Adobe", 100)
        pdng = self.pending()
        page = self.tap(self.end(), "Review")["next"]
        _, bound = self.page_of(page)
        # PLAY 0.11.2 (r7 fix): the stated `also` payment is bound to its summary line
        self.assertEqual(bound, sorted([booked, pdng]))
        self.assertEqual(self.scope_of(page)["missing"], [booked])
        self.assertEqual(self.scope_of(page)["also"], [pdng])
        self.assertIn("Never for Adobe would also change 1 more payment of this quarter.",
                      page["text"].splitlines())
        self.assertNotIn("· pending", page["text"])
        out = self.tap(page, "Never for Adobe")
        self.assertEqual(out["receipt"], "Adobe never needs an invoice: 2 payments changed.")
        self.assertEqual(tuple(self.status(booked)), ("no-document", "none"))
        self.assertEqual(tuple(self.status(pdng)), ("no-document", "none"))

    def test_a_pending_and_a_proposed_payment_are_listed_marked_and_bound(self):
        missing = self.pay("Adobe", 100)
        pdng = self.pending()
        proposed = self.pay("Adobe", 300)
        self.propose(proposed, amount_minor=300)
        import cards
        with db.tx(self.conn):
            self.assertEqual(cards.never_set(self.conn, "Adobe"),
                             sorted([missing, pdng, proposed]))
        end = self.end()
        page = self.tap(end, "Review")["next"]                  # the proposal card first
        page = self.tap(page, "Leave for now")["next"]            # then Adobe's vendor card
        rid, bound = self.page_of(page)
        # PLAY 0.11.2: only the missing one is a line; the others are frozen in `also` and
        # bound to its summary line (r7 fix: Never refuses one that changed)
        self.assertEqual(bound, sorted([missing, pdng, proposed]))
        lines = page["text"].splitlines()
        self.assertIn("Never for Adobe would also change 2 more payments of this quarter.",
                      lines)
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        self.assertEqual(sorted(scope["also"]), sorted([pdng, proposed]))
        self.assertEqual(scope["missing"], [missing])             # what the exemption binds
        out = self.tap(page, "Never for Adobe")
        self.assertEqual(out["receipt"], "Adobe never needs an invoice: 3 payments changed.")
        self.assertEqual(self.status(pdng)["status"], "no-document")
        self.assertEqual(tuple(self.status(proposed)), ("proposed", "none"))

    def test_no_invoice_needed_exempts_only_the_missing_lines(self):
        missing = self.pay("Adobe", 100)
        pdng = self.pending()
        page = self.tap(self.end(), "Review")["next"]
        out = self.tap(page, "No invoice needed for these")
        self.assertEqual(out["receipt"], "No invoice needed (Adobe): EUR 1.00 · 2 Sep.")
        self.assertEqual(self.status(missing)["status"], "exempt")
        self.assertEqual(self.status(pdng)["status"], "open")

    def test_an_exemption_is_bound_only_to_the_missing_lines_it_acts_on(self):
        """d1 re-review minor: a revision move on a matched line of the same page (listed
        because Never would change it) does not refuse [No invoice needed for these] of the
        page's unchanged missing line — Never on that page still refuses."""
        import documents
        missing = self.pay("Adobe", 100)
        matched = self.pay("Adobe", 400)
        doc = self.doc(amount_minor=400)
        self.machine_match(matched, doc, self.token)
        page = self.tap(self.end(), "Review")["next"]
        # PLAY 0.11.2: the matched one is no line; it is stated in `also`, bound to that line
        self.assertEqual(self.page_of(page)[1], sorted([missing, matched]))
        self.assertEqual(self.scope_of(page)["also"], [matched])
        rev = self.rev(matched)
        documents.update_document_metadata(self.conn, doc, document_number="RENUMBERED")
        self.assertNotEqual(self.rev(matched), rev)              # the matched line moved
        never = self.tap(page, "Never for Adobe")
        self.assertIn("nothing applied", never["receipt"])
        page = self.tap(self.end(), "Review")["next"]
        rev = self.rev(matched)
        documents.update_document_metadata(self.conn, doc, document_number="AGAIN")
        self.assertNotEqual(self.rev(matched), rev)
        out = self.tap(page, "No invoice needed for these")
        self.assertEqual(out["receipt"], "No invoice needed (Adobe): EUR 1.00 · 2 Sep.")
        self.assertEqual(self.status(missing)["status"], "exempt")
        self.assertEqual(self.status(matched)["status"], "matched")

    def _refuses(self, arrive):
        """A card composed before `arrive()` adds a payment the rule changes: Never commits
        nothing, and the next card is a fresh page 1 that lists the newcomer."""
        self.pay("Adobe", 100)
        page = self.tap(self.end(), "Review")["next"]
        new = arrive()
        out = self.tap(page, "Never for Adobe")
        self.assertIn("nothing applied", out["receipt"])
        self.assertIsNone(self.conn.execute("SELECT exp_kind FROM counterparties WHERE"
                                            " name='Adobe'").fetchone())
        self.assertNotEqual(self.status(new)["exp_kind"], "none")
        self.assertIn("missing invoices · Adobe", out["next"]["text"])
        # PLAY 0.11.2: the fresh page 1 lists the missing ones and states the newcomer
        # (pending or proposed, not missing) in its frozen `also`
        self.assertNotIn(new, self.scope_of(out["next"])["missing"])     # no line of its own
        self.assertIn(new, self.scope_of(out["next"])["also"])
        return out

    def test_never_on_a_card_that_omitted_a_pending_payment_refuses(self):
        self._refuses(self.pending)

    def test_never_on_a_card_that_omitted_a_proposed_payment_refuses(self):
        def proposed():
            p = self.pay("Adobe", 300)
            self.propose(p, amount_minor=300)
            return p
        out = self._refuses(proposed)
        self.assertIn("Never for Adobe would also change 1 more payment of this quarter.",
                      out["next"]["text"].splitlines())

    def test_the_rehearsal_leaves_nothing_behind(self):
        import cards
        p = self.pay("Adobe", 100)
        before = self.status(p)["exp_kind"], self.rev(p)
        renders = self.conn.execute("SELECT count(*) FROM renders").fetchone()[0]
        with db.tx(self.conn):
            self.assertEqual(cards.never_set(self.conn, "Adobe"), [p])
        self.assertEqual((self.status(p)["exp_kind"], self.rev(p)), before)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders").fetchone()[0],
                         renders)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM counterparties WHERE"
                                            " exp_kind='none'").fetchone())


class LateHandover(StoreCase):
    """Astra d1 S1: a handover requested after the run's end message was composed."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=1)

    def until(self, jid, target, started="operator"):
        """Claim `jid` and carry out units until `target` is handed out (not done)."""
        import job
        self.drv.claim(jid, started)
        for _ in range(60):
            u = self.drv.next()
            self.drv.calls += 1
            if u["unit"] == target:
                return u
            self.drv.do(u, self.drv.token)
        self.fail(f"{target} was never handed out")

    def test_astras_sequence_the_handover_waits_for_the_continuation(self):
        import cards, job
        jid = "dadadada-1"
        self.drv.gmail.invoice("Zapier", 1000, "EUR", "2026-07-05", "ORIGINAL")
        # #93: "all accounted for" has nothing to tap — the end message is a plain post
        first = self.until(jid, "post")                       # the end message is composed
        composed = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                     (jid,)).fetchone()[0]
        self.assertEqual(first["render_ids"], [composed])
        path = self.publish("competitor.pdf", b"%PDF-1.4 competitor", producer="telegram")
        doc = self.drv._tool("ingest_document", dict(
            source_path=path, kind="invoice", source="manual-telegram",
            extraction_author="desk", issuer="Zapier", amount_minor=1100, currency="USD",
            document_date="2026-07-05", document_number="COMPETITOR"))
        ask = self.drv._tool("request_work", dict(kind="handover", trigger="operator",
                                                  doc_ids=[doc["doc_id"]]))
        import asks
        said = asks.ask_state(self.conn, "work", ask["request_id"])   # Astra's window
        self.assertEqual((said["state"], said["live_run"], said["line"]),
                         ("queued", False, asks.BUSY_NO_RESULT))
        u = self.drv.next()
        self.assertEqual(u["unit"], "post")                    # not the handover's vendor
        self.assertEqual(u["render_ids"], [composed])
        self.drv.do(u, self.drv.token)
        units = [x["unit"] for x in self.drv._loop(jid)]
        self.assertEqual(units, ["complete"])
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests WHERE"
                                           " request_id=?", (ask["request_id"],)).fetchone()[0],
                         "queued")
        delivered = self.conn.execute("SELECT render_id, text FROM renders WHERE"
                                      " delivered_at IS NOT NULL").fetchall()
        self.assertEqual([r[0] for r in delivered], [composed])
        self.assertIn("accounted for", delivered[0][1])            # the composed one
        self.assertEqual(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                           " status='proposed'").fetchone()[0], 0)
        # the next run — the continuation — takes the handover and posts its question (rev
        # 18.4 §R18.3: the payment already has a document; only the operator replaces it)
        cont = "dadadada-2"
        v = self.until(cont, "payment")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests WHERE"
                                           " request_id=?", (ask["request_id"],)).fetchone()[0],
                         "taken")
        pay = v
        self.assertEqual((pay["why"], pay["candidates"][0]["doc_id"]), ("handover", doc["doc_id"]))
        out = self.drv._tool("decide", dict(pass_token=self.drv.token, entries=[dict(
            pid=pay["pid"], outcome="propose", doc_id=doc["doc_id"],
            expected_revision=pay["revision"], document_date="2026-07-05")]))
        self.assertEqual(out["applied"], 0)                   # never the job's to replace
        out = self.drv._tool("decide", dict(pass_token=self.drv.token, entries=[dict(
            pid=pay["pid"], outcome="replace", doc_id=doc["doc_id"],
            expected_revision=pay["revision"])]))
        self.assertEqual(out["applied"], 1)
        self.drv._loop(cont)
        end = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                (cont,)).fetchone()[0]
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (end,)).fetchone()
        self.assertIsNotNone(r["delivered_at"])
        self.assertIn("1 handed-over document to check — Review shows it.", r["text"])
        self.assertEqual([b[0] for b in cards.buttons(self.conn, r)],
                         ["Review", "Close"])
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests WHERE"
                                           " request_id=?", (ask["request_id"],)).fetchone()[0],
                         "reported")                             # the continuation's result


class MirrorFailureAlert(StoreCase):
    """Astra d1 S2: a scheduled run whose mirror write bank-feed refused."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=1)
        self.until = LateHandover.until.__get__(self)

    def posts(self, units):
        return [u for u in units if u["unit"] in ("view", "post")]

    def texts(self, units):
        out = []
        for u in self.posts(units):
            for rid in ([u["render_id"]] if u["unit"] == "view" else u["render_ids"]):
                out.append(self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                             (rid,)).fetchone()[0])
        return out

    def test_astras_sequence_one_message_then_never_again(self):
        import version
        self.drv.run_job("babababa-1")                         # the missing item delivered
        rid = self.drv.bf.rows()[0]["row_id"]
        self.drv.bf.call("untag_transaction", row_ids=[rid], tags=["acct::open"],
                         workflow=version.WORKFLOW,
                         expected_generation=self.drv.bf.generation())
        u = self.until("babababa-2", "mirror", "scheduled")    # the repair
        self.drv.bf.purge_before("2027-01-01")                 # the row erased after export
        c = u["calls"][0]
        reply = self.drv.bf.call(c["tool"], **c["args"])
        self.assertIn("no transaction #", reply)
        self.drv._tool("record_mirror", dict(pass_token=self.drv.token, done=[],
                                             failed=[dict(n=c["n"], error=reply)]))
        units = self.drv._loop("babababa-2")
        texts = self.texts(units)
        self.assertEqual(len(texts), 1)
        self.assertEqual(sum("did not go through" in ln for ln in texts[0].splitlines()), 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM alerts WHERE kind='mirror-failed'"
                                            " AND sent_at IS NULL").fetchone())
        again = self.drv.run_job("babababa-3", started_by="scheduled")
        self.assertEqual(self.posts(again), [])

    def failing(self):
        """bank-feed refuses every mirror write (the same payload each run)."""
        def refuse(u, token):
            self.drv._tool("record_mirror", {"pass_token": token, "done": [], "failed": [
                {"n": c["n"], "error": "refused: stale generation"} for c in u["calls"]]})
        self.drv._mirror = refuse

    def test_the_same_failure_recurring_is_said_once(self):
        self.drv.run_job("cacacaca-1")                         # the missing item delivered
        self.failing()
        self.drv.add_payments(["2026-08-20"])                  # a new missing item
        units = self.drv.run_job("cacacaca-2", started_by="scheduled")
        texts = self.texts(units)
        self.assertEqual(len(texts), 1)                        # the new item, with the line
        self.assertEqual(sum("did not go through" in ln for ln in texts[0].splitlines()), 1)
        keys = [r[0] for r in self.conn.execute("SELECT occurrence_key FROM alerts WHERE"
                                                " kind='mirror-failed'")]
        self.assertTrue(keys)
        self.assertEqual(self.posts(self.drv.run_job("cacacaca-3", started_by="scheduled")),
                         [])                                   # same writes refused again
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT occurrence_key FROM alerts WHERE kind='mirror-failed'")), sorted(keys))

    def test_an_operator_run_carries_the_line_in_its_end_message(self):
        self.failing()
        units = self.drv.run_job("edededed-1")
        (text,) = self.texts(units)
        self.assertIn("1 missing", text)
        self.assertEqual(sum("did not go through" in ln for ln in text.splitlines()), 1)
