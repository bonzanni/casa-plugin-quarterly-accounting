"""#99 (+ #93 #94 #96): one light house style for every card, shared in one place
(views.title / views.groups): a bold title line, the question on its own line above the
list it asks about, one tight line per item, blank lines only between groups, no legend
for the buttons, short names, no empty section. Close goes beside real actions and never
alone (a card with nothing to act on goes plain); Get package only where the desk judged
the operator wants the package; a proposed pair is stated once."""

from tests._base import LoopCase, StoreCase, apply_now
from tests.fakebroker import FakeBroker
import db
import views

SKILL = (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "skills/quarterly-accounting/SKILL.md").read_text()


def flat(s):
    return " ".join(s.split())


class Helpers(StoreCase):
    def test_title_groups_and_what_the_operator_sees(self):
        self.assertEqual(views.title("Q3 · 2 payments"), "**Q3 · 2 payments**")
        self.assertEqual(views.groups(["a", "b"], [], ["c"], ["d"]),
                         ["a", "b", "", "c", "", "d"])
        text = views.title(views.field("*Acme* BV")) + "\n1 missing"
        self.assertEqual(views.displayed(text), "*Acme* BV\n1 missing")
        self.assertEqual(views.bold_spans(text), ["*Acme* BV"])


class Cards(LoopCase):
    def c(self, fn, *a, **kw):
        with db.tx(self.conn):
            return fn(self.conn, *a, **kw)

    def text(self, rid):
        return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]

    def labels(self, rid):
        import cards
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        with db.tx(self.conn):
            return [b[0] for b in cards.buttons(self.conn, r)]

    def test_an_end_card_has_a_title_a_question_tight_rows_and_no_legend(self):
        import cards
        for i in range(3):
            p = self.pay("V%d" % i, 1000 + i)
            self.propose(p, amount_minor=1000 + i, document_number="pi_3UKZ%d" % i)
        self.pay("Twilio", 2000)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        lines = self.text(rid).split("\n")
        self.assertTrue(lines[0].startswith("**Q3 checked · 4 payments**"), lines[0])
        i = lines.index("**" + cards.CONFIRM_Q + "**")
        self.assertEqual(lines[i - 1], "")                       # its own group
        rows = lines[i + 1:i + 4]
        self.assertEqual([r[:3] for r in rows], ["1. ", "2. ", "3. "])   # tight, no blank
        self.assertNotIn("pi_3UKZ", self.text(rid))                    # short names
        self.assertNotIn("Review:", self.text(rid))                    # no legend
        self.assertEqual(self.labels(rid), ["Review", "Confirm all", "Invoice links", "Close"])

    def test_a_quote_of_a_bold_titled_card_binds_it(self):
        import cards
        p = self.pay("Zapier", 1958)
        self.propose(p, amount_minor=1958)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.c(cards.deposit_of, rid)
        quote = "\U0001f4ca Finance\n" + views.displayed(self.text(rid))
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], rid)
        out = apply_now(self.conn, "all good", quoted=self.text(rid))
        self.assertTrue(out["applied"])

    def test_a_handover_card_names_what_happened_and_states_a_pair_once(self):
        import cards
        q = self.pay("Runpod.io", 8792)
        held = self.propose(q, amount_minor=8792, document_number="pi_3UKZabc",
                            issuer="Runpod Inc")
        rid = self.c(cards.compose_end, self.job_id, scheduled=False, handover_docs=[held])
        lines = self.text(rid).split("\n")
        self.assertTrue(lines[0].startswith("**1 document you sent**"), lines[0])
        self.assertEqual(sum("Runpod" in ln for ln in lines), 1, lines)   # #96: once
        self.assertNotIn("— confirm?", self.text(rid))

    def test_a_job_end_card_and_a_completion_notice_offer_no_package(self):
        import cards
        p = self.pay("Adobe", 100)
        self.machine_match(p, self.doc(amount_minor=100), self.token)
        end = self.c(cards.compose_end, self.job_id, scheduled=False)
        self.assertEqual(self.labels(end), [])                          # #94, #93: plain
        ready = self.c(cards.compose_ready, ["2026-Q3"])
        self.assertEqual(self.labels(ready), [])


class Views(StoreCase):
    def test_a_view_with_nothing_to_act_on_goes_plain(self):
        import posting
        self.seed_payments([{"counterparty": "Adobe"}])
        with FakeBroker() as b:
            out = posting.show_view(self.conn, view="missing", quarter="2026-Q3")
        self.assertEqual((out["view"], b.deposits), (None, []))
        self.assertIn("post_results", out["note"])
        with FakeBroker() as b:
            posting.post_results(self.conn, out["post"][0])
        self.assertEqual(b.deposits[0]["slot"], "results")
        self.assertTrue(b.deposits[0]["value"].startswith("**Missing · Q3 2026**"))

    def test_missing_rows_are_tight(self):
        rows = [{"counterparty": "Vendor %d" % i, "amount_minor": 100 + i} for i in range(4)]
        self.seed_payments(rows)
        text = views.build_review(self.conn, "missing", quarter="2026-Q3")["text"]
        lines = text.split("\n")
        at = lines.index("**Missing**")
        self.assertEqual(len([ln for ln in lines[at + 1:at + 5] if ln.startswith("Vendor")]), 4)

    def test_the_open_card_offers_the_package_only_when_the_desk_asks(self):
        import posting
        self.seed_payments([{"counterparty": "Adobe"}])
        for package, want in ((False, False), (True, True)):
            with FakeBroker() as b:
                posting.show_view(self.conn, view="open", quarter="2026-Q3", package=package)
            labels = [x["label"] for x in b.proposal()["buttons"]]
            self.assertEqual("Get package" in labels, want, labels)
            self.assertEqual(labels[-1], "Close")


class Links(LoopCase):
    def test_no_known_link_is_one_line_not_a_list_of_unknowns(self):
        import cards
        self.pay("Runpod", 3000)
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3", links=True)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]
        self.assertIn("No download links known for these vendors.", text)
        self.assertNotIn("no link known", text)


class Skill(StoreCase):
    def test_the_desk_skill_carries_the_style_post_and_package(self):
        s = flat(SKILL)
        for phrase in ("One with `post` has nothing to act on: post it with `post_results` as "
                       "its note says.",
                       "Your own replies about the books take the cards' shape: a bold first "
                       "line, a question on its own line, one line per item.",
                       "Add `package=true` only when their words make clear they want the "
                       "package"):
            self.assertIn(phrase, s, phrase)
        self.assertLessEqual(len(SKILL), 10_000)
