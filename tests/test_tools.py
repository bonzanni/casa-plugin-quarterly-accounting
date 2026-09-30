# tests/test_tools.py
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

from tests._base import ROOT, StoreCase, TempEnv
import db  # noqa: E402
import qa_server  # noqa: E402

sys.modules.setdefault("qa_server", qa_server)

EXPECTED = {
    "ingest_document", "update_document_metadata", "mark_irrelevant", "list_unmatched_documents",
    "get_counterparty", "upsert_counterparty", "set_expectation",
    "record_match", "propose_match", "confirm_match", "reject_match", "relabel_match",
    "set_exemption",
    "import_ledger_export", "list_projections", "record_observation",
    "begin_pass", "end_pass", "record_probe", "check_setup", "bind_account", "set_watermark",
    "set_package_name", "reset_store",
    "record_search", "stop_chasing",
    "list_quarter_state", "build_review", "mark_rendering_delivered", "apply_reply",
    "build_quarterly_package", "stage_for_delivery", "record_delivery",
    "record_step", "continue_pass", "read_document",
}


def _server(env):
    return subprocess.Popen([sys.executable, str(ROOT / "server/qa_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)


def _call(proc, name, rid, **args):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())["result"]["content"][0]["text"]


def _fresh_conn(case):
    """tools.conn() is one connection per server process; in-process tests
    drop it so each test reaches its own temporary store."""
    import tools

    def drop():
        if tools._CONN is not None and tools._CONN is not getattr(case, "conn", None):
            tools._CONN.close()
        tools._CONN = None
    drop()
    case.addCleanup(drop)
    return tools


def _tool(name, **args):
    out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    return out["result"]


def _text(name, **args):
    return _tool(name, **args)["content"][0]["text"]


def _json(name, **args):
    text = _text(name, **args)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AssertionError(f"{name} answered {text!r}") from None


class TestSurface(TempEnv):
    def setUp(self):
        super().setUp()
        _fresh_conn(self)

    def test_exactly_the_planned_tools(self):
        import tools  # noqa: F401
        self.assertEqual(set(qa_server.TOOLS), EXPECTED)
        self.assertEqual(len(EXPECTED), 36)

    def test_manifest_agrees(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/check_tool_agreement.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(len(m["casa"]["provides_tools"]), 36)
        # Casa's uninstall eraser (v0.329.0): argument-free, declared safe, protected
        self.assertEqual(m["casa"]["eraseTool"], "reset_store")
        self.assertEqual([t["name"] for t in m["casa"]["protectedTools"]], ["reset_store"])
        import tools  # noqa: F401
        self.assertEqual(qa_server.TOOLS["reset_store"]["schema"].get("required", []), [])

    def test_the_eraser_answers_erasure_and_report_as_one_json_object(self):
        import tools  # noqa: F401
        out = qa_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                "params": {"name": "reset_store", "arguments": {}}})
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(set(body), {"erasure", "report"})

    def test_schemas_are_objects_and_required_args_are_declared(self):
        import tools  # noqa: F401
        for name, t in qa_server.TOOLS.items():
            self.assertEqual(t["schema"]["type"], "object", name)
            for req in t["schema"].get("required", []):
                self.assertIn(req, t["schema"]["properties"], (name, req))
            self.assertGreater(len(t["description"]), 40, name)

    def test_missing_argument_is_a_refusal_not_a_crash(self):
        import tools  # noqa: F401
        out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "apply_reply", "arguments": {}}})
        self.assertTrue(out["result"]["content"][0]["text"].startswith("refused:"))

    def test_a_refusal_is_not_an_error(self):
        import tools  # noqa: F401
        res = _tool("record_match", pid="1", doc_id=1, author="auto", expected_revision=0)
        self.assertTrue(res["content"][0]["text"].startswith("refused: pid must be an integer"))
        self.assertNotIn("isError", res)


