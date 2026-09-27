#!/usr/bin/env python3
"""No account identifier in the tree (spec §Privacy). Exits 1 on any
IBAN-shaped token in a tracked file outside tests/upstream/ (vendored
upstream test fixtures carry bank-feed's own synthetic IBANs)."""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z]{4}\d{10}\b")


def main(root: str) -> int:
    files = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True,
                           text=True, check=True).stdout.split()
    hits = []
    for f in files:
        if f.startswith("tests/upstream/"):
            continue
        p = pathlib.Path(root) / f
        try:
            text = p.read_text("utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for m in IBAN.finditer(text):
            hits.append(f"{f}: {m.group(0)[:4]}…")
    for h in hits:
        print(h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
