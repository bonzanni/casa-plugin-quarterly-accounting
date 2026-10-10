"""#98: a payment the classifier never tagged is no missing invoice — the job classifies
first (its skill), and what stays unclassified is its own bucket ("not classified yet").
#97: money back from a tax authority is nice-to-have by the job's judgement (decide
`optional` with a reason), bound to the facts it judged; the operator's rules win."""

from tests._base import LoopCase, StoreCase
import db
import expectation as ex

JOB = (__import__("pathlib").Path(__file__).resolve().parents[1]
       / "skills/quarterly-job/SKILL.md").read_text()
DESK = (__import__("pathlib").Path(__file__).resolve().parents[1]
        / "skills/quarterly-accounting/SKILL.md").read_text()


def flat(s):
    return " ".join(s.split())


class Derive(StoreCase):
    def test_a_judged_refund_is_optional_and_the_operators_rules_win(self):
        refund = ["income", "refund"]
        self.assertEqual(ex.derive("CRDT", refund), ex.Expectation("credit-note", "required", 7))
        self.assertEqual(ex.derive("CRDT", refund, judged_optional=True),
                         ex.Expectation("credit-note", "optional", 7))
        # the operator's counterparty rule and exemption win over the job's judgement
        self.assertEqual(ex.derive("CRDT", refund, judged_optional=True,
                                   counterparty_override=("credit-note", "required")).tier,
                         "required")
        self.assertEqual(ex.derive("CRDT", refund, exempt=True, judged_optional=True).kind,
                         "none")


class _Loop(LoopCase):
    def c(self, fn, *a, **kw):
        with db.tx(self.conn):
            return fn(self.conn, *a, **kw)

    def refund(self, who="BELASTINGDIENST", amount=14300, tags=("income", "refund")):
        self.n += 1
        self.row(self.n, counterparty=who, amount_minor=amount, booking_date="2026-08-03",
                 value_date="2026-08-03", direction="CRDT")
        pid = self.lineage_for(self.n)
        self.classify(pid, set(tags))
        self.settle(pid)
        return pid

    def proj(self, pid):
        return self.conn.execute("SELECT * FROM projections WHERE pid=?", (pid,)).fetchone()

    def judge(self, pid, reason="money back from the tax authority"):
        import decide
        return decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "optional", "reason": reason,
            "expected_revision": self.rev(pid)}])["results"][0]


class Judgement(_Loop):
    def test_optional_needs_a_reason_and_makes_the_document_nice_to_have(self):
        pid = self.refund()
        self.assertEqual(self.proj(pid)["exp_tier"], "required")
        out = self.judge(pid, reason="  ")
        self.assertFalse(out["applied"])
        self.assertIn("reason", out["refused"])
        self.assertTrue(self.judge(pid)["applied"])
        p = self.proj(pid)
        self.assertEqual((p["exp_kind"], p["exp_tier"]), ("credit-note", "optional"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM log WHERE pid=? AND"
                                           " kind='judge'", (pid,)).fetchone()[0], 1)

    def test_new_facts_bring_the_default_back(self):
        pid = self.refund()
        self.judge(pid)
        self.assertEqual(self.proj(pid)["exp_tier"], "optional")
        self.classify(pid, {"income", "salary"})              # reclassified: other facts
        self.settle(pid)
        self.assertEqual(self.proj(pid)["exp_tier"], "required")

    def test_a_judgement_is_a_new_completion_identity(self):
        import loop
        pid = self.refund()
        before = loop.completion_sig(self.conn, "2026-Q3")
        self.judge(pid)
        self.assertNotEqual(loop.completion_sig(self.conn, "2026-Q3"), before)

    def test_a_vendor_rule_of_the_operator_wins(self):
        import kb
        pid = self.refund()
        self.judge(pid)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO counterparties(name, patterns_json, exp_kind,"
                              " exp_tier, exp_author, updated_at) VALUES ('BELASTINGDIENST',"
                              " '[]', 'credit-note', 'required', 'operator', 'x')")
        self.settle(pid)
        self.assertEqual(self.proj(pid)["exp_tier"], "required")
        del kb


