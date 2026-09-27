"""The plugin's version and the workflow string every bank-feed write carries
(spec §Setup, "Test install"). One source: .claude-plugin/plugin.json."""
from __future__ import annotations

import json
import pathlib

_MANIFEST = pathlib.Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
PLUGIN_VERSION: str = json.loads(_MANIFEST.read_text("utf-8"))["version"]
WORKFLOW: str = "acct@" + PLUGIN_VERSION
