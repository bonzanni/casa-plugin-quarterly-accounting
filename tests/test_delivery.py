# tests/test_delivery.py
import contextlib
import importlib.util
import os
import pathlib
import unittest
import zipfile  # noqa: F401
from unittest import mock

from tests._base import ROOT, StoreCase
import db  # noqa: E402
import delivery  # noqa: E402
import ledger  # noqa: E402
import package  # noqa: E402
import reducer  # noqa: E402
import views  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        self.pkg = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)


class TestTelegram(Base):
    def test_staged_atomically_into_the_outbox_under_a_name_of_its_own(self):
        # issue #2: the staged file's name is random and never reused; the operator sees
        # the package's name through send_media's filename argument
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        self.assertEqual(os.path.dirname(out["path"]), str(self.outbox))
        self.assertRegex(os.path.basename(out["path"]), r"^qa-[0-9a-f]{16}\.zip$")
        self.assertEqual(out["filename"], self.pkg["filename"])
        self.assertIn("filename=<filename>", out["note"])
        self.assertEqual(pathlib.Path(out["path"]).read_bytes(),
                         pathlib.Path(self.pkg["path"]).read_bytes())
        self.assertFalse([f for f in os.listdir(self.outbox) if ".part" in f])

    def test_oversize_is_refused_for_telegram(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE packages SET oversize=1")
        with self.assertRaises(db.Refusal) as cm:
            delivery.stage_for_delivery(self.conn, channel="telegram",
                                        package_id=self.pkg["package_id"])
        self.assertIn("20 MB", str(cm.exception))
        self.assertEqual(os.listdir(self.outbox), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_delivered_records_the_rows_the_accountant_now_holds(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"], outcome="delivered")
        rows = self.conn.execute("SELECT row_id, pid FROM delivered_rows").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, self.pid)])
        self.assertEqual(self.conn.execute("SELECT package_name_announced FROM binding")
                         .fetchone()[0], 1)

    def test_delivered_rows_carry_the_facts_the_delivered_check_compares(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"], outcome="delivered")
        bank = dict(self.conn.execute("SELECT * FROM bank_rows WHERE row_id=1").fetchone())
        fp, kind = self.conn.execute("SELECT facts_fp, kind FROM delivered_rows").fetchone()
        self.assertEqual(fp, db.canonical(reducer.facts_of(bank)))
        self.assertEqual(kind, "invoice")
        # unchanged facts raise nothing; a corrected amount raises exactly one alert
        with db.tx(self.conn):
            self.assertEqual(ledger.check_delivered_bank_half(self.conn, {1: bank}), 0)
            self.assertEqual(ledger.check_delivered_kind_half(self.conn, self.pid), 0)
            self.assertEqual(ledger.check_delivered_bank_half(
                self.conn, {1: dict(bank, amount_minor=10001)}), 1)

    def test_uncertain_is_offered_in_words_and_never_resent_by_itself(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"], outcome="uncertain")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
        text = views.build_review(self.conn, view="status", quarter="2026-Q3")["text"]
        self.assertIn(self.pkg["filename"], text)
        self.assertIn('say "send it again"', text)
        self.assertEqual(delivery.resendable(self.conn), self.pkg["package_id"])
        again = delivery.stage_for_delivery(self.conn, channel="telegram",
                                            package_id=delivery.resendable(self.conn))
        self.assertEqual(pathlib.Path(again["path"]).read_bytes(),
                         pathlib.Path(self.pkg["path"]).read_bytes())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0], 1)

    def test_the_offer_goes_once_the_resend_is_delivered(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"], outcome="uncertain")
        again = delivery.stage_for_delivery(self.conn, channel="telegram",
                                            package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=again["delivery_id"],
                                 outcome="delivered")
        text = views.build_review(self.conn, view="status", quarter="2026-Q3")["text"]
        self.assertNotIn("send it again", text)

    def test_the_offer_goes_through_the_view_machinery(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"], outcome="uncertain")
        for view, page in (("status", None), ("all", 1)):
            text = views.build_review(self.conn, view=view, quarter="2026-Q3", page=page)["text"]
            self.assertIn('say "send it again"', text, view)
            self.assertLessEqual(views.utf16_len(text), views.TELEGRAM_LIMIT)
            for line in text.splitlines():
                self.assertLessEqual(len(line), views.WIDTH, (view, line))
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text, (view, word))
        # a view that does not fit is cut by the final fit, never extended after it
        with mock.patch.object(views, "TELEGRAM_LIMIT", 120):
            text = views.build_review(self.conn, view="status", quarter="2026-Q3")["text"]
            self.assertLessEqual(views.utf16_len(text), 120)

    def test_resend_follows_each_packages_latest_send(self):
        old = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        delivery.record_delivery(self.conn, delivery_id=old["delivery_id"], outcome="delivered")
        newer = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        for outcome in ("uncertain", "failed"):
            d = delivery.stage_for_delivery(self.conn, channel="telegram",
                                            package_id=newer["package_id"])
            delivery.record_delivery(self.conn, delivery_id=d["delivery_id"], outcome=outcome)
        self.assertEqual(delivery.resendable(self.conn), self.pkg["package_id"])


class TestPassFence(Base):
    def test_a_stale_pass_is_refused_at_staging_and_nothing_is_staged(self):
        stale = self.token
        self.pass_()
        with self.assertRaises(db.Refusal):
            delivery.stage_for_delivery(self.conn, channel="telegram",
                                        package_id=self.pkg["package_id"], pass_token=stale)
        self.assertEqual(os.listdir(self.outbox), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_a_stale_pass_is_refused_at_the_delivery_log_write(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"],
                                          pass_token=self.token)
        stale = self.token
        live = self.pass_()
        with self.assertRaises(db.Refusal):
            delivery.record_delivery(self.conn, delivery_id=out["delivery_id"],
                                     outcome="delivered", pass_token=stale)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries").fetchone()[0],
                         "staged")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delivered_rows")
                         .fetchone()[0], 0)
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"],
                                 outcome="delivered", pass_token=live)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries").fetchone()[0],
                         "delivered")


