"""S2 INV-J8 replaced (spec §15; design revision 6): a batch progressed iff, under its
claims, the job earned a credit it had not earned before in that pass. A credit is a
`credits(pass_id, key, gen)` row inserted with the work, under the unit's claim token;
every key is drawn from a set finite per pass.

(L) Liveness: a batch that does real work reports progress — the old rule (a fall of the
remaining work, plus rebasing hooks) broke six times on work that creates work.
(T) Termination: a job that cannot finish stops reporting progress, so Casa ends it
after three batches (INV-BGJOB-002), or its run completes (MAX_PASSES_PER_JOB).

The reproductions are the design and diff review rounds' (rounds-2026-10-02-s2-diff),
each named on its test."""
import datetime

from tests._base import StoreCase
from tests.sim_job import JobDriver
from tests.test_s2_diff_r1 import A, B, Tools


def _three_false(flags):
    return len(flags) >= 3 and flags[-3:] == [False, False, False]


def _no_three_false(flags):
    return all(flags[i:i + 3] != [False] * 3 for i in range(len(flags)))


def run_batches(test, drv, job_id=A, answer=None, batches_max=8, before_batch=None):
    """Casa's loop for one job: claim, job_next until `end-batch` (the next claim is a new
    batch) or `complete`. `answer(u, token)` gives the judged answers to try for a judge
    unit, in order: a refused one is followed by the next (a worker corrected by the
    refusal). `before_batch(n)` runs before batch n's claim (a write between batches).
    Returns (the end-batch flags, the last unit, the refusals)."""
    import db, job
    answer = answer or (lambda u, t: [drv.do(u, t)])
    flags, refusals = [], []
    if before_batch:
        before_batch(0)
    drv.token = job.claim(test.conn, job_id)
    u = job.next_unit(test.conn, drv.token)
    for _ in range(20000):
        if u["unit"] == "complete":
            return flags, u, refusals
        if u["unit"] == "end-batch":
            flags.append(u["progress"]["progressed"])
            if len(flags) >= batches_max:
                return flags, u, refusals
            if before_batch:
                before_batch(len(flags))
            drv.token = job.claim(test.conn, job_id)
            u = job.next_unit(test.conn, drv.token)
            continue
        if u["unit"] == "judge":
            for judged in answer(u, drv.token):
                try:
                    u = job.next_unit(test.conn, drv.token, judged=judged)
                    break
                except db.Refusal as e:
                    refusals.append(str(e))
            else:
                raise AssertionError(f"every answer was refused: {refusals[-3:]}")
            continue
        drv.do(u, drv.token)
        u = job.next_unit(test.conn, drv.token)
    raise AssertionError("the loop never ended a batch")


def misreported(drv):
    """The judge answers every page — its handed-over documents' verdicts included — but
    the last page's answer says triage was left: the judgment never comes out whole, so a
    handover is never covered and the judgment is started again, forever (an UNBOUNDED
    cause: the judge epoch stays)."""
    def answer(u, token):
        out = drv.do(u, token)
        if out["page_next"] is None:
            out["triage_remaining"] = 1
        return [out]
    return answer


def upsert(**args):
    """upsert_counterparty, as the registered tool takes it (its `name` argument)."""
    import qa_server
    return qa_server.TOOLS["upsert_counterparty"]["fn"](args)


def credits(test, prefix=""):
    return test.conn.execute("SELECT count(*) FROM credits WHERE substr(key, 1, ?)=?",
                             (len(prefix), prefix)).fetchone()[0]