class TestDeliverableBoundary(TempEnv):
    """fix wave D round 2: an operator-facing text over Telegram's limit never
    leaves a tool as a result — it is an error (isError), loud."""
    def setUp(self):
        super().setUp()
        _fresh_conn(self)

    def test_every_operator_text_key_is_checked(self):
        from unittest import mock
        import delivery
        import passes
        import reply
        import views
        big = "x" * 4097
        cases = [("build_review", views, "build_review", {"render_id": "r1", "text": big},
                  {}),
                 ("end_pass", passes, "end_pass", {"speak": {"render_id": "r1", "text": big}},
                  {"pass_token": 1, "outcome": "complete"}),
                 ("apply_reply", reply, "apply_reply",
                  {"receipt": "ok", "receipt_pages": ["ok", big]}, {"text": "all good"}),
                 # fix wave F: the offer an uncertain package send returns
                 ("record_delivery", delivery, "record_delivery",
                  {"speak": {"render_id": "r1", "text": big}},
                  {"delivery_id": 1, "outcome": "uncertain"}),
                 ("apply_reply", reply, "apply_reply",
                  {"receipt": big, "receipt_pages": [big]}, {"text": "all good"})]
        for tool, mod, fn, out, args in cases:
            with mock.patch.object(mod, fn, lambda *a, _o=out, **k: _o):
                res = _tool(tool, **args)
            self.assertTrue(res.get("isError"), (tool, res))
            self.assertIn("4097 UTF-16 units", res["content"][0]["text"])
        fits = {"render_id": "r1", "text": "\U0001d518" * 2048}          # 4096 units exactly
        with mock.patch.object(views, "build_review", lambda *a, **k: fits):
            res = _tool("build_review")
        self.assertNotIn("isError", res)
        with mock.patch.object(views, "build_review",
                               lambda *a, **k: {"render_id": "r1", "text": "\U0001d518" * 2049}):
            self.assertTrue(_tool("build_review").get("isError"))


class ToolCase(StoreCase):
    """The tools reach this test's store connection (tools.conn())."""
    def setUp(self):
        super().setUp()
        tools = _fresh_conn(self)
        tools._CONN = self.conn


class TestPassTokens(ToolCase):
    def test_end_pass_requires_the_token(self):
        # D10 gap: end_pass(None) would end whichever pass is live, or crash with none
        self.bind()
        token = self.pass_()
        self.assertEqual(_text("end_pass", outcome="complete"),
                         "refused: missing argument(s): pass_token")
        import passes
        self.assertIsNotNone(passes.current_pass(self.conn))       # the live pass still runs
        self.assertEqual(_json("end_pass", pass_token=token, outcome="complete")["outcome"],
                         "complete")
        self.assertIsNone(passes.current_pass(self.conn))

    def test_end_pass_with_no_pass_running_is_a_refusal(self):
        res = _tool("end_pass", pass_token=5, outcome="complete")
        self.assertTrue(res["content"][0]["text"].startswith("refused:"), res)
        self.assertNotIn("isError", res)

    def test_a_stale_token_is_refused_at_the_delivery_log(self):
        import package
        self.bind()
        token = self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        pkg = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        self.pass_()                                          # a newer pass reclaims the marker
        self.assertTrue(_text("stage_for_delivery", channel="telegram",
                              package_id=pkg["package_id"], pass_token=token)
                        .startswith("refused: this pass is no longer the current one"))
        staged = _json("stage_for_delivery", channel="telegram", package_id=pkg["package_id"])
        self.assertTrue(_text("record_delivery", delivery_id=staged["delivery_id"],
                              outcome="delivered", pass_token=token)
                        .startswith("refused: this pass is no longer the current one"))


