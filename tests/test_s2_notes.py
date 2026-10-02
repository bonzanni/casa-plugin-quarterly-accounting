# tests/test_s2_notes.py
"""INV-J11 on note_confirmed itself: claims, issue gens and the ledger probe are rows."""
import datetime as _dt
from unittest import mock

from tests._base import StoreCase
from tests.test_sweep_real import Base as RealBase

T0 = _dt.datetime(2026, 10, 2, 12, 0, 0)


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class NoteConfirmation(StoreCase):
    def setUp(self):
        super().setUp()
        import db, version
        with db.tx(self.conn):
            for gen, at in ((1, T0), (2, T0 + _dt.timedelta(minutes=5))):
                self.conn.execute("INSERT INTO claims(gen, job_id, at) VALUES (?,?,?)",
                                  (gen, "aaaaaaaa-1", ts(at)))
        self.workflow = version.WORKFLOW

    def ledger(self, gen, registered=True, missing=()):
        import db, json
        data = {"registered": {self.workflow: "b1"} if registered else {},
                "missing": list(missing)}
        with db.tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO probes(kind, ok, detail, data_json,"
                              " observed_at, pass_id, gen) VALUES ('ledger',1,'',?,?,NULL,?)",
                              (json.dumps(data), ts(T0), gen))

    def proj(self, seen_gen, seen_at, other_gen=1):
        return {"note_body": "x", "note_seq": 2, "note_seen_seq": 2, "note_seen_rev": 7,
                "note_seen_at": ts(seen_at), "note_seen_gen": seen_gen,
                "note_issued_seq": 2, "note_issued_gen": 1, "note_other_issued_gen": other_gen,
                "note_issued_at": None, "note_other_issued_at": None}

    def confirmed(self, proj):
        import sweep
        return sweep.note_confirmed(proj, 7, epoch=None, conn=self.conn)

    def test_a_same_claim_read_never_confirms_an_other_text_issue(self):
        self.ledger(gen=2)
        self.assertFalse(self.confirmed(self.proj(1, T0 + _dt.timedelta(days=1))))

    def test_z_counts_from_the_first_later_claim(self):
        import sweep
        self.ledger(gen=2)
        g2 = T0 + _dt.timedelta(minutes=5)
        z = _dt.timedelta(seconds=sweep.Z_S)
        self.assertFalse(self.confirmed(self.proj(2, g2 + z - _dt.timedelta(seconds=1))))
        self.assertTrue(self.confirmed(self.proj(2, g2 + z)))

    def test_waits_for_the_mints_registration(self):
        import sweep
        g2 = T0 + _dt.timedelta(minutes=5)
        late = g2 + _dt.timedelta(seconds=sweep.Z_S + 60)
        self.ledger(gen=2, registered=False)
        self.assertFalse(self.confirmed(self.proj(2, late)))
        self.ledger(gen=2, registered=True, missing=[self.workflow])
        self.assertFalse(self.confirmed(self.proj(2, late)))
        self.ledger(gen=1, registered=True)                 # a probe not after the issue
        self.assertFalse(self.confirmed(self.proj(2, late)))
        self.ledger(gen=2, registered=True)
        self.assertTrue(self.confirmed(self.proj(2, late)))

    def test_no_other_text_issue_needs_no_margin(self):
        self.ledger(gen=1)
        self.assertTrue(self.confirmed(self.proj(1, T0, other_gen=None)))

    def test_an_older_texts_own_issue_counts_as_another_texts(self):
        # note_issued_gen bounds the read when note_issued_seq is not the current note
        self.ledger(gen=2)
        p = dict(self.proj(1, T0 + _dt.timedelta(days=1), other_gen=None), note_issued_seq=1)
        self.assertFalse(self.confirmed(p))
        self.assertTrue(self.confirmed(dict(p, note_seen_gen=2)))

    def test_a_generation_without_conn_is_refused(self):
        import sweep
        with self.assertRaises(TypeError):
            sweep.note_confirmed(self.proj(2, T0 + _dt.timedelta(days=1)), 7, epoch=None)

    def test_a_merge_carries_the_losers_issue_generation(self):
        # Astra plan review: the merge carried the loser's issue TIMES but not its
        # generations, so a read under the issuing claim confirmed (0 -> 1 confirmation)
        import db, ledger, lineage, sweep
        self.ledger(gen=2)
        for r in (1, 2):
            self.row(r)
        survivor, loser = self.lineage_for(1), self.lineage_for(2)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET note_body='x', note_seq=5, note_seen_seq=5,"
                              " note_seen_rev=7, note_seen_at=?, note_seen_gen=2 WHERE pid=?",
                              (ts(T0 + _dt.timedelta(days=1)), survivor))
            self.conn.execute("UPDATE projections SET note_body='y', note_seq=4, note_issued_seq=4,"
                              " note_issued_at=?, note_issued_gen=2 WHERE pid=?",
                              (ts(T0 + _dt.timedelta(minutes=6)), loser))
            ledger.merge(self.conn, survivor, loser)
        p = lineage.projection(self.conn, survivor)
        self.assertEqual(p["note_other_issued_gen"], 2)
        self.assertFalse(sweep.note_confirmed(p, 7, epoch=None, conn=self.conn))

    def test_a_merge_keeps_the_highest_issue_generation(self):
        import db, ledger, lineage
        for r in (1, 2):
            self.row(r)
        survivor, loser = self.lineage_for(1), self.lineage_for(2)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET note_other_issued_gen=3 WHERE pid=?",
                              (survivor,))
            self.conn.execute("UPDATE projections SET note_issued_gen=2,"
                              " note_other_issued_gen=1 WHERE pid=?", (loser,))
            ledger.merge(self.conn, survivor, loser)
        self.assertEqual(lineage.projection(self.conn, survivor)["note_other_issued_gen"], 3)