# --- (T) termination ---------------------------------------------------------------------
class NeverWhole(StoreCase):
    """A judgment that never comes out whole, with a handover it never covers: restarted
    for an unbounded cause, it re-judges pages already credited (the epoch stays), so
    after its first round nothing more is earned and Casa's guard ends the job. The old
    rule needed the high-water mark for this (final review FW-I1; diff r1)."""

    def setUp(self):
        super().setUp()
        self.bind()

    def run_one(self, payments, batches_max=10):
        import asks
        drv = JobDriver(self, payments=payments)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        flags, last, _ = run_batches(self, drv, answer=misreported(drv),
                                     batches_max=batches_max)
        self.assertEqual(last["unit"], "end-batch")
        self.assertTrue(flags[0])                       # the bank read, the searches
        self.assertTrue(_three_false(flags), flags)
        return flags

    def test_a_never_covered_handover_one_page(self):
        self.run_one(2)

    def test_a_never_whole_judgment_of_three_pages(self):
        """Three triage pages, two judge units a batch: counted per judgment, a batch
        reached past its start every third batch (the coordinator's ruling's case). Its
        pages' keys repeat in one epoch: never progress again once each start is earned."""
        flags = self.run_one(20, batches_max=12)
        tail = len(flags) - 1 - max(i for i, f in enumerate(flags) if f)
        self.assertGreaterEqual(tail, 6, flags)

    def test_the_epoch_stays_on_an_unbounded_restart(self):
        import asks, job
        drv = JobDriver(self, payments=2)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        run_batches(self, drv, answer=misreported(drv), batches_max=6)
        p = job.live_job_pass(self.conn)
        self.assertEqual(p["judge_epoch"], 1)           # the first judgment only
        self.assertEqual(credits(self, "judge:"), 1)    # judge:1:0, earned once

    def test_a_handover_taken_earns_fresh_credit_then_settles_again(self):
        """Taking a handover is a bounded restart: the epoch moves on, so the loop's
        judgment, started again for the new documents, earns its pages again."""
        import asks
        drv = JobDriver(self, payments=20)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        flags, _, _ = run_batches(self, drv, answer=misreported(drv), batches_max=10)
        self.assertTrue(_three_false(flags), flags)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        flags, last, _ = run_batches(self, drv, answer=misreported(drv), batches_max=8)
        self.assertEqual(last["unit"], "end-batch")
        self.assertTrue(flags[0], flags)                # fresh credit for the new ask
        self.assertTrue(_three_false(flags), flags)     # and it settles again


class JudgeKeys(StoreCase):
    """`judge:<epoch>:<after>`: `page_next` is validated before anything is stored —
    `[pid]`, an existing payment, past the page's start (design r3, Astra and Terra S1:
    a model-authored cursor minted credits forever)."""

    def setUp(self):
        super().setUp()
        self.bind()

    def state(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT p.judge_after, p.judge_epoch, s.finished_at, s.started_seq,"
            " (SELECT count(*) FROM credits), (SELECT group_concat(verdicts_json) FROM"
            " work_requests) FROM passes p JOIN pass_steps s ON s.pass_id=p.pass_id AND"
            " s.step='judge'")]

    def test_free_values_are_refused_and_nothing_is_stored(self):
        import asks, db, job, views
        drv = JobDriver(self, payments=10)              # two pages: page 1's next is [8]
        doc = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        u = drv.run_until(A, "judge")
        t = drv.token
        good = drv.do(u, t)
        self.assertEqual(len(good["page_next"]), 1)
        top = self.conn.execute("SELECT max(pid) FROM projections").fetchone()[0]
        before = self.state()
        for bad in ([top + 1000], [0], [-3], [True], ["8"], good["page_next"] * 2,
                    {"pid": good["page_next"][0]}, "next", 7):
            with self.assertRaises(db.Refusal) as cm:
                job.next_unit(self.conn, t, judged={**good, "page_next": bad})
            text = str(cm.exception)
            self.assertIn("page_next", text)
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text)
            self.assertEqual(self.state(), before)      # nothing written
        u2 = job.next_unit(self.conn, t, judged=good)   # the page's own `next`: taken
        self.assertEqual(u2["after"], good["page_next"])
        with self.assertRaises(db.Refusal):             # not past the new start
            job.next_unit(self.conn, t, judged={**drv.do(u2, t), "page_next": good["page_next"]})

    def test_free_page_next_values_never_keep_a_judgment_going(self):
        """Terra and Astra r3: one payment, a worker answering page_next=[serial] with a new
        serial each time. Refused, it answers its page honestly — a judgment never whole —
        and the job settles."""
        import asks
        drv = JobDriver(self, payments=1)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        serial = [1000000]

        def answer(u, token):
            honest = misreported(drv)(u, token)[0]
            serial[0] += 1
            return [{**honest, "page_next": [serial[0]], "triage_remaining": 1}, honest]
        flags, last, refusals = run_batches(self, drv, answer=answer, batches_max=12)
        self.assertTrue(refusals)
        self.assertEqual(last["unit"], "end-batch")
        self.assertTrue(_three_false(flags), flags)

    def test_resubmitting_page_one_earns_nothing(self):
        """Astra r2: 16 payments, a worker that lists the first page again and again and
        submits its `next`, echoing the unit. The resubmission is refused (not past the
        start); answered honestly, the judgment's page keys repeat in one epoch."""
        import asks, work
        drv = JobDriver(self, payments=16)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])

        def answer(u, token):
            first = work.list_quarter_state(self.conn, triage_only=True, limit=8)
            again = {"judgment": u["judgment"], "after": u["after"],
                     "page_next": first["next"], "triage_remaining": first["remaining"],
                     "documents": {}}
            return [again] + misreported(drv)(u, token)
        flags, last, refusals = run_batches(self, drv, answer=answer, batches_max=12)
        self.assertTrue(refusals)
        self.assertTrue(_three_false(flags), flags)


