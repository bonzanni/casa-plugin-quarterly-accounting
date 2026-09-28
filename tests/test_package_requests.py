# tests/test_package_requests.py
"""A package request across turns (issue #2): its own rotating token, handed over
inside end_pass; token checks that bind in the committing transaction; a staged
send that a claim takes back before it settles it; staged paths never reused; and
the package notices that tell the operator what a continuation owes them."""
import json
import os
import pathlib
import threading
import unittest
from unittest import mock

from tests.test_continuation import STALE, Flow
from tests import sim
import alerts  # noqa: E402
import db  # noqa: E402
import delivery  # noqa: E402
import package  # noqa: E402
import passes  # noqa: E402
import steps  # noqa: E402

TAKEN = "refused: this package request has been taken over by a later turn"


class Requests(Flow):
    def snapshot(self, channel="telegram", finish=True, stopped=None, do_import=True):
        """Packaging step 1 up to the claim: begin_pass(package), the snapshot step,
        the specialist's probes, import and quarter sweep, its finish."""
        self.assertIsNone(self.claim()["continue"])
        t = self.begin("package")
        self.start(t, step="snapshot", quarter="2026-Q3", channel=channel)
        if do_import:
            self.probe_import(t)
            self.sweep(t, quarter="2026-Q3")
        if finish:
            self.call("record_step", pass_token=t, step="snapshot", action="finish",
                      remaining_in_cycle=0, **({"stopped": stopped} if stopped else {}))
        return t

    def handed_over(self, channel="telegram"):
        """Through end_pass: returns (end_pass's answer, the claim's pass token)."""
        self.snapshot(channel)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass-then-build")
        return self.call("end_pass", pass_token=c["pass_token"], outcome="complete"), c

    def built(self, channel="telegram"):
        end, _ = self.handed_over(channel)
        p = end["package_token"]
        return p, self.call("build_quarterly_package", quarter="2026-Q3", package_token=p)

    def staged(self, channel="telegram"):
        p, pkg = self.built(channel)
        d = self.call("stage_for_delivery", channel=channel, package_id=pkg["package_id"],
                      package_token=p)
        return p, pkg, d

    def request(self):
        return self.conn.execute("SELECT * FROM package_requests ORDER BY request_id DESC"
                                 " LIMIT 1").fetchone()

    def alerts_of(self, kind):
        return self.conn.execute("SELECT * FROM alerts WHERE kind=?", (kind,)).fetchall()

    def fill_alerts(self, n=16):
        """Older undelivered alerts that fill more than one message on their own."""
        with db.tx(self.conn):
            for i in range(n):
                self.conn.execute("INSERT INTO alerts(kind, occurrence_key, detail, raised_at)"
                                  " VALUES ('gmail', ?, ?, ?)",
                                  (f"gmail:old#{i}", db.canonical({"detail": "x" * 300}),
                                   db.now()))

    def scope(self, speak):
        return json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                            (speak["render_id"],)).fetchone()[0])


class TestHandOver(Requests):
    def test_end_pass_hands_the_request_its_own_token_and_nothing_can_claim_it_between(self):
        # (9)
        self.seed(1, documents=1)
        end, c = self.handed_over()
        self.assertEqual(c["request"]["quarter"], "2026-Q3")
        p = end["package_token"]
        self.assertNotEqual(p, c["pass_token"])
        self.assertEqual((end["next"], end["request"]["state"]), ("build", "snapshot-done"))
        other = db.open_store()
        self.addCleanup(other.close)
        self.assertEqual(steps.claim(other), {"continue": None, "held": True})
        self.assertEqual(self.text("build_quarterly_package", quarter="2026-Q3"),
                         "refused: missing argument(s): package_token")
        self.assertTrue(self.text("build_quarterly_package", quarter="2026-Q3",
                                  package_token=c["pass_token"]).startswith(TAKEN))
        self.assertTrue(self.text("build_quarterly_package", quarter="2026-Q2",
                                  package_token=p).startswith(
                                      "refused: this package_token is for the Q3 2026"))
        pkg = self.call("build_quarterly_package", quarter="2026-Q3", package_token=p)
        r = self.request()
        self.assertEqual((r["state"], r["package_id"]), ("built", pkg["package_id"]))

    def test_staging_twice_returns_the_same_send(self):
        # (10)
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        again = self.call("stage_for_delivery", channel="telegram",
                          package_id=pkg["package_id"], package_token=p)
        self.assertEqual((again["delivery_id"], again["path"], again["filename"]),
                         (d["delivery_id"], d["path"], pkg["filename"]))
        self.assertTrue(again["already"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
        self.assertEqual(os.listdir(self.outbox), [os.path.basename(d["path"])])
        self.assertTrue(self.text("stage_for_delivery", channel="telegram",
                                  package_id=pkg["package_id"]).startswith(
                                      "refused: this package belongs to a package request"))
        self.assertTrue(self.text("record_delivery", delivery_id=d["delivery_id"],
                                  outcome="delivered").startswith(
                                      "refused: this package belongs to a package request"))
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="delivered",
                        package_token=p)
        self.assertEqual(out["status"], "delivered")
        self.assertEqual(self.request()["state"], "delivered")
        self.assertEqual(self.claim(), {"continue": None, "speak": None})

    def test_a_stopped_snapshot_is_told_once_through_its_notice(self):
        # (14, double tell)
        self.seed(1)
        self.snapshot(stopped="the bound account is gone from bank-feed")
        c = self.claim()["continue"]
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="stopped")
        self.assertIsNone(end["next"])
        self.assertEqual(end["request"]["state"], "stopped")
        self.assertEqual(end["speak"]["text"], "I couldn't build the Q3 2026 package: the bound "
                                               "account is gone\nfrom bank-feed.")
        self.assertTrue(self.text("end_pass", pass_token=c["pass_token"],
                                  outcome="stopped").startswith(STALE))
        first, second = self.claim(), self.claim()
        self.assertEqual(first["speak"], end["speak"])
        self.assertEqual(second["speak"], end["speak"])
        self.assertEqual(len(self.alerts_of("package-stopped")), 1)
        self.assertTrue(self.text("build_quarterly_package", quarter="2026-Q3",
                                  package_token=end["package_token"]).startswith(
                                      "refused: the Q3 2026 package was already stopped"))
        self.call("mark_rendering_delivered", render_id=end["speak"]["render_id"])
        self.assertEqual(self.claim(), {"continue": None, "speak": None})


    def test_a_request_whose_holder_keeps_writing_is_not_claimed(self):
        self.seed(1, documents=1)
        end, _ = self.handed_over()
        self.clock.advance(590)
        self.call("build_quarterly_package", quarter="2026-Q3", package_token=end["package_token"])
        self.clock.advance(310)
        self.assertEqual(self.claim(), {"continue": None, "held": True})
        self.clock.advance(290)
        self.assertEqual(self.claim()["continue"]["next"], "stage")

    def test_a_newer_request_for_the_quarter_supersedes_one_not_yet_staged(self):
        self.seed(1, documents=1)
        p1, pkg = self.built()
        p2, _ = self.built()
        self.assertTrue(self.text("stage_for_delivery", channel="telegram",
                                  package_id=pkg["package_id"], package_token=p1)
                        .startswith(TAKEN))
        states = [r[0] for r in self.conn.execute("SELECT state FROM package_requests"
                                                  " ORDER BY request_id")]
        self.assertEqual(states, ["superseded", "built"])
        self.assertNotEqual(p1, p2)


