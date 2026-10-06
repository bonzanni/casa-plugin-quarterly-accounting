"""S2 diff review round 1 (R1, R3, R4): a large check never reports three batches without
progress; informational pages never take the operator's reply; a ledger instance switched
mid-pass after the reset acknowledgement stops the pass.

The relay tests drive the registered tools (qa_server.TOOLS) against a synthetic bank,
as the round's reproductions did (Astra, /tmp/s2-review-XQEBYB/review_repro.py)."""
import datetime

from tests._base import StoreCase, apply_now
from tests.sim_job import JobDriver

A, B = "aaaaaaaa-1", "bbbbbbbb-2"
OTHER = "b" * 32


class LargeCheck(StoreCase):
    """R1, simple loop Task 10: a run of many batches never reports three batches in a row
    without progress (Casa ends a job after three): every vendor batch persists a search or
    a decision (claims.progressed)."""

    def test_eighty_payments_never_three_no_progress_batches(self):
        import asks, job
        self.bind()
        drv = JobDriver(self, payments=0)
        for i in range(80):
            drv.add_payments(["2026-08-05"], counterparty=f"Vendor {i:02d}")
        asks.request_work(self.conn, "check", "cron")
        units = drv.run_job(A, started_by="scheduled")
        flags = [u["progress"]["progressed"] for u in units if u["unit"] == "end-batch"]
        self.assertGreaterEqual(len(flags), 3, flags)    # several batches
        for i in range(len(flags) - 2):
            self.assertNotEqual(flags[i:i + 3], [False] * 3, flags)
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_work WHERE outcome IS"
                                           " NULL").fetchone()[0], 0)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("reported", "complete"))


class Tools(StoreCase):
    """The registered tools, a synthetic bank of `n` classified payments."""

    def setUp(self):
        super().setUp()
        import tools
        self.bind()
        tools._CONN = self.conn
        self.addCleanup(setattr, tools, "_CONN", None)
        self.rows, self.bank, self.instance = [], {}, self.LEDGER
        self.handed = []                # S7 §5: the post/view units, their receipts withheld

    def call(self, name, **kw):
        import qa_server
        return qa_server.TOOLS[name]["fn"](kw)

    def start(self, n):
        self.rows = [{"row_id": i, "tags": ["software"], "amount_minor": 10000 + i}
                     for i in range(1, n + 1)]
        self.bank = {i: {"tags": ["software"], "notes": [], "rev": 0} for i in range(1, n + 1)}
        self.call("request_work", kind="check", trigger="operator")
        return self.call("job_next", job_id=A)

    def do(self, u):
        t, k = u["pass_token"], u["unit"]
        if k == "probes":
            for kind, data in (("bank_tools", {}),
                               ("bank_accounts", {"accounts": [{"account_id": "acc-biz",
                                                                "category": "company",
                                                                "label": "Zakelijk"}]}),
                               ("bank_sync", {}),
                               ("ledger", {"generation": 0, "instance": self.instance,
                                           "registered": {}, "missing": []})):
                self.call("record_probe", pass_token=t, kind=kind, ok=True, data=data,
                          **({"acq": u["acq"]} if kind == "bank_sync" else {}))
        elif k == "snapshot":
            rows = [dict(r, tags=self.bank[r["row_id"]]["tags"],
                         tag_revision=self.bank[r["row_id"]]["rev"]) for r in self.rows]
            self.call("import_ledger_export", path=self.export_csv(rows), pass_token=t,
                      ledger_instance=self.instance, acq=u["acq"])
        elif k == "filing":
            self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        elif k == "vendor":                 # simple loop Task 10: nothing found, missing
            self.call("decide", pass_token=t, entries=[
                {"pid": x["pid"], "outcome": "missing", "expected_revision": x["revision"]}
                for x in u["payments"]])
        elif k == "mirror":
            self.call("record_mirror", pass_token=t, done=[c["n"] for c in u["calls"]])
        elif k in ("post", "view"):
            self.handed.append(u)       # posted; its receipt arrives when deliver() says
        else:
            raise AssertionError(u)
        return self.call("job_next", pass_token=t, calls_made=0)

    def until(self, u, unit, job_id=A):
        for _ in range(400):
            if u["unit"] == unit:
                return u
            u = self.call("job_next", job_id=job_id) if u["unit"] == "end-batch" else self.do(u)
        self.fail(f"no {unit} unit")

    def deliver(self):
        """Casa's receipts for what the job posted since the last call (S7 §5), in the
        order handed out: each rendering marked once. Returns their render ids."""
        out = []
        for u in self.handed:
            for rid in u.get("render_ids") or [u["render_id"]]:
                if rid not in out:
                    self.call("mark_rendering_delivered", render_id=rid)
                    out.append(rid)
        self.handed = []
        return out

    def kind(self, render_id):
        return self.conn.execute("SELECT kind FROM renders WHERE render_id=?",
                                 (render_id,)).fetchone()[0]