class HolderChanges(StoreCase):
    """The adoption budget counts holder changes, a return of an earlier holder too
    (design r1, Astra S2: A→B→A claims made 30 bank reads in one pass while `adoptions`
    stayed 1)."""

    def test_a_b_a_returns_spend_the_budget_and_the_third_change_stops_the_pass(self):
        import asks, db, job
        self.bind()
        drv = JobDriver(self, payments=24)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        drv.run_until(A, "judge")
        pid = job.live_job_pass(self.conn)["pass_id"]
        real, holders = db._clock, []
        try:
            for n in range(12):
                later = real() + datetime.timedelta(seconds=job.W_S * (n + 2))
                db._clock = lambda later=later: later
                who = (B, A)[n % 2]
                token = job.claim(self.conn, who)
                holders.append(who)
                u = job.next_unit(self.conn, token)
                while u["unit"] not in ("end-batch", "complete"):
                    if u["unit"] == "judge":
                        u = job.next_unit(self.conn, token,
                                          judged=misreported(drv)(u, token)[0])
                    else:
                        drv.do(u, token)
                        u = job.next_unit(self.conn, token)
                if job.live_job_pass(self.conn) is None:
                    break
        finally:
            db._clock = real
        p = self.conn.execute("SELECT * FROM passes WHERE pass_id=?", (pid,)).fetchone()
        self.assertEqual(holders, [B, A, B])            # A→B, B→A, and A→B is the third
        self.assertEqual(p["adoptions"], job.ADOPTIONS_MAX)
        self.assertEqual(p["outcome"], "stopped")
        self.assertLessEqual(self.conn.execute("SELECT count(*) FROM snapshots WHERE"
                                               " pass_id=?", (pid,)).fetchone()[0],
                             1 + job.ADOPTIONS_MAX + job.W_REFRESH_MAX)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        # S7 §5: the job posted the result and its receipt was marked
        self.assertEqual((r["state"], r["outcome"]), ("reported", "stopped"))


