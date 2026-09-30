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

from tests._base import StoreCase
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
        t = self.begin("package", quarter="2026-Q3", channel=channel)
        self.start(t, step="snapshot")
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
        self.assertEqual(c["next"], "gmail-round")
        c = self.rest_of_round(c["pass_token"], c["work"]["triage"])
        return self.call("end_pass", pass_token=c["pass_token"], outcome="complete"), c

    def rest_of_round(self, token, items=()):
        """Issue #15: after the snapshot, Ellen's Gmail round (its probe, each work item
        searched) and the judge step finished whole; returns the judge's continuation."""
        self.call("record_probe", pass_token=token, kind="gmail", ok=True)
        for it in items:
            self.call("record_search", pid=it["pid"], pass_token=token, queries=["q"])
        self.start(token, step="judge")
        self.call("record_step", pass_token=token, step="judge", action="finish",
                  triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        return c

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

    def test_a_revoked_send_goes_back_to_its_check(self):
        # (12) the next pass's import revokes an unsent first send. Issue #15 (design
        # D2): the operator asked for the package, so its request is queued again and
        # the package follows a fresh check — nothing to ask again, nothing to tell
        self.seed(1, documents=1)
        _, pkg, d = self.staged()
        out = sim.run_pass(self.conn, self.bf)      # its import supersedes the build's
        self.assertEqual(out["import"]["revoked_deliveries"], [d["delivery_id"]])
        r = self.request()
        self.assertEqual((r["state"], r["package_id"], r["delivery_id"], r["token"]),
                         ("queued", None, None, None))
        self.assertEqual(len(self.alerts_of("package-revoked")), 0)
        self.assertTrue(out["end"]["more"])
        self.assertFalse(pathlib.Path(d["path"]).exists())
        # the old build is never sent as a first send: its request no longer owns it
        self.assertIn("no longer the one its request will send",
                      self.text("stage_for_delivery", channel="telegram",
                                package_id=pkg["package_id"], package_token=1))
        self.assertEqual(self.claim()["continue"]["next"], "snapshot")


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
        # issue #15: the round read the bank but never searched or judged — the request
        # waits for its next round, which follows the pass that reclaimed it
        self.assertEqual((self.request()["state"], self.request()["round"]), ("queued", 1))
        self.assertIsNone(self.claim()["continue"])          # the operator's pass is live
        end = self.call("end_pass", pass_token=out["pass_token"], outcome="complete")
        self.assertTrue(end["more"])
        c = self.claim()["continue"]
        self.assertEqual((c["next"], c["request"]["id"], c["request"]["round"]),
                         ("snapshot", rid, 2))
        t2 = c["pass_token"]
        self.start(t2, step="snapshot")
        self.probe_import(t2)
        self.call("record_step", pass_token=t2, step="snapshot", action="finish",
                  remaining_in_cycle=0)
        c = self.claim()["continue"]
        c = self.rest_of_round(c["pass_token"], c["work"]["triage"])
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="complete")
        self.assertEqual(end["next"], "build")
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=end["package_token"])
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

    def test_a_snapshot_out_of_time_after_its_import_is_built(self):
        # issue #10: a `stopped` said for the clock running out does not cost the package
        self.seed(1)
        t = self.snapshot(finish=False)
        self.clock.advance(steps.SWEEP_STOP_S)
        self.assertTrue(self.text("record_step", pass_token=t, step="snapshot",
                                  action="finish", remaining_in_cycle=0,
                                  stopped="time budget exhausted").startswith(
                                      "refused: your step's time is up"))
        self.call("record_step", pass_token=t, step="snapshot", action="finish",
                  remaining_in_cycle=0, out_of_time=True)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "gmail-round")
        c = self.rest_of_round(c["pass_token"], c["work"]["triage"])
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="complete")
        self.assertEqual(self.request()["state"], "snapshot-done")
        self.assertEqual(self.alerts_of("package-stopped"), [])
        self.call("build_quarterly_package", quarter="2026-Q3",
                  package_token=end["package_token"])

    def test_a_refusal_after_the_time_is_up_builds_no_package(self):
        # issue #10 R1 (Terra): the ledger changed under the pass after 450 s — the
        # confirmed stop closes the request `stopped`, as on 0.3.3
        self.seed(1)
        t = self.snapshot(finish=False)
        self.clock.advance(steps.SWEEP_STOP_S + 5)
        self.call("record_step", pass_token=t, step="snapshot", action="finish",
                  remaining_in_cycle=0, stopped="the bank ledger changed during this pass",
                  stopped_by_refusal=True)
        c = self.claim()["continue"]
        self.call("end_pass", pass_token=c["pass_token"], outcome="stopped")
        self.assertEqual(self.request()["state"], "stopped")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 0)

    def test_a_refused_late_stop_never_retried_builds_no_package(self):
        # R2 Astra: the stop the finish was refused for is kept on the step; a step that
        # then expires ends stopped, as 0.3.3 ended it on the stop itself
        self.seed(1)
        t = self.snapshot(finish=False)
        self.clock.advance(599)
        self.assertTrue(self.text("record_step", pass_token=t, step="snapshot",
                                  action="finish", remaining_in_cycle=0,
                                  stopped="the bank ledger changed during this pass").startswith(
                                      "refused: your step's time is up"))
        self.clock.advance(1)
        c = self.claim()["continue"]
        self.assertEqual(c["ended"], "expired")
        self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        self.assertEqual(self.request()["state"], "stopped")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 0)

    def test_the_residents_own_finish_keeps_a_refused_late_stop(self):
        # the delegation came back in Ellen's turn without a finish: hers keeps the stop,
        # counts or not (R6 Terra: a counted finish is not taken for the specialist's)
        for extra in ({}, {"failed": True}, {"remaining_in_cycle": 0}):
            with self.subTest(**extra):
                self.setUp()
                self.seed(1)
                t = self.snapshot(finish=False)
                self.clock.advance(steps.SWEEP_STOP_S)
                self.text("record_step", pass_token=t, step="snapshot", action="finish",
                          stopped="the bank ledger changed during this pass")
                self.call("record_step", pass_token=t, step="snapshot", action="finish", **extra)
                c = self.claim()["continue"]
                self.assertIn("ledger changed", c["finish"]["stopped"])
                self.call("end_pass", pass_token=c["pass_token"], outcome="stopped")
                self.assertEqual(self.request()["state"], "stopped")

    def test_out_of_time_goes_with_no_stop(self):
        self.seed(1)
        t = self.snapshot(finish=False)
        self.assertTrue(self.text("record_step", pass_token=t, step="snapshot",
                                  action="finish", stopped="x", out_of_time=True).startswith(
                                      "refused: out_of_time=true is a finish without"))

    def test_failed_does_not_let_a_late_stop_through(self):
        # R2 Terra: failed=true with a time-out said as a stop is refused like any other
        self.seed(1)
        t = self.snapshot(finish=False)
        self.clock.advance(steps.SWEEP_STOP_S)
        self.assertTrue(self.text("record_step", pass_token=t, step="snapshot",
                                  action="finish", stopped="time budget exhausted",
                                  failed=True).startswith("refused: your step's time is up"))

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
        t = self.begin("package", quarter="2026-Q3", channel="telegram")
        self.start(t, step="snapshot")
        self.call("record_step", pass_token=t, step="snapshot", action="finish", failed=True)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
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
    def pass_in(self, step_state, imported, rest=True):
        self.assertIsNone(self.claim()["continue"])
        t = self.begin("package", quarter="2026-Q3", channel="telegram")
        if step_state != "none":             # "none": a request whose step row never landed
            self.start(t, step="snapshot")
        if imported == "this pass":
            self.probe_import(t)
        if imported == "this pass" and rest:
            # the rest of the round (issue #15), so the check half of the fate is met and
            # this class sees the bank half alone
            self.call("record_probe", pass_token=t, kind="gmail", ok=True)
            for it in self.call("list_quarter_state", triage=True, quarter="2026-Q3",
                                pass_token=t)["triage"]:
                self.call("record_search", pid=it["pid"], pass_token=t, queries=["q"])
            self.start(t, step="judge")
            self.call("record_step", pass_token=t, step="judge", action="finish",
                      triage_remaining=0)
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
        self.pass_in("started", "this pass", rest=False)
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"]), ("expired", "gmail-round"))
        c = self.rest_of_round(c["pass_token"], c["work"]["triage"])
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

    def test_end_pass_after_a_revocation_says_the_package_follows(self):
        # issue #15: a request's revoked send is not a notice any more — its request is
        # queued again, and end_pass says continue_pass has work (`more`)
        self.seed(1, documents=1)
        self.staged()
        self.fill_alerts()
        end = sim.run_pass(self.conn, self.bf)["end"]         # its import revokes the send
        self.assertTrue(end["more"])
        self.assertEqual(self.request()["state"], "queued")

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


