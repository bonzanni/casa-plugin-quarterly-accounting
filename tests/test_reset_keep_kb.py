"""Simple loop §6.8: a second run on a fresh store that keeps the KB — the store is reset,
the KB tables (learned hints included) and the binding and watermark come back."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/reset_keep_kb.py"

# Every table of schema 12 that the script keeps (rows come back) — all others must be empty
# or reset after it runs; a new table fails test_every_table_is_classified until decided.
KEPT_TABLES = {"counterparties", "chain_overrides", "binding", "meta", "counters"}
WIPED_TABLES = {"passes", "probes", "documents", "snapshots", "bank_rows", "projections",
                "aliases", "matches", "log", "match_state", "residue", "renders",
                "render_items", "shown", "packages", "deliveries", "delivered_rows", "alerts",
                "operator_refs", "claims", "work_requests", "runs", "readings", "render_keys",
                "account_choices", "post_offers", "run_work", "run_mirror", "run_items", "render_states",
                "quarter_notices", "pass_marker"}


def load():
    spec = importlib.util.spec_from_file_location("reset_keep_kb", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ResetKeepKB(StoreCase):
    def test_the_kb_and_the_binding_survive_and_the_rest_is_gone(self):
        import kb
        self.bind(watermark="2026-07-01")
        kb.upsert_counterparty(self.conn, "Adobe", patterns=["ADOBE *SYSTEMS"],
                               hint_sender="billing@adobe.com", hint_subject="Your invoice",
                               source="email")
        self.row(1)
        self.lineage_for(1)
        self.doc()
        out = load().reset_keep_kb(self.conn)
        self.assertEqual(out["erasure"], "complete")
        b = self.conn.execute("SELECT account_id, watermark FROM binding").fetchone()
        self.assertEqual(tuple(b), ("acc-biz", "2026-07-01"))
        cp = kb.get_counterparty(self.conn, "ADOBE *SYSTEMS")
        self.assertEqual((cp["hint_sender"], cp["hint_subject"]),
                         ("billing@adobe.com", "Your invoice"))
        for t in ("projections", "documents", "bank_rows", "log"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0, t)

    def test_every_table_is_classified_and_the_wiped_ones_are_empty(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(names, KEPT_TABLES | WIPED_TABLES)
        self.bind()
        self.row(1)
        self.lineage_for(1)
        self.doc()
        load().reset_keep_kb(self.conn)
        for t in sorted(WIPED_TABLES):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0, t)

    def test_chain_overrides_and_the_binding_columns_come_back_verbatim(self):
        self.bind(watermark="2026-07-01")
        self.conn.execute(
            "UPDATE binding SET package_name='Q-pack', package_name_announced=1,"
            " watermark_announced=1, row_high_water=7, ledger_generation=3,"
            " ledger_instance='L', ledger_reset_ack=1")
        self.conn.execute(
            "INSERT INTO chain_overrides (scope, kind, tier, rows_json, key_json, author, set_at)"
            " VALUES ('Adobe', 'invoice', 'a', '[1,2]', '{\"k\":1}', 'operator', '2026-08-01')")
        before_b = dict(self.conn.execute("SELECT * FROM binding").fetchone())
        before_c = [tuple(r) for r in self.conn.execute("SELECT * FROM chain_overrides")]
        out = load().reset_keep_kb(self.conn)
        self.assertEqual(out["kept"], {"counterparties": 0, "chain_overrides": 1})
        self.assertEqual(out["binding"], {"account_id": "acc-biz", "watermark": "2026-07-01"})
        after_b = dict(self.conn.execute("SELECT * FROM binding").fetchone())
        for k in ("account_id", "account_label", "watermark", "bound_at", "package_name",
                  "package_name_announced", "watermark_announced"):
            self.assertEqual(after_b[k], before_b[k], k)
        self.assertEqual((after_b["row_high_water"], after_b["ledger_generation"],
                          after_b["ledger_instance"], after_b["ledger_reset_ack"]),
                         (0, None, None, 0))
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM chain_overrides")],
                         before_c)

    def test_a_store_that_is_not_schema_12_is_refused_untouched(self):
        self.bind()
        self.conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
        with self.assertRaises(db.Refusal):
            load().reset_keep_kb(self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM binding").fetchone()[0], 1)

    def test_main_refuses_a_non_12_store_without_migrating_and_backs_up_a_12_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, CLAUDE_PLUGIN_DATA=d)
            conn = db.open_store(pathlib.Path(d) / db.DB_NAME)
            conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
            conn.close()
            r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 2, r.stderr)
            c = db.sqlite3.connect(str(pathlib.Path(d) / db.DB_NAME))
            self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema_version'"
                                       ).fetchone()[0], "11")
            c.close()
            c = db.sqlite3.connect(str(pathlib.Path(d) / db.DB_NAME))
            c.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
            c.commit()
            c.close()
            # no binding: refused, but the backup beside it was taken first
            r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertTrue(list(pathlib.Path(d).glob(db.DB_NAME + ".pre-reset-*")))

    def test_main_success_path_backs_up_the_pre_reset_rows_privately(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dbp = pathlib.Path(d) / db.DB_NAME
            env = dict(os.environ, CLAUDE_PLUGIN_DATA=d)
            conn = db.open_store(dbp)
            conn.execute("INSERT INTO binding (id, account_id, account_label, watermark,"
                         " bound_at, package_name) VALUES (1,'acc','L','2026-07-01','t','p')")
            conn.execute("INSERT INTO documents (sha256, ext, size, kind, source,"
                         " extraction_author, ingested_at, ingest_quarter)"
                         " VALUES ('ab','pdf',1,'invoice','email','a','t','2026Q3')")
            conn.close()
            outs = []
            for _ in range(2):
                r = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                outs.append(json.loads(r.stdout))
            self.assertNotEqual(outs[0]["backup"], outs[1]["backup"])
            first = pathlib.Path(outs[0]["backup"])
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            c = db.sqlite3.connect(str(first))
            self.assertEqual(c.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
            c.close()
            c = db.sqlite3.connect(str(dbp))
            self.assertEqual(c.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
            c.close()