class ReopenedForever(Tools):
    """The K_STATES cap: an operator who re-opens every row again and again — each time to
    a state never seen (a new invoice link for the vendor) — keeps the job's sweep busy,
    and each settled state earns credit only until K_STATES of them per payment and
    acquisition; then the job settles (design r2, Terra S1: an uncapped generation)."""

    def do(self, u):
        if u["unit"] != "judge":
            return super().do(u)
        t = u["pass_token"]
        page = self.call("list_quarter_state", pass_token=t, triage=True, limit=8,
                         quarter=u["quarter"], after=u["after"])
        return self.call("job_next", pass_token=t, judged={
            "judgment": u["judgment"], "after": u["after"], "page_next": page["next"],
            "triage_remaining": page["remaining"] if page["next"] else 1,
            "documents": {str(i): "no-payment-yet" for i in u["documents_first"]}})

    def test_an_operator_reopening_forever_settles_at_the_cap(self):
        import job
        doc = self.doc()
        self.rows = [{"row_id": i, "tags": ["software"], "amount_minor": 10000 + i}
                     for i in (1, 2)]
        self.bank = {i: {"tags": ["software"], "notes": [], "rev": 0} for i in (1, 2)}
        self.call("request_work", kind="handover", trigger="operator", doc_ids=[doc])
        u = self.call("job_next", job_id=A)
        flags = []
        for n in range(3 * job.K_STATES):
            while u["unit"] != "end-batch":
                u = self.do(u)
            flags.append(u["progress"]["progressed"])
            if _three_false(flags):
                break
            # the operator, through Ellen (no pass_token): a new link for the vendor
            upsert(name="Adobe", patterns=["Adobe"],
                      document_link=f"https://example.invalid/invoices/{n}")
            u = self.call("job_next", job_id=A)
        self.assertTrue(_three_false(flags), flags)
        self.assertGreater(flags.count(True), 20)       # every new state earned, up to the cap
        p = job.live_job_pass(self.conn)
        for pid in (1, 2):
            pre = f"sweep:{job._acq(self.conn, p)}:{pid}:"
            self.assertEqual(credits(self, pre), job.K_STATES)


class UnboundedAsks(StoreCase):
    """Requests keep coming while the job runs. A live pass takes at most LATE_TAKES_MAX
    of them (design r1, Terra S1); one Casa job run begins at most MAX_PASSES_PER_JOB
    passes, then completes, and the next start takes the rest (design r4, Terra S1; r5;
    S7 §4.2: nothing restarts a job)."""

    def setUp(self):
        super().setUp()
        self.bind()

    def test_late_asks_into_a_live_pass_are_bounded(self):
        import asks, job
        drv = JobDriver(self, payments=2)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        flags, last, _ = run_batches(
            self, drv, answer=misreported(drv), batches_max=14,
            before_batch=lambda n: asks.request_work(self.conn, "check", "operator"))
        p = job.live_job_pass(self.conn)
        self.assertEqual(p["late_takes"], job.LATE_TAKES_MAX)
        self.assertGreater(self.conn.execute("SELECT count(*) FROM work_requests WHERE"
                                             " state='queued'").fetchone()[0], 0)
        self.assertLessEqual(self.conn.execute("SELECT count(*) FROM snapshots WHERE"
                                               " pass_id=?", (p["pass_id"],)).fetchone()[0],
                             1 + job.LATE_TAKES_MAX)
        self.assertTrue(_three_false(flags), flags)

    def test_a_run_begins_at_most_four_passes_then_the_next_job_takes_the_rest(self):
        import asks, job

        class Asking(JobDriver):
            def _gmail_probe(self, u, token):           # Ellen records three asks meanwhile
                for _ in range(3):
                    asks.request_work(self.conn, "check", "operator")
                return super()._gmail_probe(u, token)
        drv = Asking(self, payments=2)
        asks.request_work(self.conn, "check", "operator")
        flags, last, _ = run_batches(self, drv, batches_max=60)
        self.assertEqual(last["unit"], "complete")
        self.assertEqual(job.run_passes(self.conn, A), job.MAX_PASSES_PER_JOB)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0],
                         job.MAX_PASSES_PER_JOB)
        queued = self.conn.execute("SELECT count(*) FROM work_requests WHERE"
                                   " state='queued'").fetchone()[0]
        self.assertGreater(queued, 0)
        self.assertTrue(_no_three_false(flags), flags)
        # ONE completion predicate: job_status says done exactly when job_next completes
        self.assertEqual(job.status(self.conn, A),
                         {"done": True, "text": "Accounting work finished."})
        self.assertFalse(job.status(self.conn, B)["done"])     # a fresh run has budget
        # S7 §4.2: nothing restarts; the next start takes what was queued
        t = job.claim(self.conn, B)
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "probes")
        self.assertEqual(job.run_passes(self.conn, B), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests WHERE"
                                           " state='queued'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests WHERE"
                                           " state='taken'").fetchone()[0], queued)

    def test_job_status_done_is_job_next_complete(self):
        import asks, job
        drv = JobDriver(self, payments=2)
        asks.request_work(self.conn, "check", "operator")
        self.assertFalse(job.status(self.conn, A)["done"])
        drv.run_until(A, "sweep")
        self.assertFalse(job.status(self.conn, A)["done"])         # a pass is live
        drv.next_until(drv.token, "complete")
        self.assertTrue(job.status(self.conn, A)["done"])
        asks.request_work(self.conn, "check", "operator")
        self.assertFalse(job.status(self.conn, A)["done"])         # budget left: not done
        import db
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET passes=? WHERE job_id=?",
                              (job.MAX_PASSES_PER_JOB, A))
        self.assertFalse(job.status(self.conn, A)["done"])         # S7 §4.2: the left line
        t = job.claim(self.conn, A)
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "post")
        import views
        views.mark_rendering_delivered(self.conn, u["render_ids"][0])
        self.assertTrue(job.status(self.conn, A)["done"])          # budget spent: done
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")


