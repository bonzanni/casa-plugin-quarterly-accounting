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
        self.pkg = package.build_quarterly_package(self.conn, "2026-Q3")


class TestTelegram(Base):
    def test_staged_atomically_into_the_outbox_under_its_built_name(self):
        out = delivery.stage_for_delivery(self.conn, channel="telegram",
                                          package_id=self.pkg["package_id"])
        self.assertEqual(os.path.dirname(out["path"]), str(self.outbox))
        self.assertEqual(os.path.basename(out["path"]), self.pkg["filename"])
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
        newer = package.build_quarterly_package(self.conn, "2026-Q3")
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
            package.build_quarterly_package(self.conn, "2026-Q3")


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
        self.assertEqual(os.path.basename(out["path"]), "2026-07-02_Adobe_1.00.pdf")


if __name__ == "__main__":
    unittest.main()
