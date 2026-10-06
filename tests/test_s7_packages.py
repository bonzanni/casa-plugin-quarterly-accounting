# tests/test_s7_packages.py
"""S7 §6: the job builds and posts the package itself; the file keeps its package name
through Casa's filename; the caption is one line and the rest a rendering; first sends at
most once; resend and send-last from the desk; no email."""
import json
from tests._base import StoreCase
from tests.fakebroker import FakeBroker

A = "aaaaaaaa-1"


class Packages(StoreCase):
    def test_a_package_ask_is_built_and_posted_by_the_job(self):
        import asks
        asks.request_package(self.conn, "2026-Q3")
        with FakeBroker() as b:
            units = self.drive(A, deliver=True)
        kinds = [u["unit"] for u in units]
        self.assertLess(kinds.index("build"), kinds.index("deliver"))
        dep = next(d for d in b.deposits if d["slot"] == "package")
        self.assertEqual(dep["kind"], "zip")
        self.assertRegex(dep["value"], r"/qa-[0-9a-f]{16}\.zip$")
        name = self.conn.execute("SELECT filename FROM packages").fetchone()[0]
        self.assertEqual(dep["filename"], name)
        self.assertNotIn("\n", dep["caption"])
        self.assertLessEqual(len(dep["caption"]), 900)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "delivered")
        # simple loop §1: the caption is the whole message — no package note follows
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE"
                                           " kind='package-note'").fetchone()[0], 0)

    def test_a_build_after_a_reread_goes_back_to_its_check_inside_the_cursor(self):
        import asks, job
        asks.request_package(self.conn, "2026-Q3")
        self.drive(A, deliver=True, stop_before="build")
        self.import_again()                          # a newer import lands
        tok = job.claim(self.conn, A)
        u = job.next_unit(self.conn, tok)
        self.assertNotEqual(u["unit"], "build")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "snapshot")

    def test_a_staged_first_send_is_never_handed_out_again(self):
        import asks, job
        asks.request_package(self.conn, "2026-Q3")
        self.drive(A, deliver=True, stop_after="stage")   # staged, then the turn dies
        tok = job.claim(self.conn, A)
        u = job.next_unit(self.conn, tok)
        self.assertNotEqual(u["unit"], "deliver")

    def test_a_refused_filename_settles_the_send_and_says_so(self):
        import asks, db, posting  # noqa: F401
        asks.request_package(self.conn, "2026-Q3")
        did, tok = self.drive_to_staged(A)
        with FakeBroker() as b:
            b.refuse = "bad_filename"
            import tools, qa_server  # noqa: F401
            out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did,
                                                         "package_token": tok})
        self.assertIsNone(out["package"])
        self.assertIn("could not be sent under its name", out["refused"])
        d = self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                              (did,)).fetchone()[0]
        self.assertEqual(d, "failed")
        r = self.conn.execute("SELECT state FROM package_requests").fetchone()[0]
        self.assertEqual(r, "stopped")
        staged = self.conn.execute("SELECT staged_path FROM deliveries WHERE delivery_id=?",
                                   (did,)).fetchone()[0]
        import pathlib
        self.assertFalse(pathlib.Path(staged).exists())        # the staged copy taken back

    def test_send_last_twice_and_resend_after_delivered_refuses(self):
        """§6.1 pin, §19.3: uncertain → "send it again" delivers; then "send me the last
        package you built" twice, each under the package's filename from its own path; a
        resend after a delivered send refuses as v0.9.0."""
        import delivery, posting
        self.delivered_package(first_outcome="uncertain")    # built, first send uncertain
        names, paths = [], []
        for how in ("resend", "last", "last"):
            st = delivery.stage_for_delivery(self.conn, resend=(how == "resend"),
                                             package_id=None if how == "last" else
                                             delivery.resend_target(self.conn),
                                             last_built=(how == "last"))
            with FakeBroker() as b:
                posting.post_package(self.conn, st["delivery_id"])
            names.append(b.deposits[0]["filename"])
            paths.append(b.deposits[0]["value"])
            delivery.record_delivery(self.conn, delivery_id=st["delivery_id"],
                                     outcome="delivered")
        self.assertEqual(len(set(names)), 1)
        self.assertEqual(len(set(paths)), 3)
        import db
        with self.assertRaises(db.Refusal) as cm:
            delivery.resend_target(self.conn)
        self.assertIn("nothing to send again", str(cm.exception))

    def test_no_email(self):
        import delivery, db
        with self.assertRaises(db.Refusal) as cm:
            delivery.stage_for_delivery(self.conn, channel="email", package_id=1)
        self.assertEqual(str(cm.exception), delivery.EMAIL_GONE)

    def test_an_oversized_package_is_closed_with_its_notice_not_offered_again(self):
        """Plan round 2, Astra S2."""
        import asks, package
        asks.request_package(self.conn, "2026-Q3")
        self.patch(package, "MAX_ZIP_BYTES", 10)       # every zip is oversize
        units = self.drive(A, deliver=True)
        self.assertNotIn("deliver", [u["unit"] for u in units])
        self.assertEqual(units[-1]["unit"], "complete")
        r = self.conn.execute("SELECT state, reason FROM package_requests").fetchone()
        self.assertEqual(r["state"], "stopped")
        self.assertIn("20 MB", r["reason"])

    def test_job_status_serves_a_stale_package_request(self):
        """Plan round 2, Astra S1: a snapshot-done request whose check an import
        superseded is not 'done' — the cursor would requeue and serve it."""
        import asks, job, db
        asks.request_package(self.conn, "2026-Q3")
        self.drive(A, deliver=True, stop_before="build")
        self.import_again()
        with db.tx(self.conn):
            self.assertFalse(job.done(self.conn, A))
        self.assertFalse(job.status(self.conn, A)["done"])
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "snapshot-done")       # done() changed nothing


