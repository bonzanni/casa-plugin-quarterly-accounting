"""S2 diff review round 3, R8 (INV-J8): every rise of a measure component is either a
fall's opposite inside the job's own unit or work added through job.work_added. A
request Ellen records while a batch runs (cause `request`) and any other write made
outside the job (cause `outside`, routed at the tool layer through db.tx_hook) move the
running batch's baseline by exactly what they changed — no credit, no loss. Astra r3
S2: a package ask enqueued during each of three batches made [F, F, F] while rows
settled 0 → 50 → 120 → 190."""
from tests._base import StoreCase
from tests.test_s2_diff_r1 import A, Tools


class MidBatchAsks(Tools):
    def run_with_asks(self, ask):
        """Astra's reproduction: a check of 400 payments; an ask recorded at the start
        of each of three batches. Returns the end-batch flags and the settled counts."""
        u = self.start(400)
        flags, settled = [], []
        for batch in range(3):
            ask(batch)
            while u["unit"] != "end-batch":
                u = self.do(u)
            flags.append(u["progress"]["progressed"])
            settled.append(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                             " observed_revision IS NOT NULL").fetchone()[0])
            if batch < 2:
                u = self.call("job_next", job_id=A)
        return flags, settled

    def test_package_asks_mid_batch_keep_progress(self):
        flags, settled = self.run_with_asks(
            lambda b: self.call("request_package", quarter=f"2026-Q{b + 1}",
                                channel="telegram"))
        self.assertEqual(settled, sorted(set(settled)))       # rows settled every batch
        self.assertGreater(settled[0], 0)
        self.assertEqual(flags, [True, True, True], settled)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], 3)

    def test_check_asks_mid_batch_keep_progress(self):
        """A check ask taken by the live pass forces a refresh each time (F condition 2),
        so its rows are read again: still never three batches without progress."""
        flags, settled = self.run_with_asks(
            lambda b: self.call("request_work", kind="check", trigger="operator"))
        self.assertNotEqual(flags, [False, False, False], settled)
        self.assertTrue(flags[0], (flags, settled))

    def test_an_ask_with_no_live_job_pass_moves_nothing(self):
        import job
        self.call("request_work", kind="check", trigger="cron")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM claims").fetchone()[0], 0)
        u = self.call("job_next", job_id=A)                   # the claim stamps it
        self.assertEqual(u["unit"], "probes")
        import db
        with db.tx(self.conn):
            self.assertEqual(job.current_batch(self.conn), u["pass_token"])


class OutsideWrites(Tools):
    def test_a_tool_call_without_a_pass_token_is_routed(self):
        """The tool layer routes every write of a call made without a pass_token (the
        operator's decisions through Ellen, her package flow): a queued ask written by
        such a call moves the batch's baseline, so it is neither credit nor loss; the
        same write under the job's pass_token is the job's own and is not routed."""
        import db, qa_server, tools

        def write(args):
            with db.tx(tools.conn()):
                tools.conn().execute(
                    "INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                    " created_at, state) VALUES ('check', 'cron', '[]', 0, ?, 'queued')",
                    (db.now(),))
            return {}
        tools.register("zz_outside_write", "test only", {"type": "object"})(write)
        self.addCleanup(qa_server.TOOLS.pop, "zz_outside_write", None)
        u = self.until(self.start(2), "sweep")
        tok = u["pass_token"]

        def base():
            return json.loads(self.conn.execute("SELECT measure_json FROM claims WHERE gen=?",
                                                (tok,)).fetchone()[0])
        before = base()
        self.call("zz_outside_write")
        self.assertEqual(base(), [before[0] + 1] + before[1:])
        self.call("zz_outside_write", pass_token=tok)
        self.assertEqual(base(), [before[0] + 1] + before[1:])

    def test_the_hook_measures_inside_each_transaction_of_the_store_only(self):
        import db, job, sqlite3
        u = self.until(self.start(2), "sweep")
        tok = u["pass_token"]
        base = json.loads(self.conn.execute("SELECT measure_json FROM claims WHERE gen=?",
                                            (tok,)).fetchone()[0])
        other = sqlite3.connect(":memory:", isolation_level=None)
        other.execute("CREATE TABLE t(x)")
        with job.outside_writes(lambda c: c is self.conn):
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json,"
                                  " created_seq, created_at, state) VALUES ('check', 'cron',"
                                  " '[]', 0, ?, 'queued')", (db.now(),))
            with db.tx(other):                                 # another database: untouched
                other.execute("INSERT INTO t VALUES (1)")
        after = json.loads(self.conn.execute("SELECT measure_json FROM claims WHERE gen=?",
                                             (tok,)).fetchone()[0])
        self.assertEqual(after, [base[0] + 1] + base[1:])


import json  # noqa: E402


class ImportRequeue(Tools):
    def test_a_revoked_first_send_requeued_by_an_import_is_part_of_the_import(self):
        """An import that revokes a staged first send puts its package ask back in the
        queue (component 0 rises). The hook runs at the import's end, so that rise is
        the import's — moved into the baseline — and never a loss for the batch."""
        import db, job
        self.call("request_package", quarter="2026-Q3", channel="telegram")
        self.until(self.call("job_next", job_id=A), "complete")
        tok = self.call("job_report", job_id=A, status="ok")["continue"]["package_token"]
        pkg = self.call("build_quarterly_package", quarter="2026-Q3", package_token=tok)
        self.call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                  package_token=tok)                      # staged, never sent
        self.call("request_work", kind="check", trigger="cron")
        u = self.call("job_next", job_id=A)
        u = self.do(u)                                    # probes
        self.assertEqual(u["unit"], "snapshot")
        queued = self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                   " state='queued'").fetchone()[0]
        self.do(u)                                        # the import: the send is revoked
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], queued + 1)
        base = json.loads(self.conn.execute("SELECT measure_json FROM claims WHERE gen=?",
                                            (u["pass_token"],)).fetchone()[0])
        with db.tx(self.conn):
            now = job.measure(self.conn)
        self.assertEqual(now[0], base[0])                 # the requeue: no loss, no credit