class TestReviewC6(Requests):
    def test_a_late_delivered_report_tells_a_reclassification_too(self):
        # K1: the delivered path runs the pass's own change detection, both halves
        self.seed(1, documents=1)
        self.assertEqual(len(sim.run_pass(self.conn, self.bf)["triage"]["matched"]), 1)
        p, pkg, d = self.staged()
        self.clock.advance(steps.LEASE_S)
        r = self.claim()
        self.call("mark_rendering_delivered", render_id=r["speak"]["render_id"])
        rid = self.active()[0]["row_id"]
        self.bf.call("untag_transaction", row_ids=[rid], tags=["software"])
        self.classify(rid, "refund")
        out = sim.run_pass(self.conn, self.bf)              # import and sweep: not delivered yet
        if out["end"]["speak"]:
            self.call("mark_rendering_delivered", render_id=out["end"]["speak"]["render_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE"
                                           " kind='delivered-changed'").fetchone()[0], 0)
        up = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="delivered")
        changes = [json.loads(a[0])["change"] for a in self.conn.execute(
            "SELECT detail FROM alerts WHERE kind='delivered-changed'")]
        self.assertEqual(changes, ["reclassified"])
        self.assertIn("now categorised differently", up["speak"]["text"])

    def test_a_failed_send_stays_offered_in_the_status_view(self):
        # K2: a delivered status view rebinds "send it again" — it must still offer the
        # package whose send failed, by the same rule the notice follows
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="failed",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        os.unlink(d["path"])
        view = self.call("build_review", view="status", quarter="2026-Q3")
        self.assertIn(pkg["filename"], view["text"])
        self.assertIn("didn't go out", view["text"])
        self.call("mark_rendering_delivered", render_id=view["render_id"])
        again = self.call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(again["filename"], pkg["filename"])

    def test_a_revoked_send_is_not_offered_in_the_status_view(self):
        self.seed(1, documents=1)
        _, pkg, d = self.staged()
        sim.run_pass(self.conn, self.bf)                     # revokes the unsent first send
        view = self.call("build_review", view="status", quarter="2026-Q3")
        self.assertNotIn(pkg["filename"], view["text"])