class ManyPasses(StoreCase):
    """A batch spans several passes (design r1, Astra S2): sixteen empty-quarter package
    asks; the progress query looks for credits in any pass, so passes that ended inside
    the batch count."""

    def test_sixteen_empty_package_rounds(self):
        import asks, job
        self.bind()
        drv = JobDriver(self, payments=0)
        for n in range(16):
            asks.request_package(self.conn, f"{2023 + n // 4}-Q{n % 4 + 1}")
        runs, spanning = [], 0
        for k in range(8):
            job_id = f"{k + 1:08x}-{k + 1}"
            passes_before = self.conn.execute("SELECT count(*) FROM passes").fetchone()[0]
            flags, last, _ = run_batches(self, drv, job_id=job_id, batches_max=30)
            runs.append(flags)
            self.assertEqual(last["unit"], "complete", flags)
            # every batch ends a package round and starts the next: credits earned in a
            # pass that ended inside the batch count (no batch here is without them)
            self.assertTrue(all(flags), (k, flags))
            self.assertLessEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0]
                                 - passes_before, job.MAX_PASSES_PER_JOB)
            if not self.conn.execute("SELECT 1 FROM package_requests WHERE state='queued'"
                                     ).fetchone():
                break
        # S7 §6.1: each is built and posted by the run that checked it
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='delivered'").fetchone()[0], 16)
        self.assertEqual(len(runs), 4)                  # four passes per run


