"""No call budget: the server hands out work until the run completes, and a session that
loses its pass_token (a context summary, a Casa restart) claims again and carries on — a
payment interrupted mid-way is redone. A model sending running totals changes nothing. A
mirror unit is a chunk of MIRROR_CALLS bank-feed calls: an interruption inside one may repeat
up to that many calls (tag calls are no-ops; a note call adds an identical line)."""
import collections

from tests._base import StoreCase
from tests.sim_job import Interrupted, JobDriver


def quarter(test, n, job_id, mode):
    drv = JobDriver(test, payments=n)
    for i in range(n):
        drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
    drv.calls_mode = mode
    drv.interrupt_at = 80                         # the session loses its token every 80 calls
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
        self.assertLessEqual(len(drv.segment_calls), 12, drv.segment_calls)

    def test_f3_6_no_unit_ends_a_segment(self):
        """No `end-batch`: every segment but the last runs until the session is interrupted."""
        drv, units = quarter(self, 83, "b0b0b0b0-06", None)
        self.assertNotIn("end-batch", [u["unit"] for u in units])  # removed-name: asserted absent
        self.assertEqual(drv.interruptions, len(drv.segment_calls) - 1)
        self.assertTrue(all("max_calls" not in u for u in units))  # removed-name: asserted absent
        self.assertEqual(self.statuses(), {"matched": 83})

    def test_a_payment_interrupted_mid_way_is_redone_and_decided_once(self):
        drv = JobDriver(self, payments=3)
        for i in range(3):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        real, cut = drv._file_vendor, []

        def cut_after_filing(token, vendor, refs):
            out = real(token, vendor, refs)
            if not cut:
                cut.append(token)
                raise Interrupted()                   # filed, never decided
            return out
        drv._file_vendor = cut_after_filing
        drv.interrupt_at = 80
        units = drv.run_job("b0b0b0b0-07")
        self.assertEqual(drv.interruptions, 1)
        first = next(u for u in units if u["unit"] == "payment")
        after = [u for u in units if u["pass_token"] != first["pass_token"]]
        self.assertEqual(after[0]["unit"], "payment")
        self.assertEqual(after[0]["pid"], first["pid"])                 # the same payment
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 3)
        self.assertEqual(self.statuses(), {"matched": 3})
        self.assertEqual(drv.tool_calls.get("decide"), 3)                # decided once each

    def test_an_interruption_inside_a_mirror_chunk_repeats_at_most_eight_calls(self):
        import loop
        self.assertEqual(loop.MIRROR_CALLS, 8)
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        real, cuts, seen = drv._mirror, [], set()

        def cut_mid_chunk(u, token):
            # three different chunks (a chunk of 8 never straddles two 80-call segment ends)
            first = u["calls"][0]["n"] if u["calls"] else None
            if len(cuts) < 3 and len(u["calls"]) > 4 and first not in seen:
                seen.add(first)
                for c in u["calls"][:5]:              # five bank writes, then interrupted
                    drv._bank(c["tool"], **c["args"])
                cuts.append(len(u["calls"]))
                raise Interrupted()
            return real(u, token)
        drv._mirror = cut_mid_chunk
        drv.interrupt_at = 80
        units = drv.run_job("b0b0b0b0-08")
        self.assertEqual(len(cuts), 3)
        self.assertTrue(all(len(u["calls"]) <= 8 for u in units if u["unit"] == "mirror"))
        notes = collections.Counter(a for t, a in drv.bank_log if t == "add_note")
        repeated = sum(n - 1 for n in notes.values())
        self.assertLessEqual(repeated, 8 * len(cuts))
        again = drv.run_job("b0b0b0b0-09", started_by="scheduled")      # nothing left owed
        self.assertEqual(sum(len(u["calls"]) for u in again if u["unit"] == "mirror"), 0)


class OneDeliveryAfterAnInterruption(StoreCase):
    """Casa #1312 dedupes a re-posted message by its key.
    The end message is deposited, the session is interrupted before
    mark_rendering_delivered, the next claim re-hands it — and the operator gets it once."""

    def test_the_end_message_posted_interrupted_and_rehanded_is_delivered_once(self):
        from tests.fakebroker import FakeBroker
        self.bind()
        drv = JobDriver(self, payments=3)
        # #93: one stays missing, so the end message has something to tap (a card, `view`)
        for i in range(2):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        real, cut = drv._view, []

        def cut_after_the_post(u, token):
            if not cut:
                cut.append(u["render_id"])
                drv._tool("show_view", {"render_id": u["render_id"]})
                raise Interrupted()                  # deposited; mark_rendering_delivered lost
            return real(u, token)
        drv._view = cut_after_the_post
        drv.interrupt_at = 80
        with FakeBroker() as b:
            b.honour_keys = True
            drv.broker = b
            units = drv.run_job("b0b0b0b0-0b")
        views = [u for u in units if u["unit"] == "view"]
        self.assertEqual([u["render_id"] for u in views], cut * 2)       # re-handed once
        keyed = [d for d in b.deposits if (d.get("key") or "").startswith("view:")
                 and d["key"].endswith(f":{cut[0]}")]
        self.assertEqual(len(keyed), 2)                                  # deposited twice
        self.assertEqual(len([d for d in b.sent if d.get("key") == keyed[0]["key"]]), 1)
        self.assertIsNotNone(self.conn.execute("SELECT delivered_at FROM renders WHERE"
                                               " render_id=?", (cut[0],)).fetchone()[0])

    def test_a_reset_store_never_suppresses_a_new_message_under_an_old_key(self):
        """Render ids restart after reset_store — the key names the store
        (meta.store_id, new after every reset), so a new message is sent, never deduped."""
        from tests.fakebroker import FakeBroker
        import binding, posting
        self.bind()
        with FakeBroker() as b:
            b.honour_keys = True
            # #93: a payment missing, so the status view has something to act on (a card)
            self.seed_payments([{"counterparty": "Adobe"}])
            posting.show_view(self.conn, view="status", quarter="2026-Q3")
            binding.reset_store(self.conn)
            self.seed_payments([{"counterparty": "Adobe"}])
            posting.show_view(self.conn, view="status", quarter="2026-Q3")
        self.assertEqual(len(b.deposits), 2)
        self.assertEqual(len(b.sent), 2, [d.get("key") for d in b.deposits])
        self.assertNotEqual(b.deposits[0]["key"], b.deposits[1]["key"])
