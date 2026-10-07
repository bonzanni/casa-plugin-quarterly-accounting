"""Simple loop §3: schema 12 — the run's work list and mirror calls, the quarter's
ready notice, mirror_note, the filing vendor, alternatives, keyed documents, item states,
the Gmail streak, batch progress, the learned hint."""
import sqlite3
from tests._base import StoreCase


class Schema12(StoreCase):
    def cols(self, table, conn=None):
        return {r[1] for r in (conn or self.conn).execute(f"PRAGMA table_info({table})")}

    def test_version_tables_and_columns(self):
        import db
        self.assertEqual(db.SCHEMA_VERSION, 15)
        self.assertTrue({"job_id", "pid", "vendor", "why", "outcome", "reason", "attempts",
                         "searches", "searched_seq"}
                        <= self.cols("run_work"))
        self.assertTrue({"question_id", "pid", "match_id", "new_doc_id", "state"}
                        <= self.cols("replace_questions"))
        self.assertTrue({"job_id", "n", "tool", "args_json", "pids_json", "state", "error"}
                        <= self.cols("run_mirror"))
        self.assertTrue({"quarter", "sig", "times", "render_id"}
                        <= self.cols("quarter_notices"))
        self.assertTrue({"render_id", "pid", "item_state"} <= self.cols("render_states"))
        for table, col in (("projections", "mirror_note"), 
                           ("documents", "vendor"),
                           ("documents", "filed_seq"), ("matches", "alternatives_json"),
                           ("render_keys", "doc_id"), ("render_states", "item_state"),
                           ("probes", "fail_runs"), ("claims", "progressed"),
                           ("counterparties", "hint_sender"),
                           ("counterparties", "hint_subject"), ("runs", "started_by"),
                           ("runs", "end_render_id"), ("runs", "partial"),
                           ("runs", "mirror_at"), ("runs", "listed_at"),
                           ("run_work", "attempts"), ("run_work", "closed_seq"),
                           ("run_mirror", "attempts"), ("run_items", "state")):
            self.assertIn(col, self.cols(table), f"{table}.{col}")

    def test_sqlite_can_drop_columns(self):
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 35, 0))   # Task 11's drops

    def test_run_work_outcomes_are_closed(self):
        import db
        with self.assertRaises(sqlite3.IntegrityError):
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO run_work(job_id, pid, vendor, why, outcome)"
                                  " VALUES ('j', 1, 'Adobe', 'open', 'not-needed')")

    def test_migration_from_11_keeps_data_and_fresh_equals_migrated(self):
        import db
        from tests.schema_history import build_v11_store
        path = self.tmp / "v11" / "accounting.sqlite"
        path.parent.mkdir()
        build_v11_store(path, with_rows=True)
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], "15")
        self.assertEqual(conn.execute("SELECT alternatives_json FROM matches").fetchone()[0],
                         "[]")
        self.assertIsNone(conn.execute("SELECT mirror_note FROM projections").fetchone()[0])

        def shape(c):
            return sorted((r["name"], tuple(sorted((x[1], x[2], x[3], x[5])
                          for x in c.execute(f"PRAGMA table_info({r['name']})"))))
                          for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                             " AND name<>'sqlite_sequence'"))
        self.assertEqual(shape(conn), shape(self.conn))

    def test_reset_store_wipes_the_run_tables(self):
        import binding, db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO run_work(job_id, pid, vendor, why) VALUES"
                              " ('j', 1, 'Adobe', 'open')")
            self.conn.execute("INSERT INTO quarter_notices(quarter, sig) VALUES"
                              " ('2026-Q3', 'x')")
            self.conn.execute("INSERT INTO render_states(render_id, pid, item_state) VALUES"
                              " ('r1', 1, 'missing')")
        binding.reset_store(self.conn)
        for t in ("run_work", "run_mirror", "render_states", "quarter_notices"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0, t)
        with db.tx(self.conn):          # the sequence restarted: the same render id again
            self.conn.execute("INSERT INTO render_states(render_id, pid, item_state) VALUES"
                              " ('r1', 1, 'missing')")

    def test_run_claim_makes_a_real_run(self):
        """The one fixture later tasks start from: a real claim, pass, run and probes."""
        import job
        token = self.run_claim(started_by="cron")
        self.assertEqual(token, self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0])
        self.assertRegex(self.job_id, job.JOB_ID_RE)
        run = self.conn.execute("SELECT started_by, pass_id FROM runs WHERE job_id=?",
                                (self.job_id,)).fetchone()
        self.assertEqual((run[0], run[1]), ("scheduled", self.pass_id))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM probes").fetchone()[0], 4)
        first = self.job_id
        self.run_claim()
        self.assertNotEqual(first, self.job_id)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.work_rows([1, 2])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_work WHERE job_id=?",
                                           (self.job_id,)).fetchone()[0], 2)

    def test_the_deleted_machinery_leaves_no_table_or_column(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE"
                                                  " type='table'")}
        self.assertFalse({"credits", "cursor", "pass_steps", "package_requests"} & tables)  # removed-name: asserted absent
        self.assertFalse({"judge_epoch", "w_refreshes", "adoptions"} & self.cols("passes"))  # removed-name: asserted absent
        self.assertFalse({"note_seq", "readback_owed", "observed_revision"}  # removed-name: asserted absent
                         & self.cols("projections"))
        self.assertNotIn("spent", self.cols("claims"))

    def test_the_deleted_modules_are_gone(self):
        import importlib.util
        for mod in ("steps", "sweep"):
            self.assertIsNone(importlib.util.find_spec(mod), mod)

    def test_an_unsaid_package_request_notice_is_said_as_not_sent_after_the_upgrade(self):
        """Task 11: the stopped / bank-unread notices lose their wording with their raisers;
        one still unsaid at 11 -> 12 is said as the not-sent notice, with its reason."""
        import alerts, db, json
        from tests.schema_history import build_v11_store
        path = self.tmp / "v11n" / "accounting.sqlite"
        path.parent.mkdir()
        build_v11_store(path)
        import sqlite3
        c = sqlite3.connect(path)
        for i, (kind, sent) in enumerate((("package-stopped", None),  # removed-name: asserted absent
                                          ("package-failed", None),   # removed-name: asserted absent
                                          ("package-stopped", "x"))):  # removed-name: asserted absent
            c.execute("INSERT INTO alerts(kind, occurrence_key, detail, raised_at, sent_at)"
                      " VALUES (?,?,?,?,?)", (kind, f"k{i}", json.dumps(
                          {"quarter": "2026-Q2", "reason": "the gate refused" if i == 0
                           else "", "package_id": None, "pass_id": ""}), "x", sent))
        c.commit()
        c.close()
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts ORDER BY alert_id")]
        self.assertEqual(kinds, ["package-not-sent", "package-not-sent",
                                 "package-stopped"])  # removed-name: asserted absent (said already)
        r = alerts.pending_rendering(conn)
        self.assertIn("I couldn't send the Q2 2026 package (the gate refused)", r["text"])
        self.assertEqual(json.loads(conn.execute("SELECT scope_json FROM renders WHERE"
                                                 " render_id=?", (r["render_id"],))
                                    .fetchone()[0])["alerts"], [1, 2])

    def test_an_open_package_request_is_told_once_when_its_table_goes(self):
        """Task 11 review: 11 -> 12 drops the package asks; each one still open (queued,
        being checked, buildable or built) is told once as not sent, a settled one not."""
        import alerts, db, sqlite3
        from tests.schema_history import build_v11_store
        path = self.tmp / "v11r" / "accounting.sqlite"
        path.parent.mkdir()
        build_v11_store(path)
        c = sqlite3.connect(path)
        for i, state in enumerate(("queued", "snapshot", "snapshot-done", "built",
                                   "delivered", "stopped"), 1):
            c.execute("INSERT INTO package_requests(request_id, quarter, channel, state,"  # removed-name: asserted absent
                      " created_at, updated_at) VALUES (?, '2026-Q2', 'telegram', ?, 'x', 'x')",
                      (i, state))
        c.commit()
        c.close()
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        keys = [r[0] for r in conn.execute("SELECT occurrence_key FROM alerts WHERE"
                                           " kind='package-not-sent' ORDER BY alert_id")]
        self.assertEqual(keys, [f"request:{i}:dropped" for i in (1, 2, 3, 4)])
        text = alerts.pending_rendering(conn)["text"]
        self.assertIn("I couldn't send the Q2 2026 package (it was asked for before the "
                      "update) — ask again when you want it.", " ".join(text.split()))

    def test_a_legacy_note_or_left_line_never_becomes_the_last_delivered(self):
        import db
        with db.tx(self.conn):
            for rid, kind, seq in (("r-s", "status", 1), ("r-n", "package-note", 2),  # removed-name: asserted absent
                                   ("r-l", "job-left", 3)):  # removed-name: asserted absent
                self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                                  " delivered_at, text, membership_json, delivered_seq) VALUES"
                                  " (?, ?, '{}', 'x', 'x', 't', '[]', ?)", (rid, kind, seq))
        self.assertEqual(db.last_delivered(self.conn)["render_id"], "r-s")

