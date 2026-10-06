"""Simple loop §2 (rev 17): one Casa job run is one pass; the units in order; payments are
handed out while calls_made < 65, then end-batch; progress is reported only at a batch's
end; an operator-started run ends with ONE end message; a scheduled run is silent unless an
item is in a new state, and then lists only those (rev 17); the mirror leaves a rerun with
nothing to write."""
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported
from tests.sim_job import JobDriver


class Run(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=3)

    def test_an_operator_run_unit_by_unit(self):
        units = [u["unit"] for u in self.drv.run_job("aaaaaaaa-1", started_by="operator")]
        self.assertEqual(units[:3], ["probes", "snapshot", "filing"])
        self.assertIn("vendor", units)
        self.assertLess(units.index("vendor"), units.index("mirror"))
        self.assertEqual(units[-2:], ["view", "complete"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 1)
        end = self.conn.execute("SELECT end_render_id FROM runs").fetchone()[0]
        self.assertIn("3 missing", self.conn.execute("SELECT text FROM renders WHERE"
                                                     " render_id=?", (end,)).fetchone()[0])

    def test_payments_are_handed_out_only_below_65_calls(self):
        import job, loop
        tok = job.claim(self.conn, "aaaaaaaa-2", started_by="Started by: operator")
        for _ in range(3):                                  # probes, snapshot, filing
            u = job.next_unit(self.conn, tok, 0)
            self.drv.do(u, tok)
        u = job.next_unit(self.conn, tok, loop.CALLS_SOFT)
        self.assertEqual(u["unit"], "end-batch")
        self.assertTrue(u["report"])
        self.assertTrue(u["progress"]["progressed"])        # the filing and import progressed
        tok = job.claim(self.conn, "aaaaaaaa-2")
        u = job.next_unit(self.conn, tok, 0)
        self.assertEqual(u["unit"], "vendor")
        self.assertFalse(u["report"])

    def test_a_scheduled_run_lists_only_new_state_items_and_omits_never(self):
        self.drv.run_job("aaaaaaaa-3", started_by="operator")       # the 3 shown once
        units = [u["unit"] for u in self.drv.run_job("aaaaaaaa-4", started_by="scheduled")]
        self.assertNotIn("view", units)
        self.assertNotIn("post", units)                              # silent
        self.drv.add_payments(["2026-09-15"])
        units = self.drv.run_job("aaaaaaaa-5", started_by="scheduled")
        (view,) = [u for u in units if u["unit"] == "view"]
        text = self.conn.execute("SELECT text, scope_json FROM renders WHERE render_id=?",
                                 (view["render_id"],)).fetchone()
        self.assertIn("3 earlier items still open", text[0])
        import cards, json
        with db.tx(self.conn):
            page = cards.card(self.conn, view["render_id"], 0)
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (page,)).fetchone()
        self.assertNotIn("Never for", " ".join(b[0] for b in cards.buttons(self.conn, r)))
        self.assertEqual(len(json.loads(r["membership_json"])), 1)

    def test_an_immediate_rerun_makes_no_mirror_write_and_no_decision_write(self):
        self.drv.run_job("aaaaaaaa-6", started_by="operator")
        seq = self.conn.execute("SELECT max(seq) FROM log").fetchone()[0]
        units = self.drv.run_job("aaaaaaaa-7", started_by="operator")
        mirror_calls = sum(len(u["calls"]) for u in units if u["unit"] == "mirror")
        self.assertEqual(mirror_calls, 0)
        self.assertEqual(self.conn.execute("SELECT max(seq) FROM log").fetchone()[0], seq)

    def test_gmail_down_three_runs_says_so_once(self):
        self.drv.gmail.down = True
        for n in range(4):
            self.drv.run_job(f"bbbbbbbb-{n}", started_by="scheduled")
        said = self.conn.execute("SELECT count(*) FROM alerts WHERE kind='gmail' AND sent_at"
                                 " IS NOT NULL").fetchone()[0]
        self.assertEqual(said, 1)


