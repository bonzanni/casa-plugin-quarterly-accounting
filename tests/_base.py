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


class StoreCase(TempEnv):
    def setUp(self):
        super().setUp()
        import db
        self.conn = db.open_store()
        self.addCleanup(self.conn.close)

    def bind(self, account="acc-biz", label="Zakelijk", watermark="2026-07-01"):
        import binding
        import db
        binding.bind_account(self.conn, account, label)
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark=?", (watermark,))

    LEDGER = "a" * 32             # the bank-feed ledger instance id the fixtures bind to

    def pass_(self, trigger="test", generation=0, registered=None, accounts=None,
              instance=None):
        """End any live pass, begin a new one, record the probes a real pass
        records first (the ledger probe carries list_backups' instance id).
        Returns the new pass token."""
        import passes
        cur = passes.current_pass(self.conn)
        if cur is not None:
            passes.end_pass(self.conn, cur["generation"], "complete", {})
        token = passes.begin_pass(self.conn, trigger)["pass_token"]
        b = self.conn.execute("SELECT account_id FROM binding").fetchone()
        accts = accounts if accounts is not None else (
            [{"account_id": b[0], "category": "company", "label": "Zakelijk"}] if b else [])
        passes.record_probe(self.conn, token, "bank_tools", True)
        passes.record_probe(self.conn, token, "bank_sync", True)
        passes.record_probe(self.conn, token, "bank_accounts", True, data={"accounts": accts})
        passes.record_probe(self.conn, token, "ledger", True,
                            data={"generation": generation, "registered": registered or {},
                                  "instance": instance or self.LEDGER})
        return token