class TestAStaleHolderCannotSend(Requests):
    def test_a_reclaimed_staged_send_is_taken_back_before_it_is_settled(self):
        # (11) and (12b)
        self.seed(1, documents=1)
        p1, pkg, d = self.staged()
        x = pathlib.Path(d["path"])
        self.assertTrue(x.exists())
        self.clock.advance(steps.LEASE_S)
        r = self.claim()
        c = r["continue"]
        p2 = c["package_token"]
        self.assertNotEqual(p2, p1)
        self.assertIsNone(c["next"])
        self.assertEqual(r["speak"]["text"], "\n".join(delivery.offer_lines(pkg["filename"])))
        self.assertEqual(self.scope(r["speak"])["offers"], [pkg["package_id"]])
        self.assertFalse(x.exists())                                     # nothing to send
        row = self.conn.execute("SELECT status, withdrawn_at FROM deliveries").fetchone()
        self.assertEqual(row["status"], "uncertain")
        self.assertIsNotNone(row["withdrawn_at"])
        self.assertEqual(self.request()["state"], "withdrawn")
        # the stalled holder: refused at staging and at settling, with or without a token
        # (only a report that it WAS delivered is taken, as evidence: TestAnEmailWaitsForItsTap)
        self.assertTrue(self.text("stage_for_delivery", channel="telegram",
                                  package_id=pkg["package_id"], package_token=p1)
                        .startswith(TAKEN))
        for tok in ({"package_token": p1}, {}):
            for outcome in ("uncertain", "failed"):
                self.assertTrue(self.text("record_delivery", delivery_id=d["delivery_id"],
                                          outcome=outcome, **tok).startswith("refused: "),
                                (tok, outcome))
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries").fetchone()[0],
                         "uncertain")
        # an import that lands next revokes nothing new
        out = sim.run_pass(self.conn, self.bf)
        self.assertEqual(out["import"]["revoked_deliveries"], [])
        self.assertEqual(os.listdir(self.outbox), [])
        # the offer, delivered, binds "send it again" to exactly that package
        speak = out["end"]["speak"]
        self.assertEqual(speak["render_id"], r["speak"]["render_id"])     # the same offer
        self.call("mark_rendering_delivered", render_id=speak["render_id"])
        self.assertIn("resend", self.call("apply_reply", text="send it again")["instructions"])
        y = self.call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(y["filename"], pkg["filename"])
        self.assertNotEqual(y["path"], d["path"])
        self.assertFalse(x.exists())
        self.assertEqual(pathlib.Path(y["path"]).read_bytes(),
                         pathlib.Path(pkg["path"]).read_bytes())
        self.call("record_delivery", delivery_id=y["delivery_id"], outcome="uncertain")
        os.unlink(y["path"])
        speak = self.claim()["speak"]
        self.call("mark_rendering_delivered", render_id=speak["render_id"])
        z = self.call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(len({d["path"], y["path"], z["path"]}), 3)       # two resends, two paths

    def test_the_email_entry_is_taken_back_too(self):
        self.seed(1, documents=1)
        _, _, d = self.staged(channel="email")
        entry = pathlib.Path(d["path"]).parent
        self.assertTrue(entry.exists())
        self.clock.advance(delivery.EMAIL_RECOVERY_LEASE_S)        # an email waits for its tap
        self.assertIsNone(self.claim()["continue"]["next"])
        self.assertFalse(entry.exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores the mode")
    def test_a_removal_that_fails_refuses_the_claim_and_changes_nothing(self):
        self.seed(1, documents=1)
        p1, _, d = self.staged()
        os.chmod(self.outbox, 0o500)
        self.addCleanup(os.chmod, self.outbox, 0o770)
        self.clock.advance(steps.LEASE_S)
        before = self.digest()
        self.assertEqual(self.text("continue_pass"),
                         "refused: could not take back a staged package — nothing changed")
        self.assertEqual(self.digest(), before)
        self.assertTrue(pathlib.Path(d["path"]).exists())
        self.assertEqual((self.request()["state"], self.request()["token"]), ("staged", p1))
        os.chmod(self.outbox, 0o770)
        self.assertIsNone(self.claim()["continue"]["next"])
        self.assertFalse(pathlib.Path(d["path"]).exists())

    def test_the_withdrawn_offer_is_in_the_rendering_however_many_alerts_wait(self):
        self.seed(1, documents=1)
        _, pkg, _ = self.staged()
        self.fill_alerts()
        self.clock.advance(steps.LEASE_S)
        speak = self.claim()["speak"]
        self.assertIn("\n".join(delivery.offer_lines(pkg["filename"])), speak["text"])
        self.assertEqual(self.scope(speak)["offers"], [pkg["package_id"]])
        self.assertTrue(speak["text"].endswith(alerts.MORE_CLOSING))


class TestTheBindingCheckIsInTheCommit(Requests):
    """(12a) a holder rotated while it waited for the custody lock commits nothing
    and leaves no bytes behind."""
    def waiting(self, target, spy_on, spy_name):
        """Run `target` in a thread on its own connection; return once it has made its
        early token check (and so is waiting for the custody lock the test holds)."""
        checked, result = threading.Event(), {}
        real = getattr(spy_on, spy_name)

        def spy(*a, **kw):
            out = real(*a, **kw)
            checked.set()
            return out

        def run():
            conn = db.open_store()
            try:
                result["out"] = target(conn)
            except Exception as exc:              # the refusal is the result under test
                result["out"] = exc
            finally:
                conn.close()
        p = mock.patch.object(spy_on, spy_name, spy)
        p.start()
        self.addCleanup(p.stop)
        t = threading.Thread(target=run)
        return t, checked, result

    def test_a_build_rotated_while_it_waited_registers_nothing(self):
        self.seed(1, documents=1)
        end, _ = self.handed_over()
        p1 = end["package_token"]
        with db.custody_lock():
            t, checked, result = self.waiting(
                lambda c: package.build_quarterly_package(c, "2026-Q3", p1),
                package, "_request_for_build")
            t.start()
            self.assertTrue(checked.wait(30))
            self.clock.advance(steps.LEASE_S)
            p2 = self.claim()["continue"]["package_token"]
            self.assertNotEqual(p2, p1)
        t.join(60)
        self.assertIsInstance(result["out"], db.Refusal)
        self.assertTrue(str(result["out"]).startswith("this package request has been taken"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 0)
        self.assertEqual(os.listdir(db.data_dir() / "packages"), [])
        self.assertEqual(self.request()["state"], "snapshot-done")

    def test_a_stage_rotated_while_it_waited_stages_nothing(self):
        self.seed(1, documents=1)
        p1, pkg = self.built()
        with db.custody_lock():
            t, checked, result = self.waiting(
                lambda c: delivery.stage_for_delivery(c, channel="telegram",
                                                      package_id=pkg["package_id"],
                                                      package_token=p1),
                passes, "check_package_token")
            t.start()
            self.assertTrue(checked.wait(30))
            self.clock.advance(steps.LEASE_S)
            c = self.claim()["continue"]
            self.assertEqual(c["next"], "stage")
        t.join(60)
        self.assertIsInstance(result["out"], db.Refusal)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.outbox), [])
        d = self.call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                      package_token=c["package_token"])
        self.assertTrue(pathlib.Path(d["path"]).exists())


    def test_a_stage_rotated_while_it_wrote_its_copy_records_nothing_and_removes_it(self):
        # the copy is written between the early checks and the committing transaction
        self.seed(1, documents=1)
        p1, pkg = self.built()
        other = db.open_store()
        self.addCleanup(other.close)
        real = delivery._to_outbox

        def write_then_rotate(path, data):
            out = real(path, data)
            self.clock.advance(steps.LEASE_S)
            self.assertEqual(steps.claim(other)["continue"]["next"], "stage")
            return out
        with mock.patch.object(delivery, "_to_outbox", write_then_rotate):
            self.assertTrue(self.text("stage_for_delivery", channel="telegram",
                                      package_id=pkg["package_id"], package_token=p1)
                            .startswith(TAKEN))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.outbox), [])
        self.assertEqual(self.request()["state"], "built")


