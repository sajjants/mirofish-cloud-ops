import json
import random
import threading
import time
import unittest

from ops.config import ConfigError, RetrySettings
from ops.provider import (ProviderClient, ProviderError, QuotaExhausted, RateLimiter, Response, RetryPolicy,
                          Stats, classify_completion, parse_usage, retry_hint, send_with_retry)
from ops.redact import REDACTED
from ops.tests.fake_provider import FAKE_GOOGLE_KEY, FakeProvider, completion

FAST_RETRY = RetrySettings(max_retries=3, base_delay_seconds=0.5, max_delay_seconds=10.0)


def make_client(fake: FakeProvider, sleeps: list, retry: RetrySettings = FAST_RETRY) -> ProviderClient:
    return ProviderClient(fake.base_url, FAKE_GOOGLE_KEY, retry=retry, timeout=5,
                          sleep=sleeps.append, rng=random.Random(7))


def user(text: str = "hi") -> list:
    return [{"role": "user", "content": text}]


class RetryTests(unittest.TestCase):
    def test_429_is_retried_after_the_retry_after_header(self):
        sleeps: list = []
        with FakeProvider() as fake:
            fake.script = [(429, {"Retry-After": "3"}, {"error": {"message": "slow down"}}),
                           (200, {}, completion("ok"))]
            client = make_client(fake, sleeps)
            result = client.chat("m", user())
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(3.0 <= sleeps[0] <= 3.3, sleeps)
        stats = client.stats.to_dict()
        self.assertEqual((stats["rate_limited"], stats["retries"], stats["ok"]), (1, 1, 1))

    def test_5xx_is_retried_with_backoff(self):
        sleeps: list = []
        with FakeProvider() as fake:
            fake.script = [(503, {}, {}), (500, {}, {}), (200, {}, completion("ok"))]
            client = make_client(fake, sleeps)
            result = client.chat("m", user())
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(client.stats.server_errors, 2)
        self.assertTrue(0 <= sleeps[0] <= 0.5 and 0 <= sleeps[1] <= 1.0, sleeps)

    def test_exhausted_retries_return_a_failed_result(self):
        sleeps: list = []
        with FakeProvider() as fake:
            fake.script = [(503, {}, {"error": {"message": "overloaded"}})] * 4
            client = make_client(fake, sleeps)
            result = client.chat("m", user())
        self.assertEqual((result.outcome, result.status), ("error", 503))
        self.assertIn("overloaded", result.error)
        self.assertEqual(client.stats.http_attempts, FAST_RETRY.max_retries + 1)
        self.assertEqual(client.stats.failed, 1)

    def test_client_errors_are_not_retried(self):
        sleeps: list = []
        with FakeProvider() as fake:
            fake.script = [(400, {}, {"error": {"message": "bad request"}})]
            client = make_client(fake, sleeps)
            result = client.chat("m", user())
        self.assertEqual(result.status, 400)
        self.assertEqual(client.stats.http_attempts, 1)
        self.assertEqual(sleeps, [])

    def test_a_wait_longer_than_the_cap_stops_instead_of_sleeping(self):
        with FakeProvider() as fake:
            fake.script = [(429, {"Retry-After": "3600"}, {})]
            client = make_client(fake, [])
            with self.assertRaises(QuotaExhausted):
                client.chat("m", user())

    def test_network_errors_are_retried_then_raised_redacted(self):
        def broken_send() -> Response:
            raise OSError(f"connection reset while sending key={FAKE_GOOGLE_KEY}")
        sleeps: list = []
        with self.assertRaises(ProviderError) as caught:
            send_with_retry(broken_send, RetryPolicy(FAST_RETRY), Stats(), sleep=sleeps.append)
        self.assertNotIn(FAKE_GOOGLE_KEY, str(caught.exception))
        self.assertEqual(len(sleeps), FAST_RETRY.max_retries)

    def test_full_jitter_stays_within_the_exponential_bound(self):
        policy = RetryPolicy(RetrySettings(5, 1.0, 8.0), random.Random(1))
        for attempt in range(8):
            for _ in range(50):
                self.assertTrue(0 <= policy.delay(attempt) <= min(8.0, 2 ** attempt))


