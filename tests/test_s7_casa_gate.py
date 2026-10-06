"""Runs the Casa gate when CASA_TREE and CASA_TESTS are set (with CASA_PY: Casa's
interpreter); otherwise skips, naming the command (Global Constraints). The gate also fails
on an empty, truncated or thinned file, and on a deposit with no display expectation and no
explicit skip (review r1)."""
import json, os, subprocess, sys, tempfile, unittest
from tests._base import ROOT
from tests.gen_casa_shapes import KINDS as KIND, kind_of


class GeneratorCovers(unittest.TestCase):
    """The generator side, stdlib only (Task 17): every new deposit shape of the simple
    loop is generated, every tap's next card is recorded, and [Get package] buttons reach
    the deposits Casa's real validator judges."""

    def test_the_generator_covers_every_new_shape(self):
        from tests import gen_casa_shapes as g
        records = g.generate().records
        cases = {r["case"] for r in records}
        for prefix in ("end:operator", "end:scheduled", "end:nothing", "end:handover",
                       "end:completion", "open-items", "all-answered", "ready",
                       "review:candidates", "review:set", "review:single", "vendor:page1",
                       "vendor:last", "vendor:scheduled", "get_package"):
            self.assertTrue(any(c.startswith(prefix) for c in cases), prefix)
        nexts = [r for r in records if "next" in r]
        self.assertTrue(nexts)
        self.assertTrue(all(r["tool"] == "verdict" and isinstance(r["next"], dict)
                            for r in nexts))
        deposits = [r for r in records if "body" in r]      # stored_call records have none
        get = [r for r in deposits if r.get("tool") == "show_view" and any(
            b["call"]["tool"] == "get_package"
            for b in json.loads(r["body"]["value"])["buttons"] if "call" in b)]
        self.assertTrue(get)                       # #1303 buttons reach the real validator


@unittest.skipUnless(os.environ.get("CASA_TREE") and os.environ.get("CASA_TESTS")
                     and os.environ.get("CASA_PY"),
                     "the Casa gate needs CASA_TREE, CASA_TESTS, CASA_PY — see the plan's "
                     "Global Constraints")
class CasaGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.shapes = os.path.join(cls._dir.name, "shapes.jsonl")
        subprocess.run([sys.executable, str(ROOT / "tests/gen_casa_shapes.py"), cls.shapes],
                       check=True, capture_output=True)
        with open(cls.shapes) as f:
            cls.lines = f.read().splitlines()

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def check(self, lines):
        path = os.path.join(self._dir.name, "variant.jsonl")
        with open(path, "w") as f:
            f.write("".join(line + "\n" for line in lines))
        return subprocess.run([os.environ["CASA_PY"], str(ROOT / "scripts/check_casa_shapes.py"),
                               path], capture_output=True, text=True)

    def test_every_deposit_passes_casas_validators(self):
        r = subprocess.run([os.environ["CASA_PY"], str(ROOT / "scripts/check_casa_shapes.py"),
                            self.shapes], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"OK: \d+ records \([1-9]\d* next_card, .*display checked"
                                   r" [1-9]\d*, skipped \d+; quotes bound [1-9]\d*")

    def test_an_empty_file_fails(self):
        r = self.check([])
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("no header", r.stdout)

    def test_a_header_alone_or_a_truncated_file_fails(self):
        self.assertNotEqual(self.check(self.lines[:1]).returncode, 0)
        r = self.check(self.lines[:len(self.lines) // 2])
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("declared cases not accepted", r.stdout)

    def test_a_deposit_with_neither_expectation_nor_skip_fails(self):
        lines = list(self.lines)
        i = next(i for i, line in enumerate(lines) if '"display_expect"' in line)
        rec = json.loads(lines[i])
        del rec["display_expect"]
        lines[i] = json.dumps(rec, ensure_ascii=False)
        r = self.check(lines)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("neither a display expectation nor an explicit skip", r.stdout)

    def test_turning_a_check_into_a_skip_falls_under_the_floor(self):
        lines = list(self.lines)
        i = next(i for i, line in enumerate(lines) if '"display_expect"' in line)
        rec = json.loads(lines[i])
        del rec["display_expect"]
        rec["display_skip"] = "thinned"
        lines[i] = json.dumps(rec, ensure_ascii=False)
        r = self.check(lines)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("display checks, the header declares", r.stdout)

    def thinned(self, drop):
        """The real file without every deposit `drop(rec)` selects, its header rewritten to
        match — so only the coverage floor can catch the loss."""
        head, *recs = [json.loads(line) for line in self.lines]
        keep = [r for r in recs if r["case"] == "stored_call" or not drop(r)]
        deposits = [r for r in keep if r["case"] != "stored_call"]
        kinds = {}
        for r in deposits:
            k = kind_of(r)                  # a next record's tool is verdict: next_card
            kinds[k] = kinds.get(k, 0) + 1
        head.update(cases=[r["case"] for r in deposits], kinds=kinds,
                    display_checked=sum(1 for r in deposits if "display_expect" in r))
        return [json.dumps(r, ensure_ascii=False) for r in [head] + keep]

    def test_a_capability_tool_with_no_deposit_fails(self):
        """Part 14b: every capability tool the manifest declares must be judged."""
        r = self.check(self.thinned(lambda rec: rec["tool"] == "propose_account"))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("no propose_account deposit was judged", r.stdout)

    def test_a_deposit_kind_with_no_deposit_fails(self):
        r = self.check(self.thinned(lambda rec: "next" not in rec
                                    and KIND.get(rec["tool"]) == "operator_file"))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("no operator_file deposit was judged", r.stdout)
        for tool in ("post_package", "get_package"):
            self.assertIn(f"no {tool} deposit was judged", r.stdout)


    def variant(self, pick, change):
        """The real file with the first record `pick` selects changed by `change(rec)`."""
        lines = list(self.lines)
        i = next(i for i, line in enumerate(lines) if pick(json.loads(line)))
        rec = json.loads(lines[i])
        change(rec)
        lines[i] = json.dumps(rec, ensure_ascii=False)
        return self.check(lines), rec["case"]

    def test_a_next_card_casa_refuses_fails(self):
        """#1302: a next card over Casa's button limit is refused by proposal_ok."""
        def seven(rec):
            rec["next"]["buttons"] = (rec["next"]["buttons"] * 7)[:7]
        r, case = self.variant(lambda rec: "next" in rec, seven)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertRegex(r.stdout, rf"FAIL \d+ \S+ {case}")

    def test_a_next_card_without_a_receipt_fails(self):
        r, case = self.variant(lambda rec: "next" in rec, lambda rec: rec.update(receipt=" "))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"the next card is not read beside a receipt {case}", r.stdout)

    def test_a_refusal_carrying_a_link_fails(self):
        """#1303: a no-post answer must hold every slot null."""
        r, case = self.variant(lambda rec: "result" in rec,
                               lambda rec: rec["result"].update(package="ref"))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"not Casa's no-post shape {case}", r.stdout)

    def test_no_next_card_fails(self):
        r = self.check(self.thinned(lambda rec: "next" in rec))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("no next_card deposit was judged", r.stdout)
