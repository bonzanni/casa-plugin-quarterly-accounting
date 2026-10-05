"""T16 phase 1 (live test of S7): #43 — the job's deliver unit, carried out the way the
quarterly-job skill says, through the real tools; #44 — "send it again" on the package
note and on the file; the desk's "show me the X payment" and partial vendor names."""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker

A = "aaaaaaaa-1"


class DeliverWithPassToken(StoreCase):
    """#43: job._choose ends the package pass before _sends hands out build/deliver, so the
    unit's pass_token names no live pass; the deliver writes are fenced by the claim's
    package_token (check_package_token), never by the dead pass marker."""

    def test_the_deliver_unit_stages_posts_and_records_with_the_units_pass_token(self):
        import asks, db, tools, qa_server  # noqa: F401
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        self.assertEqual(u["unit"], "deliver")
        # the package pass ended at the build: no live marker while deliver is handed out
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)
        self.assertIn("pass_token", u)                       # job._account hands it out
        T = qa_server.TOOLS
        try:
            st = T["stage_for_delivery"]["fn"]({"package_id": u["package_id"],
                                                "package_token": u["package_token"],
                                                "pass_token": u["pass_token"]})
        except db.Refusal as e:
            self.fail(f"#43: deliver unit refused at staging: {e}")
        with FakeBroker():
            out = T["post_package"]["fn"]({"delivery_id": st["delivery_id"],
                                           "package_token": u["package_token"],
                                           "pass_token": u["pass_token"]})
        self.assertIsNotNone(out["package"], out)
        T["record_delivery"]["fn"]({"delivery_id": st["delivery_id"], "outcome": "delivered",
                                    "package_token": u["package_token"],
                                    "pass_token": u["pass_token"]})
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "delivered")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_staging_twice_with_the_pass_token_returns_the_same_send(self):
        import asks, qa_server
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        args = {"package_id": u["package_id"], "package_token": u["package_token"],
                "pass_token": u["pass_token"]}
        st = qa_server.TOOLS["stage_for_delivery"]["fn"](dict(args))
        again = qa_server.TOOLS["stage_for_delivery"]["fn"](dict(args))
        self.assertEqual((again["delivery_id"], again.get("already")), (st["delivery_id"], True))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_a_superseded_claims_tokens_still_stage_nothing(self):
        """The fence the fix leans on: check_package_token refuses an older claim's gen."""
        import asks, db, job, qa_server
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        job.claim(self.conn, A)                                  # a newer turn claims
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"package_id": u["package_id"],
                                                         "package_token": u["package_token"],
                                                         "pass_token": u["pass_token"]})
        self.assertIn("no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_a_superseded_claim_posts_and_records_nothing(self):
        """post_package and record_delivery under a superseded claim's tokens (pass_token ==
        package_token) are refused: the send stays staged, never posted. (A send already
        posted is recovered `uncertain` by the newer claim, and its `delivered` upgrade is
        accepted on evidence alone — with or without tokens, as before.)"""
        import asks, db, job, qa_server
        asks.request_package(self.conn, "2026-Q3")
        u = self.drive(A, deliver=True, stop_before="deliver")[-1]
        T = qa_server.TOOLS
        st = T["stage_for_delivery"]["fn"]({"package_id": u["package_id"],
                                            "package_token": u["package_token"],
                                            "pass_token": u["pass_token"]})
        job.claim(self.conn, A)                                  # a newer turn claims
        args = {"delivery_id": st["delivery_id"], "package_token": u["package_token"],
                "pass_token": u["pass_token"]}
        with FakeBroker() as b:
            out = T["post_package"]["fn"](dict(args))
        self.assertEqual((out["package"], len(b.deposits)), (None, 0))
        self.assertIn("no longer the current one", out["refused"])
        with self.assertRaises(db.Refusal):
            T["record_delivery"]["fn"]({**args, "outcome": "uncertain"})
        self.assertEqual(self.conn.execute("SELECT status, posted_at FROM deliveries")
                         .fetchone()[:], ("staged", None))

    def test_a_pass_token_never_admits_a_send_bound_to_no_request(self):
        """last_built with a dead pass_token is refused, even when it equals the
        package_token: no request, no package fence to lean on."""
        import db, qa_server
        self.delivered_package()
        n = self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        g = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"last_built": True, "pass_token": g,
                                                         "package_token": g})
        self.assertIn("this pass is no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n)

    def test_a_pass_token_never_admits_a_resend(self):
        """A resend ("send it again") is bound to no request: a dead pass_token is refused
        even when it equals the package_token, early and in the committing transaction."""
        import db, delivery, qa_server
        self.delivered_package(first_outcome="uncertain")
        g = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        n = self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True, "pass_token": g,
                                                         "package_token": g})
        self.assertIn("this pass is no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n)
        # the committing transaction's own fence (past the early check)
        from unittest import mock
        real, calls = delivery._check_pass, []

        def late_only(*a):
            calls.append(a)
            if len(calls) > 1:
                real(*a)
        with mock.patch.object(delivery, "_check_pass", late_only):
            with self.assertRaises(db.Refusal):
                qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True, "pass_token": g,
                                                             "package_token": g})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n)
        # control: without the dead token the resend stages
        st = qa_server.TOOLS["stage_for_delivery"]["fn"]({"resend": True})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], n + 1)
        with FakeBroker():
            qa_server.TOOLS["post_package"]["fn"]({"delivery_id": st["delivery_id"]})
        # its record, too, keeps the pass fence: no request, so no package fence instead
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["record_delivery"]["fn"]({"delivery_id": st["delivery_id"],
                                                      "outcome": "delivered",
                                                      "pass_token": g, "package_token": g})
        self.assertIn("this pass is no longer the current one", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (st["delivery_id"],)).fetchone()[0], "staged")

    def test_a_dead_pass_says_so_in_plain_words(self):
        """Fix B: a dead marker never claims that another turn continued the pass."""
        import db, passes
        self.delivered_package()
        with self.assertRaises(db.Refusal) as cm:
            passes.check_token(self.conn, 999)
        self.assertTrue(str(cm.exception).startswith("this pass is no longer the current one"))
        self.assertNotIn("another turn", str(cm.exception))

    def test_the_skill_and_the_tools_say_the_deliver_unit_passes_its_package_token(self):
        """Fix B: the deliver section and the three tools' descriptions name the
        package_token as what admits a deliver unit's calls."""
        import pathlib, qa_server, tools  # noqa: F401
        root = pathlib.Path(__file__).resolve().parents[1]
        job = (root / "skills/quarterly-job/SKILL.md").read_text()
        deliver = " ".join(job[job.index("### `deliver`"):job.index("## Never")].split())
        self.assertIn("Each of the three calls below takes the unit's `package_token`: the "
                      "check's pass has ended, so that token is what admits them.", deliver)
        for n in ("stage_for_delivery", "post_package", "record_delivery"):
            d = qa_server.TOOLS[n]["description"]
            self.assertIn("deliver unit", d, n)
            self.assertIn("package_token", d, n)
