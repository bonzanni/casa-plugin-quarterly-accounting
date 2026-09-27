import multiprocessing
import os
import threading
import time
import unittest
from unittest import mock

from tests._base import StoreCase
from tests import _procs
import db  # noqa: E402
import documents  # noqa: E402
import lineage  # noqa: E402
import reducer as R  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def ingest(conn, path, **over):
    kw = dict(source_path=path, kind="invoice", source="gmail", extraction_author="resident",
              counterparty="Adobe", issuer="Adobe", document_number="A-1",
              amount_minor=5445, currency="EUR", document_date="2026-09-14")
    kw.update(over)
    return documents.ingest_document(conn, **kw)


class TestCustody(StoreCase):
    def test_bytes_are_copied_by_hash_and_ingest_is_idempotent(self):
        path = self.publish("Adobe invoice.pdf", PDF)
        first = ingest(self.conn, path)
        again = ingest(self.conn, self.publish("renamed.pdf", PDF))
        self.assertTrue(first["created"])
        self.assertEqual((again["doc_id"], again["created"]), (first["doc_id"], False))
        stored = documents.path_of(self.conn, first["doc_id"])
        self.assertEqual(stored.read_bytes(), PDF)
        self.assertEqual(stored.parent.name, first["sha256"][:2])
        self.assertEqual(stored.name, first["sha256"] + ".pdf")

    def test_the_same_bytes_under_another_extension_reuse_the_held_file(self):
        # fix wave F: the second filing left <sha>.png beside <sha>.pdf, which no row
        # names and the reaper never removed (its stem is a held hash)
        first = ingest(self.conn, self.publish("scan.pdf", PDF))
        again = ingest(self.conn, self.publish("scan.png", PDF))
        self.assertEqual(again["doc_id"], first["doc_id"])
        d = self.data / "documents" / first["sha256"][:2]
        self.assertEqual(sorted(f.name for f in d.iterdir()), [f"{first['sha256']}.pdf"])

    def test_the_reaper_removes_a_held_hash_under_a_name_no_row_claims(self):
        first = ingest(self.conn, self.publish("scan.pdf", PDF))
        d = self.data / "documents" / first["sha256"][:2]
        stray = d / f"{first['sha256']}.png"                  # left by an earlier version
        stray.write_bytes(PDF)
        old = time.time() - 7200
        os.utime(stray, (old, old))
        os.utime(d / f"{first['sha256']}.pdf", (old, old))
        self.assertEqual(documents.reap_orphans(self.conn), 1)
        self.assertEqual(sorted(f.name for f in d.iterdir()), [f"{first['sha256']}.pdf"])

    def test_a_path_outside_the_handoff_folder_is_refused(self):
        outside = self.tmp / "secret.pdf"
        outside.write_bytes(PDF)
        with self.assertRaises(db.Refusal):
            ingest(self.conn, str(outside))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_unsupported_file_type_is_refused(self):
        with self.assertRaises(db.Refusal):
            ingest(self.conn, self.publish("macro.docm", b"PK\x03\x04"))

    def test_a_crash_before_indexing_leaves_only_a_reapable_file(self):
        path = self.publish("a.pdf", PDF)
        real = documents._install

        def install_then_crash(*a, **kw):     # bytes installed, index row not committed
            real(*a, **kw)
            raise RuntimeError("crash")
        with mock.patch.object(documents, "_install", install_then_crash):
            with self.assertRaises(RuntimeError):
                ingest(self.conn, path)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        files = list((self.data / "documents").rglob("*.pdf"))
        self.assertEqual(len(files), 1)
        old = time.time() - 7200
        os.utime(files[0], (old, old))
        self.assertEqual(documents.reap_orphans(self.conn, older_than_s=3600), 1)
        self.assertTrue(ingest(self.conn, path)["created"])

    def test_issuer_and_number_collision_across_different_bytes(self):
        a = ingest(self.conn, self.publish("a.pdf", PDF))
        b = ingest(self.conn, self.publish("b.pdf", PDF + b"re-rendered"))
        self.assertEqual(b["collisions"], [a["doc_id"]])
        self.assertEqual(documents.collisions(self.conn, a["doc_id"]), [b["doc_id"]])
        documents.mark_irrelevant(self.conn, b["doc_id"])
        self.assertEqual(documents.collisions(self.conn, a["doc_id"]), [])


