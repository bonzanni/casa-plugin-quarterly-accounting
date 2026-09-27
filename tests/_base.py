"""Shared test scaffolding. Puts server/ first on sys.path; every test gets a
fresh data dir, handoff folder and outbox, and os.environ restored after."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))


class TempEnv(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = pathlib.Path(self._tmpdir.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.handoff = self.tmp / "handoff"
        self.handoff.mkdir(mode=0o770)
        self.outbox = self.tmp / "outbox"
        self.outbox.mkdir(mode=0o770)
        saved = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(saved)
        self.addCleanup(restore)
        os.environ["CLAUDE_PLUGIN_DATA"] = str(self.data)
        os.environ["CASA_HANDOFF_DIR"] = str(self.handoff)
        os.environ["CASA_PLUGIN_OUTBOX_DIR"] = str(self.outbox)