class TestPackageNotices(Requests):
    def test_an_uncertain_send_is_offered_until_delivered(self):
        # (12c)
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.assertEqual(len(self.alerts_of("package-uncertain")), 1)
        speak = out["speak"]
        self.assertEqual(speak["text"], "\n".join(delivery.offer_lines(pkg["filename"])))
        self.assertEqual(self.scope(speak)["offers"], [pkg["package_id"]])
        self.assertEqual(self.request()["state"], "uncertain")
        # a "crash" before mark_rendering_delivered: the next turn offers the same text
        again = self.claim()["speak"]
        self.assertEqual(again, speak)
        self.call("mark_rendering_delivered", render_id=speak["render_id"])
        self.assertIsNone(self.claim()["speak"])
        os.unlink(d["path"])
        self.assertIn("resend", self.call("apply_reply", text="send it again")["instructions"])
        self.assertEqual(self.call("stage_for_delivery", channel="telegram",
                                   resend=True)["filename"], pkg["filename"])

    def test_the_new_notice_is_in_the_rendering_however_many_alerts_wait(self):
        # record_delivery's own notice is never deferred behind older alerts
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.fill_alerts()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                        package_token=p)
        notice = self.alerts_of("package-uncertain")[0]["alert_id"]
        speak = out["speak"]
        self.assertIn("\n".join(delivery.offer_lines(pkg["filename"])), speak["text"])
        scope = self.scope(speak)
        self.assertIn(notice, scope["alerts"])
        self.assertEqual(scope["offers"], [pkg["package_id"]])
        self.assertTrue(speak["text"].endswith(alerts.MORE_CLOSING))       # the rest waits
        self.assertLess(len(scope["alerts"]), 17)
        import views
        self.assertLessEqual(views.utf16_len(speak["text"]), views.TELEGRAM_LIMIT)
        self.call("mark_rendering_delivered", render_id=speak["render_id"])
        rest = self.claim()["speak"]                                      # the older ones follow
        self.assertNotIn("may not have arrived", rest["text"])

    def test_a_revoked_send_raises_one_notice(self):
        # (12) the next pass's import revokes an unsent first send
        self.seed(1, documents=1)
        _, pkg, d = self.staged()
        out = sim.run_pass(self.conn, self.bf)      # its import supersedes the build's
        self.assertEqual(out["import"]["revoked_deliveries"], [d["delivery_id"]])
        self.assertEqual(self.request()["state"], "revoked")
        self.assertEqual(len(self.alerts_of("package-revoked")), 1)
        self.assertEqual(out["end"]["speak"]["text"],
                         "The bank was re-read before I could send the Q3 2026 package —\n"
                         "ask for it again and I'll rebuild it.")
        self.assertFalse(pathlib.Path(d["path"]).exists())


