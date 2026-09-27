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

    def publish(self, name, data, producer="gmail"):
        import casa_handoff
        return casa_handoff.publish(producer, name, data=data)["path"]


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

    _doc_n = 0

    def row(self, row_id, **over):
        import db
        r = {"row_id": row_id, "account_id": "acc-biz", "first_seen": "2026-07-01T00:00:00Z",
             "booking_date": "2026-07-03", "value_date": "2026-07-03", "amount_minor": 10000,
             "currency": "EUR", "direction": "DBIT", "status": "BOOK", "counterparty": "Adobe",
             "remittance": "", "state": "active", "superseded_by": None, "needs_review": 0,
             "review_reason": None, "snapshot_id": 0}
        r.update(over)
        with db.tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO bank_rows(%s) VALUES (%s)"
                              % (",".join(r), ",".join("?" * len(r))), tuple(r.values()))
        return r

    def lineage_for(self, row_id):
        import db
        with db.tx(self.conn):
            cur = self.conn.execute("INSERT INTO projections(dest_row_id, admitted_at)"
                                    " VALUES (?, ?)", (row_id, db.now()))
            pid = cur.lastrowid
            self.conn.execute("INSERT INTO aliases(row_id, pid, first_seen) VALUES (?,?,?)",
                              (row_id, pid, "2026-07-01T00:00:00Z"))
        return pid

    def doc(self, kind="invoice", **over):
        import db
        StoreCase._doc_n += 1
        d = {"sha256": "%064x" % (StoreCase._doc_n + id(self)), "ext": "pdf", "size": 10,
             "kind": kind, "counterparty": "Adobe", "issuer": "Adobe",
             "document_date": "2026-07-02", "document_number": "N%d" % StoreCase._doc_n,
             "amount_minor": 10000, "currency": "EUR", "recipient": "Voorbeeld BV",
             "source": "gmail", "extraction_author": "resident",
             "ingested_at": db.now(), "ingest_quarter": "2026-Q3"}
        d.update(over)
        with db.tx(self.conn):
            cur = self.conn.execute("INSERT INTO documents(%s) VALUES (%s)"
                                    % (",".join(d), ",".join("?" * len(d))), tuple(d.values()))
        return cur.lastrowid

    def classify(self, pid, tags):
        import db
        import json
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_tags_json=?, class_observed_at=?"
                              " WHERE pid=?", (json.dumps(sorted(tags)), db.now(), pid))

    def settle(self, pid):
        import db
        import lineage
        with db.tx(self.conn):
            return lineage.settle(self.conn, pid)
