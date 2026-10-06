#!/usr/bin/env python3
"""The plugin's side of the Casa gate's quote check (binding r7): for each {"store",
"quote"} read from stdin (JSON list), views.bound_rendering on that store copy; prints a
JSON list of {"render_id"} or {"refused": <line>}. Stdlib only, run in a process of its own
with only the plugin's server/ on the path (Casa's modules would shadow the plugin's)."""
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
import views  # noqa: E402


def main() -> int:
    out = []
    for item in json.load(sys.stdin):
        conn = sqlite3.connect(f"file:{item['store']}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            r = views.bound_rendering(conn, item["quote"])
            out.append({"render_id": r["render_id"] if r is not None else None})
        except views.QuoteRefusal as exc:
            out.append({"refused": exc.line})
        finally:
            conn.close()
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
