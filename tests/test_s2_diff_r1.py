"""S2 diff review round 1 (R1, R3, R4): a large check's later Gmail rounds earn judge
credit again; informational relay pages never take the operator's reply; a ledger
instance switched mid-pass after the reset acknowledgement stops the pass.

The relay tests drive the registered tools (qa_server.TOOLS) against a synthetic bank,
as the round's reproductions did (Astra, /tmp/s2-review-XQEBYB/review_repro.py)."""
import datetime

from tests._base import StoreCase
from tests.sim_job import JobDriver

A, B = "aaaaaaaa-1", "bbbbbbbb-2"
OTHER = "b" * 32


class LargeCheck(StoreCase):
    """R1: a judge restart earns fresh credit iff its cause is drawn from a finite
    budget — so a check of many Gmail chunk rounds is never ended by Casa's guard."""

    def test_eighty_payments_never_three_no_progress_batches(self):
        import asks, job
        self.bind()
        drv = JobDriver(self, payments=80)
        asks.request_work(self.conn, "check", "cron")
        drv.token = job.claim(self.conn, A)
        flags, judged, rounds = [], None, set()
        for _ in range(20000):
            u = job.next_unit(self.conn, drv.token, judged=judged)
            judged = None
            if u["unit"] == "complete":
                break
            if u["unit"] == "end-batch":
                flags.append(u["progress"]["progressed"])
                self.assertNotEqual(flags[-3:], [False] * 3, flags)
                drv.token = job.claim(self.conn, A)
                continue
            if u["unit"] == "judge":
                rounds.add(u["judgment"])
            judged = drv.do(u, drv.token)
        else:
            self.fail("the check never completed")
        self.assertGreater(len(rounds), 3)               # several Gmail chunk rounds
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("reported", "complete"))   # a cron check


class Tools(StoreCase):
    """The registered tools, a synthetic bank of `n` classified payments."""

    def setUp(self):
        super().setUp()
        import tools
        self.bind()
        tools._CONN = self.conn
        self.addCleanup(setattr, tools, "_CONN", None)
        self.rows, self.bank, self.instance = [], {}, self.LEDGER

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
        t, k, ans = u["pass_token"], u["unit"], {}
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
        elif k == "sweep":
            page = self.call("list_projections", pass_token=t, limit=10, quarter=u["quarter"])
            for it in page["projections"]:
                b = self.bank[it["row_id"]]
                for _ in range(6):
                    w = self.call("record_observation", pid=it["pid"], pass_token=t,
                                  snapshot_id=page["snapshot_id"], observed_tags=b["tags"],
                                  observed_notes=b["notes"],
                                  observed_first_seen="2026-07-03T08:00:00Z",
                                  observed_tag_revision=b["rev"])["instructions"]
                    if not w:
                        break
                    if "tag" in w:
                        b["tags"] = sorted(set(b["tags"]) | set(w["tag"]))
                        b["rev"] += 1
                    if "untag" in w:
                        b["tags"] = sorted(set(b["tags"]) - set(w["untag"]))
                        b["rev"] += 1
                    if "add_note" in w:
                        b["notes"].append(w["add_note"])
                else:
                    self.fail("the sweep did not settle")
        elif k == "gmail-probe":
            self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        elif k == "filing":
            self.call("record_filing", pass_token=t)
        elif k == "item":
            self.call("record_search", pass_token=t, pid=u["item"]["pid"],
                      queries=["invoice"], exhausted=True)
        elif k == "judge":
            page = self.call("list_quarter_state", pass_token=t, triage=True, limit=8,
                             quarter=u["quarter"], after=u["after"])
            ans = {"judged": {"judgment": u["judgment"], "after": u["after"],
                              "page_next": page["next"], "triage_remaining": page["remaining"],
                              "documents": {str(i): "no-payment-yet"
                                            for i in u["documents_first"]}}}
        else:
            raise AssertionError(u)
        return self.call("job_next", pass_token=t, **ans)

    def until(self, u, unit, job_id=A):
        for _ in range(400):
            if u["unit"] == unit:
                return u
            u = self.call("job_next", job_id=job_id) if u["unit"] == "end-batch" else self.do(u)
        self.fail(f"no {unit} unit")

    def deliver(self, report):
        out = []
        for page in ([report["speak"]] if report.get("speak") else []) + report["texts"]:
            self.call("mark_rendering_delivered", render_id=page["render_id"])
            out.append(page["render_id"])
        return out

    def kind(self, render_id):
        return self.conn.execute("SELECT kind FROM renders WHERE render_id=?",
                                 (render_id,)).fetchone()[0]