class TestResend(ToolCase):
    """'send it again': apply_reply emits the instruction `resend`; Ellen calls
    stage_for_delivery(resend=true), which stages what the last delivered
    rendering offered (delivery.resend_target)."""
    def setUp(self):
        super().setUp()
        import package
        self.bind()
        self.token = self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        self.a = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        self.b = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        self.assertNotEqual(self.a["filename"], self.b["filename"])

    def send(self, pkg_id, outcome):
        d = _json("stage_for_delivery", channel="telegram", package_id=pkg_id)
        _json("record_delivery", delivery_id=d["delivery_id"], outcome=outcome)
        for f in os.listdir(self.outbox):          # Casa consumes the outbox copy on send
            os.unlink(self.outbox / f)
        return d

    def show(self):
        r = _json("build_review", view="status", quarter="2026-Q3")
        _json("mark_rendering_delivered", render_id=r["render_id"])
        return r["text"]

    def test_send_it_again_stages_the_offered_package(self):
        self.send(self.a["package_id"], "uncertain")
        self.send(self.b["package_id"], "delivered")
        self.assertIn(self.a["filename"], self.show())
        self.assertIn("resend", _json("apply_reply", text="send it again")["instructions"])
        staged = _json("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(staged["filename"], self.a["filename"])
        self.assertEqual(pathlib.Path(staged["path"]).read_bytes(),
                         pathlib.Path(self.a["path"]).read_bytes())

    def test_nothing_offered_is_a_refusal_in_words(self):
        self.send(self.a["package_id"], "uncertain")
        _json("build_review", view="status", quarter="2026-Q3")          # never delivered
        self.assertEqual(_text("stage_for_delivery", channel="telegram", resend=True),
                         "refused: nothing is waiting to be sent again")

    def test_resend_names_no_other_target(self):
        self.send(self.a["package_id"], "uncertain")
        self.show()
        for extra in ({"package_id": self.b["package_id"]}, {"doc_id": 1}):
            self.assertTrue(_text("stage_for_delivery", channel="telegram", resend=True,
                                  **extra).startswith("refused:"), extra)


class TestPaging(ToolCase):
    def test_next_is_the_exact_call_for_the_rest(self):
        import work
        self.bind()
        token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p1', 'x', 0, 0, '2026-09-20')")
        for i in range(1, 201):
            self.row(i, counterparty=f"Vend{i:03d}", amount_minor=1000 + i,
                     booking_date="2026-09-14", value_date="2026-09-14")
            pid = self.lineage_for(i)
            self.classify(pid, {"software"})
            with db.tx(self.conn):
                self.conn.execute("UPDATE projections SET class_observed_at=? WHERE pid=?",
                                  ("2026-09-20T10:00:00Z", pid))
            self.settle(pid)
            work.record_search(self.conn, pid=pid, token=token, queries=["x"])
        r = _json("build_review", view="missing", quarter="2026-Q3")
        self.assertIn('say "all of them"', r["text"])
        seen, pages = set(re.findall(r"Vend\d{3}", r["text"])), 0
        nxt = r["next"]
        while nxt is not None:
            self.assertLess(pages, 50, "paging never ends: `next` is not honoured")
            r = _json("build_review", **json.loads(json.dumps(nxt)))   # as Ellen passes it
            pages += 1
            seen |= set(re.findall(r"Vend\d{3}", r["text"]))
            nxt = r["next"]
        self.assertGreater(pages, 1)
        self.assertEqual(len(seen), 200)


class TestMachineWritesNeedAPass(ToolCase):
    """D10: a specialist's write belongs to a pass (spec: a stale pass is refused
    at every write into this plugin's own store); operator-side writes carry none."""
    def test_a_specialist_filing_without_a_token_is_refused(self):
        path = self.publish("s.pdf", b"%PDF-1.4\ns\n%%EOF\n")
        self.assertEqual(_text("ingest_document", source_path=path, kind="invoice",
                               source="gmail", extraction_author="specialist"),
                         "refused: a specialist's filing belongs to a pass: pass the pass_token")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        self.bind()
        token = self.pass_()
        self.assertIn("doc_id", _json("ingest_document", source_path=path, kind="invoice",
                                      source="gmail", extraction_author="specialist",
                                      pass_token=token))

    def test_a_specialist_expectation_without_a_token_is_refused(self):
        self.assertEqual(_text("set_expectation", scope_type="counterparty", scope="Adobe",
                               kind="none", author="specialist"),
                         "refused: a specialist's expectation belongs to a pass: pass the "
                         "pass_token")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0],
                         0)

    def test_every_optional_token_write_says_to_pass_it(self):
        import tools  # noqa: F401
        optional = [n for n, t in qa_server.TOOLS.items()
                    if "pass_token" in t["schema"]["properties"]
                    and "pass_token" not in t["schema"]["required"]]
        self.assertEqual(len(optional), 11, optional)       # + list_quarter_state (the clock)
        for n in optional:
            self.assertIn("During a pass, pass the pass_token.",
                          qa_server.TOOLS[n]["description"], n)


