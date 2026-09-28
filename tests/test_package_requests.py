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
    def snapshot(self, channel="telegram", finish=True, stopped=None):
        """Packaging step 1 up to the claim: begin_pass(package), the snapshot step,
        the specialist's probes, import and quarter sweep, its finish."""
        self.assertIsNone(self.claim()["continue"])
        t = self.begin("package")
        self.start(t, step="snapshot", quarter="2026-Q3", channel=channel)
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
                                  package_token=end["package_token"]).startswith(TAKEN))
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
        self.assertTrue(self.text("stage_for_delivery", channel="telegram",
                                  package_id=pkg["package_id"], package_token=p1)
                        .startswith(TAKEN))
        for tok in ({"package_token": p1}, {}):
            self.assertTrue(self.text("record_delivery", delivery_id=d["delivery_id"],
                                      outcome="delivered", **tok).startswith("refused: "), tok)
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
        self.clock.advance(steps.LEASE_S)
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
        # (13, variant) and (14, lost crash)
        self.seed(1)
        self.snapshot(finish=False)
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


if __name__ == "__main__":
    unittest.main()