class InformationalPages(Tools):
    """R3: handover case lines and stop lines never move the reply binding."""

    def test_an_offer_relayed_in_an_operators_turn_binds_nothing(self):
        """R5: the same resend offer, handed out by a no-id job_report (an operator's
        turn) and delivered, is non-binding: "send it again" in the next message binds
        to what the operator saw before it — here nothing — and is refused."""
        self.uncertain_package()
        relay = self.call("job_report")
        self.assertIn("send it again", relay["speak"]["text"])
        self.deliver(relay)
        import db
        with self.assertRaises(db.Refusal):
            self.call("stage_for_delivery", channel="telegram", resend=True)

    def uncertain_package(self):
        self.call("request_package", quarter="2026-Q3", channel="telegram")
        self.until(self.call("job_next", job_id=A), "complete")
        tok = self.call("job_report", job_id=A, status="ok")["continue"]["package_token"]
        pkg = self.call("build_quarterly_package", quarter="2026-Q3", package_token=tok)
        staged = self.call("stage_for_delivery", channel="telegram",
                           package_id=pkg["package_id"], package_token=tok)
        self.call("record_delivery", delivery_id=staged["delivery_id"], outcome="uncertain",
                  package_token=tok)
        return pkg

    def test_a_handover_page_after_the_resend_offer_keeps_it(self):
        """Astra's reproduction: an uncertain package send, then a finished handover;
        `speak` (the resend offer) and the handover page, relayed on the job's
        notification, delivered in that order. "Send it again" still resends the
        offered package."""
        self.call("request_package", quarter="2026-Q3", channel="telegram")
        self.until(self.call("job_next", job_id=A), "complete")
        tok = self.call("job_report")["continue"]["package_token"]
        pkg = self.call("build_quarterly_package", quarter="2026-Q3", package_token=tok)
        staged = self.call("stage_for_delivery", channel="telegram",
                           package_id=pkg["package_id"], package_token=tok)
        self.call("record_delivery", delivery_id=staged["delivery_id"], outcome="uncertain",
                  package_token=tok)
        self.call("request_work", kind="handover", trigger="operator", doc_ids=[self.doc()])
        self.until(self.call("job_next", job_id=B), "complete", job_id=B)
        relay = self.call("job_report", job_id=B, status="ok")       # its notification
        self.assertIn("send it again", relay["speak"]["text"])
        shown = self.deliver(relay)
        self.assertEqual(self.kind(shown[-1]), "handover")
        before = self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        again = self.call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(self.conn.execute("SELECT package_id FROM deliveries WHERE"
                                           " delivery_id=?", (again["delivery_id"],)
                                           ).fetchone()[0], pkg["package_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0],
                         before + 1)

    def test_all_good_after_a_handover_page_binds_to_the_sheet_before_it(self):
        with self.patch_clock(datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)):
            u = self.until(self.start(2), "judge")
            pid = self.conn.execute("SELECT min(pid) FROM projections").fetchone()[0]
            it = self.call("list_quarter_state", pid=pid, pass_token=u["pass_token"])["item"]
            self.call("record_match", pid=pid, doc_id=self.doc(amount_minor=10001),
                      author="auto", pass_token=u["pass_token"],
                      expected_revision=it["revision"], row_digest=it["row_digest"],
                      document_date="2026-07-02", labels=["no-ref"])
            self.until(self.do(u), "complete")
            sheet = self.deliver(self.call("job_report", job_id=A, status="ok"))[-1]
            self.assertEqual(self.kind(sheet), "status")
            self.call("request_work", kind="handover", trigger="operator",
                      doc_ids=[self.doc(amount_minor=55555)])
            self.until(self.call("job_next", job_id=B), "complete", job_id=B)
            shown = self.deliver(self.call("job_report", job_id=B, status="ok"))
            self.assertIn("handover", [self.kind(r) for r in shown])
            self.assertEqual(self.kind(shown[-1]), "handover")
            self.call("apply_reply", text="all good")
        pairs = [tuple(r) for r in self.conn.execute(
            "SELECT pid, render_id FROM log WHERE kind='pair' AND author='operator'")]
        self.assertEqual(pairs, [(pid, sheet)])


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

    def test_a_non_binding_render_is_skipped(self):
        """R5: a rendering stamped non-binding (binding=0) is never the last delivered
        one; NULL (never handed out by job_report) and 1 bind."""
        import db
        with db.tx(self.conn):
            for rid, binding in (("r1", None), ("r2", 1), ("r3", 0)):
                self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                                  " delivered_at, text, membership_json, delivered_seq,"
                                  " binding) VALUES (?, 'status', '{}', ?, ?, '', '[]', ?, ?)",
                                  (rid, db.now(), db.now(), db.next_seq(self.conn), binding))
        self.assertEqual(db.last_delivered(self.conn)["render_id"], "r2")
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET binding=NULL WHERE render_id='r3'")
        self.assertEqual(db.last_delivered(self.conn)["render_id"], "r3")