class Review(StoreCase):
    """Rulings and review carries for Task 11."""

    def test_job_status_is_not_done_while_a_package_is_buildable_or_deliverable(self):
        """Task 10 review: job.status evaluates _sends with no token, read-only inside
        done()'s rolled-back savepoint — never a crash, never a write."""
        import asks, job, package
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="build")[-1]
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM package_requests")]
        self.assertFalse(job.status(self.conn, A)["done"])           # buildable
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT * FROM package_requests")], before)
        package.build_quarterly_package(self.conn, u["quarter"], u["package_token"],
                                        request_id=u["request_id"])
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM package_requests")]
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "built")
        self.assertFalse(job.status(self.conn, A)["done"])           # deliverable
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT * FROM package_requests")], before)
        tok = job.claim(self.conn, A)
        self.assertEqual(job.next_unit(self.conn, tok)["unit"], "deliver")

    def test_two_packages_in_one_claim_each_build_and_deliver_their_own(self):
        """Ruling F8: every request one claim hands out holds the claim's gen, so the
        build is looked up by the unit's request_id, never by the token alone."""
        import asks, job
        self.patch(job, "TURNS_PER_BATCH", 100_000)     # the whole run is one claim
        asks.request_package(self.conn, "2026-Q2")
        asks.request_package(self.conn, "2026-Q3")
        with FakeBroker() as b:
            units = self.drive(A, deliver=True)
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertEqual(len({u["pass_token"] for u in units}), 1)
        builds = [u for u in units if u["unit"] == "build"]
        delivers = [u for u in units if u["unit"] == "deliver"]
        self.assertEqual(len(builds), 2)
        self.assertEqual(len(delivers), 2)
        self.assertEqual(builds[0]["package_token"], builds[1]["package_token"])
        rows = self.conn.execute("SELECT r.quarter, r.state, p.quarter FROM package_requests r"
                                 " JOIN packages p ON p.package_id=r.package_id ORDER BY"
                                 " r.request_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2026-Q2", "delivered", "2026-Q2"),
                                                    ("2026-Q3", "delivered", "2026-Q3")])
        names = [d["filename"] for d in b.deposits if d["slot"] == "package"]
        self.assertEqual(len(set(names)), 2)

    def test_build_and_deliver_earn_their_credits(self):
        """§6.1: req:pkg:<id>:built and req:pkg:<id>:delivered, once each."""
        import asks
        rid = asks.request_package(self.conn, "2026-Q3")["request_id"]
        with FakeBroker():
            self.drive(A, deliver=True)
        keys = {r[0] for r in self.conn.execute("SELECT key FROM credits")}
        self.assertIn(f"req:pkg:{rid}:built", keys)
        self.assertIn(f"req:pkg:{rid}:delivered", keys)

    def test_an_uncertain_first_send_earns_delivered_and_its_notice_is_posted(self):
        import asks
        rid = asks.request_package(self.conn, "2026-Q3")["request_id"]
        with FakeBroker():
            self.drive(A, deliver=True, package_receipt=False)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "uncertain")
        self.assertIn(f"req:pkg:{rid}:delivered",
                      {r[0] for r in self.conn.execute("SELECT key FROM credits")})
        a = self.conn.execute("SELECT render_id FROM alerts WHERE kind='package-uncertain'"
                              ).fetchone()[0]
        self.assertIsNotNone(self.conn.execute("SELECT delivered_at FROM renders WHERE"
                                               " render_id=?", (a,)).fetchone()[0])

    def test_a_send_last_caption_says_when_it_was_built(self):
        import delivery, posting
        self.delivered_package()
        st = delivery.stage_for_delivery(self.conn, last_built=True)
        with FakeBroker() as b:
            out = posting.post_package(self.conn, st["delivery_id"])
        self.assertTrue(out["package"].startswith("casa-cap-"))
        self.assertIn(", as it was then", b.deposits[0]["caption"])
        self.assertNotIn("\n", b.deposits[0]["caption"])

    def test_post_package_refuses_a_send_not_waiting(self):
        import asks, tools, qa_server  # noqa: F401
        asks.request_package(self.conn, "2026-Q3")
        with FakeBroker() as b:
            self.drive(A, deliver=True)
            did = self.conn.execute("SELECT delivery_id FROM deliveries").fetchone()[0]
            out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        self.assertIsNone(out["package"])
        self.assertIn("no longer waiting", out["refused"])
        self.assertEqual(sum(1 for d in b.deposits if d["slot"] == "package"), 1)

    def test_post_package_checks_the_package_token(self):
        import asks, tools, qa_server  # noqa: F401
        asks.request_package(self.conn, "2026-Q3")
        did, tok = self.drive_to_staged(A)
        with FakeBroker() as b:
            out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        self.assertIsNone(out["package"])
        self.assertEqual(b.deposits, [])

    def test_the_manifest_declares_the_delivered_filename(self):
        import pathlib
        m = json.loads((pathlib.Path(__file__).resolve().parents[1] / ".claude-plugin"
                        / "plugin.json").read_text())
        self.assertEqual(m["casa"]["resultContract"]["tools"]["post_package"],
                         {"result": "capability", "provides": ["package"],
                          "delivers": {"package": "operator_file"}, "filename": True})

    def test_no_tool_description_names_ellen_a_resident_or_job_report(self):
        import tools, qa_server  # noqa: F401
        for name, t in qa_server.TOOLS.items():
            text = json.dumps([t.get("description"), t["schema"]])
            for word in ("Ellen", "resident", "job_report", "send_media", "send_email"):
                self.assertNotIn(word, text, (name, word))