class LastDelivered(StoreCase):
    def test_handover_and_stop_pages_are_skipped(self):
        """R3: the reply binds to the last delivered rendering that offers something —
        a stop line or a handover's case lines delivered after it never take it."""
        import db
        with db.tx(self.conn):
            for rid, kind in (("r1", "status"), ("r2", "job-stop"), ("r3", "handover")):
                self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                                  " delivered_at, text, membership_json, delivered_seq)"
                                  " VALUES (?,?, '{}', ?, ?, '', '[]', ?)",
                                  (rid, kind, db.now(), db.now(), db.next_seq(self.conn)))
        self.assertEqual(db.last_delivered(self.conn)["render_id"], "r1")
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET delivered_seq=? WHERE render_id='r1'",
                              (db.next_seq(self.conn) - 1000,))
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " delivered_at, text, membership_json, delivered_seq) VALUES"
                              " ('r4', 'check', '{}', ?, ?, '', '[]', ?)",
                              (db.now(), db.now(), db.next_seq(self.conn)))
        self.assertEqual(db.last_delivered(self.conn)["render_id"], "r4")

    def test_a_non_binding_render_is_the_last_delivered(self):
        """A rendering stamped non-binding (binding=0) IS the last delivered one — never
        skipped to an older one. S7 §9 deletes the boundary (db.non_binding) it was."""
        import db
        with db.tx(self.conn):
            for rid, binding in (("r1", None), ("r2", 1), ("r3", 0)):
                self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                                  " delivered_at, text, membership_json, delivered_seq,"
                                  " binding) VALUES (?, 'status', '{}', ?, ?, '', '[]', ?, ?)",
                                  (rid, db.now(), db.now(), db.next_seq(self.conn), binding))
        self.assertEqual(db.last_delivered(self.conn)["render_id"], "r3")


    # At the S7 merge (§9): test_the_latest_hand_out_wins and test_a_notification_re_offer_binds_again
    # are deleted (§9 deletes this mechanism): both pinned renders.binding, the stamp
    # job_report's hand-out wrote (1 notification, 0 operator turn); §9 deletes job_report
    # and leaves the column unused, so nothing writes it any more.


class InstanceSwitch(Tools):
    """R4: FW-I2's instance half. After the operator's "the bank ledger was reset", a
    ledger instance that changes between two bank reads of ONE pass still stops it: the
    pass's gate was decided on the first instance."""

    def test_an_instance_switched_mid_pass_after_the_ack_stops_the_pass(self):
        """Simple loop Task 10: the pass read the bank, its turn died before the import,
        and the next batch reads another ledger instance: nothing is imported, the binding
        stays, and the pass stops."""
        import binding, db, job
        self.until(self.start(2), "complete")             # run A: the store is on LEDGER
        snaps = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.call("request_work", kind="check", trigger="operator")
        self.until(self.call("job_next", job_id=B), "snapshot", job_id=B)   # read, not imported
        self.granted(binding.acknowledge_ledger_reset_in_tx)
        self.instance = OTHER                             # the next read sees another ledger
        u = self.call("job_next", job_id=B)
        self.assertEqual(u["unit"], "probes")             # a new batch reads again
        u = self.do(u)
        self.assertEqual(u["unit"], "snapshot")
        with self.assertRaises(db.Refusal) as cm:
            self.do(u)
        self.assertIn("nothing was imported", str(cm.exception))
        b = binding.get(self.conn)
        self.assertEqual((b["ledger_instance"], b["ledger_reset_ack"]), (self.LEDGER, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0],
                         snaps)
        u = self.call("job_next", pass_token=u["pass_token"], calls_made=0)
        self.assertEqual(u["unit"], "view")               # the run's one message: the stop
        self.assertIsNone(job.live_job_pass(self.conn))
        r = self.conn.execute("SELECT state, outcome FROM work_requests ORDER BY request_id"
                              " DESC").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("reported", "stopped"))
