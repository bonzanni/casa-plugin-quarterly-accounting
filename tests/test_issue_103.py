"""#103 (operator ruling 2026-10-08): "check Q3" asks where the quarter stands — the status
card — and a check job starts only when the operator asks for the work. The model judges
from the operator's own words, also when a delegate relays them as "run the check"; the
skill and request_work's description carry the ruling (no keyword gate)."""
from tests._base import StoreCase, ROOT

DESK = (ROOT / "skills/quarterly-accounting/SKILL.md").read_text()


def flat(s):
    return " ".join(s.split())


class CheckIsStatus(StoreCase):
    def test_the_desk_reads_a_bare_check_as_the_status_question(self):
        start = DESK.index("**Where a quarter stands**")
        s = flat(DESK[start:DESK.index("**Get a quarter done**")])
        self.assertIn('"check Q3"): nothing runs.', s)
        self.assertIn('A bare "check" is this, even relayed by a delegate as "run the '
                      'check": the operator\'s words decide.', s)
        self.assertLessEqual(len(DESK), 10_000)

    def test_request_work_says_a_bare_check_is_not_its_job(self):
        import qa_server, tools  # noqa: F401
        d = flat(qa_server.TOOLS["request_work"]["description"])
        self.assertIn("when the operator asks for the work", d)
        self.assertIn('A bare "check Q3" asks where the quarter stands: '
                      'show_view(view="open", quarter="2026-Q3"), not this.', d)   # #126