class TestReclaimRecovers(Requests):
    def test_a_reclaimed_snapshot_pass_is_recovered_to_its_build(self):
        # (13)
        self.seed(1, documents=1)
        t = self.snapshot()
        self.clock.advance(passes.STALE_AFTER_S)
        out = self.call("begin_pass", trigger="operator")
        rid = self.request()["request_id"]
        self.assertEqual((out["reclaimed"], out["recovered"]), (True, rid))
        old = self.conn.execute("SELECT * FROM passes WHERE generation=?", (t,)).fetchone()
        self.assertEqual(old["outcome"], "interrupted")
        self.assertIsNotNone(old["ended_at"])
        c = self.claim()["continue"]
        self.assertEqual((c["next"], c["request"]["id"]), ("build", rid))
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=c["package_token"])
        self.assertEqual(self.request()["package_id"], pkg["package_id"])

    def test_an_unfinished_snapshot_fails_with_one_notice_until_delivered(self):
        # (13, variant) and (14, lost crash): the pass died before it read the bank
        self.seed(1)
        self.snapshot(finish=False, do_import=False)
        self.clock.advance(passes.STALE_AFTER_S)
        out = self.call("begin_pass", trigger="operator")
        self.assertEqual(self.request()["state"], "recovery-failed")
        self.assertEqual(out["recovered"], self.request()["request_id"])
        self.assertEqual(len(self.alerts_of("package-failed")), 1)
        first = self.claim()
        self.assertNotIn("speak", first)                 # the new pass is running: not now
        self.call("end_pass", pass_token=out["pass_token"], outcome="failed")
        speak = self.claim()["speak"]
        self.assertEqual(speak["text"], "I couldn't read the bank for the Q3 2026 package — ask "
                                        "for it\nagain.")
        # the turn "crashes" before marking it delivered: the next one offers it again
        self.assertEqual(self.claim()["speak"], speak)
        self.call("mark_rendering_delivered", render_id=speak["render_id"])
        self.assertEqual(self.claim(), {"continue": None, "speak": None})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE sent_at IS NULL")
                         .fetchone()[0], 0)


class TestHandoverPairing(Requests):
    def test_the_documents_carry_their_recorded_pairing(self):
        # (16)
        self.seed(2, documents=1)
        self.assertEqual(len(sim.run_pass(self.conn, self.bf)["triage"]["matched"]), 1)
        matched = self.conn.execute("SELECT doc_id, pid FROM match_state WHERE state='matched'"
                                    ).fetchone()
        other = self.conn.execute("SELECT pid FROM projections WHERE pid<>?",
                                  (matched["pid"],)).fetchone()[0]
        proposed = self.doc(amount_minor=1001)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                              " activation) VALUES (999, ?, ?, 'proposed', 'auto', 1)",
                              (other, proposed))
        for _ in range(60):
            self.doc(amount_minor=777)                 # more than a page of unmatched ones
        unpaired = self.doc(amount_minor=555)
        irrelevant = self.doc(amount_minor=444)
        self.call("mark_irrelevant", doc_id=irrelevant)
        t = self.begin("handover")
        self.start(t, step="handover", doc_ids=[matched["doc_id"], proposed, unpaired,
                                                irrelevant])
        self.specialist(t, step="handover")
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass-then-case")
        got = {d["doc_id"]: d for d in c["documents"]}
        self.assertEqual(got[matched["doc_id"]]["pairing"], "matched")
        self.assertEqual(got[matched["doc_id"]]["payment"],
                         {"date": "2026-07-05", "amount_minor": 1000, "currency": "EUR",
                          "payee": "Adobe"})
        self.assertEqual(got[proposed]["pairing"], "proposed")
        self.assertEqual(got[unpaired], {"doc_id": unpaired, "pairing": "unpaired"})
        self.assertEqual(got[irrelevant]["pairing"], "irrelevant")
        self.call("end_pass", pass_token=c["pass_token"], outcome="complete")



class TestReviewC1(Requests):
    """Code review round C1 on the package request."""
    def test_one_request_builds_one_package(self):
        # A1: a second build under the same token would unlink the first package from
        # its request, and that package could then be sent outside the send-once rule
        self.seed(1, documents=1)
        p, pkg = self.built()
        self.assertTrue(self.text("build_quarterly_package", quarter="2026-Q3",
                                  package_token=p).startswith(
                                      "refused: this package request already built its package"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 1)
        self.assertEqual(self.request()["package_id"], pkg["package_id"])

    def test_the_second_build_is_refused_in_the_registering_transaction_too(self):
        self.seed(1, documents=1)
        end, _ = self.handed_over()
        p = end["package_token"]
        real = package._freeze

        def freeze_after_another_build(conn, quarter):
            frozen = real(conn, quarter)
            other = db.open_store()
            try:            # another turn holding the same token registers first
                with mock.patch.object(package, "_freeze", real):
                    package.build_quarterly_package(other, "2026-Q3", p)
            finally:
                other.close()
            return frozen
        with mock.patch.object(package, "_freeze", freeze_after_another_build), \
                mock.patch.object(db, "custody_lock", _no_lock):
            self.assertTrue(self.text("build_quarterly_package", quarter="2026-Q3",
                                      package_token=p).startswith(
                                          "refused: this package request already built"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 1)
        self.assertEqual(len(os.listdir(db.data_dir() / "packages")), 1)

    def test_a_reclaimed_stopped_snapshot_is_told_not_built(self):
        # I4
        self.seed(1)
        self.snapshot(stopped="the bound account is gone from bank-feed")
        self.clock.advance(passes.STALE_AFTER_S)
        out = self.call("begin_pass", trigger="operator")
        self.assertEqual(out["recovered"], self.request()["request_id"])
        self.assertEqual(self.request()["state"], "stopped")
        self.assertEqual(len(self.alerts_of("package-stopped")), 1)
        self.assertEqual(self.alerts_of("package-failed"), [])
        self.assertNotIn("continue", {k for k, v in self.claim().items() if v})
        self.call("end_pass", pass_token=out["pass_token"], outcome="failed")
        self.assertIn("the bound account is gone", self.claim()["speak"]["text"])

    def test_a_failed_snapshot_pass_hands_over_nothing_to_build(self):
        # I5: a pass that read no bank must not build from an older import
        self.seed(1)
        t = self.begin("package")
        self.start(t, step="snapshot", quarter="2026-Q3", channel="telegram")
        self.call("record_step", pass_token=t, step="snapshot", action="finish", failed=True)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass-then-build")
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="failed")
        self.assertIsNone(end["next"])
        self.assertEqual(end["request"]["state"], "recovery-failed")
        self.assertEqual(end["speak"]["text"], "I couldn't read the bank for the Q3 2026 "
                                               "package — ask for it\nagain.")
        self.assertTrue(self.text("build_quarterly_package", quarter="2026-Q3",
                                  package_token=end["package_token"]).startswith("refused: "))

    def test_a_closed_request_is_refused_in_words_not_as_taken_over(self):
        # I7: "taken over" makes Ellen stop in silence; a finished request is said plainly
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.call("record_delivery", delivery_id=d["delivery_id"], outcome="delivered",
                  package_token=p)
        text = self.text("stage_for_delivery", channel="email", package_id=pkg["package_id"],
                         package_token=p)
        self.assertEqual(text, "refused: the Q3 2026 package was already sent — nothing "
                               "changed. To have it again, or by email, ask for the package "
                               "again.")
        import views
        for word in views.FORBIDDEN:
            self.assertNotIn(word, text)

    def test_a_request_is_staged_on_its_own_channel(self):
        # M4
        self.seed(1, documents=1)
        p, pkg = self.built()
        self.assertEqual(self.text("stage_for_delivery", channel="email",
                                   package_id=pkg["package_id"], package_token=p),
                         "refused: this package was asked for by telegram — stage it by "
                         "telegram, or ask for the package again by email")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)


