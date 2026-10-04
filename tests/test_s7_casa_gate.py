"""Runs the Casa gate when CASA_TREE and CASA_TESTS are set (with CASA_PY: Casa's
interpreter); otherwise skips, naming the command (Global Constraints)."""
import os, subprocess, sys, tempfile, unittest
from tests._base import ROOT


@unittest.skipUnless(os.environ.get("CASA_TREE") and os.environ.get("CASA_TESTS")
                     and os.environ.get("CASA_PY"),
                     "the Casa gate needs CASA_TREE, CASA_TESTS, CASA_PY — see the plan's "
                     "Global Constraints")
class CasaGate(unittest.TestCase):
    def test_every_deposit_passes_casas_validators(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "shapes.jsonl")
            subprocess.run([sys.executable, str(ROOT / "tests/gen_casa_shapes.py"), out],
                           check=True)
            r = subprocess.run([os.environ["CASA_PY"], str(ROOT / "scripts/check_casa_shapes.py"),
                                out], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