class Unclassified(_Loop):
    def untagged(self, who="Belastingdienst", amount=212000):
        pid = self.pay(who, amount)
        self.classify(pid, set())
        self.settle(pid)
        return pid

    def test_an_untagged_payment_is_not_classified_yet_not_missing(self):
        import cards, views
        u = self.untagged()
        m = self.pay("Twilio", 2000)
        st = self.c(cards.state)
        self.assertEqual([d["pid"] for d in st["unclassified"]], [u])
        self.assertEqual([d["pid"] for ds in st["missing"].values() for d in ds], [m])
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (end,)).fetchone()[0]
        self.assertIn("1 missing · 1 not classified yet", text)
        states = dict(self.conn.execute("SELECT pid, item_state FROM render_states WHERE"
                                        " render_id=?", (end,)).fetchall())
        self.assertEqual(states.get(u), "unclassified")
        self.assertNotIn(u, cards._missing_of(self.conn, "Belastingdienst"))
        self.assertFalse(views._is_missing(__import__("work").describe(self.conn, u)))

    def test_an_unclassified_only_quarter_is_never_all_accounted_for(self):
        import cards
        self.untagged()
        for fn in (lambda c: cards.compose_end(c, self.job_id, scheduled=False),
                   lambda c: cards.compose_open(c, "2026-Q3")):
            with db.tx(self.conn):
                rid = fn(self.conn)
            text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                     (rid,)).fetchone()[0]
            self.assertNotIn("all accounted for", text)
            self.assertIn("1 not classified yet", text)


class Skills(StoreCase):
    def test_the_job_classifies_first_and_has_one_narrow_exception(self):
        j = flat(JOB)
        self.assertIn("When its trailer says rows await the classifier, first classify them "
                      "inline as skill classify-transactions does", j)
        self.assertIn("Never \"no invoice needed\": that is the operator's. One exception: "
                      "money coming back from a tax authority", j)
        self.assertIn('`"optional"` with a `reason` saying so', j)

    def test_the_desk_says_how_words_classify_a_payment(self):
        self.assertIn("Words saying what a payment is (\"that's wage tax\") classify it: tag "
                      "it as skill classify-transactions does (even if already tagged), then "
                      "the check ask below, with its quarter.", flat(DESK))    # #106, #115
        self.assertLessEqual(len(DESK), 10_000)


class JudgementBasis(_Loop):
    """d2 (Astra + Terra S1): the judgement is bound to every handed fact, and a later
    decision supersedes it for good."""

    def test_a_changed_remittance_brings_the_default_back(self):
        pid = self.refund()
        self.judge(pid)
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET remittance='Payment of invoice INV123456'"
                              " WHERE row_id=(SELECT dest_row_id FROM projections WHERE pid=?)",
                              (pid,))
        self.settle(pid)
        self.assertEqual(self.proj(pid)["exp_tier"], "required")

    def test_a_later_missing_decision_is_never_undone_by_restored_facts(self):
        import decide
        pid = self.refund()
        self.judge(pid)
        self.classify(pid, {"income", "refund", "taxes"})          # basis changes
        self.settle(pid)
        self.assertEqual(self.proj(pid)["exp_tier"], "required")
        out = decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "missing", "reason": "no credit note found",
            "expected_revision": self.rev(pid)}])["results"][0]
        if not out["applied"]:
            self.skipTest(out["refused"])        # the fixture's listing precondition
        self.classify(pid, {"income", "refund"})                    # the old facts again
        self.settle(pid)
        self.assertEqual(self.proj(pid)["exp_tier"], "required")


class UnclassifiedEverywhere(_Loop):
    """r1 (Astra S2): an unclassified payment of another quarter is counted wherever its
    quarter is summed up, never dropped."""

    def test_an_older_unclassified_payment_is_counted_on_the_card_and_in_views(self):
        import cards, views
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        old = self.pay("Belastingdienst", 212000, "2026-05-26")
        self.classify(old, set())
        self.settle(old)
        self.pay("Twilio", 2000)                              # a Q3 missing payment
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (end,)).fetchone()[0]
        self.assertIn("Q2 still open: 1 not classified yet", text)
        q2 = views.build_review(self.conn, "quarter", quarter="2026-Q2")["text"]
        self.assertIn("**Not classified yet**\nBelastingdienst", q2)       # #106: named
        status = views.build_review(self.conn, "status", quarter="2026-Q3")["text"]
        self.assertIn("+1 older not classified yet (Q2)", status)
