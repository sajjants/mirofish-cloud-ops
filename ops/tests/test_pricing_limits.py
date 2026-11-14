import copy
import unittest

import yaml

from ops.config import ConfigError
from ops.limits import validate_limits
from ops.paths import CONFIG_DIR
from ops.pricing import ModelPrice, billable_tokens, cost, parse_prices, worst_case_cost


def example_limits() -> dict:
    return yaml.safe_load((CONFIG_DIR / "limits.example.yaml").read_text(encoding="utf-8"))


def prices_raw(**model) -> dict:
    return {"currency": "USD", "source_url": "https://example.invalid/pricing", "checked_on": "2026-11-15",
            "models": {"m": model or {"input_per_million": 1.0, "output_per_million": 4.0}}}


class PricingTests(unittest.TestCase):
    def test_output_is_billed_from_total_minus_prompt_so_thinking_counts(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1400}
        self.assertEqual(billable_tokens(usage), (1000, 400))
        self.assertAlmostEqual(cost(ModelPrice(1.0, 4.0), usage), (1000 * 1.0 + 400 * 4.0) / 1e6)

    def test_without_total_reasoning_tokens_are_added(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 20}
        self.assertEqual(billable_tokens(usage), (10, 25))

    def test_long_context_tier(self):
        price = ModelPrice(1.0, 2.0, 200_000, 2.0, 4.0)
        self.assertEqual(price.rates(100), (1.0, 2.0))
        self.assertEqual(price.rates(300_000), (2.0, 4.0))

    def test_worst_case_errs_high(self):
        price = ModelPrice(1.0, 1.0)
        self.assertGreater(worst_case_cost(price, 3000, None), cost(price, {"prompt_tokens": 750, "total_tokens": 1000}))

    def test_missing_prices_and_provenance_are_refused(self):
        with self.assertRaises(ConfigError):
            parse_prices(prices_raw(input_per_million=None, output_per_million=1.0))
        raw = prices_raw()
        raw["checked_on"] = ""
        with self.assertRaises(ConfigError):
            parse_prices(raw)
        self.assertEqual(parse_prices(prices_raw()).missing(["m", "other"]), ["other"])


class LimitsTests(unittest.TestCase):
    def test_example_is_valid(self):
        limits = validate_limits(example_limits())
        self.assertEqual(limits["rounds"]["max_rounds"], 40)
        self.assertEqual(limits["pilot"]["max_agents"], 12)

    def test_rounds_ceiling_is_40(self):
        raw = example_limits()
        raw["rounds"]["max_rounds"] = 41
        with self.assertRaises(ConfigError):
            validate_limits(raw)

    def test_inconsistent_values_are_refused(self):
        for section, key, value in (("rounds", "real_min", 35), ("pilot", "max_spend", 50.0),
                                    ("pilot", "max_agents", 99), ("simulation", "platform", "reddit"),
                                    ("caps", "max_spend", True), ("estimates", "high_factor", 0.5)):
            raw = copy.deepcopy(example_limits())
            raw[section][key] = value
            with self.assertRaises(ConfigError, msg=f"{section}.{key}={value}"):
                validate_limits(raw)


if __name__ == "__main__":
    unittest.main()