class TestCustody(Base):
    def test_staging_holds_the_custody_lock_before_any_transaction(self):
        seen = []
        real = db.custody_lock

        @contextlib.contextmanager
        def spy(*a, **kw):
            seen.append(self.conn.in_transaction)
            with real(*a, **kw):
                yield
        with mock.patch.object(db, "custody_lock", spy):
            delivery.stage_for_delivery(self.conn, channel="telegram",
                                        package_id=self.pkg["package_id"])
        self.assertEqual(seen, [False])

    def test_staging_refuses_to_run_inside_a_transaction(self):
        with self.assertRaises(RuntimeError):
            with db.tx(self.conn):
                delivery.stage_for_delivery(self.conn, channel="telegram",
                                            package_id=self.pkg["package_id"])

    def test_a_build_takes_the_custody_lock_directly(self):
        # a renamed lock fails the build loudly, never degrades to no lock
        saved = db.custody_lock
        del db.custody_lock
        self.addCleanup(setattr, db, "custody_lock", saved)
        with self.assertRaises(AttributeError):
            package.build_quarterly_package(self.conn, "2026-Q3", bound=False)


class TestEmail(Base):
    def test_published_to_the_handoff_with_a_request_id(self):
        out = delivery.stage_for_delivery(self.conn, channel="email",
                                          package_id=self.pkg["package_id"])
        self.assertTrue(out["path"].startswith(str(self.handoff)))
        self.assertTrue(out["request_id"])
        self.assertIn("operator's own address", out["note"])

    def test_an_email_over_the_handoff_cap_is_refused(self):
        with mock.patch.object(delivery, "GMAIL_ATTACHMENT_LIMIT", 10):
            with self.assertRaises(db.Refusal) as cm:
                delivery.stage_for_delivery(self.conn, channel="email",
                                            package_id=self.pkg["package_id"])
        self.assertIn("25 MB", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_a_package_too_large_for_telegram_can_still_be_emailed(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE packages SET oversize=1")
        out = delivery.stage_for_delivery(self.conn, channel="email",
                                          package_id=self.pkg["package_id"])
        self.assertTrue(out["request_id"])

    def test_email_is_delivered_only_with_a_message_id(self):
        out = delivery.stage_for_delivery(self.conn, channel="email",
                                          package_id=self.pkg["package_id"])
        with self.assertRaises(db.Refusal):
            delivery.record_delivery(self.conn, delivery_id=out["delivery_id"],
                                     outcome="delivered")
        delivery.record_delivery(self.conn, delivery_id=out["delivery_id"], outcome="delivered",
                                 message_id="18c0f")

    def test_a_retry_after_a_timeout_sends_twice_which_is_why_we_never_retry(self):
        spec = importlib.util.spec_from_file_location(
            "gmail_sent_log", ROOT / "tests/upstream/gmail-v0.9.0/sent_log.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        log = mod.SentLog(str(self.tmp / "sent_log.json"))
        sends = []

        def send_email(request_id, to, subject, fail_after_send):
            # gmail 0.9.0's send path: check, send, THEN record (server.py 571-577)
            if request_id and log.check(request_id, to, subject):
                return "dedup"
            sends.append(request_id)
            if fail_after_send:
                raise TimeoutError("transport timeout after the request was accepted")
            log.record(request_id, "msg-%d" % len(sends), to, subject)
            return "sent"

        with self.assertRaises(TimeoutError):
            send_email("qa-1", "me@example.org", "Q3", True)
        send_email("qa-1", "me@example.org", "Q3", False)
        self.assertEqual(len(sends), 2)

    def test_a_single_invoice_can_be_staged(self):
        import documents
        path = self.publish("inv.pdf", b"%PDF-1.4\n%%EOF\n")
        doc_id = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                           source="gmail", extraction_author="resident",
                                           counterparty="Adobe", amount_minor=100,
                                           document_date="2026-07-02")["doc_id"]
        out = delivery.stage_for_delivery(self.conn, channel="telegram", doc_id=doc_id)
        self.assertEqual(out["filename"], "2026-07-02_Adobe_1.00.pdf")
        self.assertRegex(os.path.basename(out["path"]), r"^qa-[0-9a-f]{16}\.pdf$")


class TestResendTarget(Base):
    def send(self, pkg_id, outcome):
        d = delivery.stage_for_delivery(self.conn, channel="telegram", package_id=pkg_id)
        delivery.record_delivery(self.conn, delivery_id=d["delivery_id"], outcome=outcome)
        for f in os.listdir(self.outbox):          # Casa consumes the outbox copy on send
            os.unlink(self.outbox / f)

    def shown(self, quarter="2026-Q3"):
        r = views.build_review(self.conn, view="status", quarter=quarter)
        views.mark_rendering_delivered(self.conn, r["render_id"])
        return r["text"]

    def test_the_offered_uncertain_package_not_a_newer_delivered_one(self):
        a = self.pkg
        self.send(a["package_id"], "uncertain")
        b = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        self.send(b["package_id"], "delivered")
        text = self.shown()
        self.assertIn(a["filename"], text)
        self.assertNotIn(b["filename"], text)
        self.assertEqual(delivery.resend_target(self.conn), a["package_id"])
        self.assertEqual(delivery.resendable(self.conn), a["package_id"])

    def test_the_offer_binds_to_the_rendering_delivered_last_within_one_second(self):
        # fix wave D: sheet A offers nothing, sheet B offers the uncertain package;
        # B then A delivered in one second — "send it again" answers A, which offered
        # nothing, never B by creation order.
        sheet_a = views.build_review(self.conn, view="status", quarter="2026-Q3")
        self.send(self.pkg["package_id"], "uncertain")
        sheet_b = views.build_review(self.conn, view="status", quarter="2026-Q3")
        self.assertIn("send it again", sheet_b["text"])
        with mock.patch.object(db, "now", lambda: "2026-09-27T10:00:00Z"):
            views.mark_rendering_delivered(self.conn, sheet_b["render_id"])
            views.mark_rendering_delivered(self.conn, sheet_a["render_id"])
        with self.assertRaises(db.Refusal) as cm:
            delivery.resend_target(self.conn)
        self.assertIn("nothing is waiting to be sent again", str(cm.exception))

    def test_two_offered_asks_which_by_the_names(self):
        a = self.pkg
        b = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        self.send(a["package_id"], "uncertain")
        self.send(b["package_id"], "uncertain")
        self.shown()
        with self.assertRaises(db.Refusal) as cm:
            delivery.resend_target(self.conn)
        self.assertIn(a["filename"], str(cm.exception))
        self.assertIn(b["filename"], str(cm.exception))
        self.assertIn("which one", str(cm.exception))

    def test_an_offer_never_delivered_binds_nothing(self):
        self.send(self.pkg["package_id"], "uncertain")
        views.build_review(self.conn, view="status", quarter="2026-Q3")
        with self.assertRaises(db.Refusal) as cm:
            delivery.resend_target(self.conn)
        self.assertIn("nothing is waiting to be sent again", str(cm.exception))

    def test_once_the_resend_arrived_nothing_is_waiting(self):
        self.send(self.pkg["package_id"], "uncertain")
        self.shown()
        self.send(delivery.resend_target(self.conn), "delivered")
        with self.assertRaises(db.Refusal):
            delivery.resend_target(self.conn)

    def test_the_offer_is_scoped_to_the_views_quarter(self):
        self.send(self.pkg["package_id"], "uncertain")
        self.assertNotIn("send it again", self.shown("2026-Q4"))
        self.assertEqual(delivery.uncertain(self.conn, "2026-Q4"), [])
        with self.assertRaises(db.Refusal):
            delivery.resend_target(self.conn)


class TestOutboxNames(Base):
    def ingest(self, body):
        import documents
        path = self.publish("inv.pdf", body)
        return documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                         source="gmail", extraction_author="resident",
                                         counterparty="Adobe", amount_minor=100,
                                         document_date="2026-07-02")["doc_id"]

    def test_two_invoices_sharing_a_name_never_overwrite_each_other(self):
        first = b"%PDF-1.4\nAAA\n%%EOF\n"
        o1 = delivery.stage_for_delivery(self.conn, channel="telegram", doc_id=self.ingest(first))
        o2 = delivery.stage_for_delivery(self.conn, channel="telegram",
                                         doc_id=self.ingest(b"%PDF-1.4\nBBB\n%%EOF\n"))
        self.assertNotEqual(o1["path"], o2["path"])
        self.assertEqual(pathlib.Path(o1["path"]).read_bytes(), first)
        paths = [r[0] for r in self.conn.execute("SELECT staged_path FROM deliveries")]
        self.assertEqual(sorted(paths), sorted([o1["path"], o2["path"]]))

    def test_a_file_already_in_the_outbox_is_never_touched(self):
        other = self.outbox / self.pkg["filename"]
        other.write_bytes(b"not this package")
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        self.assertNotEqual(out["path"], str(other))
        self.assertEqual(other.read_bytes(), b"not this package")
        with mock.patch.object(delivery, "_nonce", side_effect=["taken", "free"]):
            (self.outbox / "qa-taken.zip").write_bytes(b"waiting")
            out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                              package_id=self.pkg["package_id"])
        self.assertEqual(os.path.basename(out["path"]), "qa-free.zip")
        self.assertEqual((self.outbox / "qa-taken.zip").read_bytes(), b"waiting")

    def consumed(self):
        for f in os.listdir(self.outbox):          # Casa consumed the outbox copy on send
            os.unlink(self.outbox / f)

    def test_a_repeated_name_is_drawn_again_and_never_written(self):
        # a path any delivery ever named is never staged at again, even once its file
        # is gone: a superseded holder may still hold that path
        with mock.patch.object(delivery, "_nonce", side_effect=["aaaa", "aaaa", "bbbb"]):
            first = delivery.stage_for_delivery(self.conn, channel="telegram",
                                                package_id=self.pkg["package_id"])
            self.consumed()
            written = []
            real = delivery._to_outbox

            def spy(path, data):
                written.append(os.path.basename(path))
                return real(path, data)
            with mock.patch.object(delivery, "_to_outbox", spy):
                second = delivery.stage_for_delivery(self.conn, channel="telegram",
                                                     package_id=self.pkg["package_id"])
        self.assertEqual(os.path.basename(first["path"]), "qa-aaaa.zip")
        self.assertEqual(os.path.basename(second["path"]), "qa-bbbb.zip")
        self.assertEqual(written, ["qa-bbbb.zip"])
        self.assertFalse(os.path.exists(first["path"]))

    def test_a_collision_the_name_check_misses_is_refused_by_the_store_and_drawn_again(self):
        # the UNIQUE staged path is the durable guard, not the random draw
        with mock.patch.object(delivery, "_nonce", side_effect=["aaaa", "aaaa", "bbbb"]):
            first = delivery.stage_for_delivery(self.conn, channel="telegram",
                                                package_id=self.pkg["package_id"])
            self.consumed()
            with mock.patch.object(delivery, "_path_taken", lambda conn, path: False):
                second = delivery.stage_for_delivery(self.conn, channel="telegram",
                                                     package_id=self.pkg["package_id"])
        self.assertEqual(os.path.basename(second["path"]), "qa-bbbb.zip")
        self.assertFalse(os.path.exists(first["path"]))      # its copy was removed again
        paths = [r[0] for r in self.conn.execute("SELECT staged_path FROM deliveries"
                                                 " ORDER BY delivery_id")]
        self.assertEqual(paths, [first["path"], second["path"]])

    def test_a_refused_log_write_never_removes_a_copy_it_did_not_create(self):
        first = delivery.stage_for_delivery(self.conn, channel="telegram",
                                            package_id=self.pkg["package_id"])
        import passes
        calls = []

        def check(conn, token):
            calls.append(token)
            if len(calls) == 2:                     # refused at the log write
                raise db.Refusal("stale")
        with mock.patch.object(passes, "check_token", check):
            with self.assertRaises(db.Refusal):
                delivery.stage_for_delivery(self.conn, channel="telegram",
                                            package_id=self.pkg["package_id"])
        self.assertTrue(os.path.exists(first["path"]))


if __name__ == "__main__":
    unittest.main()
