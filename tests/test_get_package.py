"""Simple loop §1 "The package" (#1303): get_package always sends, open items or not,
built from the store's latest state; ONE message: the file with ONE caption line naming the
latest check's date; the details live in the zip; a tap after a confirmation rebuilds;
nothing is sent unrequested (only this tool and the desk's typed asks send).
Ruling P8: every call runs under a fixed clock (CLOCK), so "Q3" names 2026-Q3 whatever
day the suite runs."""
import datetime
import zipfile

from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported
from tests.fakebroker import FakeBroker

CLOCK = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.timezone.utc)
NO_CHECK = "Nothing to package yet — ask me to check the bank first."


class Case(StoreCase):
    def stored_doc(self, **over):
        """A document row with its bytes in the store (a package ships them)."""
        doc_id = self.doc(**over)
        sha = self.conn.execute("SELECT sha256 FROM documents WHERE doc_id=?",
                                (doc_id,)).fetchone()[0]
        folder = self.data / "documents" / sha[:2]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sha}.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n" + sha.encode())
        return doc_id

    def assert_tally(self, path, caption):
        """The property (Task 9 review ruling): the caption's tally is the ledger's status
        tally — in scope = every line not UNTRACKED, open = the OPEN statuses."""
        import csv, io, re, package
        with zipfile.ZipFile(path) as z:
            rows = list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))
        n = sum(r["status"] != "UNTRACKED" for r in rows)
        open_ = sum(r["status"] in package.OPEN for r in rows)
        m = re.search(r" · (\d+) of (\d+) documented · (\d+) open$", caption)
        self.assertIsNotNone(m, caption)
        self.assertEqual(tuple(map(int, m.groups())), (n - open_, n, open_))
        return {r["counterparty"]: r["status"] for r in rows}


