"""OPERATOR RULING (2026-10-07, after diff round f3): no call budget. Progress no longer
depends on one — a cut batch's persisted work is reported by the next claim (said_seq), a
payment cut mid-way is redone — so the server hands out work until Casa cuts the batch at
its turn limit. f3's #5 (a model sending running totals) and #6 (a closing report past the
budget) vanish by construction. A mirror unit is a chunk of MIRROR_CALLS bank-feed calls:
a cut inside one may repeat up to that many calls (tag calls are no-ops; a note call adds
an identical line)."""
import collections

from tests._base import StoreCase
from tests.sim_job import CasaCut, JobDriver


def quarter(test, n, job_id, mode):
    drv = JobDriver(test, payments=n)
    for i in range(n):
        drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
    drv.skip_attached_reports = True              # run 1's model
    drv.calls_mode = mode
    drv.casa_cut = 80                             # Casa's turn limit, its 20-batch cap
    units = drv.run_job(job_id)
    return drv, units


class NoBudget(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def statuses(self):
        return dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall())

    def test_f3_5_a_model_sending_running_totals_finishes_the_quarter(self):
        drv, _ = quarter(self, 83, "b0b0b0b0-05", "total")
        self.assertEqual(self.statuses(), {"matched": 83})
        self.assertLessEqual(len(drv.batch_calls), 12, drv.batch_calls)

    def test_f3_6_only_casa_ends_a_batch(self):
        """No `end-batch`: every batch but the last runs to Casa's cut, and each reports."""
        drv, units = quarter(self, 83, "b0b0b0b0-06", None)
        self.assertNotIn("end-batch", [u["unit"] for u in units])  # removed-name: asserted absent
        self.assertEqual(drv.cuts, len(drv.batch_calls) - 1)
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertTrue(all("max_calls" not in u for u in units))  # removed-name: asserted absent
        self.assertEqual(self.statuses(), {"matched": 83})

    def test_a_payment_cut_mid_way_is_redone_and_decided_once(self):
        drv = JobDriver(self, payments=3)
        for i in range(3):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        real, cut = drv._file_vendor, []

        def cut_after_filing(token, vendor, refs):
            out = real(token, vendor, refs)
            if not cut:
                cut.append(token)
                raise CasaCut()                   # filed, never decided: Casa's turn limit
            return out
        drv._file_vendor = cut_after_filing
        drv.casa_cut = 80
        units = drv.run_job("b0b0b0b0-07")
        self.assertEqual(drv.cuts, 1)
        first = next(u for u in units if u["unit"] == "payment")
        after = [u for u in units if u["pass_token"] != first["pass_token"]]
        self.assertEqual([u["unit"] for u in after[:2]], ["report", "payment"])
        self.assertEqual(after[1]["pid"], first["pid"])                 # the same payment
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 3)
        self.assertEqual(self.statuses(), {"matched": 3})
        self.assertEqual(drv.tool_calls.get("decide"), 3)                # decided once each

    def test_a_cut_inside_a_mirror_chunk_repeats_at_most_eight_calls(self):
        import loop
        self.assertEqual(loop.MIRROR_CALLS, 8)
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        real, cuts, seen = drv._mirror, [], set()

        def cut_mid_chunk(u, token):
            # three different chunks (a chunk of 8 never straddles two 80-call batch ends)
            first = u["calls"][0]["n"] if u["calls"] else None
            if len(cuts) < 3 and len(u["calls"]) > 4 and first not in seen:
                seen.add(first)
                for c in u["calls"][:5]:              # five bank writes, then Casa cuts
                    drv._bank(c["tool"], **c["args"])
                cuts.append(len(u["calls"]))
                raise CasaCut()
            return real(u, token)
        drv._mirror = cut_mid_chunk
        drv.casa_cut = 80
        units = drv.run_job("b0b0b0b0-08")
        self.assertEqual(len(cuts), 3)
        self.assertTrue(all(len(u["calls"]) <= 8 for u in units if u["unit"] == "mirror"))
        notes = collections.Counter(a for t, a in drv.bank_log if t == "add_note")
        repeated = sum(n - 1 for n in notes.values())
        self.assertLessEqual(repeated, 8 * len(cuts))
        again = drv.run_job("b0b0b0b0-09", started_by="scheduled")      # nothing left owed
        self.assertEqual(sum(len(u["calls"]) for u in again if u["unit"] == "mirror"), 0)

    def test_a_report_cut_before_it_reached_casa_is_handed_again(self):
        """g1 (Terra S1): the `report` unit is handed, then the batch ends before its
        report_job_progress — twice in a row. It never reached Casa: the next claim is
        handed the report again, and the run finishes with every later batch reported."""
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        drv.skip_attached_reports = True
        drv.cut_reports = 2
        drv.casa_cut = 80
        drv.run_job("b0b0b0b0-0a")
        self.assertEqual(drv.batch_reported[:2], [False, False])
        self.assertTrue(all(drv.batch_reported[2:]), drv.batch_reported)
        self.assertEqual(self.statuses(), {"matched": 60})


class OneDeliveryAfterACut(StoreCase):
    """g1 (Astra S1), OPERATOR RULING B: Casa #1312 dedupes a re-posted message by its key.
    The end message is deposited, Casa cuts the batch before mark_rendering_delivered, the
    next claim re-hands it — and the operator gets it once."""

    def test_the_end_message_posted_cut_and_rehanded_is_delivered_once(self):
        from tests.fakebroker import FakeBroker
        import posting
        self.bind()
        drv = JobDriver(self, payments=3)
        for i in range(3):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        real, cut = drv._view, []

        def cut_after_the_post(u, token):
            if not cut:
                cut.append(u["render_id"])
                drv._tool("show_view", {"render_id": u["render_id"]})
                raise CasaCut()                  # deposited; mark_rendering_delivered lost
            return real(u, token)
        drv._view = cut_after_the_post
        drv.casa_cut = 80
        with FakeBroker() as b:
            b.honour_keys = True
            drv.broker = b
            units = drv.run_job("b0b0b0b0-0b")
        views = [u for u in units if u["unit"] == "view"]
        self.assertEqual([u["render_id"] for u in views], cut * 2)       # re-handed once
        keyed = [d for d in b.deposits if d.get("key") == f"view:{cut[0]}"]
        self.assertEqual(len(keyed), 2)                                  # deposited twice
        self.assertEqual(len([d for d in b.sent if d.get("key") == keyed[0]["key"]]), 1)
        self.assertIsNotNone(self.conn.execute("SELECT delivered_at FROM renders WHERE"
                                               " render_id=?", (cut[0],)).fetchone()[0])
        self.assertEqual(posting.delivery_key("results", ["r1", "r22"]), "results:r1.r22")
