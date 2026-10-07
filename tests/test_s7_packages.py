# tests/test_s7_packages.py
"""S7 §6: the package is posted as a file (simple loop §1: never by the job); the file keeps
its package name through Casa's filename; the caption is one line; first sends at most
once; resend and send-last from the desk; no email."""
import json
from tests._base import StoreCase
from tests.fakebroker import FakeBroker

A = "aaaaaaaa-1"


class Packages(StoreCase):
    def test_a_refused_filename_settles_the_send_and_says_so(self):
        did = self.staged_package()
        with FakeBroker() as b:
            b.refuse = "bad_filename"
            import tools, qa_server  # noqa: F401
            out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        self.assertIsNone(out["package"])
        self.assertIn("could not be sent under its name", out["refused"])
        d = self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                              (did,)).fetchone()[0]
        self.assertEqual(d, "failed")
        staged = self.conn.execute("SELECT staged_path FROM deliveries WHERE delivery_id=?",
                                   (did,)).fetchone()[0]
        import pathlib
        self.assertFalse(pathlib.Path(staged).exists())        # the staged copy taken back

    def test_send_last_twice_and_resend_after_delivered_refuses(self):
        """§6.1 pin, §19.3: uncertain → "send it again" delivers; then "send me the last
        package you built" twice, each under the package's filename from its own path; a
        resend after a delivered send refuses as v0.9.0."""
        import delivery, posting
        self.sent_package(first_outcome="uncertain")    # built, first send uncertain
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

class Review(StoreCase):
    """Rulings and review carries for Task 11."""

    def test_a_send_last_caption_says_when_it_was_built(self):
        import delivery, posting
        self.sent_package()
        st = delivery.stage_for_delivery(self.conn, last_built=True)
        with FakeBroker() as b:
            out = posting.post_package(self.conn, st["delivery_id"])
        self.assertTrue(out["package"].startswith("casa-cap-"))
        self.assertIn(", as it was then", b.deposits[0]["caption"])
        self.assertNotIn("\n", b.deposits[0]["caption"])

    def test_post_package_refuses_a_send_not_waiting(self):
        import asks, tools, qa_server  # noqa: F401
        self.sent_package()
        with FakeBroker() as b:
            did = self.conn.execute("SELECT delivery_id FROM deliveries").fetchone()[0]
            out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": did})
        self.assertIsNone(out["package"])
        self.assertIn("no longer waiting", out["refused"])
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
