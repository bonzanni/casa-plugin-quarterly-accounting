"""Runs the Casa gate when CASA_TREE and CASA_TESTS are set (with CASA_PY: Casa's
interpreter); otherwise skips, naming the command (Global Constraints). The gate also fails
on an empty, truncated or thinned file, and on a deposit with no display expectation and no
explicit skip (review r1)."""
import json, os, subprocess, sys, tempfile, unittest
from tests._base import ROOT


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
        self.assertRegex(r.stdout, r"OK: \d+ records .*display checked [1-9]\d*, skipped \d+\)")

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
