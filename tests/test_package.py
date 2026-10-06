# tests/test_package.py
import contextlib
import csv
import io
import multiprocessing
import pathlib
import unittest
import zipfile
from unittest import mock

from tests._base import StoreCase
import db  # noqa: E402
import expectation as ex  # noqa: E402
import matches  # noqa: E402
import package  # noqa: E402
import xlsx  # noqa: E402

PDF = b"%PDF-1.4\n%%EOF\n"


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', '2026-09-20T10:00:00Z', 0, 0, '2026-09-20')")
        self.n = 0

    def line(self, tags=("software",), **row):
        self.n += 1
        self.row(self.n, **row)
        pid = self.lineage_for(self.n)
        self.classify(pid, set(tags))
        self.settle(pid)
        return pid

    def file_doc(self, kind="invoice", body=b"", **meta):
        import documents
        self.docs = getattr(self, "docs", 0) + 1       # distinct numbers: no identity collision
        path = self.publish("d%d.pdf" % self.docs, PDF + body + str(meta).encode())
        args = dict(source_path=path, kind=kind, source="gmail", extraction_author="desk",
                    counterparty="Adobe", issuer="Adobe", amount_minor=10000, currency="EUR",
                    document_date="2026-07-02", document_number="N%d" % self.docs)
        args.update(meta)
        return documents.ingest_document(self.conn, **args)["doc_id"]

    def pair(self, pid, doc_id, how="record"):
        fn = matches.record_match if how == "record" else matches.propose_match
        kw = {"author": "auto"} if how == "record" else {}
        return fn(self.conn, pid=pid, doc_id=doc_id, expected_revision=self.rev(pid),
                  row_snapshot=self.snapshot(pid), token=self.token, **kw)

    def build(self, q="2026-Q3"):
        out = package.build_quarterly_package(self.conn, q, bound=False)
        z = zipfile.ZipFile(out["path"])
        self.addCleanup(z.close)
        return out, z

    def ledger_rows(self, z):
        return list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))