class TestReviewC7(Requests):
    def failed_then_a_newer_import(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="failed",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        return pkg, d, sim.run_pass(self.conn, self.bf)     # a newer snapshot

    STALE = "the bank has changed since it was built — ask for the Q3 2026 package again"

    def test_a_failed_first_send_is_not_offered_after_a_newer_import(self):
        # C7 L1, as generalized in C8: nothing is revoked; the one rule simply offers no
        # resend of a send that never arrived once the bank changed (staging refuses it)
        pkg, d, run = self.failed_then_a_newer_import()
        row = self.conn.execute("SELECT status, revoked_at FROM deliveries WHERE"
                                " delivery_id=?", (d["delivery_id"],)).fetchone()
        self.assertEqual((row["status"], row["revoked_at"]), ("failed", None))
        self.assertEqual(self.alerts_of("package-revoked"), [])
        self.assertEqual(delivery.resend_refusal(self.conn, pkg["package_id"]), self.STALE)
        if run["end"]["speak"]:
            self.call("mark_rendering_delivered", render_id=run["end"]["speak"]["render_id"])
        view = self.call("build_review", view="status", quarter="2026-Q3")
        self.assertNotIn(pkg["filename"], view["text"])
        self.call("mark_rendering_delivered", render_id=view["render_id"])
        self.assertTrue(self.text("stage_for_delivery", channel="telegram",
                                  resend=True).startswith("refused: nothing is waiting"))

    def test_an_untold_failed_offer_is_told_without_the_invitation(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.call("record_delivery", delivery_id=d["delivery_id"], outcome="failed",
                  package_token=p)                       # its offer is never delivered
        run = sim.run_pass(self.conn, self.bf)
        text = " ".join(run["end"]["speak"]["text"].split())
        self.assertIn("The Q3 2026 package didn't go out — " + self.STALE, text)
        self.assertNotIn("send it again", text)
        self.assertNotIn("offers", self.scope(run["end"]["speak"]))

    def test_an_uncertain_send_stays_offered_after_a_newer_import(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        os.unlink(d["path"])
        sim.run_pass(self.conn, self.bf)
        self.assertIsNone(self.conn.execute("SELECT revoked_at FROM deliveries WHERE"
                                            " delivery_id=?", (d["delivery_id"],)).fetchone()[0])
        view = self.call("build_review", view="status", quarter="2026-Q3")
        self.assertIn(pkg["filename"], view["text"])
        self.call("mark_rendering_delivered", render_id=view["render_id"])
        again = self.call("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(again["filename"], pkg["filename"])

    def test_an_old_offer_is_not_recomposed_once_its_package_is_not_offerable(self):
        # L2: a new rendering composed with an old offer notice offers only what the one
        # rule offers now (here: a resend is already staged, so nothing is waiting)
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                  package_token=p)               # its speak is never delivered
        os.unlink(d["path"])
        with db.tx(self.conn):                   # the operator's resend is staged meanwhile
            self.conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                              " created_at, lease_at) VALUES (?, 'telegram', '/o/qa-x.zip',"
                              " 'staged', ?, ?)", (pkg["package_id"], db.now(), db.now()))
        self.fill_alerts(1)                      # another pass's alert forces a new rendering
        speak = self.claim()["speak"]
        self.assertIn(pkg["filename"], speak["text"])       # the outcome is still told
        self.assertNotIn("send it again", speak["text"])    # but not offered
        self.assertNotIn("offers", self.scope(speak))

    def test_every_offer_is_built_from_the_one_rule(self):
        # structural pin (tightened in C8): every function that composes offer wording
        # (the phrases), an offer scope ("offers" in any form) or an offer-carrying block
        # CALLS delivery.offerable() or delivery.resend_refusal() — none decides alone
        self.assertEqual(offer_builders(), {("alerts", "_units"): True,
                                            ("alerts", "pending_in_tx"): True,
                                            ("views", "_compose"): True,
                                            ("delivery", "resend_target"): True})


PHRASES = ("send it again", "may not have arrived", "didn't go out")
# the phrase's owner, the reply grammar that PARSES it, and the one collector of what
# views._compose built
EXEMPT = {("delivery", "offer_lines"), ("reply", "_clauses"), ("views", "_build_review")}


def offer_builders() -> dict:
    """{(module, function): whether it CALLS offerable( / resend_refusal(} for every
    server function that composes an offer: a string constant (not its docstring)
    carrying an offer phrase or naming "offers", a subscript or dict key "offers", or
    an offer= keyword."""
    import ast
    from tests._base import ROOT
    out = {}
    for f in (ROOT / "server").glob("*.py"):
        tree = ast.parse(f.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or (f.stem, fn.name) in EXEMPT:
                continue
            doc = fn.body[0].value if fn.body and isinstance(fn.body[0], ast.Expr) \
                and isinstance(fn.body[0].value, ast.Constant) else None
            makes, asks = False, False
            for node in (n for stmt in fn.body for n in ast.walk(stmt)):   # not decorators
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and node is not doc:
                    v = node.value
                    if v == "offers" or any(ph in v for ph in PHRASES):
                        makes = True
                if isinstance(node, ast.keyword) and node.arg in ("offer", "offers") \
                        and not (isinstance(node.value, ast.Constant)
                                 and node.value.value is None):
                    makes = True
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if name in ("offerable", "resend_refusal"):
                        asks = True
            if makes:
                out[(f.stem, fn.name)] = asks
    return out


class TestReviewC8(Requests):
    """Escalation, C6-C8: whether "send it again" is OFFERED and whether a resend can
    be STAGED are one predicate, delivery.resend_refusal."""
    def test_a_failed_report_after_a_newer_import_offers_what_staging_allows(self):
        # Astra: a send that was uncertain before a newer import is reported failed late
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="uncertain",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        os.unlink(d["path"])
        again = self.call("stage_for_delivery", channel="telegram", resend=True)
        run = sim.run_pass(self.conn, self.bf)             # a newer snapshot
        if run["end"]["speak"]:
            self.call("mark_rendering_delivered", render_id=run["end"]["speak"]["render_id"])
        late = self.call("record_delivery", delivery_id=again["delivery_id"], outcome="failed")
        why = delivery.resend_refusal(self.conn, pkg["package_id"])
        offered = "send it again" in late["speak"]["text"]
        staged = self.text("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(offered, why is None)
        if why is None:
            self.assertFalse(staged.startswith("refused: "), staged)
        else:
            self.assertEqual(staged, "refused: " + why)

    def test_asking_again_after_a_failed_send_says_nothing_about_asking_again(self):
        # Claude: the operator asks for the quarter again after a failed send; their own
        # pass's import must not tell them to ask for it again while it is being built
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        out = self.call("record_delivery", delivery_id=d["delivery_id"], outcome="failed",
                        package_token=p)
        self.call("mark_rendering_delivered", render_id=out["speak"]["render_id"])
        said = []
        end, c = self.handed_over()                        # the new request's own pass
        said.append(end["speak"])
        p2 = self.call("build_quarterly_package", quarter="2026-Q3",
                       package_token=end["package_token"])
        said.append(self.claim().get("speak"))
        for speak in said:
            if speak:
                self.assertNotIn("ask for it again", speak["text"])
                self.assertNotIn("ask for the", speak["text"])
        self.assertEqual(self.alerts_of("package-revoked"), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 2)
        self.assertTrue(p2["package_id"])


class TestOfferIsExactlyStaging(StoreCase):
    """THE pin: over every state combination, a package is offered exactly when
    stage_for_delivery(resend=True) would stage it."""
    def setUp(self):
        super().setUp()
        import package as _package
        self._package = _package
        self.bind()
        self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        self.end_live_pass()

    def snapshot(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES ('p-x', ?, 0, 0)", (db.now(),))

    def delivery(self, pkg, status, revoked=False):
        with db.tx(self.conn):
            n = self.conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]
            return self.conn.execute(
                "INSERT INTO deliveries(package_id, channel, staged_path, status, created_at,"
                " settled_at, revoked_at, lease_at) VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?)",
                (pkg, f"/gone/{n}.zip", status, db.now(),
                 None if status == "staged" else db.now(),
                 db.now() if revoked else None, db.now())).lastrowid

    def test_offered_exactly_when_staging_succeeds(self):
        import itertools
        combos = itertools.product(("uncertain", "failed", "staged", "delivered"),
                                   ("current", "newer"), (False, True), (False, True))
        seen = 0
        for latest, snap, came, revoked in combos:
            where = (latest, snap, came, revoked)
            self.snapshot()
            pkg = self._package.build_quarterly_package(self.conn, "2026-Q3",
                                                        bound=False)["package_id"]
            if came:
                self.delivery(pkg, "delivered")
            self.delivery(pkg, latest, revoked=revoked)
            if snap == "newer":
                self.snapshot()
            offered = pkg in {p for p, _, _ in delivery.offerable(self.conn)}
            why = delivery.resend_refusal(self.conn, pkg)
            # and the predicate itself: owed only when nothing arrived, the latest send
            # is settled (not in flight) and not revoked, and — unless it may have
            # arrived (uncertain) — the bank has not changed since the build
            owed = (not came and latest in ("uncertain", "failed") and not revoked
                    and (latest == "uncertain" or snap == "current"))
            self.assertEqual(why is None, owed, where)
            try:
                delivery.stage_for_delivery(self.conn, channel="telegram", package_id=pkg,
                                            resend=True)
                staged, refusal = True, None
            except db.Refusal as exc:
                staged, refusal = False, str(exc)
            self.assertEqual(offered, staged, where)
            self.assertEqual(why is None, staged, where)
            if not staged:
                self.assertEqual(refusal, why, where)
            seen += 1
        self.assertEqual(seen, 32)

    def test_an_ineligible_resend_is_refused_before_any_byte_is_written(self):
        pkg = self._package.build_quarterly_package(self.conn, "2026-Q3",
                                                    bound=False)["package_id"]
        self.delivery(pkg, "delivered")
        written = []
        with mock.patch.object(delivery, "_to_outbox",
                               lambda path, data: written.append(path)):
            with self.assertRaises(db.Refusal):
                delivery.stage_for_delivery(self.conn, channel="telegram", package_id=pkg,
                                            resend=True)
        self.assertEqual(written, [])

    def test_the_predicate_binds_in_the_committing_transaction(self):
        # the package arrives while the resend's copy is being written: nothing is staged
        pkg = self._package.build_quarterly_package(self.conn, "2026-Q3",
                                                    bound=False)["package_id"]
        self.delivery(pkg, "uncertain")
        real = delivery._to_outbox
        other = db.open_store()
        self.addCleanup(other.close)

        def write_then_arrive(path, data):
            out = real(path, data)
            with db.tx(other):
                other.execute("UPDATE deliveries SET status='delivered' WHERE package_id=?",
                              (pkg,))
            return out
        with mock.patch.object(delivery, "_to_outbox", write_then_arrive):
            with self.assertRaises(db.Refusal) as cm:
                delivery.stage_for_delivery(self.conn, channel="telegram", package_id=pkg,
                                            resend=True)
        self.assertIn("did arrive", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM deliveries WHERE"
                                           " status='staged'").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.outbox), [])


if __name__ == "__main__":
    unittest.main()
