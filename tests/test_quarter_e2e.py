# tests/test_quarter_e2e.py
"""Simple loop §6 as code: a whole quarter through the real tools (qa_server.TOOLS) and the
real bank-feed (tests/bankfeed.py, vendored component v0.21.0)."""
import collections
import datetime
import json
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported
from tests.sim_job import JobDriver


TODAY = datetime.datetime(2026, 10, 6, 9, 0, tzinfo=datetime.timezone.utc)


def pin_clock(test, at=TODAY):
    """Ruling P8: the store's clock (db._clock, dates._today) starts at `at` and ticks with
    real time, so a run's coverage (`bank_through`: the day its sync succeeded), the
    quarter's end and the sync-age alerts never depend on the day the suite runs."""
    import dates
    import time
    start = time.monotonic()

    def clock():
        return at + datetime.timedelta(seconds=time.monotonic() - start)
    test.patch(db, "_clock", clock)
    test.patch(dates, "_today", lambda: clock().date())


def _bank_writes(bf) -> tuple:
    """bank-feed's own record of every write: each row's tag revision (store
    TAG_REVISIONS_TABLE, bumped by its triggers on any tag insert or delete) and every note
    (append-only, by note_id). A delete-and-re-add moves the first; a note re-added the
    second."""
    import store
    return (bf.conn.execute("SELECT row_id, revision FROM %s ORDER BY row_id"
                            % store.TAG_REVISIONS_TABLE).fetchall(),
            bf.conn.execute("SELECT note_id, row_id, note FROM transaction_notes ORDER BY"
                            " note_id").fetchall())


def _log_seq(conn):
    return conn.execute("SELECT max(seq) FROM log").fetchone()[0]


def _mirror_calls(units) -> list:
    return [c for u in units if u["unit"] == "mirror" for c in u["calls"]]