def _dt(day):
    import datetime as dt
    return dt.datetime.fromisoformat(day + "T12:00:00+00:00")


class Carries(StoreCase):
    """The items carried into Task 10 (task-10-carries.md), each pinned on the run."""

    def setUp(self):
        super().setUp()
        self.bind()

    def end_text(self, job_id):
        rid = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                (job_id,)).fetchone()[0]
        return rid, (self.render_text(rid) if rid else None)

    def test_a_refused_bank_gate_skips_the_mirror_and_says_so_once(self):
        """Carry 1: mirror calls are handed out only while the gate allows writes."""
        import passes
        drv = JobDriver(self, payments=2)
        drv.claim("dddddddd-1")
        units = []
        while not units or units[-1]["unit"] != "filing":         # probes, snapshot read
            units.append(__import__("job").next_unit(self.conn, drv.token, drv.calls))
            if units[-1]["unit"] != "filing":
                drv.do(units[-1], drv.token)
        refused = {"allowed": False, "reason": "the ledger was restored since this store "
                   "last ran", "expected_generation": None, "expected_ledger": None}
        self.patch(passes, "bank_write_gate", lambda conn: refused)
        drv.do(units[-1], drv.token)
        units += drv._loop("dddddddd-1")
        self.assertNotIn("mirror", [u["unit"] for u in units])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_mirror").fetchone()[0], 0)
        _, text = self.end_text("dddddddd-1")
        self.assertEqual(text.count("The bank ledger was not updated: the ledger was restored"
                                    " since this store last ran."), 1, text)

    def test_a_cut_run_is_partial_counting_only_undecided_entries(self):
        """Carry 2: the partial rule counts outcome IS NULL only; 'settled' is decided."""
        import work
        drv = JobDriver(self, payments=3)
        seen = []

        def cut(u, token):                       # the batch never gets to decide
            seen.append([p["pid"] for p in u["payments"]])
            if len(seen) == 1:
                self.granted(lambda c, grant: work.leave_missing_in_tx(
                    c, [u["payments"][0]["pid"]], grant=grant))
        drv._vendor = cut
        units = drv.run_job("dddddddd-2")
        self.assertEqual([len(s) for s in seen], [3, 2])          # handed twice (D8)
        rows = dict(self.conn.execute("SELECT pid, outcome FROM run_work WHERE job_id="
                                      "'dddddddd-2'").fetchall())
        self.assertEqual(sorted(rows.values(), key=lambda v: v or ""), [None, None, "settled"])
        self.assertEqual(self.conn.execute("SELECT partial FROM runs WHERE job_id="
                                           "'dddddddd-2'").fetchone()[0], 1)
        _, text = self.end_text("dddddddd-2")
        self.assertIn("2 missing · search incomplete", text)
        self.assertEqual(units[-1]["text"], "Accounting check interrupted before it finished.")

    def test_a_payment_absent_from_the_latest_read_is_never_counted_missing(self):
        """Carry 3: not fresh (decide refuses it) → counted pending, in exactly one bucket."""
        import cards
        self.run_claim()
        self.row(9001, booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(9001)
        self.classify(pid, {"software"})
        self.settle(pid)
        with db.tx(self.conn):
            self.assertEqual(len(cards.state(self.conn)["missing"]), 1)
        self.import_again()                                    # a newer read without it
        with db.tx(self.conn):
            st = cards.state(self.conn)
            self.assertEqual(st["missing"], {})
            self.assertEqual([d["pid"] for d in st["pending"]], [pid])
            rid = cards.compose_end(self.conn, self.job_id, scheduled=False)
        self.assertIn("0 missing · 1 pending", self.render_text(rid))

    def test_a_handover_taken_mid_run_reopens_its_payment_once(self):
        """Carry 4: take_handovers gets only NEWLY taken handover documents."""
        import asks, job
        drv = self._job_driver = JobDriver(self, payments=1)
        units = self.drive("dddddddd-3", stop_before="mirror")
        self.assertEqual([u["unit"] for u in units].count("vendor"), 1)
        path = self.publish("handed.pdf", b"%PDF-1.4 handed\n", producer="telegram")
        doc = drv._tool("ingest_document", {
            "source_path": path, "kind": "invoice", "source": "manual-telegram",
            "extraction_author": "desk", "issuer": "Zapier", "amount_minor": 1000,
            "currency": "EUR", "document_date": "2026-07-04", "document_number": "Z-1"})
        asks.request_work(self.conn, "handover", "operator", [doc["doc_id"]])
        rest = []
        while not rest or rest[-1]["unit"] != "complete":
            rest.append(job.next_unit(self.conn, drv.token, drv.calls))
            if rest[-1]["unit"] not in ("complete", "end-batch"):
                drv.do(rest[-1], drv.token)
        self.assertEqual([u["unit"] for u in rest].count("vendor"), 1)
        (row,) = self.conn.execute("SELECT why, outcome, handed FROM run_work").fetchall()
        self.assertEqual(tuple(row), ("handover", "match", 1))

    def test_an_alert_raised_between_runs_is_in_the_run_message_and_sent_on_delivery(self):
        """Carries 5 and 10: compose_end binds the alert ids it prints (scope['alerts']); a
        tap's alert (settle_delivered) waits for the next run's post."""
        import alerts, json
        drv = JobDriver(self, payments=1)
        with db.tx(self.conn):
            aid = alerts.raise_package(self.conn, "package-not-sent", "request:x:oversize",
                                       quarter="2026-Q3", reason="it is too large")
        units = drv.run_job("dddddddd-4")
        self.assertEqual(len([u for u in units if u["unit"] in ("view", "post")]), 1)
        rid, text = self.end_text("dddddddd-4")
        self.assertIn("I couldn't send the Q3 2026 package (it is too large) — ask again",
                      " ".join(text.split()))
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        self.assertEqual(scope["alerts"], [aid])
        self.assertEqual(self.conn.execute("SELECT render_id FROM alerts WHERE alert_id=? AND"
                                           " sent_at IS NOT NULL", (aid,)).fetchone()[0], rid)

    def test_a_quiet_run_with_only_an_alert_posts_the_alert_alone(self):
        """§1: the failure line "is otherwise the run's one message"."""
        import alerts
        drv = JobDriver(self, payments=1)
        drv.run_job("dddddddd-5")                                   # the item shown once
        with db.tx(self.conn):
            alerts.raise_package(self.conn, "package-not-sent", "request:y:oversize",
                                 quarter="2026-Q3", reason="it is too large")
        units = drv.run_job("dddddddd-6", started_by="scheduled")
        posts = [u for u in units if u["unit"] in ("view", "post")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["unit"], "post")
        (rid,) = posts[0]["render_ids"]
        self.assertEqual(self.conn.execute("SELECT kind FROM renders WHERE render_id=?",
                                           (rid,)).fetchone()[0], "alert")
        self.assertEqual(self.end_text("dddddddd-6")[0], "")

    def test_a_check_naming_q2_heads_the_run_and_its_package(self):
        """Carry 7 (ruling Q2): watermark 2026-04-01, Q3 rows present, "check Q2"."""
        import asks, cards, json
        self.bind(watermark="2026-04-01")
        drv = JobDriver(self, payments=0)
        drv.add_payments(["2026-05-10"])
        drv.add_payments(["2026-08-10"])
        with self.patch_clock(_dt("2026-10-06")):
            asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
            drv.run_job("dddddddd-7")
            rid, text = self.end_text("dddddddd-7")
            self.assertTrue(text.startswith("Q2 checked · 1 payment"), text)
            r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
            with db.tx(self.conn):
                (get,) = [b for b in cards.buttons(self.conn, r) if b[0] == "Get package"]
            self.assertEqual(get[1:3], ("get_package", {"quarter": "2026-Q2"}))
            q2 = [d["pid"] for d in cards.state(self.conn)["by_bucket"]["missing"]
                  if d["quarter"] == "2026-Q2"]
            self.granted(lambda c, grant: __import__("work").leave_missing_in_tx(
                c, q2, grant=grant))
            asks.request_work(self.conn, "check", "operator", quarter="2026-Q2")
            units = drv.run_job("dddddddd-8")
            rid, text = self.end_text("dddddddd-8")
        self.assertEqual([u.get("render_id") for u in units if u["unit"] in ("view", "post")],
                         [rid])                    # one message: the notice is a line of it
        self.assertIn("Q2 complete · package ready", text)
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT scope_json FROM renders WHERE render_id=?", (rid,)).fetchone()[0]
        )["ready_quarters"], ["2026-Q2"])
        self.assertEqual(self.conn.execute("SELECT times FROM quarter_notices WHERE"
                                           " quarter='2026-Q2'").fetchone()[0], 1)

    def test_no_bank_tools_stops_the_run_said_once(self):
        drv = JobDriver(self, payments=1)
        drv.no_bank_tools()
        units = drv.run_job("dddddddd-9", started_by="scheduled")
        self.assertEqual([u["unit"] for u in units], ["probes", "post", "complete"])
        self.assertEqual(units[-1]["text"], "Accounting check stopped: bank-feed's tools are"
                                            " not available to the finance specialist.")
        units = drv.run_job("dddddddd-a", started_by="scheduled")
        self.assertEqual([u["unit"] for u in units], ["probes", "complete"])   # said once
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE kind="
                                           "'run-stopped' AND sent_at IS NOT NULL"
                                           ).fetchone()[0], 1)

    def test_an_erased_row_is_confirmed_through_record_not_found(self):
        import lineage
        drv = JobDriver(self, payments=1)
        self.row(9001, booking_date="2026-09-10", value_date="2026-09-10")
        pid = self.lineage_for(9001)
        drv.run_job("dddddddd-b")
        self.assertEqual(lineage.projection(self.conn, pid)["ended"], "erased")
        with db.tx(self.conn):                                     # a stale snapshot refused
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES ('x', ?, 0, 0)", (db.now(),))
        drv.claim("dddddddd-c")
        import loop
        with self.assertRaises(db.Refusal):
            loop.record_not_found(self.conn, drv.token, pid, 1)