# --- (L) liveness: work that creates work ---------------------------------------------
class SweepLiveness(Tools):
    def batch(self, u):
        units = []
        while u["unit"] != "end-batch":
            units.append(u["unit"])
            u = self.do(u)
        return u["progress"]["progressed"], units, u

    def settled(self):
        return self.conn.execute("SELECT count(*) FROM projections WHERE observed_revision"
                                 " IS NOT NULL").fetchone()[0]

    def test_sweep_discovered_classification(self):
        """Diff r4 (Astra S2): a package of 400 payments classified only after the export;
        each swept row becomes an item to search, so the old rule's higher component rose
        as fast as the sweep's fell: [F, F, F] while 50 → 120 → 190 rows settled."""
        self.rows = [{"row_id": i, "amount_minor": 10000 + i} for i in range(1, 401)]
        self.bank = {i: {"tags": [], "notes": [], "rev": 0} for i in range(1, 401)}
        self.call("request_package", quarter="2026-Q3")
        u = self.do(self.do(self.call("job_next", job_id=A)))      # probes, snapshot
        for b in self.bank.values():
            b["tags"], b["rev"] = ["software"], 1                   # classified meanwhile
        flags, settled = [], []
        for n in range(3):
            flag, _, u = self.batch(u)
            flags.append(flag)
            settled.append(self.settled())
            u = self.call("job_next", job_id=A)
        self.assertEqual(settled, sorted(set(settled)))
        self.assertEqual(flags, [True, True, True], settled)

    def sweep_then_reopen(self, n, reopen):
        """n payments swept, then `reopen()` (inside the batch that reached the Gmail
        probe); that batch is finished, then three fresh batches. Returns their flags,
        units and the rows settled in each."""
        import sweep
        u = self.until(self.start(n), "gmail-probe")
        reopen(u["pass_token"])
        self.assertEqual(len(sweep._due(self.conn)), n)
        _, _, u = self.batch(self.call("job_next", pass_token=u["pass_token"]))
        out = []
        for _ in range(3):
            due = len(sweep._due(self.conn))
            flag, units, u = self.batch(self.call("job_next", job_id=A))
            out.append((flag, set(units), due - len(sweep._due(self.conn))))
        return out

    def test_an_operator_stop_chasing_reopening_every_row(self):
        """Design r1 (Astra S1): one operator `stop_chasing` makes 400 rows due again
        under the same acquisition; the job's re-settlement earns, by the rows' new
        states."""
        import work         # S7 §8.1: the operator's tap, under its grant
        out = self.sweep_then_reopen(400, lambda t: self.granted(work.stop_chasing_in_tx,
                                                                 "2026-Q3"))
        for flag, units, settled in out:
            self.assertEqual(units, {"sweep"}, out)
            self.assertGreater(settled, 0, out)
            self.assertTrue(flag, out)

    def test_the_jobs_own_enrichment_three_times(self):
        """Design r2/r3/r4: the job's own upsert_counterparty — a canonical identity, a
        document link, a portal — each re-opens every row with its desired tags unchanged.
        Keyed by the projection's full state (`projections.digest`), each re-settlement
        earns; keyed by the desired tags it earned nothing."""
        import sweep
        changes = [{"name": "Adobe Incorporated", "patterns": ["Adobe"]},
                   {"name": "Adobe Incorporated", "document_link": "https://example.invalid/i"},
                   {"name": "Adobe Incorporated", "source": "portal"}]
        u = self.until(self.start(220), "gmail-probe")
        for change in changes:
            upsert(pass_token=u["pass_token"], **change)
            self.assertEqual(len(sweep._due(self.conn)), 220, change)
            _, _, u = self.batch(self.call("job_next", pass_token=u["pass_token"]))
            for _ in range(2):                          # 70 + 70 of 220: sweep-only batches
                flag, units, u = self.batch(self.call("job_next", job_id=A))
                self.assertEqual(set(units), {"sweep"}, change)
                self.assertTrue(flag, change)
            u = self.call("job_next", job_id=A)
            while sweep._due(self.conn):                # the rest of the re-sweep
                u = self.call("job_next", job_id=A) if u["unit"] == "end-batch" \
                    else self.do(u)

    def test_a_refresh_omitting_every_row_confirms_erasures(self):
        """Design r4 (Astra S1): a W refresh whose export no longer holds 400 imported
        rows; each confirmed erasure earns `erased` (the read-back predicate never holds
        for an erased lineage)."""
        import db, job, sweep
        u = self.until(self.start(400), "gmail-probe")
        later = db._clock() + datetime.timedelta(seconds=job.W_S + 1)
        with self.patch_clock(later):
            self.rows = []
            u = self.call("job_next", pass_token=u["pass_token"])
            while u["unit"] != "end-batch":
                u = self.do(u)
            flags, confirmed = [], []
            for _ in range(3):
                u = self.call("job_next", job_id=A)
                n, units = len(sweep._due(self.conn)), []
                while u["unit"] != "end-batch":
                    units.append(u["unit"])
                    if u["unit"] == "sweep":
                        t = u["pass_token"]
                        page = self.call("list_projections", pass_token=t, limit=10,
                                         quarter=u["quarter"])
                        for it in page["projections"]:
                            self.call("record_observation", pid=it["pid"], pass_token=t,
                                      snapshot_id=page["snapshot_id"], not_found=True)
                        u = self.call("job_next", pass_token=t)
                    else:
                        u = self.do(u)
                flags.append(u["progress"]["progressed"])
                confirmed.append(n - len(sweep._due(self.conn)))
        self.assertTrue(all(c > 0 for c in confirmed), confirmed)
        self.assertEqual(flags, [True, True, True], confirmed)
        erased = self.conn.execute("SELECT count(*) FROM projections WHERE ended='erased'"
                                   ).fetchone()[0]
        self.assertEqual(erased, sum(confirmed))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM credits WHERE key LIKE"
                                           " '%:erased'").fetchone()[0], erased)

    def test_a_refused_write_earns_refused(self):
        import job
        u = self.until(self.start(1), "sweep")
        t = u["pass_token"]
        page = self.call("list_projections", pass_token=t, limit=10)
        pid = page["projections"][0]["pid"]
        out = self.call("record_observation", pid=pid, pass_token=t,
                        snapshot_id=page["snapshot_id"], observed_tags=["software"],
                        observed_notes=[], observed_first_seen="2026-07-03T08:00:00Z",
                        observed_tag_revision=0)
        self.assertTrue(out["read_back"])
        self.assertEqual(credits(self, "sweep:"), 0)    # a write owed: not settled yet
        self.call("record_observation", pid=pid, pass_token=t,
                  snapshot_id=page["snapshot_id"], write_error="tag budget full")
        acq = job._acq(self.conn, job.live_job_pass(self.conn))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT key FROM credits")],
                         [f"sweep:{acq}:{pid}:refused"])