class TestRowSnapshotFromTheListing(ToolCase):
    """Task 22 review, item 2: get_transaction's text cannot rebuild the facts
    record_match compares (signed decimals, fenced text, labels), so the item the
    specialist judges from carries them, and passing that value back is accepted."""
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        self.row(1, remittance="INV-1 \u00b7 \"quoted\"", needs_review=1,
                 review_reason="low_confidence")
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def test_the_listed_row_digest_is_accepted(self):
        item = next(d for d in _json("list_quarter_state", triage=True)["triage"]
                    if d["pid"] == self.pid)
        self.assertEqual(len(item["row_digest"]), 64)             # the full sha256
        self.assertNotIn("row_snapshot", item)
        out = _json("record_match", pid=self.pid, doc_id=self.doc(), author="auto",
                    expected_revision=item["revision"], row_digest=item["row_digest"],
                    pass_token=self.token)
        self.assertEqual(out["state"], "matched")

    def test_a_digest_of_other_facts_is_refused(self):
        item = next(d for d in _json("list_quarter_state", triage=True)["triage"]
                    if d["pid"] == self.pid)
        for bad in ("0" * 64, item["row_digest"][:16]):
            out = _text("record_match", pid=self.pid, doc_id=self.doc(), author="auto",
                        expected_revision=item["revision"], row_digest=bad,
                        pass_token=self.token)
            self.assertTrue(out.startswith("refused: the row changed"), out)
        out = _text("record_match", pid=self.pid, doc_id=self.doc(), author="auto",
                    expected_revision=item["revision"], pass_token=self.token)
        self.assertEqual(out, "refused: pass the item's row_digest from list_quarter_state")

    def test_the_quarter_listing_and_the_one_item_carry_it_too(self):
        items = _json("list_quarter_state", quarter="2026-Q3")["items"]
        one = _json("list_quarter_state", pid=self.pid)["item"]
        self.assertEqual([d["row_digest"] for d in items if d["pid"] == self.pid],
                         [one["row_digest"]])


