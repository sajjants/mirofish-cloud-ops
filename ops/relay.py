"""The metering relay: MiroFish's only LLM endpoint (127.0.0.1), in front of Gemini.

For each chat request, in order: session token, no streaming, leak gate (sensitive terms), an
active confirmed run, budget admission, routing by stage, forwarding with retries and rate
limits, metering, thresholds. The response goes back unchanged; refusals and exhausted retries
carry x-should-retry: false so the OpenAI SDK inside MiroFish does not retry on top.

Control endpoints (second token, X-Ops-Control header) let `pilot` and `run` set the active
batch, run and stage, read the budget, and trip the kill switch. Tokens and the port live in
ops/state/relay.json (owner-only). No prompt or response text is logged anywhere.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from ops import paths
from ops.budget import LEAK_TRIP, Budget
from ops.config import LLMConfig
from ops.json_checks import clean_model_text
from ops.leakgate import TermMatcher, request_text
from ops.pricing import Prices, cost, worst_case_cost
from ops.provider import Completion, ProviderClient, ProviderError
from ops.redact import redact
from ops.stages import STAGE_GROUP, model_for, route_request

MAX_REQUEST_BYTES = 32 * 1024 * 1024
STATE_FILE = "relay.json"
CHAT_PATHS = ("/v1/chat/completions", "/chat/completions")
NO_RETRY = {"x-should-retry": "false"}


def load_relay_state(port: Optional[int] = None) -> Dict[str, Any]:
    """Port and tokens for the relay; created on first use, owner-only."""
    path = paths.STATE_DIR / STATE_FILE
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    changed = False
    for key in ("session_token", "control_token"):
        if not state.get(key):
            state[key] = secrets.token_urlsafe(32)
            changed = True
    if port is not None and state.get("port") != port:
        state["port"] = port
        changed = True
    if changed:
        paths.write_private_text(path, json.dumps(state, indent=2) + "\n")
    return state


@dataclass
class ActiveRun:
    batch_id: str
    run_id: str
    kind: str
    stage: str
    attempt: int


def _wants_json(body: Dict[str, Any]) -> bool:
    fmt = body.get("response_format") or {}
    return isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")


def analyse(request: Dict[str, Any], call: Completion) -> Dict[str, Any]:
    """Structured-output facts about one call, without keeping any of its text."""
    json_mode = _wants_json(request)
    tools = bool(request.get("tools"))
    out: Dict[str, Any] = {"json_mode": json_mode, "tools": tools, "json_ok": None, "tool_calls": None,
                           "tool_args_ok": None}
    if call.outcome != "ok" or call.body is None:
        return out
    message = ((call.body.get("choices") or [{}])[0] or {}).get("message") or {}
    if json_mode:
        try:
            json.loads(clean_model_text(str(message.get("content") or "")))
            out["json_ok"] = True
        except ValueError:
            out["json_ok"] = False
    if tools:
        calls = message.get("tool_calls") or []
        out["tool_calls"] = len(calls)
        if calls:
            try:
                for item in calls:
                    json.loads((item.get("function") or {}).get("arguments") or "{}")
                out["tool_args_ok"] = True
            except (ValueError, AttributeError):
                out["tool_args_ok"] = False
    return out


class Relay:
    """Everything the HTTP handler needs; usable in-process by tests."""

    def __init__(self, llm: LLMConfig, prices: Prices, client: ProviderClient, matcher: TermMatcher,
                 session_token: str, control_token: str, reserve_output_tokens: int, log=None):
        self.llm = llm
        self.prices = prices
        self.client = client
        self.matcher = matcher
        self.session_token = session_token
        self.control_token = control_token
        self.reserve_output_tokens = reserve_output_tokens
        self.active: Optional[ActiveRun] = None
        self.budgets: Dict[str, Budget] = {}
        self._lock = threading.Lock()
        self._log = log or (lambda message: print(message, file=sys.stderr))

    def budget(self, batch_id: str) -> Budget:
        with self._lock:
            if batch_id not in self.budgets:
                self.budgets[batch_id] = Budget(batch_id)
            return self.budgets[batch_id]

    # ----- control
    def activate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        stage = str(payload.get("stage") or "")
        if stage not in STAGE_GROUP:
            raise ValueError(f"unknown stage {stage!r}")
        missing = self.prices.missing(s.model for s in self.llm.stages.values())
        if missing:
            raise ValueError(f"prices.yaml has no price for: {', '.join(missing)}")
        caps = payload.get("caps") or {}
        budget = self.budget(str(payload.get("batch_id") or ""))
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            raise ValueError("run_id is required")
        budget.set_caps(float(caps["max_spend"]), int(caps["max_llm_calls"]), run_id, payload.get("run_max_spend"))
        if budget.tripped():
            raise PermissionError(f"batch is stopped ({budget.tripped()})")
        with self._lock:
            self.active = ActiveRun(budget.batch_id, run_id, str(payload.get("kind") or ""), stage,
                                    int(payload.get("attempt") or 1))
        return self.status(budget.batch_id)

    def deactivate(self) -> None:
        with self._lock:
            self.active = None

    def status(self, batch_id: Optional[str] = None) -> Dict[str, Any]:
        active = self.active
        batch = batch_id or (active.batch_id if active else None)
        return {
            "active": vars(active) if active else None,
            "budget": self.budget(batch).status() if batch else None,
            "provider": self.client.stats.to_dict(),
        }

    # ----- chat
    def chat(self, raw: bytes, body: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        if body.get("stream"):
            return self._refuse(400, "stream_not_supported", "streaming responses cannot be metered; "
                                "this relay only serves non-streaming requests")
        term_lines = self.matcher.term_lines(request_text(body))
        active = self.active
        if term_lines:
            if active:
                budget = self.budget(active.batch_id)
                budget.trip(LEAK_TRIP)
                budget.record({"event": "leak_gate", "run_id": active.run_id, "stage": active.stage,
                               "term_lines": term_lines})
            self._log(f"relay: LEAK GATE refused a request (terms file lines {term_lines}); run stopped")
            return self._refuse(400, "leak_gate", f"request contained sensitive terms (terms file lines "
                                f"{term_lines}); nothing was sent")
        if active is None:
            return self._refuse(409, "no_active_run", "LLM calls are only relayed during a confirmed pilot or run")

        stage_model = model_for(active.stage, self.llm)
        price = self.prices.for_model(stage_model.model)
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
        reservation = worst_case_cost(price, len(raw), int(max_tokens or self.reserve_output_tokens))
        budget = self.budget(active.batch_id)
        refusal = budget.admit(active.run_id, reservation)
        base = {"stage": active.stage, "group": STAGE_GROUP[active.stage], "attempt": active.attempt,
                "kind": active.kind, "model": stage_model.model, "effort": stage_model.reasoning_effort}
        if refusal:
            budget.record({**base, "run_id": active.run_id, "status": 402, "outcome": "refused", "reason": refusal})
            return self._refuse(402, "budget_exhausted", f"limit reached: {refusal}; the run is stopping")

        routed = route_request(body, stage_model)
        call: Optional[Completion] = None
        try:
            call = self.client.complete(routed)
        except ProviderError as error:
            budget.settle(active.run_id, reservation, 0.0, {**base, "status": 502, "outcome": "error",
                                                            "error": redact(str(error))[:200], "attempts": 0})
            return self._refuse(502, "provider_unavailable", redact(str(error)))

        spend, priced_usage = 0.0, {"prompt_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        missing_usage = 0
        for usage in call.usages:
            if usage is None:
                spend += worst_case_cost(price, len(raw), int(max_tokens or 0) or None)
                missing_usage += 1
                continue
            spend += cost(price, usage)
            prompt = int(usage.get("prompt_tokens") or 0)
            output = (int(usage["total_tokens"]) - prompt) if usage.get("total_tokens") is not None else \
                int(usage.get("completion_tokens") or 0) + int(usage.get("reasoning_tokens") or 0)
            priced_usage["prompt_tokens"] += prompt
            priced_usage["output_tokens"] += max(0, output)
            priced_usage["reasoning_tokens"] += int(usage.get("reasoning_tokens") or 0)
        stats = call.stats.to_dict()
        status = call.response.status if call.response else 502
        record = {**base, "status": status, "outcome": call.outcome, "finish_reason": call.finish_reason,
                  "latency_s": call.seconds, "attempts": stats["http_attempts"], "retries": stats["retries"],
                  "rate_limited": stats["rate_limited"], "server_errors": stats["server_errors"],
                  "refusals": stats["refusals"], "empty": stats["empty"], "missing_usage": missing_usage,
                  **priced_usage, **analyse(body, call)}
        budget.settle(active.run_id, reservation, spend, record)
        headers = {"Content-Type": "application/json"}
        if status != 200:
            headers.update(NO_RETRY)
        return status, headers, call.response.body if call.response else b"{}"

    def _refuse(self, status: int, code: str, message: str) -> Tuple[int, Dict[str, str], bytes]:
        payload = {"error": {"message": message, "type": code, "code": code}}
        return status, {"Content-Type": "application/json", **NO_RETRY}, json.dumps(payload).encode("utf-8")

    def models(self) -> Dict[str, Any]:
        names = sorted({s.model for s in self.llm.stages.values()})
        return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "relay"} for n in names]}


def make_server(relay: Relay, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "mirofish-relay"
        sys_version = ""

        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, headers: Dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8"))

        def _authorised(self, header: str, expected: str, prefix: str = "") -> bool:
            value = self.headers.get(header) or ""
            if prefix:
                if not value.startswith(prefix):
                    return False
                value = value[len(prefix):]
            return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))

        def _read_json(self) -> Tuple[bytes, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST_BYTES:
                raise OverflowError
            raw = self.rfile.read(length) if length else b""
            return raw, (json.loads(raw) if raw else {})

        def do_GET(self) -> None:
            if self.path == "/meter/health":
                return self._json(200, {"ok": True})
            if self.path == "/control/status":
                if not self._authorised("X-Ops-Control", relay.control_token):
                    return self._json(401, {"error": "bad control token"})
                return self._json(200, relay.status())
            if self.path in ("/v1/models", "/models"):
                if not self._authorised("Authorization", relay.session_token, "Bearer "):
                    return self._json(401, {"error": {"message": "invalid session token"}})
                return self._json(200, relay.models())
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                raw, body = self._read_json()
            except OverflowError:
                return self._send(*relay._refuse(413, "too_large", "request too large"))
            except ValueError:
                return self._send(*relay._refuse(400, "bad_json", "request body is not JSON"))
            if self.path in CHAT_PATHS:
                if not self._authorised("Authorization", relay.session_token, "Bearer "):
                    return self._send(*relay._refuse(401, "invalid_session_token", "invalid session token"))
                if not isinstance(body, dict):
                    return self._send(*relay._refuse(400, "bad_json", "request body must be a JSON object"))
                return self._send(*relay.chat(raw, body))
            if self.path.startswith("/control/"):
                if not self._authorised("X-Ops-Control", relay.control_token):
                    return self._json(401, {"error": "bad control token"})
                try:
                    if self.path == "/control/activate":
                        return self._json(200, relay.activate(body))
                    if self.path == "/control/deactivate":
                        relay.deactivate()
                        return self._json(200, relay.status())
                    if self.path == "/control/trip":
                        relay.budget(str(body.get("batch_id") or "")).trip(str(body.get("reason") or "manual"))
                        return self._json(200, relay.status(str(body.get("batch_id"))))
                except PermissionError as error:
                    return self._json(409, {"error": str(error)})
                except (KeyError, TypeError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            self._json(404, {"error": "not found"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int) -> int:
    """Run the relay in the foreground until interrupted (started by `start`)."""
    from ops.config import load_llm_config
    from ops.keys import get_key
    from ops.leakgate import load_terms
    from ops.limits import load_limits
    from ops.pricing import load_prices
    from ops.provider import RateLimiter

    llm = load_llm_config()
    if llm.placeholders():
        raise SystemExit(f"relay: model names are placeholders for {', '.join(llm.placeholders())}")
    prices = load_prices()
    limits = load_limits()
    key = get_key(llm.api_key_env, llm.api_key_env)
    if key is None:
        raise SystemExit(f"relay: no API key in {llm.api_key_env} or the OS keychain")
    provider = limits["provider"]
    client = ProviderClient(llm.base_url, key.value, retry=llm.retry, timeout=llm.timeout_seconds,
                            limiter=RateLimiter(provider["max_concurrency"], provider["requests_per_minute"]))
    state = load_relay_state(port)
    relay = Relay(llm, prices, client, TermMatcher(load_terms()), state["session_token"], state["control_token"],
                  provider["reserve_output_tokens"])
    server = make_server(relay, port)
    print(f"relay: listening on 127.0.0.1:{port}, forwarding to {llm.base_url}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