class RetryHintTests(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(retry_hint({"retry-after": "7"}, b""), 7.0)

    def test_retry_after_http_date(self):
        hint = retry_hint({"retry-after": "Wed, 21 Oct 2015 07:28:10 GMT"}, b"", now=1445412480.0)
        self.assertAlmostEqual(hint, 10.0, places=3)

    def test_google_retry_info_in_the_error_body(self):
        body = json.dumps([{"error": {"code": 429, "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure"},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12.5s"}]}}]).encode()
        self.assertEqual(retry_hint({}, body), 12.5)

    def test_no_hint(self):
        self.assertIsNone(retry_hint({}, b"not json"))


class RefusalTests(unittest.TestCase):
    def test_a_refusal_is_counted_retried_once_then_failed(self):
        with FakeProvider() as fake:
            fake.script = [(200, {}, completion(None, finish_reason="content_filter"))] * 2
            client = make_client(fake, [])
            result = client.chat("m", user())
            self.assertEqual(len(fake.chat_requests()), 2)
        self.assertEqual((result.outcome, result.finish_reason), ("refusal", "content_filter"))
        stats = client.stats.to_dict()
        self.assertEqual((stats["requests"], stats["refusals"], stats["failed"]), (1, 2, 1))
        self.assertEqual(stats["failure_rate"], 1.0)

    def test_an_empty_answer_is_retried_once(self):
        with FakeProvider() as fake:
            fake.script = [(200, {}, completion("  ")), (200, {}, completion("fine"))]
            client = make_client(fake, [])
            result = client.chat("m", user())
        self.assertEqual((result.outcome, result.content), ("ok", "fine"))
        self.assertEqual((client.stats.empty, client.stats.ok, client.stats.failed), (1, 1, 0))

    def test_classification(self):
        self.assertEqual(classify_completion({"choices": []}), ("empty", "no choices"))
        self.assertEqual(classify_completion({"promptFeedback": {"blockReason": "SAFETY"}})[0], "refusal")
        self.assertEqual(classify_completion(completion(None, finish_reason="SAFETY"))[0], "refusal")
        tool_only = completion(None, finish_reason="tool_calls", tool_calls=[{"function": {"name": "x"}}])
        self.assertEqual(classify_completion(tool_only)[0], "ok")


class RequestShapeTests(unittest.TestCase):
    def test_fields_sent_only_when_asked(self):
        with FakeProvider() as fake:
            fake.script = [(200, {}, completion("{}")), (200, {}, completion("x"))]
            client = make_client(fake, [])
            client.chat("fast", user(), json_mode=True, reasoning_effort="low", tools=[{"type": "function"}])
            client.chat("fast", user())
            first, second = [r["body"] for r in fake.chat_requests()]
            auth = fake.requests[0]["authorization"]
        self.assertEqual(first["response_format"], {"type": "json_object"})
        self.assertEqual(first["reasoning_effort"], "low")
        self.assertIn("tools", first)
        for absent in ("response_format", "reasoning_effort", "tools", "max_tokens", "temperature"):
            self.assertNotIn(absent, second)
        self.assertEqual(auth, f"Bearer {FAKE_GOOGLE_KEY}")

    def test_error_text_from_the_provider_is_redacted(self):
        with FakeProvider() as fake:
            fake.script = [(400, {}, {"error": {"message": f"API key {FAKE_GOOGLE_KEY} not valid"}})]
            result = make_client(fake, []).chat("m", user())
        self.assertNotIn(FAKE_GOOGLE_KEY, result.error)
        self.assertIn(REDACTED, result.error)

    def test_plain_http_is_refused_except_on_loopback(self):
        with self.assertRaises(ConfigError):
            ProviderClient("http://generativelanguage.googleapis.com/v1beta/openai/", "k", retry=FAST_RETRY)
        ProviderClient("https://generativelanguage.googleapis.com/v1beta/openai/", "k", retry=FAST_RETRY)

    def test_usage_with_reasoning_tokens(self):
        body = completion("x", usage={"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12,
                                      "completion_tokens_details": {"reasoning_tokens": 6}})
        self.assertEqual(parse_usage(body), {"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12,
                                             "reasoning_tokens": 6})
        self.assertIsNone(parse_usage({"choices": []}))


class RateLimiterTests(unittest.TestCase):
    def test_requests_per_minute_cap_waits_for_the_window(self):
        now = [0.0]
        slept: list = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(max_concurrency=5, requests_per_minute=2, clock=lambda: now[0], sleep=fake_sleep)
        for _ in range(3):
            with limiter.slot():
                pass
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 60.0)

    def test_concurrency_cap(self):
        limiter = RateLimiter(max_concurrency=2, requests_per_minute=1000)
        active, peak = [0], [0]
        lock = threading.Lock()

        def work() -> None:
            with limiter.slot():
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.02)
                with lock:
                    active[0] -= 1

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], 2)

    def test_invalid_limits(self):
        with self.assertRaises(ValueError):
            RateLimiter(0, 10)


if __name__ == "__main__":
    unittest.main()
