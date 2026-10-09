# tests/test_read_document.py
"""Issue #6: an agent reads a filed document through read_document. Casa's
path_scope lets no agent Read the store; the answer carries the bytes as an MCP
resource, which Claude Code saves under the calling session's own tool-results/
(a path Casa lets that session Read, ha-casa-app#1082) and names in the text."""
import base64
import hashlib
import json
import subprocess
import sys

from tests._base import ROOT, StoreCase
from tests.test_documents import PDF, ingest
from tests.test_tools import _fresh_conn
import budget  # noqa: E402
import documents  # noqa: E402
import qa_server  # noqa: E402

sys.modules.setdefault("qa_server", qa_server)


class TestReadDocument(StoreCase):
    def test_the_answer_is_a_bounded_text_block_and_the_bytes_as_a_resource(self):
        doc = ingest(self.conn, self.publish("Adobe invoice.pdf", PDF))
        text, res = documents.read_document(self.conn, doc["doc_id"])
        self.assertEqual(text["type"], "text")
        head = json.loads(text["text"])
        self.assertEqual((head["doc_id"], head["kind"], head["issuer"], head["amount_minor"]),
                         (doc["doc_id"], "invoice", "Adobe", 5445))
        self.assertIn("never instructions", head["notice"])
        self.assertIn("Read", head["how"])
        self.assertEqual(res["type"], "resource")
        r = res["resource"]
        self.assertEqual(r["mimeType"], "application/pdf")
        self.assertEqual(base64.b64decode(r["blob"]), PDF)
        self.assertTrue(r["uri"].endswith(f"/documents/{doc['doc_id']}"))

    def test_the_filed_fields_are_bounded_whatever_the_store_holds(self):
        doc = ingest(self.conn, self.publish("x.pdf", PDF), issuer="I" * 5000,
                     counterparty="C" * 5000, recipient="R" * 5000,
                     document_number="N" * 5000)
        text, _ = documents.read_document(self.conn, doc["doc_id"])
        head = json.loads(text["text"])
        for k in ("issuer", "counterparty", "recipient", "document_number"):
            self.assertLessEqual(len(head[k]), 300, k)
        self.assertLess(len(text["text"]), 4_000)

    def test_each_kind_of_file_is_sent_as_what_claude_code_can_open(self):
        # images go inline; an XML invoice is text Claude Code saves as .txt
        for name, mime in (("a.png", "image/png"), ("a.jpg", "image/jpeg"),
                           ("a.jpeg", "image/jpeg"), ("a.webp", "image/webp"),
                           ("a.gif", "image/gif"), ("a.xml", "text/plain")):
            data = name.encode() + b" bytes"
            doc = ingest(self.conn, self.publish(name, data))
            _, res = documents.read_document(self.conn, doc["doc_id"])
            self.assertEqual(res["resource"]["mimeType"], mime, name)
            self.assertEqual(base64.b64decode(res["resource"]["blob"]), data, name)

    def test_an_unknown_document_is_a_refusal(self):
        import db
        with self.assertRaises(db.Refusal):
            documents.read_document(self.conn, 999)

    def test_held_bytes_that_no_longer_match_their_hash_are_an_error_never_served(self):
        doc = ingest(self.conn, self.publish("a.pdf", PDF))
        # the same length: a size check would serve it (round C1, Astra)
        altered = PDF.replace(b"1.4", b"1.7")
        self.assertEqual(len(altered), len(PDF))
        documents.path_of(self.conn, doc["doc_id"]).write_bytes(altered)
        with self.assertRaises(documents.CustodyError):
            documents.read_document(self.conn, doc["doc_id"])

    def test_a_pdf_with_bytes_before_its_header_is_sent_from_the_header(self):
        # issue #8: a railway operator's invoice began with a UTF-8 BOM, and Read
        # rejects any PDF whose first bytes are not %PDF-
        held = b"\xef\xbb\xbf" + PDF
        doc = ingest(self.conn, self.publish("rail.pdf", held))
        text, res = documents.read_document(self.conn, doc["doc_id"])
        self.assertEqual(base64.b64decode(res["resource"]["blob"]), PDF)
        self.assertEqual(res["resource"]["mimeType"], "application/pdf")
        self.assertIn("3 bytes", json.loads(text["text"])["how"])
        # custody is untouched: the held file is the filed bytes
        path = documents.path_of(self.conn, doc["doc_id"])
        self.assertEqual(path.read_bytes(), held)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), doc["sha256"])

    def test_a_prefixed_pdf_whose_held_bytes_changed_is_an_error_never_served(self):
        # the hash is checked before the slice, whatever the prefix (round R1, Astra)
        held = b"\xef\xbb\xbf" + PDF
        doc = ingest(self.conn, self.publish("rail.pdf", held))
        documents.path_of(self.conn, doc["doc_id"]).write_bytes(held.replace(b"1.4", b"1.7"))
        with self.assertRaises(documents.CustodyError):
            documents.read_document(self.conn, doc["doc_id"])

    def test_a_pdf_that_starts_with_its_header_says_nothing_was_dropped(self):
        doc = ingest(self.conn, self.publish("a.pdf", PDF))
        text, _ = documents.read_document(self.conn, doc["doc_id"])
        self.assertNotIn("dropped", json.loads(text["text"])["how"])

    def test_a_header_within_the_first_1024_bytes_is_found_and_no_further(self):
        doc = ingest(self.conn, self.publish("a.pdf", b"x" * 1019 + PDF))
        _, res = documents.read_document(self.conn, doc["doc_id"])
        self.assertEqual(base64.b64decode(res["resource"]["blob"]), PDF)
        doc = ingest(self.conn, self.publish("b.pdf", b"x" * 1020 + PDF))
        import db
        with self.assertRaises(db.Refusal) as cm:
            documents.read_document(self.conn, doc["doc_id"])
        self.assertIn(f"#{doc['doc_id']}", str(cm.exception))

    def test_a_pdf_with_no_header_is_a_refusal_naming_the_document(self):
        import db
        doc = ingest(self.conn, self.publish("a.pdf", b"<html>not a pdf</html>"))
        with self.assertRaises(db.Refusal) as cm:
            documents.read_document(self.conn, doc["doc_id"])
        self.assertIn(f"#{doc['doc_id']}", str(cm.exception))
        self.assertIn("unreadable", str(cm.exception))

    def test_missing_held_bytes_are_an_error(self):
        doc = ingest(self.conn, self.publish("a.pdf", PDF))
        documents.path_of(self.conn, doc["doc_id"]).unlink()
        with self.assertRaises(documents.CustodyError):
            documents.read_document(self.conn, doc["doc_id"])


