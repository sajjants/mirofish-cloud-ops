import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ops import doctor, keys, paths
from ops.config import RetrySettings
from ops.provider import ProviderClient
from ops.tests.fake_provider import FAKE_GOOGLE_KEY, FakeProvider, completion

GOOD_ANSWERS = {
    "entity_types": {"entity_types": [{"name": "Council", "description": "city body"},
                                      {"name": "Union", "description": "workers"}]},
    "social-media persona": {"bio": "b", "persona": "p", "age": 67, "gender": "female", "mbti": "ISFJ"},
    "timing": {"total_simulation_hours": 72, "minutes_per_round": 60, "peak_hours": [9, 19]},
    "Mayor Dana Ortiz": {"entities": [{"name": "Dana Ortiz", "type": "Person"},
                                      {"name": "Riverton Council", "type": "Org"}],
                         "relations": [{"source": "Dana Ortiz", "target": "Riverton Council",
                                        "type": "SPEAKS_FOR", "fact": "announced the cut"}]},
    "Classify the author's stance": {"stance": "opposing", "confidence": 0.8, "reason": "r"},
}
TOOL_CALL = [{"id": "c1", "type": "function",
              "function": {"name": "create_post", "arguments": json.dumps({"content": "Keep it open!"})}}]


def good_responder(models=("setup-model", "sim-model")):
    def respond(method, path, body):
        if method == "GET":
            name = path.rsplit("/", 1)[-1]
            return (200, {}, {"id": name}) if name in models else (404, {}, {"error": {"message": "not found"}})
        if body.get("tools"):
            return 200, {}, completion(None, finish_reason="tool_calls", tool_calls=TOOL_CALL)
        prompt = body["messages"][-1]["content"]
        for marker, answer in GOOD_ANSWERS.items():
            if marker in prompt:
                return 200, {}, completion(json.dumps(answer))
        return 200, {}, completion("{}")
    return respond


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name) / "config"
        self.config_dir.mkdir()
        self.state_dir = Path(self.tmp.name) / "state"
        patches = [
            mock.patch.object(paths, "STATE_DIR", self.state_dir),
            mock.patch.object(keys, "_from_keyring", return_value=None),
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": FAKE_GOOGLE_KEY}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.factory_calls = 0

    def write_config(self, base_url, setup="setup-model", simulation="sim-model", report="setup-model"):
        stages = {"setup": {"model": setup, "reasoning_effort": None},
                  "simulation": {"model": simulation, "reasoning_effort": "low"},
                  "report": {"model": report, "reasoning_effort": None}}
        config = {"provider": {"base_url": base_url, "api_key_env": "GEMINI_API_KEY"}, "stages": stages}
        (self.config_dir / "llm.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    def factory(self, config, key, stats):
        self.factory_calls += 1
        return ProviderClient(config.base_url, key.value, retry=RetrySettings(2, 0.01, 1.0), timeout=5,
                              stats=stats, sleep=lambda _s: None)

    def run_doctor(self):
        out = io.StringIO()
        code = doctor.run(out=out, client_factory=self.factory, config_dir=self.config_dir)
        return code, out.getvalue()

    def test_placeholders_fail_before_any_network_call(self):
        self.write_config("https://generativelanguage.googleapis.com/v1beta/openai/", setup="SET_ME")
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("placeholders", output)
        self.assertEqual(self.factory_calls, 0)

    def test_missing_key_fails_before_any_network_call(self):
        self.write_config("https://generativelanguage.googleapis.com/v1beta/openai/")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("no API key", output)
        self.assertEqual(self.factory_calls, 0)

    def test_healthy_setup_checks_each_distinct_model_and_writes_a_content_free_report(self):
        with FakeProvider(good_responder()) as fake:
            self.write_config(fake.base_url)
            code, output = self.run_doctor()
            requests = list(fake.requests)
        self.assertEqual(code, 0, output)
        self.assertIn("doctor passed", output)
        self.assertNotIn(FAKE_GOOGLE_KEY, output)
        looked_up = sorted(r["path"].rsplit("/", 1)[-1] for r in requests if r["method"] == "GET")
        self.assertEqual(looked_up, ["setup-model", "sim-model"])
        tool_requests = [r for r in requests if r["body"].get("tools")]
        self.assertEqual([r["body"]["model"] for r in tool_requests], ["sim-model"])
        self.assertEqual(tool_requests[0]["body"]["reasoning_effort"], "low")
        json_requests = [r for r in requests if r["body"].get("response_format")]
        self.assertEqual(len(json_requests), 10)

        report_file = self.state_dir / "doctor.json"
        text = report_file.read_text(encoding="utf-8")
        report = json.loads(text)
        self.assertEqual([m["json_passed"] for m in report["models"]], [5, 5])
        self.assertEqual(report["models"][1]["stage_groups"], ["simulation"])
        self.assertEqual(report["models"][0]["stage_groups"], ["setup", "report"])
        for leaked in (FAKE_GOOGLE_KEY, "Riverton", "Keep it open"):
            self.assertNotIn(leaked, text)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(report_file.stat().st_mode), 0o600)

    def test_rejected_key_stops_the_checks(self):
        with FakeProvider(lambda m, p, b: (401, {}, {"error": {"message": "API key not valid"}})) as fake:
            self.write_config(fake.base_url)
            code, output = self.run_doctor()
            requests = list(fake.requests)
        self.assertEqual(code, 1)
        self.assertIn("API key was rejected", output)
        self.assertEqual(len(requests), 1)

    def test_unknown_model_and_bad_json_fail(self):
        def respond(method, path, body):
            if method == "GET":
                return good_responder(models=("setup-model",))(method, path, body)
            return 200, {}, completion("Sure! Here is some prose instead of JSON.")
        with FakeProvider(respond) as fake:
            self.write_config(fake.base_url)
            code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("model 'sim-model' not found", output)
        self.assertIn("JSON mode: only 0/5", output)

    def test_rate_limits_trigger_the_low_quota_warning(self):
        inner = good_responder()
        state = {"first": True}

        def respond(method, path, body):
            if method == "POST" and state["first"]:
                state["first"] = False
                return 429, {"Retry-After": "0"}, {"error": {"message": "quota"}}
            return inner(method, path, body)
        with FakeProvider(respond) as fake:
            self.write_config(fake.base_url)
            code, output = self.run_doctor()
        self.assertEqual(code, 0, output)
        self.assertIn("low-quota key", output)
        self.assertIn("passed with warnings", output)

    def test_missing_usage_fails_because_spend_cannot_be_metered(self):
        inner = good_responder()

        def respond(method, path, body):
            status, headers, payload = inner(method, path, body)
            if isinstance(payload, dict):
                payload.pop("usage", None)
            return status, headers, payload
        with FakeProvider(respond) as fake:
            self.write_config(fake.base_url)
            code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("token usage missing", output)


if __name__ == "__main__":
    unittest.main()