@__import__("contextlib").contextmanager
def _no_lock(*a, **kw):
    yield


STEP_STATES = ("none", "started", "finished", "failed", "stopped")
OUTCOMES = ("complete", "interrupted", "stopped", "failed")
IMPORTS = ("none", "this pass")          # "none": only an older pass's import exists


def expected_fate(imported, step_state, outcome):
    """The one rule for a snapshot request's fate. Buildable only from this pass's own
    import (a `snapshots` row of this pass: the evidence it read the bank — never the
    step's say-so), and only when neither the stored finish nor the outcome says
    stopped, and the outcome is not failed. Stopped when either says so. Otherwise the
    bank was not read for it."""
    if step_state == "stopped" or outcome == "stopped":
        return "stopped"
    if imported == "this pass" and outcome != "failed":
        return "snapshot-done"
    return "recovery-failed"


class TestSnapshotFate(Requests):
    """Every path that moves a request out of `snapshot` decides its fate by one
    function, from the evidence in the store and the outcome."""
    def pass_in(self, step_state, imported):
        self.assertIsNone(self.claim()["continue"])
        t = self.begin("package")
        if step_state == "none":             # a request whose step row never landed
            with db.tx(self.conn):
                self.conn.execute(
                    "INSERT INTO package_requests(quarter, channel, pass_id, state, created_at,"
                    " updated_at) VALUES ('2026-Q3', 'telegram', ?, 'snapshot', ?, ?)",
                    (self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0],
                     db.now(), db.now()))
        else:
            self.start(t, step="snapshot", quarter="2026-Q3", channel="telegram")
        if imported == "this pass":
            self.probe_import(t)
        if step_state not in ("none", "started"):
            extra = {"failed": {"failed": True},
                     "stopped": {"stopped": "the bound account is gone from bank-feed"}}
            self.call("record_step", pass_token=t, step="snapshot", action="finish",
                      **extra.get(step_state, {}))
        return t

    def check(self, want, where):
        r = self.request()
        self.assertEqual(r["state"], want, where)
        kinds = [a["kind"] for a in self.conn.execute(
            "SELECT kind FROM alerts WHERE occurrence_key LIKE ?",
            (f"request:{r['request_id']}:%",))]
        self.assertEqual(kinds, {"stopped": ["package-stopped"],
                                 "recovery-failed": ["package-failed"],
                                 "snapshot-done": []}[want], where)
        if want == "stopped":
            self.assertTrue(r["reason"], where)
        return r

    def test_every_combination_through_end_pass(self):
        self.seed(1)
        for imported in IMPORTS:
            for step_state in STEP_STATES:
                for outcome in OUTCOMES:
                    where = (imported, step_state, outcome, "end_pass")
                    t = self.pass_in(step_state, imported)
                    end = self.call("end_pass", pass_token=t, outcome=outcome)
                    want = expected_fate(imported, step_state, outcome)
                    self.check(want, where)
                    self.assertEqual(end["next"], "build" if want == "snapshot-done" else None,
                                     where)
                    if want == "snapshot-done":        # built, so the next case starts clean
                        self.call("build_quarterly_package", quarter="2026-Q3",
                                  package_token=end["package_token"])
                    if end["speak"]:
                        self.call("mark_rendering_delivered",
                                  render_id=end["speak"]["render_id"])

    def test_every_combination_through_the_reclaim(self):
        # a reclaim ends the displaced pass `interrupted`, whatever it was about to say
        self.seed(1)
        for imported in IMPORTS:
            for step_state in STEP_STATES:
                where = (imported, step_state, "interrupted", "reclaim")
                self.pass_in(step_state, imported)
                self.clock.advance(passes.STALE_AFTER_S)
                out = self.call("begin_pass", trigger="operator")
                self.assertTrue(out["reclaimed"], where)
                want = expected_fate(imported, step_state, "interrupted")
                self.check(want, where)
                if want != "snapshot-done":    # its own notice is in its own answer
                    self.assertIsNotNone(out.get("speak"), where)
                    self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
                self.call("end_pass", pass_token=out["pass_token"], outcome="failed")
                c = self.claim()
                if want == "snapshot-done":
                    self.assertEqual(c["continue"]["next"], "build", where)
                    self.call("build_quarterly_package", quarter="2026-Q3",
                              package_token=c["continue"]["package_token"])
                else:
                    self.assertIsNone(c["continue"], where)
                if c.get("speak"):
                    self.call("mark_rendering_delivered", render_id=c["speak"]["render_id"])

    def test_the_rule_itself(self):
        self.seed(1)
        for imported in IMPORTS:
            for step_state in STEP_STATES:
                for outcome in OUTCOMES + ("interrupted",):
                    t = self.pass_in(step_state, imported)
                    pass_id = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]
                    self.assertEqual(passes.snapshot_fate(self.conn, pass_id, outcome)[0],
                                     expected_fate(imported, step_state, outcome),
                                     (imported, step_state, outcome))
                    self.call("end_pass", pass_token=t, outcome="stopped")

    def test_an_expired_step_after_its_import_builds_from_that_import(self):
        # the step never recorded a finish, but the pass did read the bank
        self.seed(1, documents=1)
        t = self.pass_in("started", "this pass")
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.claim()["continue"]
        self.assertEqual(c["ended"], "expired")
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        self.assertEqual(end["next"], "build")
        self.assertIsNone(end["speak"])


