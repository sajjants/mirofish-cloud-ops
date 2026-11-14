import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ops.budget import LEAK_TRIP, Budget


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "b1"

    def budget(self, max_spend=1.0, calls=100, run_cap=None):
        budget = Budget("b1", self.dir)
        budget.set_caps(max_spend, calls, "r1", run_cap)
        return budget

    def spend(self, budget, amount, reservation=0.01, run="r1"):
        self.assertIsNone(budget.admit(run, reservation))
        budget.settle(run, reservation, amount, {"stage": "rounds", "attempts": 1, "prompt_tokens": 10})

    def test_warns_once_at_90_percent_and_trips_at_100(self):
        budget = self.budget()
        self.spend(budget, 0.5)
        self.assertEqual(budget.state["warnings"], [])
        self.spend(budget, 0.41)
        self.spend(budget, 0.01)
        self.assertEqual([w["limit"] for w in budget.state["warnings"]], ["max_spend"])
        self.assertIsNone(budget.tripped())
        self.spend(budget, 0.1)
        self.assertEqual(budget.tripped(), "max_spend")
        self.assertEqual(budget.admit("r1", 0.0), "max_spend")

    def test_admission_counts_calls_in_flight(self):
        budget = self.budget(max_spend=1.0)
        self.assertIsNone(budget.admit("r1", 0.6))
        self.assertEqual(budget.admit("r1", 0.6), "max_spend")
        self.assertEqual(budget.tripped(), "max_spend")

    def test_call_cap_and_pilot_cap(self):
        budget = self.budget(calls=2)
        self.spend(budget, 0.0)
        self.spend(budget, 0.0)
        self.assertEqual(budget.tripped(), "max_llm_calls")
        pilot = Budget("b2", Path(self.tmp.name) / "b2")
        pilot.set_caps(10.0, 100, "pilot", 0.10)
        self.assertEqual(pilot.admit("pilot", 0.2), "pilot.max_spend")

    def test_raising_a_cap_clears_the_trip_but_a_leak_trip_never_clears(self):
        budget = self.budget(max_spend=0.1)
        self.spend(budget, 0.1)
        self.assertEqual(budget.tripped(), "max_spend")
        budget.set_caps(1.0, 100, "r1")
        self.assertIsNone(budget.tripped())
        budget.trip(LEAK_TRIP)
        budget.set_caps(100.0, 10_000, "r1")
        self.assertEqual(budget.tripped(), LEAK_TRIP)

    def test_totals_persist_and_the_ledger_holds_no_text(self):
        budget = self.budget()
        self.spend(budget, 0.25)
        reloaded = Budget("b1", self.dir)
        self.assertAlmostEqual(reloaded.state["spend"], 0.25)
        self.assertEqual(reloaded.state["calls"], 1)
        rows = reloaded.ledger("r1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]) & {"messages", "content", "prompt"}, set())
        if os.name != "nt":
            for path in (self.dir / "ledger.jsonl", self.dir / "budget.json"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        json.loads((self.dir / "budget.json").read_text())


if __name__ == "__main__":
    unittest.main()