class OperatorTurnRelay(Tools):
    """R5: a rendering handed out by job_report WITHOUT job_id (an operator's turn,
    after the operator wrote) is non-binding; one handed out on a notification
    (job_id given) binds as before. The latest hand-out wins."""

    def two_checks(self):
        """Job A pairs payment 1 and its sheet is relayed on A's notification and
        delivered; job B (an operator's check) pairs payment 2 and completes, its result
        not yet relayed. Returns (pid1, pid2, the delivered sheet)."""
        with self.patch_clock(datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)):
            u = self.until(self.start(2), "judge")
            pids = [r[0] for r in self.conn.execute("SELECT pid FROM projections ORDER BY pid")]
            self.pair(u, pids[0], 10001)
            self.until(self.do(u), "complete")
            old = self.deliver(self.call("job_report", job_id=A, status="ok"))[-1]
            self.call("request_work", kind="check", trigger="operator")
            u = self.until(self.call("job_next", job_id=B), "judge", job_id=B)
            self.pair(u, pids[1], 10002)
            self.until(self.do(u), "complete", job_id=B)
        return pids[0], pids[1], old

    def pair(self, u, pid, amount):
        it = self.call("list_quarter_state", pid=pid, pass_token=u["pass_token"])["item"]
        self.call("record_match", pid=pid, doc_id=self.doc(amount_minor=amount), author="auto",
                  pass_token=u["pass_token"], expected_revision=it["revision"],
                  row_digest=it["row_digest"], document_date="2026-07-02", labels=["no-ref"])

    def operator_pairs(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT pid, render_id FROM log WHERE kind='pair' AND author='operator'"
            " ORDER BY rowid")]

    def test_astras_reply_race_confirms_only_what_the_operator_saw(self):
        """The operator's "all good" is about the sheet they saw; Ellen relays B's
        result with a no-id job_report BEFORE apply_reply (the order R2 forbids). The
        reply still binds to the sheet they saw: one pairing, never the second."""
        p1, p2, old = self.two_checks()
        new = self.deliver(self.call("job_report"))[-1]
        self.assertNotEqual(new, old)
        self.call("apply_reply", text="all good")
        self.assertEqual(self.operator_pairs(), [(p1, old)])

    def test_a_notification_relay_still_binds(self):
        p1, p2, old = self.two_checks()
        new = self.deliver(self.call("job_report", job_id=B, status="ok"))[-1]
        self.call("apply_reply", text="all good")
        pairs = self.operator_pairs()
        self.assertEqual({p for p, _ in pairs}, {p1, p2})
        self.assertEqual({r for _, r in pairs}, {new})

    def binding(self, render_id):
        return self.conn.execute("SELECT binding FROM renders WHERE render_id=?",
                                 (render_id,)).fetchone()[0]

    def test_the_latest_hand_out_wins(self):
        """A result first offered on B's notification (not delivered: the turn was cut),
        then offered again by a no-id call in an operator's turn, is non-binding; offered
        once more on a notification, it binds again."""
        p1, p2, old = self.two_checks()
        first = self.call("job_report", job_id=B, status="ok")["texts"][-1]["render_id"]
        self.assertEqual(self.binding(first), 1)
        again = self.call("job_report")["texts"][-1]["render_id"]
        self.assertEqual(again, first)                 # the same rendering, re-offered
        self.assertEqual(self.binding(first), 0)
        self.call("mark_rendering_delivered", render_id=first)
        self.call("apply_reply", text="all good")
        self.assertEqual(self.operator_pairs(), [(p1, old)])
        self.assertEqual(self.binding(old), 1)


    def test_a_notification_re_offer_binds_again(self):
        """The other direction: first offered by a no-id call, then on a notification."""
        p1, p2, old = self.two_checks()
        first = self.call("job_report")["texts"][-1]["render_id"]
        self.assertEqual(self.binding(first), 0)
        again = self.call("job_report", job_id=B, status="ok")["texts"][-1]["render_id"]
        self.assertEqual((again, self.binding(first)), (first, 1))
        self.call("mark_rendering_delivered", render_id=first)
        self.call("apply_reply", text="all good")
        self.assertEqual({r for _, r in self.operator_pairs()}, {first})


class InstanceSwitch(Tools):
    """R4: FW-I2's instance half. After the operator's "the bank ledger was reset", a
    ledger instance that changes between two bank reads of ONE pass still stops it: the
    pass's gate was decided on the first instance."""

    def test_an_instance_switched_mid_pass_after_the_ack_stops_the_pass(self):
        import binding, db, job
        u = self.until(self.start(2), "judge")
        snaps = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        binding.acknowledge_ledger_reset(self.conn)
        self.instance = OTHER                         # the next read sees another ledger
        later = db._clock() + datetime.timedelta(seconds=job.W_S + 1)
        with self.patch_clock(later):
            u = self.call("job_next", job_id=A)
            self.assertEqual(u["unit"], "probes")      # W: the bank is read again
            u = self.do(u)
            self.assertEqual(u["unit"], "snapshot")
            with self.assertRaises(db.Refusal) as cm:
                self.do(u)
            self.assertIn("nothing was imported", str(cm.exception))
            b = binding.get(self.conn)
            self.assertEqual((b["ledger_instance"], b["ledger_reset_ack"]), (self.LEDGER, 1))
            self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0],
                             snaps)
            u = self.call("job_next", pass_token=u["pass_token"])
            self.assertIn(u["unit"], ("complete", "end-batch"))
        self.assertIsNone(job.live_job_pass(self.conn))
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))