class Quarter(StoreCase):
    def setUp(self):
        super().setUp()
        pin_clock(self)
        self.bind()
        self.drv = JobDriver(self, payments=0)
        self.rows = self.drv.quarter_fixture()
        self.units = self.drv.run_job("eeeeeeee-1", started_by="operator")

    def buckets(self):
        import cards
        st = cards.state(self.conn)
        out = {}
        for name in ("matched", "proposed", "missing", "not_needed", "pending"):
            for d in st["by_bucket"][name]:
                self.assertNotIn(d["pid"], out, "a payment in two buckets")
                out[d["pid"]] = name
        return out

    def end_text(self, job_id):
        end = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                (job_id,)).fetchone()[0]
        return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (end,)).fetchone()[0]

    def test_every_payment_appears_exactly_once(self):
        got = self.buckets()
        self.assertEqual(len(got), 9)
        want = {1: "matched", 2: "matched", 3: "proposed", 4: "missing", 5: "matched",
                6: "not_needed", 7: "pending", 8: "not_needed", 9: "matched"}
        self.assertEqual({self.rows[r]: b for r, b in want.items()}, got)
        self.assertEqual(self.conn.execute("SELECT partial FROM runs WHERE job_id="
                                           "'eeeeeeee-1'").fetchone()[0], 0)
        self.assertEqual(self.units[-1]["unit"], "complete")

    def test_the_exact_fit_on_file_needed_no_search(self):
        aws = [q for q in self.drv.gmail.searches if "aws" in q.lower()]
        self.assertEqual(aws, [])
        self.assertEqual(self.drv.searches_of("AWS"), [])
        doc = self.conn.execute("SELECT d.document_number FROM projections p JOIN matches m"
                                " ON m.match_id=p.current_match JOIN documents d ON"
                                " d.doc_id=m.doc_id WHERE p.pid=?", (self.rows[9],)).fetchone()
        self.assertEqual(doc[0], "AWS-0901")

    def test_the_learned_hint_is_stored(self):
        cp = self.conn.execute("SELECT hint_sender FROM counterparties WHERE name='Adobe'"
                               ).fetchone()
        self.assertEqual(cp[0], "billing@adobe.com")
        # the next run's Adobe search, if one were needed, is the hinted one
        self.assertEqual([k for k, _ in self.drv.searches_of("Adobe")], ["plain"])

    def test_the_mirror_is_exact_and_an_immediate_rerun_writes_nothing(self):
        import mirror
        for pid, bucket in self.buckets().items():
            p = self.conn.execute("SELECT dest_row_id, desired_json FROM projections WHERE"
                                  " pid=?", (pid,)).fetchone()
            owned = [t for t in self.drv.bf.tags(p[0]) if t.startswith("acct::")]
            notes = [n for n in self.drv.bf.notes(p[0]) if n.startswith("Accounting")]
            if bucket == "pending":                       # D18: nothing mirrored while PDNG
                self.assertEqual((owned, notes), ([], []))
                continue
            self.assertEqual(sorted(owned), sorted(json.loads(p[1])))
            if bucket != "not_needed":
                self.assertTrue(owned, bucket)
            self.assertEqual(notes[-1], mirror.note_text(self.conn, pid))
        bank = _bank_writes(self.drv.bf)
        self.assertTrue(bank[0] and bank[1])
        seq = _log_seq(self.conn)
        units = self.drv.run_job("eeeeeeee-2", started_by="operator")
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertEqual(_mirror_calls(units), [])
        self.assertEqual(_log_seq(self.conn), seq)
        # measured on the real bank-feed: no tag revision moved, no note added
        self.assertEqual(_bank_writes(self.drv.bf), bank)

    def test_a_re_decision_writes_nothing(self):
        """The rerun re-decides Twilio `missing` (still open, still on the list) and a
        decide of Adobe's own pairing again: neither writes a log entry or moves a
        revision."""
        import qa_server, tools  # noqa: F401
        adobe, twilio = self.rows[1], self.rows[4]
        revs = (self.rev(adobe), self.rev(twilio))
        seq = _log_seq(self.conn)
        units = self.drv.run_job("eeeeeeee-7", started_by="operator")
        vendors = [u["vendor"] for u in units if u["unit"] == "vendor"]
        self.assertEqual(vendors, ["Twilio"])               # re-decided, missing again
        held = self.conn.execute("SELECT m.doc_id, d.document_date FROM projections p JOIN"
                                 " matches m ON m.match_id=p.current_match JOIN documents d"
                                 " ON d.doc_id=m.doc_id WHERE p.pid=?", (adobe,)).fetchone()
        tok = self.drv.claim("eeeeeeee-9")
        out = qa_server.TOOLS["decide"]["fn"]({"pass_token": tok, "entries": [
            {"pid": adobe, "outcome": "match", "doc_id": held[0], "document_date": held[1],
             "expected_revision": self.rev(adobe)}]})
        self.assertEqual([(r["applied"], r["wrote"]) for r in out["results"]], [(True, False)])
        self.assertEqual((self.rev(adobe), self.rev(twilio)), revs)
        self.assertEqual(_log_seq(self.conn), seq)

    def test_the_pending_row_is_pending_until_booked_then_missing(self):
        figma = self.rows[7]
        row_id = self.conn.execute("SELECT dest_row_id FROM projections WHERE pid=?",
                                   (figma,)).fetchone()[0]
        self.assertEqual([t for t in self.drv.bf.tags(row_id) if t.startswith("acct::")], [])
        self.assertFalse([n for n in self.drv.bf.notes(row_id) if n.startswith("Accounting")])
        self.assertIn("1 pending", self.end_text("eeeeeeee-1"))
        import loop
        self.assertFalse(loop.complete(self.conn, "2026-Q3"))
        self.drv.book(row_no=7)                         # a later fetch books the row
        self.drv.run_job("eeeeeeee-6", started_by="operator")
        self.assertEqual(self.buckets()[self.drv.pid_of(7)], "missing")
        row_id = self.conn.execute("SELECT dest_row_id FROM projections WHERE pid=?",
                                   (self.drv.pid_of(7),)).fetchone()[0]
        self.assertIn("acct::open", self.drv.bf.tags(row_id))
        self.assertEqual(self.drv.bf.notes(row_id)[-1],
                         "Accounting: invoice missing (quarterly check)")
        self.assertNotIn("pending", self.end_text("eeeeeeee-6"))

    def test_a_stale_hint_falls_back_to_the_plain_search(self):
        """§2.2 rev 17: the hint's sender changed; the hinted search finds nothing, the plain
        search finds the invoice; the hint is replaced by what found it."""
        import kb
        kb.upsert_counterparty(self.conn, "Linear", hint_sender="old@linear.app",
                               hint_subject="Linear invoice", token=None)
        self.drv.gmail.invoice("Linear", 800, "EUR", "2026-09-12", "LIN-7",
                               sender="billing@linear.app")
        no = self.drv.pay_once("Linear", 800, "2026-09-12")
        self.drv.run_job("eeeeeeee-8", started_by="operator")
        kinds = [k for k, _ in self.drv.searches_of("Linear")]
        self.assertEqual(kinds, ["hinted", "plain"])
        self.assertEqual(self.buckets()[self.drv.pid_of(no)], "matched")
        self.assertEqual(kb.get_counterparty(self.conn, "Linear")["hint_sender"],
                         "billing@linear.app")

    def test_notes_group_and_read_as_plain_words(self):
        calls = _mirror_calls(self.units)
        needs_none = [c for c in calls if c["tool"] == "add_note"
                      and c["args"]["note"].startswith("Accounting: no invoice needed")]
        self.assertEqual(len(needs_none), 1)               # rows 6 and 8 in ONE call
        self.assertEqual(len(needs_none[0]["args"]["row_ids"]), 2)

    def test_a_decide_with_one_refused_entry_applies_the_other(self):
        import qa_server, tools  # noqa: F401
        tok = self.drv.claim("eeeeeeee-3")
        tw = self.rows[4]
        usd = self.drv.file_document(vendor="Twilio", amount_minor=2150, currency="USD")
        out = qa_server.TOOLS["decide"]["fn"]({"pass_token": tok, "entries": [
            {"pid": tw, "outcome": "match", "doc_id": usd, "document_date": "2026-07-09",
             "expected_revision": self.rev(tw)},
            {"pid": self.rows[3], "outcome": "missing", "reason": "x",
             "expected_revision": self.rev(self.rows[3])}]})
        self.assertEqual([r["applied"] for r in out["results"]], [False, False])
        out = qa_server.TOOLS["decide"]["fn"]({"pass_token": tok, "entries": [
            {"pid": tw, "outcome": "propose", "doc_id": usd, "document_date": "2026-07-09",
             "expected_revision": self.rev(tw)},
            {"pid": tw, "outcome": "missing", "reason": "x",
             "expected_revision": self.rev(tw)}]})
        self.assertEqual([r["applied"] for r in out["results"]], [True, False])
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (tw,)).fetchone()[0], "proposed")

    def reopened_by(self, file_later, job_id):
        """A document of Adobe's amount filed after run 1 (`file_later`), then run
        `job_id`: July's machine match becomes ONE proposal — its held document chosen, the
        later one its alternative — and August's re-decision (the same document is now
        July's alternative) writes nothing."""
        import matches
        adobe, adobe_aug = self.rows[1], self.rows[2]
        held = self.conn.execute("SELECT m.doc_id FROM projections p JOIN matches m ON"
                                 " m.match_id=p.current_match WHERE p.pid=?",
                                 (adobe,)).fetchone()[0]
        doc = file_later()
        rev_aug = self.rev(adobe_aug)
        self.drv.run_job(job_id, started_by="operator")
        got = self.buckets()
        self.assertEqual((got[adobe], got[adobe_aug]), ("proposed", "matched"))
        cur = self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                (adobe,)).fetchone()[0]
        chosen = self.conn.execute("SELECT doc_id FROM matches WHERE match_id=?",
                                   (cur,)).fetchone()[0]
        self.assertEqual((chosen, matches.alternatives(self.conn, cur)), (held, [doc]))
        self.assertEqual(self.rev(adobe_aug), rev_aug)
        self.assertIn("To confirm", self.end_text(job_id))

    def test_a_later_document_reopens_a_machine_match_as_one_proposal(self):
        """The desk's filing, handed over (§2.5)."""
        self.reopened_by(lambda: self.drv.hand_over(
            vendor=None, amount_minor=10000, currency="EUR", document_date="2026-07-02"),
            "eeeeeeee-4")

    def test_a_document_filed_later_without_a_handover_reopens_it_too(self):
        """Filed with no handover (§2.2 "Reopening": newly filed, fitting exactly)."""
        self.reopened_by(lambda: self.drv.file_document(
            vendor=None, amount_minor=10000, currency="EUR", document_date="2026-07-02"),
            "eeeeeeee-a")

    def test_confirm_all_after_a_review_answer(self):
        end = self.drv.posted_end("eeeeeeee-1")             # the end message's deposit
        card = self.drv.tap(end, "Review 2")["next"]        # OpenRouter's card, then Twilio's
        self.assertIn("OpenRouter", card["text"])
        self.drv.tap(card, "Wrong")
        out = self.drv.tap(end, "Confirm all 1")
        self.assertIn("Confirmed 0 of 1", out["receipt"])   # the one listed was answered
        self.assertEqual(self.conn.execute("SELECT count(*) FROM match_state WHERE pid=? AND"
                                           " state='matched'", (self.rows[3],)).fetchone()[0],
                         0)

    def test_a_scheduled_run_with_nothing_new_is_silent(self):
        renders = self.conn.execute("SELECT count(*) FROM renders").fetchone()[0]
        units = self.drv.run_job("eeeeeeee-5", started_by="scheduled")
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertFalse([u for u in units if u["unit"] in ("view", "post")])
        self.assertEqual(self.conn.execute("SELECT end_render_id FROM runs WHERE job_id="
                                           "'eeeeeeee-5'").fetchone()[0], "")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders").fetchone()[0],
                         renders)


