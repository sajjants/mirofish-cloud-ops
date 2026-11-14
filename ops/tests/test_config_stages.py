import tempfile
import unittest
from pathlib import Path

from ops.config import ConfigError, StageModel, load_llm_config, parse_llm_config
from ops.paths import CONFIG_DIR
from ops.stages import STAGE_GROUP, group_for, model_for, route_request

BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def raw_config(setup="a", simulation="b", report="a", efforts=(None, "low", None)):
    names = ("setup", "simulation", "report")
    return {
        "provider": {"base_url": BASE},
        "stages": {n: {"model": m, "reasoning_effort": e} for n, m, e in zip(names, (setup, simulation, report), efforts)},
    }


class ConfigTests(unittest.TestCase):
    def test_example_config_loads_with_placeholders_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            example = (CONFIG_DIR / "llm.example.yaml").read_text(encoding="utf-8")
            (Path(tmp) / "llm.example.yaml").write_text(example, encoding="utf-8")
            config = load_llm_config(Path(tmp))
        self.assertEqual(config.base_url, BASE)
        self.assertEqual(config.placeholders(), ["setup", "simulation", "report"])

    def test_distinct_models_deduplicates_by_model_and_effort(self):
        config = parse_llm_config(raw_config())
        self.assertEqual([g for g, _ in config.distinct_models()], ["setup", "simulation"])
        same_model_other_effort = parse_llm_config(raw_config(report="a", efforts=(None, "low", "high")))
        self.assertEqual(len(same_model_other_effort.distinct_models()), 3)

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ConfigError):
            parse_llm_config(raw_config(efforts=("extreme", None, None)))
        bad_url = raw_config()
        bad_url["provider"]["base_url"] = "http://example.com/v1/"
        with self.assertRaises(ConfigError):
            parse_llm_config(bad_url)
        unknown = raw_config()
        unknown["stages"]["judge"] = {"model": "x"}
        with self.assertRaises(ConfigError):
            parse_llm_config(unknown)


class StageRoutingTests(unittest.TestCase):
    def test_every_pipeline_stage_has_a_group(self):
        for stage in ("ontology", "graph", "simulation", "prepare", "schedule", "rounds", "close_env",
                      "report", "interview"):
            self.assertIn(group_for(stage), ("setup", "simulation", "report"))
        self.assertEqual(STAGE_GROUP["rounds"], "simulation")
        with self.assertRaises(ValueError):
            group_for("unknown")

    def test_route_request_substitutes_model_and_effort_without_mutating(self):
        config = parse_llm_config(raw_config())
        original = {"model": "whatever-mirofish-sent", "messages": [{"role": "user", "content": "x"}]}
        routed = route_request(original, model_for("rounds", config))
        self.assertEqual((routed["model"], routed["reasoning_effort"]), ("b", "low"))
        self.assertEqual(original["model"], "whatever-mirofish-sent")
        self.assertNotIn("reasoning_effort", original)

    def test_no_effort_leaves_the_request_alone(self):
        routed = route_request({"model": "x", "reasoning_effort": "medium"}, StageModel("a", None))
        self.assertEqual(routed, {"model": "a", "reasoning_effort": "medium"})


if __name__ == "__main__":
    unittest.main()
