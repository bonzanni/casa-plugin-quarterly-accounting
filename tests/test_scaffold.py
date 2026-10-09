import json
import pathlib
import subprocess
import sys
import unittest

from tests._base import ROOT, TempEnv

import qa_server  # noqa: E402  (server/ is on sys.path via tests._base)
import version    # noqa: E402


class TestScaffold(TempEnv):
    def test_manifest_has_no_setup_tool_env_or_triggers(self):
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["name"], "quarterly-accounting")
        casa = m["casa"]
        # S2: `jobs` is declared now (the finance job, spec §3); test_s2_surface pins it
        for forbidden in ("setupTool", "setupProvides", "callbacks", "triggers",
                          "systemRequirements", "eraseDataOnlyTool", "dropOffs"):
            self.assertNotIn(forbidden, casa, forbidden)
        mcp = json.loads((ROOT / ".mcp.json").read_text())
        server = mcp["mcpServers"]["quarterly-accounting"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["${CLAUDE_PLUGIN_ROOT}/server/qa_server.py"])
        self.assertNotIn("env", server)   # no required environment variables

    def test_workflow_string_is_derived_from_the_manifest_version(self):
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(version.PLUGIN_VERSION, m["version"])
        self.assertEqual(version.WORKFLOW, "acct@" + m["version"])

    def test_initialize_and_tools_list(self):
        init = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "quarterly-accounting")
        listed = qa_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIsInstance(listed["result"]["tools"], list)

    def test_unknown_tool_is_an_error(self):
        out = qa_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(out["error"]["code"], -32601)

    def test_refusal_and_exception_rendering(self):
        import db

        @qa_server.register("_t_refuse", "test", {"type": "object"})
        def _refuse(args):
            raise db.Refusal("the ledger was restored")

        @qa_server.register("_t_boom", "test", {"type": "object"})
        def _boom(args):
            raise KeyError("x")
        self.addCleanup(qa_server.TOOLS.pop, "_t_refuse")
        self.addCleanup(qa_server.TOOLS.pop, "_t_boom")
        r = qa_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "_t_refuse", "arguments": {}}})
        self.assertEqual(r["result"]["content"][0]["text"], "refused: the ledger was restored")
        self.assertFalse(r["result"].get("isError", False))
        b = qa_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "_t_boom", "arguments": {}}})
        self.assertTrue(b["result"]["isError"])
        self.assertTrue(b["result"]["content"][0]["text"].lstrip("*").startswith("error: KeyError"))

    def test_casa_handoff_is_vendored_verbatim(self):
        up = ROOT / "tests/upstream/component-v0.21.0/plugins/bank-feed/server/casa_handoff.py"
        if not up.exists():
            self.skipTest("upstream tree arrives in Task 2")
        self.assertEqual((ROOT / "server/casa_handoff.py").read_bytes(), up.read_bytes())

    def test_tool_agreement_script_passes(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/check_tool_agreement.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_server_runs_as_a_process(self):
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        r = subprocess.run([sys.executable, str(ROOT / "server/qa_server.py")],
                           input=req, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout.splitlines()[0])["id"], 1, r.stderr)


if __name__ == "__main__":
    unittest.main()