class CheckQ2(StoreCase):
    """Ruling Q2 (live acceptance on Q2, watermark 2026-04-01): an operator check that names
    Q2, with Q3 rows present, is headed Q2 and its [Get package] builds Q2; Q2's completion
    notice is reachable because a Q3 row booked after 30 Jun covers Q2."""

    def setUp(self):
        super().setUp()
        pin_clock(self)
        self.bind(watermark="2026-04-01")
        self.drv = JobDriver(self, payments=0)
        g = self.drv.gmail
        self.q2 = self.drv._add_rows([("Adobe", 10000, "2026-04-03", "software"),
                                      ("Adobe", 10000, "2026-05-03", "software")])
        self.q3 = self.drv._add_rows([("Twilio", 2000, "2026-07-21", "software")])
        g.invoice("Adobe", 10000, "EUR", "2026-04-03", "ADB-04")
        g.invoice("Adobe", 10000, "EUR", "2026-05-03", "ADB-05")

    def test_check_q2_is_headed_q2_and_its_completion_is_reachable(self):
        import asks, loop
        asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
        self.drv.run_job("aaaaaaaa-2", started_by="operator")
        self.assertTrue(loop.covered(self.conn, "2026-Q2"))
        self.assertTrue(loop.complete(self.conn, "2026-Q2"))
        self.assertFalse(loop.complete(self.conn, "2026-Q3"))     # Twilio missing
        end = self.drv.posted_end("aaaaaaaa-2")
        self.assertTrue(end["text"].startswith("Q2 "), end["text"])
        self.assertIn('Q2 complete · package ready', end["text"])
        get = [b["call"] for b in end["buttons"] if b["label"] == "Get package"]
        self.assertEqual([c["arguments"]["quarter"] for c in get], ["2026-Q2"])
        # the notice was delivered with the message: owed no more
        self.assertNotIn("2026-Q2", loop.owed_notices(self.conn))


    def test_a_pending_row_keeps_q2_open_until_it_is_booked(self):
        """D18 + §1: a pending Q2 row is counted pending, carries nothing on the bank, and
        Q2 stays incomplete — no completion notice — until a later fetch books it."""
        import asks, loop
        late = self.drv._add_rows([("Figma", 1200, "2026-06-29", "software", "PDNG")])[0]
        self.drv.gmail.invoice("Figma", 1200, "EUR", "2026-06-29", "FIG-06")
        asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
        self.drv.run_job("aaaaaaaa-3", started_by="operator")
        self.assertTrue(loop.covered(self.conn, "2026-Q2"))
        self.assertFalse(loop.complete(self.conn, "2026-Q2"))
        end = self.drv.posted_end("aaaaaaaa-3")["text"]
        self.assertTrue(end.startswith("Q2 "), end)
        self.assertIn("1 pending", end)
        self.assertNotIn("complete", end)
        row = self.drv.row_id_of(late)
        self.assertEqual(([t for t in self.drv.bf.tags(row) if t.startswith("acct::")],
                          self.drv.bf.notes(row)), ([], []))
        self.drv.book(late, day="2026-06-30")
        asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
        self.drv.run_job("aaaaaaaa-4", started_by="operator")
        self.assertTrue(loop.complete(self.conn, "2026-Q2"))
        self.assertIn("Q2 complete · package ready", self.drv.posted_end("aaaaaaaa-4")["text"])


