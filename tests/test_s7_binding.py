# tests/test_s7_binding.py
"""S7 binding: one bound rendering per typed reading (rounds-2026-10-03-s7-diff/binding/
design.md, v3 + the d3 amendment). A reading binds exactly one rendering R — the one its
quote identifies, or, for words with no quote, db.last_delivered — and every resolution
step reads only R's own render_items and scope_json. Each class ports a reviewer's
reproduction (round 4: review_r4.py, review_job_binding.py; d1: review_binding.py; d2:
review_d2.py; d3: review_migration.py) as a stdlib pin, asserting counts, not statuses."""
import ast
import datetime as dt
import inspect
import json
import pathlib
import re
import sqlite3
import unittest
from unittest import mock

from tests._base import StoreCase
from tests.fakebroker import FakeBroker

NOW = dt.datetime(2026, 9, 15, 12, tzinfo=dt.timezone.utc)
ROOT = pathlib.Path(__file__).resolve().parents[1]
LABEL = "\U0001f4ca Finance\n"


def call(name, **args):
    import qa_server
    import tools  # noqa: F401 — registers the tools
    return qa_server.TOOLS[name]["fn"](args)


def tap(prop, label):
    c = next(x["call"] for x in prop["buttons"] if x["label"] == label)
    return call(c["tool"], **c["arguments"])


class LegacyReceipt(StoreCase):
    """review_r4.py::test_legacy_receipt, inverted, and review_migration.py (d3)."""

    def _v10(self, status="staged", channel="telegram"):
        from tests.schema_history import build_v10_store
        import db
        import tools
        path = self.tmp / f"v10-{status}-{channel}.sqlite"
        build_v10_store(path, staged_email=True)
        c = sqlite3.connect(path)
        c.execute("UPDATE deliveries SET channel=?, created_at='2026-09-01T00:00:00Z',"
                  " lease_at='2026-09-01T00:00:00Z'", (channel,))
        if status == "uncertain":
            # what v0.9.0's recover_staged left: settled uncertain, its staged copy taken back
            c.execute("UPDATE deliveries SET status='uncertain', settled_at="
                      "'2026-09-02T00:00:00Z', withdrawn_at='2026-09-02T00:00:00Z'")
        c.execute("UPDATE counters SET value=1 WHERE name='pass_generation'")
        c.commit()
        c.close()
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        old = tools._CONN
        tools._CONN = conn
        self.addCleanup(setattr, tools, "_CONN", old)
        return conn

    def delivered(self, c):
        return c.execute("SELECT count(*) FROM deliveries WHERE status='delivered'"
                         ).fetchone()[0]

    def test_a_migrated_staged_send_takes_its_late_receipt(self):
        c = self._v10()
        self.assertEqual(c.execute("SELECT count(*) FROM deliveries WHERE posted_at IS NULL"
                                   ).fetchone()[0], 0)
        call("record_delivery", delivery_id=1, outcome="delivered", message_id="telegram-42")
        self.assertEqual(self.delivered(c), 1)

    def test_recovered_after_the_migration_then_a_late_receipt(self):
        c = self._v10()
        call("job_next", job_id="aaaaaaaa-1")
        self.assertEqual(c.execute("SELECT status FROM deliveries").fetchone()[0], "uncertain")
        call("record_delivery", delivery_id=1, outcome="delivered", message_id="telegram-42")
        self.assertEqual(self.delivered(c), 1)

    def test_recovered_before_the_migration_then_a_late_receipt(self):
        """d3 Astra S2: v0.9.0 recovered the send `uncertain` before the upgrade."""
        c = self._v10(status="uncertain")
        self.assertIsNotNone(c.execute("SELECT posted_at FROM deliveries").fetchone()[0])
        call("record_delivery", delivery_id=1, outcome="delivered", message_id="telegram-42")
        self.assertEqual(self.delivered(c), 1)

    def test_email_sends_are_not_backfilled(self):
        c = self._v10(channel="email")
        self.assertEqual(c.execute("SELECT count(*) FROM deliveries WHERE posted_at IS NOT"
                                   " NULL").fetchone()[0], 0)

    def test_a_fresh_unposted_s7_send_still_refuses(self):
        import asks
        import db
        import delivery
        asks.request_package(self.conn, "2026-Q3")
        did, tok = self.drive_to_staged("aaaaaaaa-1")
        with self.assertRaises(db.Refusal):
            delivery.record_delivery(self.conn, delivery_id=did, outcome="delivered",
                                     package_token=tok)
        self.assertEqual(self.delivered(self.conn), 0)


# ---------------------------------------------------------------------------------------
# Red cases 7, 8, 11, 13, 15 — R1 quote matching, V2 tags, V3 same facts
# ---------------------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
