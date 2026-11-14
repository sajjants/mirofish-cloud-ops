import json
import unittest

from ops.budget import Budget
from ops.tests.fake_provider import FAKE_GOOGLE_KEY, FakeProvider, completion
from ops.tests.helpers import SESSION, RelayFixture, TempState, chat, control

CAPS = {"max_spend": 1.0, "max_llm_calls": 100}


class RelayTests(unittest.TestCase):
    def setUp(self):
        TempState(self)
        self.fake = FakeProvider(lambda m, p, b: (200, {}, completion("ok", usage={
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1300})))
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)
        self.relay = RelayFixture(self, self.fake.base_url)

    def activate(self, stage="rounds", caps=CAPS, **extra):
        return control(self.relay.url, "activate", {"batch_id": "b1", "run_id": "b1_pilot", "kind": "pilot",
                                                    "stage": stage, "attempt": 1, "caps": caps, **extra})

    def test_wrong_session_token_is_refused(self):
        status, headers, _ = chat(self.relay.url, token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(self.fake.requests, [])

    def test_calls_without_an_active_run_are_refused(self):
        status, _, body = chat(self.relay.url)
        self.assertEqual((status, body["error"]["code"]), (409, "no_active_run"))
        self.assertEqual(self.fake.requests, [])

    def test_streaming_is_refused(self):
        self.activate()
        status, _, body = chat(self.relay.url, stream=True)
        self.assertEqual((status, body["error"]["code"]), (400, "stream_not_supported"))

    def test_leak_gate_refuses_and_stops_the_batch(self):
        self.activate()
        status, _, body = chat(self.relay.url, "a message about zebulon")
        self.assertEqual((status, body["error"]["code"]), (400, "leak_gate"))
        self.assertNotIn("zebulon", json.dumps(body).lower())
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Budget("b1").tripped(), "leak_gate")
        status, _, _ = self.activate()
        self.assertEqual(status, 409)

    def test_routes_by_stage_swaps_the_key_and_meters_spend(self):
        self.activate(stage="rounds")
        status, _, body = chat(self.relay.url, "agent turn")
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "ok")
        self.activate(stage="report")
        chat(self.relay.url, "report", response_format={"type": "json_object"})
        sent = self.fake.chat_requests()
        self.assertEqual([(r["body"]["model"], r["body"]["reasoning_effort"]) for r in sent],
                         [("sim-m", "low"), ("setup-m", "medium")])
        self.assertEqual({r["authorization"] for r in sent}, {f"Bearer {FAKE_GOOGLE_KEY}"})
        rows = Budget("b1").ledger("b1_pilot")
        self.assertEqual([r["group"] for r in rows], ["simulation", "report"])
        self.assertEqual(rows[0]["output_tokens"], 300)
        self.assertAlmostEqual(rows[0]["cost"], (1000 * 0.5 + 300 * 1.0) / 1e6)
        self.assertAlmostEqual(rows[1]["cost"], (1000 * 1.0 + 300 * 2.0) / 1e6)
        self.assertIs(rows[1]["json_ok"], False)
        text = Budget("b1").ledger_path.read_text()
        for leaked in ("agent turn", SESSION, FAKE_GOOGLE_KEY):
            self.assertNotIn(leaked, text)

    def test_budget_exhaustion_answers_402_without_retry(self):
        self.activate(caps={"max_spend": 0.0001, "max_llm_calls": 100})
        status, headers, body = chat(self.relay.url)
        self.assertEqual((status, body["error"]["code"]), (402, "budget_exhausted"))
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Budget("b1").tripped(), "max_spend")

    def test_provider_errors_pass_through_with_no_retry_header(self):
        self.fake.responder = lambda m, p, b: (503, {}, {"error": {"message": "overloaded"}})
        self.activate()
        status, headers, _ = chat(self.relay.url)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(len(self.fake.chat_requests()), 2)

    def test_refusals_are_retried_once_and_both_attempts_are_priced(self):
        self.fake.responder = lambda m, p, b: (200, {}, completion(None, finish_reason="content_filter"))
        self.activate()
        status, _, _ = chat(self.relay.url)
        self.assertEqual(status, 200)
        row = Budget("b1").ledger("b1_pilot")[0]
        self.assertEqual((row["outcome"], row["refusals"]), ("refusal", 2))
        self.assertAlmostEqual(row["cost"], 2 * (10 * 0.5 + 5 * 1.0) / 1e6)

    def test_control_needs_its_own_token_and_valid_stages(self):
        status, _, _ = control(self.relay.url, "activate", {}, token=SESSION)
        self.assertEqual(status, 401)
        status, _, _ = self.activate(stage="nonsense")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