class RealisticQuarter(StoreCase):
    """§5, §6.1 as code (plan round 6, Terra S2): a Q3-shaped quarter of 60 payments over 18
    vendors — recurring monthly charges, FX rows with USD invoices, tax and fee rows, one
    0.00 authorisation, one PDNG row — driven through sim_job with calls_made counted per
    tool call. Completion, no partial, at most 7 batches, every payment exactly once. The
    dollar cost is not measurable offline (no model): PLAY measures it (§6.7)."""

    def test_sixty_payments_finish_within_seven_batches(self):
        import cards
        pin_clock(self)
        self.bind()
        drv = JobDriver(self, payments=0)
        rows = drv.quarter_fixture_60()
        units = drv.run_job("ffffffff-1", started_by="operator")
        batches = 1 + sum(1 for u in units if u["unit"] == "end-batch")
        self.assertLessEqual(batches, 7)
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertEqual(self.conn.execute("SELECT partial FROM runs WHERE job_id="
                                           "'ffffffff-1'").fetchone()[0], 0)
        seen = {}
        for name, ds in cards.state(self.conn)["by_bucket"].items():
            for d in ds:
                self.assertNotIn(d["pid"], seen)
                seen[d["pid"]] = name
        self.assertEqual(sorted(seen), sorted(rows.values()))
        self.assertEqual(len(seen), 60)
        self.assertEqual({rows[n]: b for n, b in drv.expected.items()}, seen)
        self.assertEqual(collections.Counter(seen.values()),
                         {"matched": 42, "proposed": 5, "missing": 5, "not_needed": 7,
                          "pending": 1})
        # every vendor group searched at most once per kind; the hinted vendors only hinted
        kinds = collections.Counter((v, k) for v, k, _ in drv.search_log)
        self.assertEqual(max(kinds.values()), 1)
        self.assertEqual({k for (v, k) in kinds if v == "Notion"}, {"hinted"})
        self.assertFalse(drv.searches_of("IKEA Business") + drv.searches_of("Conrad"))
        # every turn stays inside Casa's 80-call turn (§2.2), counted honestly
        self.assertLess(max(drv.batch_calls), 80, drv.batch_calls)
        self.assertEqual(sum(drv.batch_calls), drv.calls_total)
