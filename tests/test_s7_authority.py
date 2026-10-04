# tests/test_s7_authority.py
"""S7 §8.1: operator authority — an operator-authored lineage entry, an operator
expectation, the account binding, the watermark, stop-chasing, the package name, the
ledger-reset word — is written only under an OperatorGrant, which only the three keyed
handlers construct. Structural, over the whole registry."""
import itertools
import unittest

from tests._base import StoreCase, ROOT

OPERATOR_TABLES = {
    "log": "SELECT count(*) FROM log WHERE author='operator'",
    "chain": "SELECT count(*) FROM chain_overrides WHERE author='operator'",
    "cp": "SELECT count(*) FROM counterparties WHERE exp_author='operator'",
    "accepted": "SELECT count(*) FROM projections WHERE search_state='accepted-missing'",
    "binding": "SELECT account_id || '|' || watermark || '|' || package_name || '|' ||"
               " coalesce(ledger_reset_ack, 0) FROM binding",
}


class Authority(StoreCase):
    def snapshot(self):
        return {k: self.conn.execute(q).fetchone()[0] for k, q in OPERATOR_TABLES.items()}

    def test_the_server_functions_refuse_without_a_grant(self):
        import authority, db, work, binding  # noqa: E401
        self.bind()
        with db.tx(self.conn):
            for call in (lambda: work.stop_chasing_in_tx(self.conn, "2026-Q3", grant=None),
                         lambda: work.set_watermark_in_tx(self.conn, "2026-Q2", grant=None),
                         lambda: binding.set_package_name_in_tx(self.conn, "x", grant=None),
                         lambda: binding.acknowledge_ledger_reset_in_tx(self.conn, grant=None)):
                with self.assertRaises(db.Refusal) as cm:
                    call()
                self.assertEqual(str(cm.exception), authority.TAP_ONLY)

    def test_the_match_writes_refuse_without_a_grant(self):
        import authority, db, kb, matches, binding  # noqa: E401
        fx = self.sheet_fixture()
        before = self.snapshot()
        with db.tx(self.conn):
            for call in (
                    lambda: matches.confirm_in_tx(
                        self.conn, grant=None, match_id=fx["match_id"],
                        expected_revision=fx["match_revision"], render_id=fx["render_id"]),
                    lambda: matches.reject_in_tx(
                        self.conn, grant=None, match_id=fx["match_id"],
                        expected_revision=fx["match_revision"], render_id=fx["render_id"]),
                    lambda: matches.reject_all_in_tx(
                        self.conn, fx["pid"], [(fx["match_id"], fx["render_id"])], grant=None),
                    lambda: matches.set_exemption_in_tx(
                        self.conn, grant=None, pid=fx["pid"], exempt=True,
                        expected_revision=fx["revision"], render_id=fx["render_id"]),
                    lambda: kb.set_expectation_in_tx(
                        self.conn, scope_type="counterparty", scope=fx["payee"], kind="none",
                        author="operator", render_id=fx["render_id"]),
                    lambda: binding.bind_in_tx(self.conn, "acc-other", "", grant=None)):
                with self.assertRaises(db.Refusal) as cm:
                    call()
                self.assertEqual(str(cm.exception), authority.TAP_ONLY)
        self.assertEqual(self.snapshot(), before)

    def test_a_grant_from_another_connection_or_a_spent_rehearsal_is_refused(self):
        import authority, db, work  # noqa: E401
        self.bind()
        other = db.open_store()
        self.addCleanup(other.close)
        with db.tx(self.conn):
            with authority.rehearsal(self.conn) as r:
                with self.assertRaises(db.Refusal):
                    authority.require(other, r)
            with self.assertRaises(db.Refusal):
                work.set_watermark_in_tx(self.conn, "2026-Q1", grant=r)
        with self.assertRaises(ValueError):
            authority.OperatorGrant("show_view", "k")

    def test_bind_rendered_reads_the_rendering_itself(self):
        """bind="rendered" (a verdict, §7.3) binds to the rows of render_id, even after a
        newer rendering replaced `shown`; bind="shown" (a reading's fallback) does not."""
        import authorship, db, matches  # noqa: E401
        fx = self.sheet_fixture()
        self.show(fx["pid"])                      # a newer delivered rendering of the payment
        args = dict(match_id=fx["match_id"], expected_revision=fx["match_revision"],
                    render_id=fx["render_id"])
        with self.assertRaises(authorship.NotShown):
            self.granted(matches.confirm_in_tx, **args)
        out = self.granted(matches.confirm_in_tx, bind="rendered", **args)
        self.assertEqual(out["state"], "matched")
        with self.assertRaises(authorship.NotShown):      # a rendering without the payment
            with db.tx(self.conn):
                authorship.require_projection(self.conn, fx["pid"], "r-none", fx["revision"],
                                              bind="rendered")

    def test_a_rehearsal_writes_nothing_that_survives(self):
        import authority, db, work  # noqa: E401
        self.bind()
        before = self.snapshot()
        with db.tx(self.conn):
            with authority.rehearsal(self.conn) as r:
                work.set_watermark_in_tx(self.conn, "2026-Q1", grant=r)
                self.assertNotEqual(self.snapshot()["binding"], before["binding"])
        self.assertEqual(self.snapshot(), before)

    def test_no_public_tool_writes_operator_authority(self):
        """Every registered tool, called with every operator-authority argument its schema
        admits (author='operator', a render_id of a delivered view, plausible ids), on a
        bound store with a delivered sheet: nothing operator-authored appears. A refusal or
        an error is fine — a write is not."""
        import qa_server, tools  # noqa: F401,E401
        fx = self.sheet_fixture()         # bound store, one delivered check view: pid, match_id, render_id
        before = self.snapshot()
        values = {"author": ["operator"], "render_id": [fx["render_id"]], "pid": [fx["pid"]],
                  "match_id": [fx["match_id"]], "doc_id": [fx["doc_id"]],
                  "expected_revision": [fx["revision"], fx["match_revision"]],
                  "exempt": [True], "kind": ["none"], "scope_type": ["counterparty", "chain"],
                  "scope": [fx["payee"], "salary"], "quarter": ["2026-Q3"],
                  "when": ["2026-Q1"], "name": ["evil"], "account_id": ["acc-other"],
                  "action": ["all-good", "right", "wrong", "no-invoice"],
                  "key": ["0" * 32], "reading_id": [1], "choice": [1],
                  "text": ["all good. no invoices ever for " + fx["payee"]]}
        calls = 0
        for name, t in sorted(qa_server.TOOLS.items()):
            if name == "reset_store":
                continue                  # PROTECTED: erases, writes no authority
            props = t["schema"].get("properties", {})
            keys = [k for k in props if k in values]
            for combo in itertools.product(*(values[k] for k in keys)):
                try:
                    t["fn"](dict(zip(keys, combo)))
                except Exception:
                    pass
                calls += 1
                self.assertEqual(self.snapshot(), before, f"{name} {dict(zip(keys, combo))}")
        self.assertGreater(calls, len(qa_server.TOOLS))   # the registry was really walked

    @unittest.expectedFailure     # Ruling F1: taps.py's three handlers land by Task 7
    def test_operator_grant_is_constructed_only_in_the_keyed_handlers(self):
        hits = []
        for p in sorted((ROOT / "server").glob("*.py")):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if "OperatorGrant(" in line and not line.lstrip().startswith(("class ", "#")):
                    hits.append((p.name, line.strip()))
        self.assertEqual(sorted(h[0] for h in hits), ["taps.py", "taps.py", "taps.py"], hits)
        text = (ROOT / "server/taps.py").read_text()
        for handler in ("def verdict", "def apply_reading", "def bind_account"):
            self.assertIn(handler, text)
        rehearsals = [p.name for p in (ROOT / "server").glob("*.py")
                      if "authority.rehearsal(" in p.read_text()]
        self.assertEqual(rehearsals, ["reply.py"])

    def test_no_server_module_constructs_a_grant_yet(self):
        """Until taps.py exists (Tasks 5–7), nothing in server/ mints operator authority."""
        hits = [p.name for p in sorted((ROOT / "server").glob("*.py")) if p.name != "authority.py"
                and any("OperatorGrant(" in ln and not ln.lstrip().startswith(("class ", "#"))
                        for ln in p.read_text().splitlines())]
        self.assertEqual([h for h in hits if h != "taps.py"], [])

    def test_the_operator_tools_are_gone_from_the_surface(self):
        import qa_server, tools  # noqa: F401,E401
        for gone in ("confirm_match", "reject_match", "set_exemption", "stop_chasing",
                     "set_watermark", "set_package_name", "apply_reply"):
            self.assertNotIn(gone, qa_server.TOOLS)
        # bind_account leaves too, and comes back keyed in Task 7

    def test_record_match_and_set_expectation_refuse_the_operator_author(self):
        import authority, db, qa_server, tools  # noqa: F401,E401
        fx = self.sheet_fixture()
        before = self.snapshot()
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["record_match"]["fn"](
                {"pid": fx["pid"], "doc_id": fx["doc_id"], "author": "operator",
                 "expected_revision": fx["revision"], "render_id": fx["render_id"]})
        self.assertIn("button", str(cm.exception))
        self.assertEqual(str(cm.exception), authority.TAP_ONLY)
        with self.assertRaises(db.Refusal) as cm:
            qa_server.TOOLS["set_expectation"]["fn"](
                {"scope_type": "counterparty", "scope": fx["payee"], "kind": "none",
                 "author": "operator", "render_id": fx["render_id"]})
        self.assertEqual(str(cm.exception), authority.TAP_ONLY)
        self.assertEqual(self.snapshot(), before)