class Credits(Tools):
    """What earns, and what never does."""

    def test_an_import_alone_earns_nothing_whatever_it_requeues(self):
        """Diff r3 (R8), on S7's units (§6.1): an import that revokes a staged first send
        puts its package ask back in the queue; it earns nothing and loses nothing — there
        is no baseline."""
        self.call("request_package", quarter="2026-Q3")
        u = self.until(self.call("job_next", job_id=A), "deliver")
        self.call("stage_for_delivery", package_id=u["package_id"],
                  package_token=u["package_token"])     # staged, never sent
        self.call("request_work", kind="check", trigger="cron")
        u = self.until(self.call("job_next", job_id=A), "snapshot")
        before = credits(self)
        queued = self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                   " state='queued'").fetchone()[0]
        u = self.do(u)                                  # the import: the send is revoked
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], queued + 1)
        self.assertEqual(credits(self), before)
        self.assertFalse(u["progress"]["progressed"])

    def test_a_write_outside_the_job_earns_nothing(self):
        u = self.until(self.start(2), "gmail-probe")
        before = credits(self)
        import work
        self.granted(work.stop_chasing_in_tx, "2026-Q3")    # the operator's tap (S7 §8.1)
        self.call("request_package", quarter="2026-Q2")
        upsert(name="Adobe", source="portal")
        self.assertEqual(credits(self), before)

    def test_every_key_is_under_the_claim_that_earned_it(self):
        self.until(self.start(2), "complete")
        keys = {r["key"].split(":")[0] for r in self.conn.execute("SELECT key FROM credits")}
        # simple loop Task 4: a recorded search earns no credit (job.credit_search deleted;
        # claims.progressed carries it, tests/test_decide.py)
        self.assertTrue({"sweep", "judge", "file", "req"} <= keys, keys)
        self.assertNotIn("search", keys)
        bad = self.conn.execute("SELECT count(*) FROM credits WHERE gen NOT IN (SELECT gen"
                                " FROM claims)").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_a_package_request_earns_at_its_job_side_end(self):
        self.call("request_package", quarter="2026-Q3")
        self.until(self.call("job_next", job_id=A), "complete")
        rid = self.conn.execute("SELECT request_id, state FROM package_requests").fetchone()
        # S7 §6.1: the job builds and posts it too, each earning its own credit
        self.assertEqual(rid["state"], "delivered")
        n = rid["request_id"]
        self.assertEqual({r[0] for r in self.conn.execute(
            "SELECT key FROM credits WHERE key LIKE 'req:pkg:%'")},
            {f"req:pkg:{n}", f"req:pkg:{n}:built", f"req:pkg:{n}:delivered"})