class TestContents(Base):
    def test_cross_quarter_invoice_ships_with_its_payment(self):
        pid = self.line(booking_date="2026-07-01", value_date="2026-07-01")
        self.pair(pid, self.file_doc(document_date="2026-06-30"))
        _, z = self.build()
        names = [n for n in z.namelist() if n.startswith("invoices/")]
        self.assertEqual(names, ["invoices/2026-06-30_Adobe_100.00.pdf"])

    def test_routing_is_total_and_exclusive(self):
        for kind in ex.KINDS:
            for tier in ex.TIERS:
                self.assertIn(package.route(kind, tier),
                              ("invoices", "sales-invoices", "credit-notes", "documents"))
        self.assertEqual(package.route("invoice", "required"), "invoices")
        self.assertEqual(package.route("invoice", "optional"), "documents")
        self.assertEqual(package.route("receipt", "required"), "documents")
        self.assertEqual(package.route("credit-note", "required"), "credit-notes")
        self.assertEqual(package.route("sales-invoice", "required"), "sales-invoices")

    def test_a_proposed_line_never_reaches_a_folder_and_its_candidate_is_unresolved(self):
        pid = self.line()
        self.pair(pid, self.file_doc(), how="propose")
        _, z = self.build()
        self.assertFalse([n for n in z.namelist() if n.startswith("invoices/")])
        self.assertEqual(len([n for n in z.namelist() if n.startswith("unresolved/")]), 1)
        self.assertEqual(self.ledger_rows(z)[0]["status"], "UNCONFIRMED")

    def test_a_superseded_predecessor_is_never_summed(self):
        self.row(1, state="superseded", superseded_by=2, status="PDNG", amount_minor=9900)
        self.n = 1
        self.line(amount_minor=9900)
        _, z = self.build()
        rows = self.ledger_rows(z)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "99.00")
        self.assertIn("superseded", z.read("notes.md").decode())

    def test_notes_name_rows_by_date_and_amount_never_by_machine_id(self):
        # fix wave F: "#12 → #13 (absent)" and "#1 superseded" are machine row ids
        import re
        self.row(1, state="superseded", superseded_by=4, status="PDNG", amount_minor=9900,
                 booking_date=None, value_date="2026-07-02")
        self.row(3, state="superseded", superseded_by=77, amount_minor=5000,
                 booking_date="2026-07-04", value_date="2026-07-04")
        self.n = 3
        pid = self.line(amount_minor=9900, booking_date="2026-07-03", value_date="2026-07-03")
        broken = self.line(amount_minor=1234, booking_date="2026-07-05", value_date="2026-07-05")
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET broken_floor=? WHERE pid=?",
                              ("#4 → #78 (absent)", broken))
        self.assertTrue(pid)
        _, z = self.build()
        notes = z.read("notes.md").decode()
        self.assertIsNone(re.search(r"#\d", notes), notes)
        history = notes.split("## Bank rows kept as history (not summed)")[1].split("\n\n")[1]
        self.assertIn("2 Jul · EUR 99.00 — superseded, replaced by the 3 Jul EUR 99.00 row",
                      history)
        self.assertIn("4 Jul · EUR 50.00 — superseded, replaced by a row the bank no longer shows",
                      history)
        self.assertIn("Adobe · EUR 12.34 · 5 Jul: bank-feed's history is broken (the row that "
                      "replaces it is missing).", notes)

    def test_statuses_and_notes_order(self):
        kbline = self.line(counterparty="Adobe")
        self.line(tags=(), counterparty="Mystery")
        self.line(tags=("internal-transfer",), counterparty="Own account")
        self.line(tags=("income", "salary"), counterparty="Payroll Co")
        import kb
        kb.upsert_counterparty(self.conn, "Adobe", document_link="https://adobe.example/invoices")
        out, z = self.build()
        st = {r["counterparty"]: r["status"] for r in self.ledger_rows(z)}
        self.assertEqual(st, {"Adobe": "MISSING", "Mystery": "MISSING",
                              "Own account": "NO-DOCUMENT", "Payroll Co": "OPTIONAL-MISSING"})
        notes = z.read("notes.md").decode()
        self.assertLess(notes.index("## Missing"), notes.index("## Not yet classified"))
        self.assertLess(notes.index("## Not yet classified"), notes.index("## Nice to have"))
        self.assertIn("https://adobe.example/invoices", notes)
        self.assertTrue(notes.rstrip().splitlines()[-1].startswith("built "))
        # simple loop §1: ONE line, as of the latest check; OPTIONAL-MISSING and
        # NO-DOCUMENT count as documented
        self.assertEqual(out["caption"], "Q3 · as of 20 Sep · 2 of 4 documented · 2 open")
        del kbline

    def test_a_retained_pairing_on_an_unknown_expectation_is_matched(self):
        # simple loop §2 table: no classification gate. A pairing kept while the
        # classification is removed is MATCHED and ships its document in its folder;
        # nothing is "not yet classified" any more.
        pid = self.line()
        self.pair(pid, self.file_doc())
        self.classify(pid, set())
        self.settle(pid)
        out, z = self.build()
        rows = self.ledger_rows(z)
        self.assertEqual([r["status"] for r in rows], ["MATCHED"])
        self.assertEqual(rows[0]["document"], "invoices/2026-07-02_Adobe_100.00.pdf")
        self.assertIn("invoices/2026-07-02_Adobe_100.00.pdf", z.namelist())
        notes = z.read("notes.md").decode()
        section = notes.split("## Not yet classified")[1].split("##")[0]
        self.assertEqual(section.strip().splitlines(), ["- none"])
        self.assertEqual(out["caption"], "Q3 · as of 20 Sep · 1 of 1 documented · 0 open")

    def test_a_retained_unconfirmed_pairing_on_an_unknown_expectation_is_unconfirmed(self):
        pid = self.line()
        self.pair(pid, self.file_doc(), how="propose")
        self.classify(pid, set())
        self.settle(pid)
        out, z = self.build()
        rows = self.ledger_rows(z)
        self.assertEqual([r["status"] for r in rows], ["UNCONFIRMED"])
        self.assertEqual(rows[0]["document"], "")
        self.assertTrue(any(n.startswith("unresolved/") for n in z.namelist()))
        self.assertEqual(out["caption"], "Q3 · as of 20 Sep · 0 of 1 documented · 1 open")

    def test_xlsx_cells_equal_the_csv(self):
        pid = self.line()
        self.pair(pid, self.file_doc())
        self.line(counterparty="Tab\tand \x07 bell")
        _, z = self.build()
        csv_rows = list(csv.reader(io.StringIO(z.read("ledger.csv").decode())))
        cells = xlsx.read_cells(z.read("ledger.xlsx"))
        self.assertEqual(cells, [[package.xml_safe(c) for c in r] for r in csv_rows])

    def test_xlsx_keeps_a_carriage_return_and_drops_xml_non_characters(self):
        # deviation: a literal CR was read back as LF, and U+FFFE made the sheet
        # unparseable — either way ledger.xlsx no longer equalled ledger.csv
        self.line(counterparty="Line\rbreak \ufffe odd")
        _, z = self.build()
        csv_rows = list(csv.reader(io.StringIO(z.read("ledger.csv").decode(), newline="")))
        cells = xlsx.read_cells(z.read("ledger.xlsx"))
        self.assertEqual(cells, [[package.xml_safe(c) for c in r] for r in csv_rows])
        self.assertIn("Line\rbreak  odd", cells[1])

    def test_names_differing_only_by_case_are_distinct_after_casefold(self):
        # fix round 1: Adobe_… and ADOBE_… are one file on Windows/macOS
        a, b = self.line(), self.line()
        self.pair(a, self.file_doc(issuer="Adobe", body=b"1"))
        self.pair(b, self.file_doc(issuer="ADOBE", body=b"2"))
        _, z = self.build()
        names = [n for n in z.namelist() if n.startswith("invoices/")]
        self.assertEqual(len(names), 2)
        self.assertEqual(len({n.casefold() for n in names}), 2)

    def test_doc_filename_never_repeats_a_name_casefolded(self):
        used = set()
        doc = {"amount_minor": 100, "document_date": "2026-07-02", "issuer": "X",
               "ext": "pdf", "sha256": "ab" * 32}
        names = [package.doc_filename(doc, used, "2026-07-02") for _ in range(4)]
        self.assertEqual(len({n.casefold() for n in names}), 4)

    def test_one_document_on_several_lines_gets_one_name_per_folder(self):
        # fix round 1: a candidate on two lines shipped twice in unresolved/ (X, X_h8)
        a, b = self.line(), self.line()
        doc = self.file_doc()
        with db.tx(self.conn):
            for pid in (a, b):
                mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                        " VALUES (?,?,?)", (pid, doc, db.next_seq(self.conn))
                                        ).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                                  " activation) VALUES (?,?,?,'conflicted','auto',1)",
                                  (mid, pid, doc))
        _, z = self.build()
        names = [n for n in z.namelist() if n.startswith("unresolved/")]
        self.assertEqual(names, ["unresolved/2026-07-02_Adobe_100.00.pdf"])
        notes = z.read("notes.md").decode()
        # twice as a candidate, once among the files named by an unread date (issue #22)
        self.assertEqual(notes.count("unresolved/2026-07-02_Adobe_100.00.pdf"), 3)
        self.assertEqual(notes.split("## Dates not yet read")[1].count("Adobe"), 1)

    def test_same_day_same_amount_same_vendor_documents_get_distinct_names(self):
        a, b = self.line(), self.line()
        self.pair(a, self.file_doc(body=b"1"))
        self.pair(b, self.file_doc(body=b"2"))
        _, z = self.build()
        names = sorted(n for n in z.namelist() if n.startswith("invoices/"))
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(n.startswith("invoices/2026-07-02_Adobe_100.00") for n in names))


