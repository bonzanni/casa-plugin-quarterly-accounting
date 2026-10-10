"""#126: a view asked for a named quarter showed another one (PLAY s91: "how's Q3?" got the
Q2 sheet). A show_view that names no quarter shows the store's default (ruling Q2b: the
quarter the newest check named), and its answer did not say which quarter that was. Now
the answer names the quarter shown, a defaulted one with a note the desk can judge by, and
the desk skill and the tool say a named quarter is passed as `quarter`."""
import pathlib

from tests._base import StoreCase
from tests.fakebroker import FakeBroker
import db
import posting
import qa_server
import tools  # noqa: F401 — registers the tools

DESK = (pathlib.Path(__file__).resolve().parents[1]
        / "skills/quarterly-accounting/SKILL.md").read_text()


def flat(s):
    return " ".join(s.split())


class ShownQuarter(StoreCase):
    def setUp(self):
        super().setUp()
        self.seed_payments([{"counterparty": "Adobe"}])              # a Q3 2026 payment
        with db.tx(self.conn):                                      # the newest check: Q2's
            self.conn.execute("INSERT INTO runs(job_id, quarter) VALUES ('j-q2', '2026-Q2')")

    def show(self, **kw):
        with FakeBroker():
            return posting.show_view(self.conn, **kw)

    def test_a_named_quarter_is_the_one_shown_and_answered(self):
        for view in ("open", "status", "missing", "check"):
            out = self.show(view=view, quarter="2026-Q3")
            self.assertEqual(out["quarter"], "2026-Q3", view)
            self.assertNotIn("No quarter was named", out["note"], view)

    def test_no_quarter_answers_the_default_with_a_note(self):
        for view in ("open", "status", "missing", "check"):
            out = self.show(view=view)
            self.assertEqual(out["quarter"], "2026-Q2", view)
            self.assertIn("No quarter was named, so this is 2026-Q2. If the operator named "
                          "another quarter, show_view again with theirs.", out["note"], view)

    def test_a_repost_and_an_item_carry_no_default_note(self):
        rid = self.show(view="status", quarter="2026-Q3")["render_id"]
        out = self.show(render_id=rid)
        self.assertEqual(out["quarter"], "2026-Q3")
        self.assertNotIn("No quarter was named", out["note"])
        pid = self.conn.execute("SELECT pid FROM projections").fetchone()[0]
        out = self.show(view="item", pid=pid)
        self.assertNotIn("quarter", out)
        self.assertNotIn("No quarter was named", out["note"])


class Words(StoreCase):
    def test_the_skill_and_the_tool_say_the_named_quarter_is_passed(self):
        self.assertIn("Every view of a quarter the operator named takes it as `quarter`: left "
                      "out, a view shows a default (the last check's quarter), which may not be "
                      "theirs. The answer's `quarter` is the one shown.", flat(DESK))
        self.assertLessEqual(len(DESK), 10_000)
        desc = qa_server.TOOLS["show_view"]["description"]
        self.assertIn("quarter: the quarter the operator named; left out, the view picks one "
                      "(the quarter the last check named, when one did), which may not be "
                      "theirs; the answer's `quarter` is the quarter shown.", desc)