class BatchIdentity(StoreCase):
    """claims.batch (design round 5, Astra S2): a claim starts a new batch iff it is its
    job id's first, or that job id's latest claim's batch was answered `end-batch` or
    `complete`, or the live pass's holder changes; otherwise it inherits the batch. A
    credit counts for the batch whose claims earned it, and never again."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=40)

    def batch(self, t):
        return self.conn.execute("SELECT batch FROM claims WHERE gen=?", (t,)).fetchone()[0]

    def test_a_reclaim_inside_a_batch_keeps_its_batch_budget_and_progress(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        u = self.drv.run_until(A, "sweep")
        t1 = self.drv.token
        self.drv.do(u, t1)
        self.assertTrue(job.next_unit(self.conn, t1)["progress"]["progressed"])
        t2 = job.claim(self.conn, A)                    # a refused turn's re-claim
        self.assertEqual(self.batch(t2), self.batch(t1))
        u = job.next_unit(self.conn, t2)
        self.assertTrue(u["progress"]["progressed"])    # the batch's earlier credits
        spent = self.conn.execute("SELECT sum(spent) FROM claims WHERE batch=?",
                                  (self.batch(t1),)).fetchone()[0]
        self.assertGreater(spent, job.UNIT_COST[u["unit"]])

    def test_after_end_batch_old_credits_never_count_again(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "end-batch")
        t1 = self.drv.token
        self.assertTrue(self.drv.last["progress"]["progressed"])
        t2 = job.claim(self.conn, A)
        self.assertEqual(self.batch(t2), t2)
        self.assertNotEqual(self.batch(t2), self.batch(t1))
        u = job.next_unit(self.conn, t2)
        self.assertFalse(u["progress"]["progressed"])   # nothing earned in it yet

    def test_a_holder_change_starts_a_new_batch(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")
        tB = job.claim(self.conn, B)
        self.assertEqual(self.batch(tB), tB)
        tB2 = job.claim(self.conn, B)                   # B's re-claim: B's batch
        self.assertEqual(self.batch(tB2), tB)


class CutBatches(StoreCase):
    """The coordinator's ruling on concern 1: a batch also closes once it holds
    turnsPerBatch claims. A Casa batch cut by max_turns before it is answered `end-batch`
    is merged with the next one's claims; the window still closes, so a pass that never
    finishes still reaches three consecutive no-progress reports."""

    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_never_finishing_pass_with_every_batch_cut_still_settles(self):
        """Under sim_job: a judgment that never comes out whole, every Casa batch one judge
        unit and then cut — an `end-batch` answer, if one comes, is never consumed (the
        turn has already ended). Each batch's report is the last answer it received."""
        import asks, job
        drv = JobDriver(self, payments=2)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        drv.run_until(A, "judge")
        reports = []
        for _ in range(3 * job.TURNS_PER_BATCH):
            t = job.claim(self.conn, A)                 # the Casa batch's one turn
            u = job.next_unit(self.conn, t)
            if u["unit"] == "judge":
                u = job.next_unit(self.conn, t, judged=misreported(drv)(u, t)[0])
            elif u["unit"] not in ("end-batch", "complete"):
                drv.do(u, t)
                u = job.next_unit(self.conn, t)
            reports.append(u["progress"]["progressed"])  # then cut, whatever u is
            if _three_false(reports):
                break
        self.assertTrue(_three_false(reports), reports)
        self.assertIsNotNone(job.live_job_pass(self.conn))   # the pass never finished

    def test_a_window_closes_at_turns_per_batch_claims(self):
        """Claims that charge the batch nothing (a turn ended between its claim and its
        first unit) close no window by the budget; the claim count does."""
        import asks, job
        drv = JobDriver(self, payments=2)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        drv.run_until(A, "judge")
        first = job.claim(self.conn, A)                 # opens a window: the last run's
        batch = lambda t: self.conn.execute("SELECT batch FROM claims WHERE gen=?",
                                            (t,)).fetchone()[0]
        window = batch(first)
        n = self.conn.execute("SELECT count(*) FROM claims WHERE batch=?",
                              (window,)).fetchone()[0]
        tokens = [job.claim(self.conn, A) for _ in range(job.TURNS_PER_BATCH - n)]
        self.assertEqual({batch(t) for t in tokens}, {window})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM claims WHERE batch=?",
                                           (window,)).fetchone()[0], job.TURNS_PER_BATCH)
        nxt = job.claim(self.conn, A)                   # the window is full: a new batch
        self.assertEqual(batch(nxt), nxt)
        u = job.next_unit(self.conn, nxt)
        self.assertFalse(u["progress"]["progressed"])   # the window's credits stay there