class TestNamingAndDeterminism(Base):
    def test_partial_name_and_first_line(self):
        with mock.patch.object(db, "now", lambda: "2026-08-14T10:00:00Z"):
            out, z = self.build()
        self.assertEqual(out["filename"], "zakelijk-2026-Q3-partial-2026-08-14.zip")
        self.assertTrue(z.read("notes.md").decode().startswith("Partial quarter"))

    def test_two_rebuilds_in_one_minute_both_survive(self):
        with mock.patch.object(db, "now", lambda: "2026-10-14T14:12:10Z"):
            a, _ = self.build()
        self.line()
        with mock.patch.object(db, "now", lambda: "2026-10-14T14:12:45Z"):
            b, _ = self.build()
        with mock.patch.object(db, "now", lambda: "2026-10-14T14:12:45Z"):
            c, _ = self.build()
        names = {a["filename"], b["filename"], c["filename"]}
        self.assertEqual(len(names), 3)
        self.assertEqual(a["filename"], "zakelijk-2026-Q3-2026-10-14.zip")
        for o in (a, b, c):
            self.assertTrue(zipfile.is_zipfile(o["path"]))

    def test_same_frozen_inputs_same_bytes(self):
        self.pair(self.line(), self.file_doc())
        with mock.patch.object(db, "now", lambda: "2026-10-14T09:00:00Z"):
            a, _ = self.build()
            b, _ = self.build()
        self.assertEqual(pathlib.Path(a["path"]).read_bytes(), pathlib.Path(b["path"]).read_bytes())
        self.assertEqual(a["digest"], b["digest"])

    def test_a_change_only_in_notes_is_not_identical(self):
        with mock.patch.object(db, "now", lambda: "2026-10-14T09:00:00Z"):
            first, _ = self.build()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                              " created_at, settled_at) VALUES (?, 'telegram', '/x', 'delivered',"
                              " 'x', '2026-10-14T09:01:00Z')", (first["package_id"],))
        with mock.patch.object(db, "now", lambda: "2026-07-20T09:00:00Z"):
            self.file_doc(document_number="LOOSE-1")        # filed in Q3, matches nothing
        with mock.patch.object(db, "now", lambda: "2026-10-15T09:00:00Z"):
            again, _ = self.build()
        self.assertNotEqual(again["digest"], first["digest"])

    def test_an_unchanged_partial_rebuild_on_another_day_is_identical(self):
        # deviation: the dated "Partial quarter" line used to enter the digest
        with mock.patch.object(db, "now", lambda: "2026-08-14T09:00:00Z"):
            first, z = self.build()
        self.assertTrue(first["partial"])
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                              " created_at, settled_at) VALUES (?, 'telegram', '/x', 'delivered',"
                              " 'x', '2026-08-14T09:01:00Z')", (first["package_id"],))
        with mock.patch.object(db, "now", lambda: "2026-08-20T09:00:00Z"):
            again, z2 = self.build()
        self.assertEqual(again["digest"], first["digest"])
        self.assertTrue(z2.read("notes.md").decode().startswith(
            "Partial quarter — built 2026-08-20"))
        with mock.patch.object(db, "now", lambda: "2026-10-02T09:00:00Z"):
            closed, _ = self.build()
        self.assertNotEqual(closed["digest"], first["digest"])

    def test_the_build_takes_the_custody_lock_outside_any_transaction(self):
        seen = []

        @contextlib.contextmanager
        def lock():
            seen.append(("enter", self.conn.in_transaction))
            yield
            seen.append(("exit", self.conn.in_transaction))
        with mock.patch.object(db, "custody_lock", lock, create=True):
            out, _ = self.build()
        self.assertEqual(seen, [("enter", False), ("exit", False)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 1)
        del out

    def test_oversize_is_kept_and_explained(self):
        self.pair(self.line(), self.file_doc(body=b"x" * 5000))
        with mock.patch.object(package, "MAX_ZIP_BYTES", 1000):
            out, z = self.build()
        self.assertTrue(out["oversize"])
        self.assertIn("## Too large to send", z.read("notes.md").decode())
        self.assertNotIn("\n", out["caption"])    # the one line; get_package refuses to send it


class TestConcurrentBuilds(Base):
    def test_two_processes_build_one_quarter(self):
        self.pair(self.line(), self.file_doc())
        path = str(self.data / db.DB_NAME)
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_build, args=(path, str(self.data), q)) for _ in range(2)]
        for p in procs:
            p.start()
        outs = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(60)
        self.assertEqual(len({o["filename"] for o in outs}), 2)
        for o in outs:
            with zipfile.ZipFile(o["path"]) as z:
                self.assertIn("ledger.csv", z.namelist())


def _build(path, data_dir, q):
    import os
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    import db as _db
    import package as _p
    conn = _db.open_store(path)
    try:
        with mock.patch.object(_db, "now", lambda: "2026-10-14T14:12:10Z"):
            q.put(_p.build_quarterly_package(conn, "2026-Q3", bound=False))
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
