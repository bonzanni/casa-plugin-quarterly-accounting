import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))

import check_removed  # noqa: E402


class ClosingGate(unittest.TestCase):
    def test_no_removed_name_has_a_caller(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_removed.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_gate_catches_a_leftover_and_spares_kept_names(self):
        import re
        hit = lambda text: any(re.search(rx, text) for rx, _ in check_removed.REMOVED)
        self.assertTrue(hit("job.credit_sweep(conn)"))
        self.assertTrue(hit("hand(conn)"))
        self.assertFalse(hit("tests._base.hand_calls(x)"))
        self.assertFalse(hit("job.TURNS_PER_BATCH"))
        self.assertFalse(hit("asks.requeue_taken(conn, pid)"))
        self.assertFalse(hit("lineage.append(conn, resolves=ids)"))

    def test_no_tool_takes_resolves(self):
        import qa_server
        import tools  # noqa: F401 -- registers the tools
        for name, t in qa_server.TOOLS.items():
            self.assertNotIn("resolves", t["schema"]["properties"], name)


if __name__ == "__main__":
    unittest.main()
