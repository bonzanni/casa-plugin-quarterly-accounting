# tests/test_s2_readback.py
"""The sweep's read-back debt (spec §5.3), against the REAL bank-feed."""
from tests.test_sweep_real import Base     # first: tests._base puts server/ on sys.path
from tests import sim                      # noqa: E402  (sim imports sweep)
import sweep  # noqa: E402


class ReadbackDebt(Base):
    def owed_item(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        page = sweep.list_projections(self.conn, token=self.token)
        return page["projections"][0], page["snapshot_id"]

    def observe(self, item, snap, **extra):
        tags, notes, first_seen, rev = sim._read(self.bf, item["row_id"])
        return sweep.record_observation(self.conn, pid=item["pid"], token=self.token,
                                        snapshot_id=snap, observed_tags=tags,
                                        observed_notes=notes, observed_first_seen=first_seen,
                                        observed_tag_revision=rev, **extra)

    def debt(self, pid):
        return self.conn.execute("SELECT readback_owed FROM projections WHERE pid=?",
                                 (pid,)).fetchone()[0]

    def test_a_cut_after_the_instruction_keeps_the_row_due(self):
        item, snap = self.owed_item()
        out = self.observe(item, snap)
        self.assertTrue(out["instructions"])         # one write is owed
        # the turn is cut here: the write is never made and the row never re-read
        self.assertIn(item["pid"], sweep._due(self.conn))
        self.assertEqual(self.debt(item["pid"]), 1)

    def test_a_confirming_read_clears_it(self):
        item, snap = self.owed_item()
        self.observe(item, snap)
        self.assertEqual(self.debt(item["pid"]), 1)  # set first, so the clear is evidence
        sim.observe_and_repair(self.conn, self.bf, self.token, item, snap)
        self.assertNotIn(item["pid"], sweep._due(self.conn))
        self.assertEqual(self.debt(item["pid"]), 0)

    def test_write_error_clears_it(self):
        item, snap = self.owed_item()
        self.observe(item, snap)
        self.assertEqual(self.debt(item["pid"]), 1)
        sweep.record_observation(self.conn, pid=item["pid"], token=self.token,
                                 snapshot_id=snap, write_error="refused: test")
        self.assertEqual(self.debt(item["pid"]), 0)