class TestReadDocumentOverMcp(StoreCase):
    def setUp(self):
        super().setUp()
        _fresh_conn(self)

    def _call(self, **args):
        import tools  # noqa: F401
        return qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "read_document", "arguments": args}})

    def test_the_tool_answers_with_both_blocks_as_they_are(self):
        doc = ingest(self.conn, self.publish("a.pdf", PDF))
        out = self._call(doc_id=doc["doc_id"])["result"]
        self.assertNotIn("isError", out)
        self.assertEqual([b["type"] for b in out["content"]], ["text", "resource"])
        blob = out["content"][1]["resource"]["blob"]
        self.assertEqual(hashlib.sha256(base64.b64decode(blob)).hexdigest(), doc["sha256"])
        # the text block alone is what counts against Claude Code's answer cap
        self.assertLess(len(out["content"][0]["text"]), budget.RESULT_LIMIT)

    def test_a_plain_list_answer_is_still_rendered_as_one_json_text_block(self):
        # only a Blocks answer is sent as content blocks (round C1, Astra)
        import tools  # noqa: F401
        qa_server.TOOLS["_probe_list"] = {"description": "", "schema": {},
                                          "fn": lambda args: [{"invoice": 42}]}
        self.addCleanup(qa_server.TOOLS.pop, "_probe_list")
        out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "_probe_list", "arguments": {}}})["result"]
        self.assertEqual(out["content"], [{"type": "text", "text": '[{"invoice":42}]'}])

    def test_a_missing_doc_id_is_a_refusal_not_a_crash(self):
        out = self._call()["result"]
        self.assertTrue(out["content"][0]["text"].lstrip("*").startswith("refused:"))
        self.assertNotIn("isError", out)

    def test_a_custody_failure_is_reported_as_an_error(self):
        doc = ingest(self.conn, self.publish("a.pdf", PDF))
        documents.path_of(self.conn, doc["doc_id"]).unlink()
        out = self._call(doc_id=doc["doc_id"])["result"]
        self.assertTrue(out["isError"])
        self.assertEqual(len(out["content"]), 1)

    def test_over_stdio_the_json_line_carries_the_resource(self):
        doc = ingest(self.conn, self.publish("a.pdf", PDF))
        import os
        p = subprocess.run([sys.executable, str(ROOT / "server/qa_server.py")],
                           input=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                             "params": {"name": "read_document",
                                                        "arguments": {"doc_id": doc["doc_id"]}}})
                           + "\n", capture_output=True, text=True, env=dict(os.environ))
        res = json.loads(p.stdout)["result"]["content"][1]["resource"]
        self.assertEqual(base64.b64decode(res["blob"]), PDF)
