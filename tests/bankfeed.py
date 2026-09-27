# tests/bankfeed.py
"""A REAL bank-feed ledger for tests: the vendored component tree, driven
through its own ingest/apply functions and its own tool bodies — never a
double (spec §Testing). One tree per process: bank-feed's module names are
global, so the below-floor tree only runs in a subprocess (run_below_floor).

bank-feed reads CLAUDE_PLUGIN_DATA only when tools_read.CONN is unset; this
harness always sets CONN, so our own store (opened by explicit path in tests)
and bank-feed's never share a directory by accident."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLOOR = "component-v0.19.0"
BELOW_FLOOR = "component-v0.13.2"
CAP_STABLE = {"ref_stable": True, "ref_scope": "account", "observed_n": 200}
CAP_UNKNOWN = {"ref_stable": False, "ref_scope": "unknown", "observed_n": 0}
_LOADED = False


def plugin_root(tag: str = FLOOR) -> pathlib.Path:
    return ROOT / "tests" / "upstream" / tag / "plugins" / "bank-feed"


def load() -> None:
    """Put the floor tree on sys.path AFTER server/ (casa_handoff resolves to
    ours; the harness test pins the two files identical) and import the tool
    modules the way bank_feed_server.main() does."""
    global _LOADED
    if _LOADED:
        return
    sys.path.append(str(plugin_root() / "server"))
    import bank_feed_server  # noqa: F401
    for mod in ("tools_read", "tools_auth", "tools_refresh", "tools_destructive",
                "tools_annotate", "tools_aggregate", "tools_rules", "tools_backup"):
        __import__(mod)
    _LOADED = True


class Ledger:
    ACCOUNT = "acc-biz"

    def __init__(self, root: pathlib.Path):
        load()
        import store
        import tools_read
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.conn = store.open_db(root / store.db_filename())
        self._saved_conn = tools_read.CONN
        tools_read.CONN = self.conn

    # --- rows -----------------------------------------------------------
    def account(self, category="company", label="Zakelijk", aid=None):
        aid = aid or self.ACCOUNT
        # Same columns as upstream tests/_toolbase.py Base.account(); the
        # category is what label_account writes (rules.py categories).
        self.conn.execute(
            "INSERT OR REPLACE INTO accounts(account_id, uid, session_id, iban_masked, name,"
            " currency, category, included, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, "uid-" + aid, "3f0c1a52-8d3e-4b7a-9c21-5e6f7a8b9c0d", "NL••1234",
             label, "EUR", category, 1, "2026-01-01", "2026-09-01"))
        self.conn.commit()

    def row(self, date, amount=1000, ref=None, counterparty="Zapier", status="BOOK",
            direction="DBIT", remittance="", value_date=None, account=None) -> dict:
        """A fetched row, shaped as ingest.normalise produces it (upstream
        tests/test_apply.py row()). A pending row may carry booking_date None."""
        return {"account_id": account or self.ACCOUNT, "booking_date": date,
                "value_date": value_date or date, "amount_minor": amount,
                "currency": "EUR", "direction": direction,
                "counterparty": counterparty, "remittance": remittance,
                "provider_ref": ref,
                "provider_ref_kind": "entry_reference" if ref else None,
                "status": status, "raw_json": "{}"}

    def fetch(self, fetched, interval=("2026-01-01", "2026-12-31"), cap=CAP_STABLE,
              account=None) -> dict:
        import apply
        import ingest
        stored = self.rows(account=account or self.ACCOUNT)
        plan = ingest.reconcile(stored, fetched, interval, cap)
        stats = apply.apply_plan(self.conn, account or self.ACCOUNT, plan)
        # What a successful sync leaves behind: transaction freshness, without
        # which the read tools refuse the account as never fetched (upstream
        # tests/_toolbase.py Base.synced). If list_transactions still refuses,
        # read tools_read's freshness predicate at the vendored tag and mirror it.
        now = "2026-09-20T08:00:00Z"
        self.conn.execute("INSERT OR REPLACE INTO sync_state(account_id, resource,"
                          " last_attempt_at, last_success_at, completeness)"
                          " VALUES (?, 'transactions', ?, ?, 'complete')",
                          (account or self.ACCOUNT, now, now))
        self.conn.commit()
        return stats

    def rows(self, state=None, account=None) -> list:
        sql, args = "SELECT * FROM transactions", []
        clauses = []
        if account:
            clauses.append("account_id=?")
            args.append(account)
        if state:
            clauses.append("state=?")
            args.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY row_id", args)]

    def purge_before(self, cutoff: str) -> dict:
        import apply
        stats = apply.purge_before(self.conn, cutoff)
        self.conn.commit()
        return stats

    # --- tools ----------------------------------------------------------
    def call(self, tool: str, **args) -> str:
        import bank_feed_server
        out = bank_feed_server.TOOLS[tool]["fn"](args)
        return out if isinstance(out, str) else str(out.get("text"))

    def export(self) -> str:
        """export_history's path; its `Ledger instance:` line (read by label, as
        bank-feed says: the dispatcher may prepend sentences) is kept in
        self.last_export_instance."""
        out = self.call("export_history", format="csv")
        m = re.search(r"^Path: (.+)$", out, re.M)
        li = re.search(r"^Ledger instance: ([0-9a-f]{32})$", out, re.M)
        if not m or not li:
            raise AssertionError(out)
        self.last_export_instance = li.group(1)
        return m.group(1).strip()

    def instance(self) -> str:
        m = re.search(r"^Ledger instance: ([0-9a-f]{32})$", self.listing(), re.M)
        return m.group(1)

    def tags(self, row_id: int) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT tag FROM transaction_tags WHERE row_id=? ORDER BY tag", (row_id,))]

    def notes(self, row_id: int) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT note FROM transaction_notes WHERE row_id=? ORDER BY note_id", (row_id,))]

    def listing(self) -> str:
        return self.call("list_backups")

    def generation(self) -> int:
        m = re.search(r"^Restore generation: (\d+)$", self.listing(), re.M)
        return int(m.group(1))

    def registered(self) -> dict:
        text = self.listing()
        out = {}
        if "Registered workflows:" in text and "Registered workflows: none" not in text:
            block = text.split("Registered workflows:", 1)[1].split("Restores:", 1)[0]
            for line in block.splitlines():          # keep the indentation the regex needs
                m = re.match(r"\s+(\S+) -> (\S+)", line)
                if m:
                    out[m.group(1)] = m.group(2)
        return out


_BELOW_FLOOR_PRELUDE = """
import pathlib, sys
PLUGIN_ROOT = pathlib.Path({root!r})
sys.path.insert(0, str(PLUGIN_ROOT / 'server'))
"""


def run_below_floor(code: str, env: dict | None = None) -> str:
    """Run `code` in a fresh interpreter whose sys.path has the v0.13.2 tree
    (bank-feed 0.12.2) and nothing of ours. `PLUGIN_ROOT` is predefined."""
    prelude = _BELOW_FLOOR_PRELUDE.format(root=str(plugin_root(BELOW_FLOOR)))
    r = subprocess.run([sys.executable, "-c", prelude + code], capture_output=True,
                       text=True, timeout=120, env={**os.environ, **(env or {})})
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout
