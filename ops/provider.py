"""OpenAI-compatible provider client with retries, rate limits and refusal handling.

Used by doctor and the wrapper's own LLM calls; send_with_retry and RateLimiter are also used by the
metering relay in front of MiroFish. Prompt and response text are never logged.

Retries: 429 and 5xx responses and network errors, with exponential backoff and full jitter. A
provider hint (Retry-After header, or Google's RetryInfo retryDelay in the error body) is honoured;
if the hint is longer than retry.max_delay_seconds the call fails instead of sleeping (quota exhausted).
Safety refusals and empty responses are counted, retried once, then recorded as failed.
"""

from __future__ import annotations

import email.utils
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional

from ops.config import RetrySettings, check_endpoint
from ops.redact import redact

REFUSAL_FINISH_REASONS = {"content_filter", "safety", "prohibited_content", "blocklist", "spii", "recitation"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ProviderError(RuntimeError):
    """A call that could not be completed; the message is already redacted."""


class QuotaExhausted(ProviderError):
    """The provider asked us to wait longer than retry.max_delay_seconds."""


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Stats:
    """Thread-safe counters reported by doctor and the pilot."""

    requests: int = 0          # logical calls
    http_attempts: int = 0
    retries: int = 0
    rate_limited: int = 0      # 429 responses
    server_errors: int = 0     # 5xx responses
    network_errors: int = 0
    refusals: int = 0
    empty: int = 0
    ok: int = 0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, **counts: int) -> None:
        with self._lock:
            for name, value in counts.items():
                setattr(self, name, getattr(self, name) + value)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            data = {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}
        done = data["ok"] + data["failed"]
        data["refusal_rate"] = round(data["refusals"] / data["requests"], 4) if data["requests"] else 0.0
        data["failure_rate"] = round(data["failed"] / done, 4) if done else 0.0
        return data


class RetryPolicy:
    def __init__(self, settings: RetrySettings, rng: Optional[random.Random] = None):
        self.max_retries = settings.max_retries
        self.base = settings.base_delay_seconds
        self.cap = settings.max_delay_seconds
        self.rng = rng or random.Random()

    def delay(self, attempt: int, hint: Optional[float] = None) -> float:
        """Seconds to wait before retry number attempt+1 (attempt counts from 0)."""
        if hint is not None:
            if hint > self.cap:
                raise QuotaExhausted(f"provider asked to wait {hint:.0f}s (more than max_delay_seconds={self.cap:.0f})")
            return hint + self.rng.uniform(0, max(0.1 * hint, 0.05))
        return self.rng.uniform(0, min(self.cap, self.base * (2 ** attempt)))


def retry_hint(headers: Dict[str, str], body: bytes, now: Optional[float] = None) -> Optional[float]:
    """Seconds the provider asked us to wait, from Retry-After or a Google RetryInfo retryDelay."""
    value = headers.get("retry-after")
    if value:
        value = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", value):
            return float(value)
        try:
            when = email.utils.parsedate_to_datetime(value).timestamp()
            return max(0.0, when - (time.time() if now is None else now))
        except (TypeError, ValueError):
            pass
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    for item in payload if isinstance(payload, list) else [payload]:
        error = item.get("error") if isinstance(item, dict) else None
        details = error.get("details") if isinstance(error, dict) else None
        for detail in details if isinstance(details, list) else []:
            delay = detail.get("retryDelay") if isinstance(detail, dict) else None
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(delay or ""))
            if match:
                return float(match.group(1))
    return None


class RateLimiter:
    """At most max_concurrency calls in flight and requests_per_minute starts per rolling minute."""

    def __init__(self, max_concurrency: int, requests_per_minute: int,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if max_concurrency < 1 or requests_per_minute < 1:
            raise ValueError("max_concurrency and requests_per_minute must be at least 1")
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._rpm = requests_per_minute
        self._starts: Deque[float] = deque()
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def _wait_for_rate(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._starts and now - self._starts[0] >= 60.0:
                    self._starts.popleft()
                if len(self._starts) < self._rpm:
                    self._starts.append(now)
                    return
                wait = 60.0 - (now - self._starts[0])
            self._sleep(max(wait, 0.01))

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._slots.acquire()
        try:
            self._wait_for_rate()
            yield
        finally:
            self._slots.release()


def http_send(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Response:
    """One HTTP exchange; HTTP error statuses are returned, network failures raise OSError."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, error.read() or b"")


def send_with_retry(send: Callable[[], Response], policy: RetryPolicy, stats: Stats,
                    limiter: Optional[RateLimiter] = None, sleep: Callable[[float], None] = time.sleep) -> Response:
    """Call send() until it returns a non-retryable response or retries run out (then the last response)."""
    for attempt in range(policy.max_retries + 1):
        last = attempt == policy.max_retries
        try:
            if limiter is not None:
                with limiter.slot():
                    response = send()
            else:
                response = send()
        except OSError as error:
            stats.add(http_attempts=1, network_errors=1)
            if last:
                raise ProviderError(redact(f"network error after {attempt + 1} attempt(s): {error}")) from None
            stats.add(retries=1)
            sleep(policy.delay(attempt))
            continue
        stats.add(http_attempts=1)
        retryable = response.status == 429 or 500 <= response.status < 600
        if not retryable:
            return response
        stats.add(rate_limited=int(response.status == 429), server_errors=int(response.status >= 500))
        if last:
            return response
        wait = policy.delay(attempt, retry_hint(response.headers, response.body))
        stats.add(retries=1)
        sleep(wait)
    raise AssertionError("unreachable")


def classify_completion(payload: Dict[str, Any]) -> tuple:
    """('ok' | 'refusal' | 'empty', finish_reason or detail) for a chat completion body."""
    choices = payload.get("choices") or []
    if not choices:
        return ("refusal", "blocked prompt") if payload.get("promptFeedback") or payload.get("prompt_feedback") \
            else ("empty", "no choices")
    choice = choices[0] or {}
    finish = str(choice.get("finish_reason") or "").lower()
    if finish in REFUSAL_FINISH_REASONS:
        return "refusal", finish
    message = choice.get("message") or {}
    if not str(message.get("content") or "").strip() and not message.get("tool_calls"):
        return "empty", finish or "no content"
    return "ok", finish


def parse_usage(payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    out = {k: int(usage[k]) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
           if isinstance(usage.get(k), (int, float))}
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), (int, float)):
        out["reasoning_tokens"] = int(details["reasoning_tokens"])
    return out or None


def error_message(response: Response) -> str:
    try:
        payload = response.json()
        error = (payload[0] if isinstance(payload, list) else payload).get("error") or {}
        text = error.get("message") or error.get("status") or ""
    except (ValueError, AttributeError, IndexError, UnicodeDecodeError):
        text = ""
    return redact(f"HTTP {response.status}" + (f": {str(text)[:300]}" if text else ""))


@dataclass
class ChatResult:
    outcome: str                    # ok | refusal | empty | error
    status: int = 0
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: Optional[Dict[str, int]] = None
    seconds: float = 0.0
    error: str = ""


class ProviderClient:
    def __init__(self, base_url: str, api_key: str, *, retry: RetrySettings, timeout: float = 120.0,
                 limiter: Optional[RateLimiter] = None, stats: Optional[Stats] = None,
                 send: Callable[..., Response] = http_send, sleep: Callable[[float], None] = time.sleep,
                 rng: Optional[random.Random] = None):
        self.base_url = check_endpoint(base_url).rstrip("/")
        self._key = api_key
        self.policy = RetryPolicy(retry, rng)
        self.timeout = timeout
        self.limiter = limiter
        self.stats = stats or Stats()
        self._send = send
        self._sleep = sleep

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Response:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        url = f"{self.base_url}/{path.lstrip('/')}"
        return send_with_retry(lambda: self._send(method, url, self._headers(), body, self.timeout),
                               self.policy, self.stats, self.limiter, self._sleep)

    def get_model(self, model: str) -> Response:
        """Zero-token check that the key is valid and the model exists."""
        return self._request("GET", f"models/{model}")

    def chat(self, model: str, messages: List[Dict[str, Any]], *, json_mode: bool = False,
             tools: Optional[List[Dict[str, Any]]] = None, reasoning_effort: Optional[str] = None,
             max_tokens: Optional[int] = None, temperature: Optional[float] = None) -> ChatResult:
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        self.stats.add(requests=1)
        started = time.monotonic()
        result = ChatResult(outcome="error")
        for _content_attempt in range(2):
            response = self._request("POST", "chat/completions", payload)
            result = ChatResult(outcome="error", status=response.status, seconds=round(time.monotonic() - started, 3))
            if response.status != 200:
                result.error = error_message(response)
                self.stats.add(failed=1)
                return result
            try:
                body = response.json()
            except (ValueError, UnicodeDecodeError):
                result.error = "response is not JSON"
                self.stats.add(failed=1)
                return result
            outcome, finish = classify_completion(body)
            choice = (body.get("choices") or [{}])[0] or {}
            message = choice.get("message") or {}
            result.outcome, result.finish_reason = outcome, finish
            result.content = str(message.get("content") or "")
            result.tool_calls = list(message.get("tool_calls") or [])
            result.usage = parse_usage(body)
            if outcome == "ok":
                self.stats.add(ok=1)
                return result
            self.stats.add(**{"refusals" if outcome == "refusal" else "empty": 1})
        self.stats.add(failed=1)
        return result