class Surface(StoreCase):
    def test_job_next_needs_calls_made_with_a_pass_token(self):
        import qa_server, tools  # noqa: F401
        self.bind()
        fn = qa_server.TOOLS["job_next"]["fn"]
        tok = fn({"job_id": "eeeeeeee-1", "started_by": "Started by: operator"})["pass_token"]
        for bad in ({}, {"calls_made": -1}, {"calls_made": True}, {"calls_made": "3"}):
            with self.assertRaises(db.Refusal):
                fn({"pass_token": tok, **bad})
        self.assertEqual(fn({"pass_token": tok, "calls_made": 3})["pass_token"], tok)

    def test_the_gmail_streak_counts_runs_not_probes(self):
        import passes
        self.bind()

        def runs():
            return self.conn.execute("SELECT fail_runs FROM probes WHERE kind='gmail'"
                                     ).fetchone()[0]
        t = self.run_claim()
        passes.record_probe(self.conn, t, "gmail", False, "down")
        passes.record_probe(self.conn, t, "gmail", False, "down")      # the same run
        self.assertEqual(runs(), 1)
        t = self.run_claim()
        passes.record_probe(self.conn, t, "gmail", False, "down")
        self.assertEqual(runs(), 2)
        passes.record_probe(self.conn, t, "gmail", True)
        self.assertEqual(runs(), 0)

    def test_a_stale_sync_is_said_once_per_streak(self):
        import alerts
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-28')")
        with self.patch_clock(_dt("2026-10-05")):
            with db.tx(self.conn):
                self.assertEqual(alerts.pending_lines(self.conn), ([], []))   # 7 days: not yet
        with self.patch_clock(_dt("2026-10-06")):
            with db.tx(self.conn):
                lines, ids = alerts.pending_lines(self.conn)
                self.assertEqual(lines, ["Bank not synced since 28 Sep · bank-feed needs"
                                         " attention"])
                alerts.evaluate(self.conn)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts").fetchone()[0], 1)