class TestArgumentTypes(ToolCase):
    def test_every_boolean_refuses_a_string(self):
        import tools  # noqa: F401
        bools = [(n, k) for n, t in qa_server.TOOLS.items()
                 for k, v in t["schema"]["properties"].items() if v.get("type") == "boolean"]
        self.assertEqual(len(bools), 15, bools)  # fix wave F: + fresh_only; + failed; #10: + stopped_by_refusal, out_of_time
        for n, k in bools:
            res = _tool(n, **{k: "false"})
            text = res["content"][0]["text"]
            if text.startswith("refused: missing argument"):
                req = qa_server.TOOLS[n]["schema"]["required"]
                filler = {"pid": 1, "doc_id": 1, "pass_token": 1, "kind": "gmail",
                          "expected_revision": 0, "render_id": "r1", "channel": "telegram",
                          "snapshot_id": 1, "step": "sweep", "action": "finish"}
                res = _tool(n, **{r: filler[r] for r in req if r != k}, **{k: "false"})
                text = res["content"][0]["text"]
            self.assertEqual(text, f"refused: {k} must be true or false", (n, k))

    def test_the_string_false_marks_nothing_irrelevant(self):
        doc = self.doc()
        self.assertEqual(_text("mark_irrelevant", doc_id=doc, irrelevant="false"),
                         "refused: irrelevant must be true or false")
        self.assertEqual(_text("mark_irrelevant", doc_id=doc, irrelevant="true"),
                         "refused: irrelevant must be true or false")
        self.assertFalse(self.conn.execute("SELECT irrelevant FROM documents WHERE doc_id=?",
                                           (doc,)).fetchone()[0])

    def test_the_string_false_spends_no_search_effort(self):
        self.bind()
        token = self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        before = self.conn.execute("SELECT passes_without_candidate, search_state FROM"
                                   " projections WHERE pid=?", (pid,)).fetchone()
        self.assertEqual(_text("record_search", pid=pid, pass_token=token, exhausted="false"),
                         "refused: exhausted must be true or false")
        self.assertEqual(tuple(before), tuple(self.conn.execute(
            "SELECT passes_without_candidate, search_state FROM projections WHERE pid=?",
            (pid,)).fetchone()))

    def test_limit_is_one_or_more(self):
        self.bind()
        token = self.pass_()
        for bad in (0, -1):
            self.assertEqual(_text("list_unmatched_documents", limit=bad),
                             "refused: limit is 1 or more")
            self.assertEqual(_text("list_projections", pass_token=token, limit=bad),
                             "refused: limit is 1 or more")

    def test_import_names_every_missing_argument(self):
        self.assertEqual(_text("import_ledger_export"),
                         "refused: missing argument(s): path, pass_token, ledger_instance")


class TestSetupSentence(ToolCase):
    def test_an_upgrade_without_restore_says_the_older_writes_remain(self):
        self.bind()
        self.pass_(generation=0, registered={"acct@0.0.9": "b0"})
        setup = _json("check_setup")
        self.assertEqual(setup["bank_writes"]["older_workflows"], ["acct@0.0.9"])
        self.assertTrue(any("the older version's writes are still present" in c.lower()
                            for c in setup["conditions"]), setup["conditions"])

    def test_no_older_version_no_sentence(self):
        self.bind()
        self.pass_(generation=0, registered={})
        self.assertFalse(any("older version" in c for c in _json("check_setup")["conditions"]))


class TestInstallSmoke(TempEnv):
    def test_resident_files_specialist_reads_the_same_record_resident_stages_it(self):
        env = dict(os.environ)
        resident, specialist = _server(env), _server(env)
        try:
            path = self.publish("smoke.pdf", b"%PDF-1.4\nsmoke\n%%EOF\n")
            filed = json.loads(_call(resident, "ingest_document", 1, source_path=path,
                                     kind="invoice", source="manual-telegram",
                                     extraction_author="resident", counterparty="Smoke",
                                     amount_minor=100, currency="EUR",
                                     document_date="2026-09-01"))
            seen = json.loads(_call(specialist, "list_unmatched_documents", 2))
            self.assertEqual([d["doc_id"] for d in seen["documents"]], [filed["doc_id"]])
            staged = json.loads(_call(resident, "stage_for_delivery", 3, channel="telegram",
                                      doc_id=filed["doc_id"]))
            self.assertTrue(os.path.exists(staged["path"]))
            stored = next((self.data / "documents").rglob("*.pdf"))
            self.assertEqual(pathlib.Path(staged["path"]).read_bytes(), stored.read_bytes())
        finally:
            for p in (resident, specialist):
                p.stdin.close()
                p.wait(10)
                p.stdout.close()
                p.stderr.close()


if __name__ == "__main__":
    unittest.main()