class TestEveryUnsentDeliveryIsTold(Requests):
    """Review C2 (F2): a revoked, failed or uncertain send raises its notice whether or
    not a package request is linked — a resend has none."""
    def resend_revoked(self, channel):
        self.seed(1, documents=1)
        p, pkg, d = self.staged(channel)
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="failed",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        again = self.call("stage_for_delivery", channel=channel, resend=True)
        self.assertEqual(again["filename"], pkg["filename"])
        end = sim.run_pass(self.conn, self.bf)["end"]         # a newer snapshot lands
        revoked = self.conn.execute("SELECT status, revoked_at FROM deliveries WHERE"
                                    " delivery_id=?", (again["delivery_id"],)).fetchone()
        self.assertEqual(revoked["status"], "failed")
        self.assertIsNotNone(revoked["revoked_at"])
        notices = self.conn.execute("SELECT * FROM alerts WHERE kind='package-revoked'"
                                    ).fetchall()
        self.assertEqual([a["occurrence_key"] for a in notices],
                         [f"delivery:{again['delivery_id']}:revoked"])
        line = ("The bank was re-read before I could send the Q3 2026 package —\n"
                "ask for it again and I'll rebuild it.")
        self.assertIn(line, end["speak"]["text"])
        # a crash before it was marked: another session, reopened, offers it again
        other = db.open_store()
        self.addCleanup(other.close)
        self.assertIn(line, steps.claim(other)["speak"]["text"])

    def test_a_revoked_resend_is_told_on_telegram(self):
        self.resend_revoked("telegram")

    def test_a_revoked_resend_is_told_on_email(self):
        self.resend_revoked("email")

    def test_a_failed_or_uncertain_resend_is_told(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        for outcome, kind in (("failed", "package-send-failed"),
                              ("uncertain", "package-uncertain")):
            for f in os.listdir(self.outbox):
                os.unlink(self.outbox / f)
            again = self.call("stage_for_delivery", channel="telegram", resend=True)
            r = self.call("record_delivery", delivery_id=again["delivery_id"], outcome=outcome)
            keys = [a[0] for a in self.conn.execute(
                "SELECT occurrence_key FROM alerts WHERE kind=?", (kind,))]
            self.assertIn(f"delivery:{again['delivery_id']}:{outcome}", keys, outcome)
            self.call("mark_rendering_delivered", render_id=r["speak"]["render_id"])


class TestAStagedSendIsRecoveredByItsDelivery(Requests):
    """Review C3 (G3): a staged package send nobody settles — linked to a request or
    not — is recovered on the DELIVERY once its lease lapses: taken back, settled
    uncertain, and told."""
    def stalled_resend(self, channel):
        self.seed(1, documents=1)
        p, pkg, d = self.staged(channel)
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        if channel == "telegram":
            os.unlink(d["path"])                       # Casa consumed the first copy
        again = self.call("stage_for_delivery", channel=channel, resend=True)
        return pkg, again

    def recovered(self, channel):
        pkg, again = self.stalled_resend(channel)
        staged = pathlib.Path(again["path"])
        target = staged.parent if channel == "email" else staged
        self.assertTrue(target.exists())
        other = db.open_store()                         # a restart: a new session
        self.addCleanup(other.close)
        self.clock.advance(24 * 3600)
        first = steps.claim(other)
        self.assertEqual(first["continue"], {"delivery_id": again["delivery_id"],
                                             "next": None})
        self.assertIn("\n".join(delivery.offer_lines(pkg["filename"])), first["speak"]["text"])
        row = self.conn.execute("SELECT status, withdrawn_at FROM deliveries WHERE"
                                " delivery_id=?", (again["delivery_id"],)).fetchone()
        self.assertEqual(row["status"], "uncertain")
        self.assertIsNotNone(row["withdrawn_at"])
        self.assertFalse(target.exists())
        keys = [a[0] for a in self.conn.execute(
            "SELECT occurrence_key FROM alerts WHERE kind='package-uncertain'")]
        self.assertIn(f"delivery:{again['delivery_id']}:uncertain", keys)
        # nothing further is recovered, and the offer stays until delivered
        self.assertIsNone(steps.claim(other)["continue"])
        out = sim.run_pass(self.conn, self.bf)
        self.assertEqual(out["import"]["revoked_deliveries"], [])
        self.assertIn(pkg["filename"], out["end"]["speak"]["text"])

    def test_a_stalled_resend_is_taken_back_and_told_on_telegram(self):
        self.recovered("telegram")

    def test_a_stalled_resend_is_taken_back_and_told_on_email(self):
        self.recovered("email")

    def test_a_resend_inside_its_lease_is_left_alone(self):
        _, again = self.stalled_resend("telegram")
        self.clock.advance(steps.LEASE_S - 1)
        self.assertIsNone(self.claim()["continue"])
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (again["delivery_id"],)).fetchone()[0], "staged")


    def test_staging_the_same_send_again_renews_its_lease(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.clock.advance(steps.LEASE_S - 10)
        self.call("stage_for_delivery", channel="telegram", package_id=pkg["package_id"],
                  package_token=p)
        self.clock.advance(20)
        self.assertIsNone(self.claim()["continue"])
        self.assertTrue(pathlib.Path(d["path"]).exists())


class TestEveryCallTellsItsOwnNotice(Requests):
    """Review C3 (G2): a tool call that raises an operator notice in its transaction
    returns that notice in its own `speak`, however many older alerts wait. The audit
    of every place a notice is raised is pinned so a new one cannot go unlisted."""
    RAISERS = {("passes", "_close"), ("delivery", "revoke_superseded_first_sends"),
               ("delivery", "record_delivery"), ("delivery", "recover_staged")}

    def test_every_notice_raising_site_is_audited(self):
        import ast
        from tests._base import ROOT
        found = set()
        for f in (ROOT / "server").glob("*.py"):
            tree = ast.parse(f.read_text())
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef):
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) \
                                == "raise_package":
                            found.add((f.stem, fn.name))
        self.assertEqual(found, self.RAISERS)

    def assert_told(self, speak, line, where):
        self.assertIsNotNone(speak, where)
        self.assertIn(line, speak["text"], where)
        self.assertTrue(speak["text"].endswith(alerts.MORE_CLOSING), where)   # others wait
        self.call("mark_rendering_delivered", render_id=speak["render_id"])

    def test_begin_pass_on_a_reclaim(self):
        self.seed(1)
        self.snapshot(stopped="the bound account is gone from bank-feed")
        self.fill_alerts()
        self.clock.advance(passes.STALE_AFTER_S)
        out = self.call("begin_pass", trigger="operator")
        self.assert_told(out["speak"], "I couldn't build the Q3 2026 package", "begin_pass")

    def test_end_pass_tells_its_pass_s_own_revocations(self):
        self.seed(1, documents=1)
        _, pkg, d = self.staged()
        self.fill_alerts()
        end = sim.run_pass(self.conn, self.bf)["end"]         # its import revokes the send
        self.assert_told(end["speak"], "The bank was re-read before I could send the Q3 2026",
                         "end_pass")

    def test_continue_pass_on_a_staged_send(self):
        self.seed(1, documents=1)
        _, pkg, _ = self.staged()
        self.fill_alerts()
        self.clock.advance(steps.LEASE_S)
        self.assert_told(self.claim()["speak"], pkg["filename"], "continue_pass")

    def test_record_delivery(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.fill_alerts()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="failed",
                        package_token=p)
        self.assert_told(out["speak"], "didn't go out", "record_delivery")

    def test_end_pass_on_a_hand_over(self):
        self.seed(1)
        self.snapshot(stopped="the bound account is gone from bank-feed")
        self.fill_alerts()
        c = self.claim()["continue"]
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="stopped")
        self.assert_told(end["speak"], "I couldn't build the Q3 2026 package", "end_pass")


