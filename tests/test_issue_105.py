"""#105: an unsettled classification is never "missing" — row 4 (untagged or parked) and
row 5 (conflicting tags) alike: not counted or listed missing, no "invoice missing" note in
the bank ledger, never decided missing; row 4 is not walked; a row-5 payment is judged by
the model (optional when it needs no invoice by its nature, else left)."""
from tests._base import LoopCase, StoreCase
import db

JOB = (__import__("pathlib").Path(__file__).resolve().parents[1]
       / "skills/quarterly-job/SKILL.md").read_text()


class _Loop(LoopCase):
    def c(self, fn, *a, **kw):
        with db.tx(self.conn):
            return fn(self.conn, *a, **kw)

    def tagged(self, tags, who="Loonadministratie", amount=148000):
        pid = self.pay(who, amount)
        self.classify(pid, set(tags))
        self.settle(pid)
        return pid

    def proj(self, pid):
        return self.conn.execute("SELECT * FROM projections WHERE pid=?", (pid,)).fetchone()

    def decide(self, pid, outcome, **kw):
        import decide
        return decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": outcome, "expected_revision": self.rev(pid), **kw}])[
            "results"][0]


class Unsettled(_Loop):
    def test_a_conflict_is_not_classified_yet_on_cards_and_in_the_ledger(self):
        import cards, mirror, views, work
        wage = self.tagged({"taxes", "payroll", "wage-tax", "recurring"})
        self.assertEqual(self.proj(wage)["exp_row"], 5)
        st = self.c(cards.state)
        self.assertEqual([d["pid"] for d in st["unclassified"]], [wage])
        self.assertEqual(st["missing"], {})
        self.assertFalse(views._is_missing(work.describe(self.conn, wage)))
        self.assertEqual(mirror.note_text(self.conn, wage),
                         "Accounting: not classified yet (quarterly check)")

    def test_a_parked_payment_is_not_walked_and_its_note_is_not_missing(self):
        import loop, mirror
        parked = self.tagged({"awaiting-operator"})
        p = self.proj(parked)
        self.assertEqual(p["exp_row"], 4)
        row = self.conn.execute("SELECT * FROM bank_rows WHERE row_id=?",
                                (p["dest_row_id"],)).fetchone()
        self.assertIsNone(loop.why_work(self.conn, parked, p, dict(row)))
        self.assertIn("not classified yet", mirror.note_text(self.conn, parked))

    def test_missing_is_refused_and_leave_closes_the_item(self):
        wage = self.tagged({"taxes", "payroll"})
        out = self.decide(wage, "missing", reason="nothing found")
        self.assertFalse(out["applied"])
        self.assertIn("not settled", out["refused"])
        self.assertTrue(self.decide(wage, "leave", reason="tags conflict")["applied"])
        self.assertEqual(self.proj(wage)["status"], "open")
        adobe = self.pay("Adobe", 100)
        self.assertFalse(self.decide(adobe, "leave")["applied"])          # settled: no

    def test_a_conflict_judged_optional_is_nice_to_have_until_its_tags_change(self):
        wage = self.tagged({"taxes", "payroll"})
        self.assertTrue(self.decide(wage, "optional",
                                    reason="wage tax: payroll and taxes")["applied"])
        self.assertEqual(self.proj(wage)["status"], "optional")
        self.classify(wage, {"taxes", "payroll", "recurring"})
        self.settle(wage)
        self.assertEqual(self.proj(wage)["status"], "open")


class Skill(StoreCase):
    def test_the_job_judges_a_conflict_and_never_decides_it_missing(self):
        j = " ".join(JOB.split())
        self.assertIn("`expectation.row` 5 (its tags conflict): judge it from its tags, "
                      "remittance and history.", j)
        self.assertIn('else `"leave"` with a reason. Never `missing`: it is not classified '
                      "yet.", j)


class D1Folds(_Loop):
    def test_an_unsettled_payment_keeps_the_quarter_from_completing(self):
        import loop
        wage = self.tagged({"taxes", "payroll"}, amount=1000)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='accepted-missing'"
                              " WHERE pid=?", (wage,))
        self.classify(wage, {"software"})                 # settled, left missing: complete
        self.settle(wage)
        from unittest import mock
        with mock.patch.object(loop, "covered", lambda c, q: True), \
                mock.patch.object(loop.dates, "is_partial", lambda q, t: False):
            before = loop.complete(self.conn, "2026-Q3")
            self.classify(wage, {"taxes", "payroll"})     # unsettled again
            self.settle(wage)
            self.assertEqual((before, loop.complete(self.conn, "2026-Q3")), (True, False))

    def test_leave_revokes_an_earlier_judgement(self):
        wage = self.tagged({"taxes", "payroll"})
        self.decide(wage, "optional", reason="wage tax")
        self.classify(wage, {"taxes", "payroll", "recurring"})          # judgement stale
        self.settle(wage)
        self.assertTrue(self.decide(wage, "leave", reason="unclear")["applied"])
        self.classify(wage, {"taxes", "payroll"})                       # old facts again
        self.settle(wage)
        self.assertEqual(self.proj(wage)["status"], "open")


class R1Folds(_Loop):
    def test_a_left_missing_payment_whose_tags_now_conflict_is_walked_again(self):
        import loop
        p = self.tagged({"software"}, amount=1000)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='accepted-missing'"
                              " WHERE pid=?", (p,))
        def why():
            pr = self.proj(p)
            row = self.conn.execute("SELECT * FROM bank_rows WHERE row_id=?",
                                    (pr["dest_row_id"],)).fetchone()
            return loop.why_work(self.conn, p, pr, dict(row))
        self.assertIsNone(why())
        self.classify(p, {"taxes", "payroll"})
        self.settle(p)
        self.assertIsNotNone(why())

    def test_an_undecided_unsettled_payment_is_no_missing_line(self):
        import loop
        wage = self.tagged({"taxes", "payroll"})
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO run_work(job_id, pid, vendor, why) VALUES"
                              " (?, ?, 'Loon', 'open')", (self.job_id, wage))
        self.assertEqual(loop.partial_lines(self.conn, self.job_id), [])
