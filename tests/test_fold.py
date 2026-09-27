"""The fold, pinned on every trace the spec states with explicit sequence
numbers (rounds 14-20). A merge is simulated exactly as the store does it:
fold the union, append the retirements it newly produced as `retire`
entries with fresh sequence numbers, then fold again."""
import itertools
import unittest

from tests._base import TempEnv  # noqa: F401  (sys.path)
import fold as F  # noqa: E402


def pair(seq, mid, doc=None, author="operator"):
    return F.Entry(seq, "pair", author, mid, doc if doc is not None else mid)


def propose(seq, mid, doc=None, resolves=()):
    return F.Entry(seq, "propose", "auto", mid, doc if doc is not None else mid,
                   resolves=tuple(resolves))


def auto_pair(seq, mid, doc=None):
    return F.Entry(seq, "pair", "auto", mid, doc if doc is not None else mid)


def unpair(seq, mid):
    return F.Entry(seq, "unpair", "operator", mid)


def exempt(seq):
    return F.Entry(seq, "exempt", "operator")


def lift(seq):
    return F.Entry(seq, "lift", "operator")


class Store:
    """The store's merge discipline, in miniature: recorded retirements."""
    def __init__(self, occupied=lambda d, m: False):
        self.next = 1000
        self.occupied = occupied

    def settle(self, entries):
        entries = list(entries)
        while True:
            st = F.fold(entries, self.occupied)
            have = {(e.match_id, e.retire_activation, e.retire_to)
                    for e in entries if e.kind == "retire"}
            new = [r for r in st.produced if (r.match_id, r.activation, r.to) not in have]
            if not new:
                return entries, st
            for r in new:
                self.next += 1
                entries.append(F.Entry(self.next, "retire", "store", r.match_id,
                                       retire_activation=r.activation, retire_to=r.to,
                                       cause=r.cause))


def states(st):
    return {m: c.state for m, c in st.cands.items()}


