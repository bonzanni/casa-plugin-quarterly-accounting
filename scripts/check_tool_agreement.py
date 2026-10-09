#!/usr/bin/env python3
"""Three-way tool-list agreement (spec §Tool surface, house disciplines): the
server's registry, plugin.json casa.provides_tools and casa.resultContract.tools
name exactly the same tools. Role allow-lists are not a third list here:
Casa grants plugin tools by assignment (spec §Setup step 1).
Result contract (S7 §3): six tools deliver a slot through Casa's broker — show_view (view),
post_results (results), propose_reading (reading), propose_account (accounts), post_package
and get_package (package, simple loop §1) — and every other entry stays `{"result": "safe"}`."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFIX = "mcp__plugin_quarterly-accounting_quarterly-accounting__"

# S7 §3: the delivered slots; every other tool is `safe`.
CAPABILITY_ENTRIES = {
    "show_view": {"result": "capability", "provides": ["view"],
                  "delivers": {"view": "operator_proposal"}},
    "post_results": {"result": "capability", "provides": ["results"],
                     "delivers": {"results": "operator_message"}},
    "propose_reading": {"result": "capability", "provides": ["reading"],
                        "delivers": {"reading": "operator_proposal"}},
    "propose_account": {"result": "capability", "provides": ["accounts"],
                        "delivers": {"accounts": "operator_proposal"}},
    "post_package": {"result": "capability", "provides": ["package"],
                     "delivers": {"package": "operator_file"}, "filename": True},
    # simple loop §1 (#1303): a [Get package] button's stored call
    "get_package": {"result": "capability", "provides": ["package"],
                    "delivers": {"package": "operator_file"}, "filename": True},
    # #56: a [See PDF] button's stored call (Casa #1362 keep_card)
    "get_document": {"result": "capability", "provides": ["document"],
                     "delivers": {"document": "operator_file"}, "filename": True},
    # #89: a rename posts its own outcome — one vendor's line and card, all vendors' summary
    "rename_vendor": {"result": "capability", "provides": ["view"],
                      "delivers": {"view": "operator_proposal"}},
    "rename_vendors_to_invoice_names": {"result": "capability", "provides": ["results"],
                                        "delivers": {"results": "operator_message"}},
}


def main() -> int:
    sys.path.insert(0, str(ROOT / "server"))
    import qa_server  # noqa: E402
    sys.modules.setdefault("qa_server", qa_server)
    import tools  # noqa: F401,E402
    server = set(qa_server.TOOLS)
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text("utf-8"))
    casa = manifest["casa"]
    provided_raw = casa["provides_tools"]
    problems = [f"provides_tools entry without the plugin prefix: {t}"
                for t in provided_raw if not t.startswith(PREFIX)]
    provided = {t[len(PREFIX):] for t in provided_raw if t.startswith(PREFIX)}
    if len(provided) != len(provided_raw):
        problems.append("provides_tools lists a tool twice or with a foreign prefix")
    contract = set(casa["resultContract"]["tools"])
    for name, other in (("provides_tools", provided), ("resultContract", contract)):
        for t in sorted(server - other):
            problems.append(f"server registers {t} but {name} does not list it")
        for t in sorted(other - server):
            problems.append(f"{name} lists {t} but the server does not register it")
    for t, c in casa["resultContract"]["tools"].items():
        want = CAPABILITY_ENTRIES.get(t, {"result": "safe"})
        if c != want:
            problems.append(f"resultContract for {t} must be {want}")
    for key in ("eraseTool",):
        if key in casa and casa[key] not in server:
            problems.append(f"casa.{key} names {casa[key]}, which the server does not register")
    for t in casa.get("protectedTools", []):
        if t.get("name") not in server:
            problems.append(f"protectedTools names {t.get('name')}, which is not registered")
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
