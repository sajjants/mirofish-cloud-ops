import unittest

from ops.plan import low_high, make_plan, prompt_growth_per_round, unmeasured_model
from ops.schedule import expected_turns, scale_schedule
from ops.tests.helpers import limits, llm_config, prices

LLM = llm_config("https://generativelanguage.googleapis.com/v1beta/openai/")


class EstimateTests(unittest.TestCase):
    def test_high_is_above_low_and_each_group_uses_its_own_model(self):
        lim = limits()
        estimate = low_high(unmeasured_model(lim), lim, LLM, prices(), rounds=20)
        self.assertGreater(estimate["high"]["spend"], estimate["low"]["spend"])
        self.assertGreater(estimate["high"]["calls"], estimate["low"]["calls"])
        self.assertEqual(estimate["low"]["groups"]["simulation"]["model"], "sim-m")
        self.assertEqual(estimate["low"]["groups"]["report"]["model"], "setup-m")

    def test_prompt_growth_from_the_two_halves_of_the_rounds(self):
        rows = [{"ts": t, "outcome": "ok", "prompt_tokens": 1000 + 100 * t} for t in range(10)]
        self.assertAlmostEqual(prompt_growth_per_round(rows, 10), 100.0)
        self.assertEqual(prompt_growth_per_round(rows[:2], 10), 0.0)

    def test_scaled_schedules_follow_upstream_selection(self):
        schedule = {"minutes_per_round": 60, "agents_per_hour_min": 1, "agents_per_hour_max": 50,
                    "peak_hours": [], "peak_activity_multiplier": 1, "off_peak_hours": [],
                    "off_peak_activity_multiplier": 1, "agents": [{"active_hours": list(range(24)),
                                                                   "activity_level": 1.0}] * 4}
        self.assertLess(expected_turns(schedule, 5), expected_turns(scale_schedule(schedule, 12), 5))


class PlanTests(unittest.TestCase):
    def test_generous_caps_plan_the_wanted_runs(self):
        lim = limits(caps__max_spend=1000.0, caps__max_llm_calls=10**6, caps__wall_clock_minutes=10**6)
        plan = make_plan(lim, LLM, prices(), unmeasured_model(lim), ["Q1", "Q2"], 0, batch_id="b1")
        self.assertTrue(plan["feasible"])
        self.assertEqual((plan["runs_per_question"], plan["rounds_per_run"]), (2, 30))
        self.assertEqual(plan["cuts"], [])
        self.assertEqual(len(plan["runs"]), 4)
        self.assertIn("unmeasured", plan["warnings"][0])

    def test_rounds_are_cut_before_runs(self):
        lim = limits(caps__max_spend=1000.0, caps__max_llm_calls=10**6, caps__wall_clock_minutes=10**6)
        model = unmeasured_model(lim)
        per_round_full = make_plan(lim, LLM, prices(), model, ["Q1"], 0)["estimate"]["high"]["spend"]
        tight = limits(caps__max_spend=round(per_round_full * 0.85, 4), caps__max_llm_calls=10**6,
                       caps__wall_clock_minutes=10**6, pilot__max_spend=0.01)
        plan = make_plan(tight, LLM, prices(), model, ["Q1"], 0)
        self.assertTrue(plan["cuts"][0].startswith("rounds per run 30 ->"))
        self.assertEqual(plan["runs_per_question"], 2)
        self.assertLessEqual(plan["estimate"]["high"]["spend"], tight["caps"]["max_spend"])

    def test_spend_already_used_counts_and_infeasible_plans_say_so(self):
        lim = limits(caps__max_llm_calls=10**6, caps__wall_clock_minutes=10**6)
        plan = make_plan(lim, LLM, prices(), unmeasured_model(lim), ["Q1"], 3, used={"spend": 4.999})
        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["runs"], [])
        self.assertTrue(any("INFEASIBLE" in w for w in plan["warnings"]))


if __name__ == "__main__":
    unittest.main()