class TestTransitions(unittest.TestCase):
    def test_operator_pair_then_unpair_ends_with_nothing_active(self):
        st = F.fold([pair(1, 10), unpair(2, 10)])
        self.assertEqual(states(st), {10: "rejected"})
        self.assertIsNone(st.operator_current())

    def test_exempt_then_lift_restores_nothing(self):
        st = F.fold([auto_pair(1, 10), exempt(2), lift(3)])
        self.assertEqual(states(st), {10: "rejected"})
        self.assertIsNone(st.exemption)

    def test_machine_write_under_exemption_is_rejected_and_recorded(self):
        st = F.fold([exempt(1), propose(2, 10)])
        self.assertEqual(states(st), {10: "rejected"})
        self.assertEqual([(r.match_id, r.to, r.cause) for r in st.produced],
                         [(10, "rejected", "exempt")])

    def test_machine_write_while_operator_current_lands_conflicted(self):
        st = F.fold([pair(1, 10), propose(2, 11)])
        self.assertEqual(states(st), {10: "matched", 11: "conflicted"})

    def test_machine_reproposal_of_the_operators_pairing_changes_nothing(self):
        st = F.fold([pair(1, 10), propose(2, 10), auto_pair(3, 10)])
        c = st.cands[10]
        self.assertEqual((c.state, c.author, c.activation), ("matched", "operator", 1))

    def test_an_operator_activation_is_checked_against_occupancy_too(self):
        st = F.fold([pair(5, 10, doc=99)], occupied=lambda d, m: d == 99)
        self.assertEqual(states(st), {10: "conflicted"})
        self.assertEqual([(r.match_id, r.to, r.cause) for r in st.produced],
                         [(10, "conflicted", "occupied")])

    def test_occupancy_retires_the_activation_conflicted(self):
        st = F.fold([auto_pair(5, 10, doc=99)], occupied=lambda d, m: d == 99)
        self.assertEqual(states(st), {10: "conflicted"})
        self.assertEqual(st.produced[0].cause, "occupied")

    def test_two_machine_candidates_collide(self):
        st = F.fold([auto_pair(1, 10), auto_pair(2, 11)])
        self.assertEqual(states(st), {10: "conflicted", 11: "conflicted"})

    def test_lone_conflicted_candidate_stays_conflicted(self):
        # round 18: A/B collide, operator confirms B, then unpairs B -> A stays conflicted
        st = F.fold([auto_pair(1, 10), auto_pair(2, 11), pair(3, 11), unpair(4, 11)])
        self.assertEqual(states(st), {10: "conflicted", 11: "rejected"})
        self.assertEqual(st.machine_set()[0].match_id, 10)

    def test_resolves_rejects_named_ids_whatever_their_state(self):
        st = F.fold([auto_pair(1, 10), auto_pair(2, 11), propose(3, 12, resolves=[10, 11])])
        self.assertEqual(states(st), {10: "rejected", 11: "rejected", 12: "proposed"})

    def test_retire_is_bound_to_the_activation(self):
        entries = [propose(10, 7), F.Entry(15, "retire", "store", 7, retire_activation=10,
                                           retire_to="rejected", cause="exempt"),
                   pair(30, 7)]
        self.assertEqual(states(F.fold(entries)), {7: "matched"})

    def test_retire_never_moves_rejected_back_to_conflicted(self):
        entries = [auto_pair(1, 7), unpair(2, 7),
                   F.Entry(3, "retire", "store", 7, retire_activation=1,
                           retire_to="conflicted", cause="collision")]
        self.assertEqual(states(F.fold(entries)), {7: "rejected"})

    def test_operator_pair_clears_only_an_older_exemption(self):
        st = F.fold([exempt(10), pair(20, 5)])
        self.assertIsNone(st.exemption)
        self.assertEqual(states(st), {5: "matched"})
        st = F.fold([pair(10, 5), exempt(20)])
        self.assertEqual(st.exemption, 20)
        self.assertEqual(states(st), {5: "rejected"})

    def test_E10_P15_E20(self):
        st = F.fold([exempt(10), pair(15, 5), exempt(20)])
        self.assertEqual((st.exemption, states(st)), (20, {5: "rejected"}))

    def test_fold_sorts_by_sequence(self):
        es = [pair(3, 5), exempt(1), auto_pair(2, 6)]
        for perm in itertools.permutations(es):
            self.assertEqual(states(F.fold(list(perm))), states(F.fold(es)))


