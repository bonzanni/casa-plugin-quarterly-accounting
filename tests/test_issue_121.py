# tests/test_issue_121.py
"""#121: the desk reads the operator's words; code applies explicit operations.

reading_context lists what the words can be about (every open payment by pid, the post's
own first); propose_reading takes operations by pid, checks only mechanical facts, and
posts the same Apply/Cancel reading as before. The regex grammar and the cards' phrase
teaching are gone."""
import json
import pathlib
import re

from tests import _base
from tests._base import apply_now
from tests.test_reply import Base
import db  # noqa: E402
import posting  # noqa: E402
import reply  # noqa: E402
import views  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def propose(conn, ops, quoted=None):
    from tests.fakebroker import FakeBroker
    with FakeBroker() as b:
        out = posting.propose_reading(conn, _base.as_ops(ops), quoted)
    return out, (json.loads(b.deposits[-1]["value"]) if b.deposits else None)


class Context(Base):
    def test_open_payments_by_pid_the_posts_own_first(self):
        off = self.item("Adobe", 5445, "2026-09-20", paired=False)
        self.deliver("check")                     # Adobe is missing: not on the check sheet
        z = self.item("Zapier", 9900, "2026-09-17")
        r = self.deliver("check")
        ctx = posting.reading_context(self.conn)
        self.assertEqual(ctx["render_id"], r["render_id"])
        self.assertEqual(ctx["post"], {"kind": "check", "quarter": "2026-Q3"})
        first, second = ctx["items"]
        self.assertEqual((first["pid"], first["on_post"], first["state"]), (z, True, "suggested"))
        self.assertEqual(first["line"], "Zapier · EUR 99.00 · 17 Sep")
        self.assertIn("Zapier", first["document"])
        self.assertEqual((second["pid"], second["on_post"]), (off, False))
        self.assertEqual(second["state"], "missing a document")

    def test_a_quote_that_binds_nothing_is_said_with_its_recovery(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver("check")
        ctx = posting.reading_context(self.conn, "something I never sent")
        self.assertEqual((ctx["say"], ctx["show_view"]), (views.UNMATCHED, {"view": "status"}))

    def test_it_writes_nothing(self):
        self.item("Zapier", 9900, "2026-09-17")
        self.deliver("check")
        before = self.conn.total_changes
        posting.reading_context(self.conn)
        self.assertEqual(self.conn.total_changes, before)


class Ops(Base):
    def test_every_write_operation_reaches_its_write(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        n = self.item("Notion", 10900, "2026-09-17")
        a = self.item("Adobe", 5445, "2026-09-18", paired=False)
        self.deliver("status")
        out = apply_now(self.conn, [("confirm", z), ("reject", n), ("no_document", a)])
        self.assertEqual([s["op"] for s in out["applied"]], ["confirm", "unpair", "exempt"])
        self.assertIn("Confirmed Zapier", out["receipt"])
        self.assertEqual(self.author(z)[0], "operator")

    def test_settings_need_no_post(self):
        out = apply_now(self.conn, [("zip_name", "acme books")])
        self.assertEqual([s["op"] for s in out["applied"]], ["name"])
        self.assertIn("acme\\-books", out["receipt"])

    def test_a_malformed_operation_is_refused_naming_the_operations(self):
        for bad in ([], [{"op": "explode"}], [{"op": "confirm"}], [{"op": "confirm", "pid": "3"}],
                    [{"op": "class_none", "kind": "invoices"}],
                    [{"op": "stop_chasing", "quarter": "soon"}], "confirm"):
            with self.assertRaises(db.Refusal, msg=bad):
                posting.propose_reading(self.conn, bad)

    def test_a_payment_not_on_the_post_is_refused_to_the_desk_with_its_card(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver("check")
        a = self.item("Adobe", 5445, "2026-09-18", paired=False)
        with self.assertRaises(db.Refusal) as cm:
            posting.propose_reading(self.conn, [{"op": "no_document", "pid": a}])
        self.assertIn(f'show_view(view="item", pid={a})', str(cm.exception))
        with self.assertRaises(db.Refusal):
            posting.propose_reading(self.conn, [{"op": "confirm", "pid": 99999}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.author(z)[0], "auto")

    def test_the_tool_answers_a_refusal_as_words_not_an_error(self):
        import qa_server
        import tools  # noqa: F401
        out = qa_server.TOOLS["propose_reading"]["fn"]({"ops": [{"op": "confirm", "pid": 7}]})
        self.assertIsNone(out["reading"])
        self.assertIn("reading_context", out["refused"])

    def test_a_reading_is_stored_as_its_operations_and_replayed(self):
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver("check")
        out, prop = propose(self.conn, [("reject", z)])
        row = self.conn.execute("SELECT text FROM readings").fetchone()
        self.assertEqual(json.loads(row["text"]), [{"op": "reject", "pid": z}])
        self.assertEqual([b["label"] for b in prop["buttons"]], ["Apply", "Cancel"])
        self.assertEqual(self.author(z)[0], "auto")              # nothing before Apply

    def test_a_reading_left_open_from_before_121_applies_nothing(self):
        import taps
        z = self.item("Zapier", 9900, "2026-09-17")
        self.deliver("check")
        _, prop = propose(self.conn, [("reject", z)])
        with db.tx(self.conn):
            self.conn.execute("UPDATE readings SET text='the Zapier one is wrong'")
        call = next(x for x in prop["buttons"] if x["label"] == "Apply")["call"]
        rec = taps.apply_reading(self.conn, **call["arguments"])
        self.assertEqual(rec["receipt"], taps.CHANGED)
        self.assertEqual(self.author(z)[0], "auto")

    def test_a_payment_changed_since_the_post_is_reshown_beside_the_reading(self):
        # d1 (Astra S2): `reshow` rides with a posted reading too
        z = self.item("Zapier", 9900, "2026-09-17")
        n = self.item("Notion", 10900, "2026-09-17")
        self.deliver("check")
        self.rejudge_any(z)
        out, prop = propose(self.conn, [("reject", z), ("reject", n)])
        self.assertIsNotNone(out["reading"])
        self.assertEqual(out["reshow"], [z])
        self.assertIn("changed since you saw it", prop["text"])

    def rejudge_any(self, pid):
        import matches
        mid = self.conn.execute("SELECT match_id FROM match_state WHERE pid=? AND state IN"
                                " ('matched','proposed')", (pid,)).fetchone()[0]
        matches.relabel_match(self.conn, match_id=mid, labels=("no-ref",), token=self.token)

    def test_a_broad_change_lists_every_payment_it_moves(self):
        # d1 (Astra S1, Terra S1): never "and N more" — the card shows every payment
        first = self.item("Adobe", 1000, "2026-09-17")
        self.deliver("check")
        for i in range(1, 9):                                   # never shown to the operator
            self.item("Adobe", 1000 + i, "2026-09-17")
        out = apply_now(self.conn, [("never", first)])
        self.assertIn("It changes 9 payments", out["proposal"])
        for i in range(9):
            self.assertIn(f"EUR {10 + i / 100:.2f}", out["proposal"])
        self.assertNotIn("more", out["proposal"])


class NoGrammar(Base):
    def test_the_phrase_grammar_is_gone(self):
        for name in ("PATTERNS", "_clauses", "_resolve", "_parse", "DIRECTIVES", "_polite"):
            self.assertFalse(hasattr(reply, name), name)

    def test_no_operator_text_teaches_a_phrase(self):
        """Operator-facing text never tells the operator words to type (#117, #121)."""
        src = (ROOT / "server" / "reply.py").read_text("utf-8")
        self.assertIsNone(re.search(r'[Ss]ay \\"|e\.g\. \\"', src))