class TestAnEmailWaitsForItsTap(Requests):
    """Review C4 (H2): an email waits for the operator's approval tap, so its staged
    send is not recovered for a day; and a send recovery settled `uncertain` that
    turns out delivered is upgraded by that evidence."""
    def test_an_email_send_has_a_day_before_it_is_recovered(self):
        self.seed(1, documents=1)
        _, _, d = self.staged("email")
        self.clock.advance(steps.LEASE_S + 60)
        self.assertIsNone(self.claim()["continue"])
        self.assertTrue(pathlib.Path(d["path"]).exists())
        self.clock.advance(delivery.EMAIL_RECOVERY_LEASE_S)
        self.assertIsNone(self.claim()["continue"]["next"])
        self.assertFalse(pathlib.Path(d["path"]).exists())

    def recovered(self, channel):
        self.seed(1, documents=1)
        p, pkg, d = self.staged(channel)
        self.clock.advance(delivery.EMAIL_RECOVERY_LEASE_S + 1)
        r = self.claim()
        self.assertIsNone(r["continue"]["next"])
        self.assertIn("may not have arrived", r["speak"]["text"])
        return p, pkg, d

    def test_a_delivery_recorded_after_recovery_upgrades_it(self):
        p, pkg, d = self.recovered("email")
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="delivered",
                        message_id="m-1", package_token=p)
        self.assertEqual(out["status"], "delivered")
        row = self.conn.execute("SELECT status, message_id FROM deliveries WHERE delivery_id=?",
                                (d["delivery_id"],)).fetchone()
        self.assertEqual(tuple(row), ("delivered", "m-1"))
        self.assertGreater(self.conn.execute("SELECT count(*) FROM delivered_rows WHERE"
                                             " package_id=?", (pkg["package_id"],)).fetchone()[0], 0)
        self.assertEqual(self.request()["state"], "delivered")
        stale = self.conn.execute("SELECT sent_at FROM alerts WHERE occurrence_key=?",
                                  (f"delivery:{d['delivery_id']}:uncertain",)).fetchone()
        self.assertIsNotNone(stale["sent_at"])                     # no stale offer remains
        self.assertIsNone(self.claim()["speak"])
        self.assertEqual(self.text("stage_for_delivery", channel="email", resend=True),
                         "refused: nothing is waiting to be sent again")

    def test_a_delivery_recorded_after_recovery_upgrades_it_without_the_token(self):
        _, _, d = self.recovered("telegram")
        self.assertEqual(self.call("record_delivery", delivery_id=d["delivery_id"],
                                   outcome="delivered")["status"], "delivered")

    def test_any_other_late_outcome_for_a_recovered_send_is_refused(self):
        p, _, d = self.recovered("telegram")
        for outcome in ("uncertain", "failed"):
            self.assertTrue(self.text("record_delivery", delivery_id=d["delivery_id"],
                                      outcome=outcome, package_token=p).startswith("refused: "),
                            outcome)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (d["delivery_id"],)).fetchone()[0], "uncertain")
        self.assertTrue(self.text("record_delivery", delivery_id=d["delivery_id"],
                                  outcome="delivered").startswith('{'))