class TestSpecTraces(unittest.TestCase):
    def test_round14_machine_A_merged_with_pair_unpair_B(self):
        _, st = Store().settle([auto_pair(5, 1), pair(10, 2), unpair(20, 2)])
        self.assertEqual(states(st), {1: "conflicted", 2: "rejected"})

    def test_round17_three_machine_candidates_every_merge_order(self):
        lineages = {"A": [auto_pair(1, 1)], "B": [auto_pair(2, 2)], "C": [propose(3, 3)]}
        results = set()
        for order in itertools.permutations("ABC"):
            s = Store()
            merged = []
            for name in order:
                merged, st = s.settle(merged + lineages[name])
            results.add(tuple(sorted(states(st).items())))
        self.assertEqual(results, {((1, "conflicted"), (2, "conflicted"), (3, "conflicted"))})

    def test_round17_confirm_one_conflicted_leaves_the_others(self):
        s = Store()
        merged, _ = s.settle([auto_pair(1, 1), auto_pair(2, 2), propose(3, 3)])
        _, st = s.settle(merged + [pair(50, 2)])
        self.assertEqual(states(st), {1: "conflicted", 2: "matched", 3: "conflicted"})

    def test_round17_exemption_merged_with_later_pair_then_third_exemption(self):
        s = Store()
        merged, st = s.settle([exempt(10)] + [pair(20, 5)])
        self.assertEqual(states(st), {5: "matched"})
        self.assertFalse([e for e in merged if e.kind == "lift"])   # derived clear, no entry
        _, st = s.settle(merged + [exempt(21)])
        self.assertEqual((st.exemption, states(st)), (21, {5: "rejected"}))

    def test_round18_machine_A_exempt_E10_operator_P20_every_parenthesisation(self):
        parts = {"A": [auto_pair(5, 1)], "E": [exempt(10)], "P": [pair(20, 2)]}
        results = set()
        for order in itertools.permutations("AEP"):
            s = Store()
            merged = []
            for name in order:
                merged, st = s.settle(merged + parts[name])
            results.add((st.exemption, tuple(sorted(states(st).items()))))
        self.assertEqual(results, {(None, ((1, "rejected"), (2, "matched")))})

    def test_round19_collided_B_stays_conflicted_after_its_invoice_moved(self):
        # B's document 22 is now active on payment D (another lineage).
        s = Store(occupied=lambda d, m: d == 22)
        merged, st = s.settle([auto_pair(10, 1, doc=21), auto_pair(30, 2, doc=22)])
        merged, st = s.settle(merged + [exempt(20), lift(25)])
        self.assertEqual(states(st)[2], "conflicted")

    def test_round19_normalization_runs_inside_the_transition(self):
        _, st = Store().settle([auto_pair(10, 1)] + [auto_pair(20, 2), unpair(30, 2)])
        self.assertEqual(states(st), {1: "conflicted", 2: "rejected"})

    def test_round19_resolves_replayed_after_a_merge(self):
        _, st = Store().settle([auto_pair(10, 1), pair(15, 2), propose(40, 3, resolves=[1, 2])])
        self.assertEqual(states(st), {1: "rejected", 2: "rejected", 3: "proposed"})

    def test_round19_lift_meeting_no_exemption_is_a_noop(self):
        st = F.fold([auto_pair(1, 1), lift(2)])
        self.assertEqual(states(st), {1: "matched"})

    def test_round20_confirmation_survives_the_retirement_of_the_proposal(self):
        s = Store()
        merged, st = s.settle([propose(10, 7), pair(30, 7)] + [exempt(20)])
        self.assertEqual(states(st), {7: "matched"})
        _, again = s.settle(merged)                  # re-fold changes nothing
        self.assertEqual(states(again), {7: "matched"})

    def test_round20_three_predecessors_every_order(self):
        parts = {"A": [auto_pair(10, 1)], "E": [exempt(20), lift(25)],
                 "C": [propose(30, 3), pair(62, 3)]}
        results = set()
        for order in itertools.permutations("AEC"):
            s = Store()
            merged = []
            for name in order:
                merged, st = s.settle(merged + parts[name])
            results.add(states(st)[3])
        self.assertEqual(results, {"matched"})

    def test_exempt_merge_rejects_an_incoming_machine_pairing(self):
        _, st = Store().settle([exempt(10)] + [auto_pair(20, 1)])
        self.assertEqual((st.exemption, states(st)), (10, {1: "rejected"}))


class TestInvariants(unittest.TestCase):
    def test_24_orders_never_break_the_invariants(self):
        """Four decisions — machine A and operator pair B on lineage X,
        exempt then lift on lineage Y — in every sequence order where the
        lift follows the exemption (write-time precondition). Whatever the
        order and whichever lineage is settled first: at most one active
        pairing; never an active pairing under a standing exemption; the
        merged log holds exactly the one lift appended. The two merge
        directions are NOT asserted equal: they are different committed
        histories, and spec §Match records says merge order is history."""
        names = ["A", "B", "E", "L"]
        for seqs in itertools.permutations([1, 2, 3, 4]):
            seq = dict(zip(names, seqs))
            if seq["L"] < seq["E"]:
                continue
            x = [auto_pair(seq["A"], 1), pair(seq["B"], 2)]
            y = [exempt(seq["E"]), lift(seq["L"])]
            for first, second in ((x, y), (y, x)):
                s = Store()
                merged, _ = s.settle(first)
                merged, st = s.settle(merged + second)
                self.assertLessEqual(len(st.active()), 1, seq)
                self.assertFalse(st.exemption is not None and st.active(), seq)
                self.assertEqual(sum(e.kind == "lift" for e in merged), 1, seq)
                again = F.fold(merged)                # the same committed log folds the same
                self.assertEqual(states(again), states(st), seq)


if __name__ == "__main__":
    unittest.main()