class GetPackage(Case):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', '2026-10-06T09:00:00Z', 0, 0,"
                              " '2026-10-06')")
        self.pids = []
        for n in (1, 2, 3):
            self.row(9000 + n, counterparty="Adobe", amount_minor=10000 + n,
                     booking_date="2026-09-0%d" % n, value_date="2026-09-0%d" % n)
            pid = self.lineage_for(9000 + n)
            self.classify(pid, {"software"})
            self.settle(pid)
            self.pids.append(pid)

    def get(self, quarter="Q3"):
        import qa_server, tools  # noqa: F401
        with self.patch_clock(CLOCK):
            return qa_server.TOOLS["get_package"]["fn"]({"quarter": quarter})

    def doc_for(self, n):
        return self.stored_doc(amount_minor=10000 + n, document_date="2026-09-0%d" % n)


    def test_one_file_one_caption_line_and_recorded_delivered(self):
        self.machine_match(self.pids[0], self.doc_for(1), self.token)
        with FakeBroker() as broker:
            out = self.get()
        self.assertTrue(out["package"].startswith("casa-cap-"))
        (dep,) = broker.deposits
        self.assertEqual(dep["kind"], "zip")
        self.assertTrue(dep["filename"].endswith(".zip"))
        self.assertEqual(dep["filename"], out["filename"])
        line = dep["caption"]                       # the one line, its binding tag at the end
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("Q3 · as of 6 Oct · 1 of 3 documented · 2 open"), line)
        self.assertEqual(self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                                           (out["delivery_id"],)).fetchone()[0], "delivered")
        # the package arrived: its rows are the accountant's copy (settle_delivered)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM delivered_rows WHERE package_id=(SELECT package_id FROM"
            " deliveries WHERE delivery_id=?)", (out["delivery_id"],)).fetchone()[0], 3)
        # the package-note follow-up is removed (§4): no such rendering is ever made
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE kind="
                                           "'package-note'").fetchone()[0], 0)
        # the stored caption IS the one line (no rest, so nothing else to post)
        self.assertEqual(self.conn.execute("SELECT caption FROM packages").fetchone()[0],
                         "Q3 · as of 6 Oct · 1 of 3 documented · 2 open")

    def test_a_tap_after_a_change_rebuilds_and_the_count_moves(self):
        with FakeBroker() as broker:
            first = self.get()
            self.machine_match(self.pids[1], self.doc_for(2), self.token)
            second = self.get()
        self.assertNotEqual(first["filename"], second["filename"])
        self.assertIn("0 of 3 documented · 3 open", broker.deposits[0]["caption"])
        self.assertIn("1 of 3 documented · 2 open", broker.deposits[1]["caption"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 2)
        for path, caption in self.conn.execute("SELECT path, caption FROM packages"):
            self.assert_tally(path, caption)

    def test_before_any_bank_check_it_refuses_in_words(self):
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM snapshots")
        with FakeBroker() as broker:
            out = self.get()
        self.assertEqual(out, {"package": None, "refused": NO_CHECK, "receipt": NO_CHECK})
        self.assertEqual(broker.deposits, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 0)

    def test_a_deposit_casa_refuses_answers_in_words_and_settles_failed(self):
        import posting
        with FakeBroker() as broker:
            broker.refuse = "invalid_filename"
            out = self.get()
        self.assertEqual(out, {"package": None, "refused": posting.PKG_REFUSED,
                               "receipt": posting.PKG_REFUSED})
        self.assertNotIn("package-file", [r[0] for r in self.conn.execute(
            "SELECT kind FROM renders")])          # the caption the operator never saw goes
        self.assertEqual([r[0] for r in self.conn.execute("SELECT status FROM deliveries")],
                         ["failed"])

    def test_a_failure_after_the_file_landed_leaves_the_send_for_recovery(self):
        """Review fix 2: once the deposit succeeded nothing may raise. A failure recording
        the send delivered leaves it staged and posted; the next claim's recovery
        (posted_unrecorded) settles it uncertain."""
        import delivery
        from unittest import mock

        def boom(conn, d):
            raise RuntimeError("check failed")
        with FakeBroker() as broker, mock.patch.object(delivery, "settle_delivered", boom):
            out = self.get()
        self.assertTrue(out["package"].startswith("casa-cap-"))
        self.assertEqual(len(broker.deposits), 1)
        self.assertEqual(out["alerts"], [])
        row = self.conn.execute("SELECT status, posted_at FROM deliveries WHERE"
                                " delivery_id=?", (out["delivery_id"],)).fetchone()
        self.assertEqual(row["status"], "staged")
        self.assertIsNotNone(row["posted_at"])
        self.assertEqual([r["delivery_id"] for r in delivery.posted_unrecorded(self.conn)],
                         [out["delivery_id"]])

    def test_other_capability_tools_keep_their_s7_refusal_shape(self):
        import qa_server, tools  # noqa: F401
        out = qa_server.TOOLS["post_package"]["fn"]({"delivery_id": 424242})
        self.assertEqual(set(out), {"package", "refused"})

    def test_a_pending_row_is_pending_in_the_zip_never_missing(self):
        import package
        self.row(9009, counterparty="Figma", amount_minor=1200, status="PDNG",
                 booking_date=None, value_date="2026-09-29")
        p = self.lineage_for(9009)
        self.classify(p, {"software"})
        self.settle(p)
        built = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        with zipfile.ZipFile(built["path"]) as z:
            ledger = z.read("ledger.csv").decode()
            notes = z.read("notes.md").decode()
        figma = [ln for ln in ledger.splitlines() if "Figma" in ln][0]
        self.assertIn(",PENDING,", figma)
        self.assertIn("## Pending at the bank", notes)
        pending = notes.split("## Pending at the bank", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Figma", pending)
        missing = notes.split("## Missing required documents", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("Figma", missing)
        # a pending row is not documented: it counts open in the one line (D18)
        self.assertTrue(built["caption"].endswith("0 of 4 documented · 4 open"),
                        built["caption"])
        self.assert_tally(built["path"], built["caption"])

    def test_every_pending_row_is_pending_whatever_its_status_as_the_cards_say(self):
        """Task 9 review ruling (spec §2.1, D18): a tracked row the bank has not booked is
        PENDING in the zip and open in the caption whatever its status — a 0.00 row, an
        exempt row and a matched row included — exactly the rows cards puts in its
        pending bucket (one predicate: work.is_pending, describe's `pending`)."""
        import cards, matches, package
        pdng = {"status": "PDNG", "booking_date": None}
        self.row(9020, counterparty="Zero", amount_minor=0, value_date="2026-09-20", **pdng)
        zero = self.lineage_for(9020)
        self.row(9021, counterparty="Exempt Co", amount_minor=500, value_date="2026-09-21",
                 **pdng)
        exempt = self.lineage_for(9021)
        for p in (zero, exempt):
            self.classify(p, {"software"})
            self.settle(p)
        self.granted(matches.set_exemption_in_tx, pid=exempt, exempt=True,
                     expected_revision=self.rev(exempt), render_id=self.show(exempt))
        self.settle(exempt)
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET status='PDNG' WHERE row_id=9003")
        self.settle(self.pids[2])
        self.operator_pair(pid=self.pids[2], doc_id=self.doc_for(3),   # the operator pairs a
                           expected_revision=self.rev(self.pids[2]),   # pending payment
                           render_id=self.show(self.pids[2]))
        self.settle(self.pids[2])
        statuses = {p: self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                         (p,)).fetchone()[0]
                    for p in (zero, exempt, self.pids[2])}
        self.assertEqual(statuses[exempt], "exempt")
        self.assertEqual(statuses[self.pids[2]], "matched")
        self.assertNotIn(statuses[zero], ("open", "proposed"), statuses)   # not open anyway
        built = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        st = self.assert_tally(built["path"], built["caption"])
        self.assertEqual({k: st[k] for k in ("Zero", "Exempt Co")},
                         {"Zero": "PENDING", "Exempt Co": "PENDING"})
        with zipfile.ZipFile(built["path"]) as z:
            rows = z.read("ledger.csv").decode().splitlines()
        self.assertEqual(sum(",PENDING," in r for r in rows), 3)     # the matched one too
        self.assertTrue(built["caption"].endswith("0 of 5 documented · 5 open"),
                        built["caption"])
        # the end message's partition counts exactly these rows pending
        self.assertEqual(sorted(d["pid"] for d in cards.state(self.conn)["pending"]),
                         sorted([zero, exempt, self.pids[2]]))

    def test_a_proposal_ships_its_alternatives_set_aside_with_the_chosen_document(self):
        import matches, package
        chosen, alt = self.doc_for(1), self.stored_doc(amount_minor=10001, document_date="2026-09-01",
                                                document_number="ALT-1")
        matches.propose_match(self.conn, pid=self.pids[0], doc_id=chosen,
                              expected_revision=self.rev(self.pids[0]), token=self.token,
                              document_date="2026-09-01", alternatives=[alt])
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (self.pids[0],)).fetchone()[0], "proposed")
        built = package.build_quarterly_package(self.conn, "2026-Q3", bound=False)
        with zipfile.ZipFile(built["path"]) as z:
            names = z.namelist()
            notes = z.read("notes.md").decode()
        aside = sorted(n for n in names if n.startswith("unresolved/"))
        self.assertEqual(len(aside), 2, names)
        listed = notes.split("## Unresolved candidates", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(sorted(x.rsplit(": ", 1)[1] for x in listed.strip().splitlines()),
                         aside)

    def test_send_the_last_one_still_sends_the_last_build_as_is(self):
        import delivery
        with FakeBroker():
            first = self.get()
        st = delivery.stage_for_delivery(self.conn, last_built=True, quarter="2026-Q3",
                                         pass_token=None)
        pk = self.conn.execute("SELECT filename FROM packages WHERE package_id=(SELECT"
                               " package_id FROM deliveries WHERE delivery_id=?)",
                               (st["delivery_id"],)).fetchone()[0]
        self.assertEqual(pk, first["filename"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 1)

    def test_send_it_again_on_the_tapped_file_says_it_arrived(self):
        import delivery
        with FakeBroker():
            out = self.get()
        pkg = self.conn.execute("SELECT package_id FROM deliveries WHERE delivery_id=?",
                                (out["delivery_id"],)).fetchone()[0]
        self.assertIn("package did arrive", delivery.resend_refusal(self.conn, pkg))


class GetPackageQ2(Case):
    """Ruling Q2 (live acceptance on Q2, watermark 2026-04-01): with Q3 rows present,
    get_package(quarter="2026-Q2") builds Q2 and its caption counts Q2 only."""

    def test_q2_builds_q2_with_its_own_caption(self):
        self.bind(watermark="2026-04-01")
        token = self.run_claim()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', '2026-10-06T09:00:00Z', 0, 0,"
                              " '2026-10-06')")
        pids = {}
        for rid, day in ((9101, "2026-05-04"), (9102, "2026-06-11"),
                         (9103, "2026-08-02"), (9104, "2026-09-15"), (9105, "2026-09-16")):
            self.row(rid, counterparty="Adobe", amount_minor=rid, booking_date=day,
                     value_date=day)
            pids[rid] = self.lineage_for(rid)
            self.classify(pids[rid], {"software"})
            self.settle(pids[rid])
        self.machine_match(pids[9101], self.stored_doc(amount_minor=9101, document_date="2026-05-04",
                                                ingest_quarter="2026-Q2"), token)
        import qa_server, tools  # noqa: F401
        with FakeBroker() as broker, self.patch_clock(CLOCK):
            out = qa_server.TOOLS["get_package"]["fn"]({"quarter": "2026-Q2"})
        self.assertIn("-2026-Q2-", out["filename"])
        (dep,) = broker.deposits
        self.assertTrue(dep["caption"].startswith("Q2 · as of 6 Oct · 1 of 2 documented · "
                                                  "1 open"), dep["caption"])
        built = self.conn.execute("SELECT quarter, path, caption FROM packages").fetchone()
        self.assert_tally(built["path"], built["caption"])
        self.assertEqual(built["quarter"], "2026-Q2")
        with zipfile.ZipFile(built["path"]) as z:
            ledger = z.read("ledger.csv").decode()
        self.assertEqual(len(ledger.strip().splitlines()), 3)      # header + the 2 Q2 rows


class CaptionLine(StoreCase):
    def test_the_ruled_format(self):
        import package
        self.assertEqual(package.caption_line("2026-Q3", "2026-10-06T09:00:00Z",
                                              {"in_scope": 60, "open": 3}),
                         "Q3 · as of 6 Oct · 57 of 60 documented · 3 open")