class TestAPackageThatArrivedIsNeverOfferedAgain(Requests):
    """Review C5 (J1): whether "send it again" is offered or honoured is a fact about
    the PACKAGE — has it a delivered send? — never about one delivery."""
    ARRIVED = "refused: the Q3 2026 package did arrive (sent 28 Sep) — nothing to send again"

    def lease(self, channel):
        return delivery.EMAIL_RECOVERY_LEASE_S if channel == "email" else steps.LEASE_S

    def upgraded_then_the_resend_stalls(self, channel):
        self.seed(1, documents=1)
        p, pkg, d1 = self.staged(channel)
        self.clock.advance(self.lease(channel))
        r = self.claim()                                     # D1 recovered: offered
        self.call("mark_rendering_delivered", render_id=r["speak"]["render_id"])
        d2 = self.call("stage_for_delivery", channel=channel, resend=True)
        self.clock.t = self.clock.t.replace(hour=12)
        d1_day = self.clock.t
        self.call("record_delivery", delivery_id=d1["delivery_id"], outcome="delivered",
                  message_id="m-1")                         # evidence: D1 did arrive
        self.clock.advance(self.lease(channel))
        r2 = self.claim()                                    # D2 stalls: recovered quietly
        self.assertIsNone(r2["continue"]["next"])
        if r2.get("speak"):
            self.assertNotIn("send it again", r2["speak"]["text"])
        offers = self.conn.execute("SELECT count(*) FROM alerts WHERE sent_at IS NULL AND"
                                   " kind IN ('package-uncertain', 'package-send-failed')"
                                   ).fetchone()[0]
        self.assertEqual(offers, 0)
        day = f"{d1_day.day} {d1_day.strftime('%b')}"
        self.assertEqual(self.text("stage_for_delivery", channel=channel, resend=True),
                         f"refused: the Q3 2026 package did arrive (sent {day}) — nothing to "
                         "send again")
        view = self.call("build_review", view="status", quarter="2026-Q3")
        self.assertNotIn("send it again", view["text"])

    def test_after_an_upgrade_a_stalled_resend_offers_nothing_on_telegram(self):
        self.upgraded_then_the_resend_stalls("telegram")

    def test_after_an_upgrade_a_stalled_resend_offers_nothing_on_email(self):
        self.upgraded_then_the_resend_stalls("email")

    def test_a_delivered_resend_closes_the_offer_still_open(self):
        self.seed(1, documents=1)
        p, pkg, d1 = self.staged()
        out = self.call("record_delivery", delivery_id=d1["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        os.unlink(d1["path"])
        d2 = self.call("stage_for_delivery", channel="telegram", resend=True)
        again = self.call("record_delivery", delivery_id=d2["delivery_id"], outcome="uncertain")
        self.assertIn("send it again", again["speak"]["text"])      # open, not delivered yet
        os.unlink(d2["path"])
        d3 = self.call("stage_for_delivery", channel="telegram", resend=True)
        self.call("record_delivery", delivery_id=d3["delivery_id"], outcome="delivered")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM alerts WHERE sent_at IS NULL AND kind='package-uncertain'"
            ).fetchone()[0], 0)
        self.assertIsNone(self.claim()["speak"])
        self.assertEqual(self.text("stage_for_delivery", channel="telegram", resend=True),
                         self.ARRIVED)

    def test_an_extra_copy_that_fails_after_the_package_arrived_offers_nothing(self):
        self.seed(1, documents=1)
        p, pkg, d1 = self.staged()
        out = self.call("record_delivery", delivery_id=d1["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        os.unlink(d1["path"])
        d2 = self.call("stage_for_delivery", channel="telegram", resend=True)
        os.unlink(d2["path"])
        # the first copy turns out delivered; the extra copy's send then fails
        self.call("record_delivery", delivery_id=d2["delivery_id"], outcome="delivered")
        with db.tx(self.conn):          # a stray copy of an arrived package, still staged
            self.conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                              " created_at, lease_at) VALUES (?, 'telegram', '/nowhere/x.zip',"
                              " 'staged', ?, ?)", (pkg["package_id"], db.now(), db.now()))
        d3 = self.conn.execute("SELECT max(delivery_id) FROM deliveries").fetchone()[0]
        r = self.call("record_delivery", delivery_id=d3, outcome="failed")
        self.assertNotIn("speak", r)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE kind="
                                           "'package-send-failed'").fetchone()[0], 0)


class TestALateDeliveryIsCheckedAgainstTheBank(Requests):
    """Review C5 (J2): a package reported delivered after a newer import is checked
    against the bank in that same transaction — the accountant holds its rows now."""
    def test_the_upgrade_says_what_changed_since_its_build(self):
        self.seed(1, documents=1)
        p, pkg, d1 = self.staged()
        self.clock.advance(steps.LEASE_S)
        r = self.claim()
        self.call("mark_rendering_delivered", render_id=r["speak"]["render_id"])
        bf = self.bf
        rid = self.active()[0]["row_id"]
        bf.call("untag_transaction", row_ids=[rid], tags=["software"])
        bf.fetch([bf.row("2026-07-05", ref="R0", amount=1500, counterparty="Adobe")])
        out = sim.run_pass(self.conn, bf)                  # the newer import: nothing yet
        self.assertEqual(out["import"]["delivered_changes"], 0)
        if out["end"]["speak"]:
            self.call("mark_rendering_delivered", render_id=out["end"]["speak"]["render_id"])
        up = self.call("record_delivery", delivery_id=d1["delivery_id"], outcome="delivered")
        self.assertEqual(up["status"], "delivered")
        changed = self.conn.execute("SELECT count(*) FROM alerts WHERE kind='delivered-changed'"
                                    ).fetchone()[0]
        self.assertGreater(changed, 0)
        text = " ".join(up["speak"]["text"].split())
        self.assertIn(f"The package {pkg['filename']} changed underneath:", text)
        self.assertIn("corrected by the bank", text)


if __name__ == "__main__":
    unittest.main()
