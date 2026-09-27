#!/usr/bin/env python3
"""quarterly-accounting MCP server. Stdlib-only stdio JSON-RPC.

This file only dispatches. Behaviour lives in focused modules that are testable
without an MCP session; tools.py registers every tool. Same shape as
bank-feed's bank_feed_server.py.
"""
from __future__ import annotations

import json
import sys

import db

TOOLS: dict = {}
PROTOCOL_VERSION = "2024-11-05"


def register(name: str, description: str, schema: dict):
    def deco(fn):
        if name in TOOLS:
            raise RuntimeError(f"tool {name!r} registered twice")
        TOOLS[name] = {"description": description, "schema": schema, "fn": fn}
        return fn
    return deco


def _result(id_, payload):
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _render(out) -> str:
    if isinstance(out, str):
        return out
    return json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1)


def handle(req: dict) -> dict | None:
    method, id_ = req.get("method"), req.get("id")
    if method == "initialize":
        import version
        return _result(id_, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "quarterly-accounting",
                                            "version": version.PLUGIN_VERSION}})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["schema"]}
            for n, t in sorted(TOOLS.items())]})
    if method == "tools/call":
        params = req.get("params") or {}
        tool = TOOLS.get(params.get("name"))
        if tool is None:
            return _error(id_, -32601, f"unknown tool {params.get('name')!r}")
        try:
            text, is_error = _render(tool["fn"](params.get("arguments") or {})), False
        except db.Refusal as exc:
            text, is_error = f"refused: {exc}", False
        except Exception as exc:                       # surfaced, never swallowed
            text, is_error = f"error: {type(exc).__name__}: {exc}", True
        payload = {"content": [{"type": "text", "text": text}]}
        if is_error:
            payload["isError"] = True
        return _result(id_, payload)
    return _error(id_, -32601, f"unknown method {method!r}")


def main() -> None:
    # Launched as a script this module is "__main__"; tools.py imports
    # "qa_server" to reach TOOLS. Alias first so both names are one module
    # (bank-feed documents the same trap in bank_feed_server.main).
    sys.modules.setdefault("qa_server", sys.modules[__name__])
    import tools  # noqa: F401  -- registers every tool; any failure is fatal
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