class LegacyIssue(RealBase):
    """A pre-upgrade issue (generation NULL) moving to `other` never erases an S2
    generation a merge brought in (Astra plan-r7 S1), on the REAL bank-feed."""
    JOB = "aaaaaaaa-1"
    T0 = _dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def setUp(self):
        import db
        super().setUp()
        self.now = self.T0
        p = mock.patch.object(db, "_clock", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        with db.tx(self.conn):
            self.conn.execute("UPDATE meta SET value=? WHERE key='store_epoch_at'",
                              (ts(self.T0 - _dt.timedelta(days=1)),))

    def later(self, seconds):
        self.now += _dt.timedelta(seconds=seconds)

    def test_a_legacy_issue_never_erases_a_merged_generation(self):
        import db, job, ledger, lineage, sweep
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        rid = self.rid()
        self.new_pass()
        self.cycle()                    # a delegation issue: note_issued_gen NULL
        survivor = self.pid_of(rid)
        self.assertIsNone(lineage.projection(self.conn, survivor)["note_issued_gen"])
        self.later(3600)
        t1 = job.claim(self.conn, self.JOB)
        self.job_pass(t1)
        self.row(9001)                  # an S2 issuer, merged into the survivor
        loser = self.lineage_for(9001)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET note_body='y', note_seq=1,"
                              " note_issued_seq=1, note_issued_at=?, note_issued_gen=?"
                              " WHERE pid=?", (db.now(), t1, loser))
            ledger.merge(self.conn, survivor, loser)
        self.assertEqual(lineage.projection(self.conn, survivor)["note_other_issued_gen"], t1)
        self.new_note_revision(survivor)
        self.cycle()                    # issued, written and re-read under t1
        p = lineage.projection(self.conn, survivor)
        self.assertEqual((p["note_other_issued_gen"], p["note_issued_gen"]), (t1, t1))
        self.later(sweep.Z_S + 100)
        self.job_pass(t1)
        self.cycle()                    # re-read under t1, past every time margin
        p = lineage.projection(self.conn, survivor)
        self.assertEqual(p["note_seen_gen"], t1)
        self.assertFalse(sweep.note_confirmed(p, self.bf.tag_revision(rid),
                                              epoch=db.epoch(self.conn), conn=self.conn))
        self.later(60)
        self.job_pass(t1)
        self.assertIn(survivor, sweep._due(self.conn))      # 0 confirmations