class TestCuration(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.doc_id = ingest(self.conn, self.publish("a.pdf", PDF))["doc_id"]
        with db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, self.pid))
            mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?,?,0)", (self.pid, self.doc_id)).lastrowid
            lineage.append(self.conn, self.pid, "pair", "operator", match_id=mid,
                           doc_id=self.doc_id, fp=R.fingerprint(R.facts_of(row), "invoice"))
            lineage.settle(self.conn, self.pid)

    def test_correcting_the_kind_moves_the_holders_revision(self):
        before = lineage.projection(self.conn, self.pid)["revision"]
        documents.update_document_metadata(self.conn, self.doc_id, kind="payslip")
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual(p["revision"], before + 1)
        self.assertIn("kind-mismatch", p["reasons_json"])

    def test_a_held_document_cannot_be_marked_irrelevant(self):
        with self.assertRaises(db.Refusal):
            documents.mark_irrelevant(self.conn, self.doc_id)

    def test_unmatched_listing_excludes_held_and_irrelevant(self):
        free = ingest(self.conn, self.publish("free.pdf", PDF + b"2"), document_number="B-2")
        junk = ingest(self.conn, self.publish("junk.pdf", PDF + b"3"), document_number="C-3")
        documents.mark_irrelevant(self.conn, junk["doc_id"])
        listed = [d["doc_id"] for d in documents.list_unmatched(self.conn)["documents"]]
        self.assertEqual(listed, [free["doc_id"]])

    def test_metadata_validation(self):
        with self.assertRaises(db.Refusal):
            documents.update_document_metadata(self.conn, self.doc_id, kind="quote")
        with self.assertRaises(db.Refusal):
            documents.update_document_metadata(self.conn, self.doc_id, sha256="x")



class TestCustodyUnderConcurrency(StoreCase):
    """fix wave B (Astra + Terra S1): after an ingest, a reset or a reap returns,
    no index row may name bytes that are gone — under any interleaving."""

    def _rows_without_bytes(self):
        return [r["doc_id"] for r in self.conn.execute("SELECT doc_id FROM documents")
                if not documents.path_of(self.conn, r["doc_id"]).exists()]

    def _race(self, child_target, child_args, paused, other):
        ctx = multiprocessing.get_context("spawn")
        ev, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        child = ctx.Process(target=child_target, args=(*child_args, ev, resume, out))
        child.start()
        self.addCleanup(child.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(ev.wait(30), f"the child never reached {paused}")
        got = {}

        def run():
            c = db.open_store()
            try:
                got["other"] = other(c)
            except BaseException as exc:
                got["other"] = exc
            finally:
                c.close()
        t = threading.Thread(target=run)
        t.start()
        t.join(1.0)                  # pre-fix, the other operation completes here
        resume.set()
        child.join(30)
        t.join(30)
        child_result = out.get(timeout=5)
        self.assertEqual(child_result[0], "ok", child_result)
        self.assertNotIsInstance(got["other"], BaseException, got.get("other"))
        return child_result[1], got["other"]

    def test_a_reset_during_an_ingest_never_leaves_a_row_over_missing_bytes(self):
        import binding
        path = self.publish("a.pdf", PDF)
        _, reset = self._race(_procs.ingest_paused, (path,), "the install",
                              binding.reset_store)
        self.assertEqual(reset["erasure"], "complete", reset)
        self.assertEqual(self._rows_without_bytes(), [])

    def test_an_ingest_during_a_reset_never_has_its_bytes_deleted_afterwards(self):
        path = self.publish("a.pdf", PDF)
        _, res = self._race(_procs.reset_paused, (), "the row wipe",
                            lambda c: ingest(c, path))
        self.assertTrue(res["created"])
        self.assertEqual(self._rows_without_bytes(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_a_reap_during_a_re_ingest_of_an_old_orphan_keeps_its_bytes(self):
        # a crash left an orphan; re-filing the same bytes re-uses the installed
        # file (its old mtime untouched), so a reap must not unlink it mid-ingest
        sha = __import__("hashlib").sha256(PDF).hexdigest()
        orphan = documents._install(PDF, sha, ".pdf")
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        path = self.publish("a.pdf", PDF)
        _, reaped = self._race(_procs.ingest_paused, (path,), "the install",
                               lambda c: documents.reap_orphans(c, older_than_s=3600))
        self.assertEqual(reaped, 0)
        self.assertEqual(self._rows_without_bytes(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_a_stale_pass_ingest_waiting_behind_a_reset_installs_nothing(self):
        # round B2 (Astra S2): the ingest captured its bytes, waited behind reset's
        # custody lock, then installed them BEFORE its token (fenced by the reset)
        # was refused — reset said "complete" and the bytes were back on disk
        import passes
        token = passes.begin_pass(self.conn, "cron")["pass_token"]
        path = self.publish("a.pdf", PDF)

        def stale_ingest(c):
            try:
                ingest(c, path, token=token)
            except db.Refusal as exc:
                return f"refused: {exc}"
            return "written"
        reset, res = self._race(_procs.reset_paused, (), "the row wipe", stale_ingest)
        self.assertEqual(reset["erasure"], "complete", reset)
        self.assertTrue(res.startswith("refused"), res)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        root = self.data / "documents"
        self.assertEqual([f for f in root.rglob("*") if f.is_file()] if root.exists() else [],
                         [])
