"""JSON-mode calls for the wrapper's own analysis tools (variants propose, factguard, compare).

They go to Gemini through the running relay, as stage 'variants', 'factguard' or 'compare' of the
pseudo-run '<batch>_tools'. So they are metered in the batch's ledger, count against its caps, stop
at its kill switch, and appear in netcheck's evidence like every other call. Before anything is sent:
  - the privacy checklist must be confirmed (privacy_report --confirm),
  - you confirm the tool's data flow with a typed phrase,
  - every prompt passes the full leak gate (sensitive terms, emails, phone numbers, long digit
    strings); the relay checks the terms again.
Callers only pass pseudonymised text; de-pseudonymisation happens locally, after the answer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from ops import leakgate
from ops.confirm import typed_phrase
from ops.json_checks import clean_model_text
from ops.stages import STAGE_GROUP

TOOL_STAGES = ("variants", "factguard", "compare")


class LLMError(RuntimeError):
    pass


class WrapperLLM:
    def __init__(self, batch_id: str, stage: str, *, relay=None, llm=None, limits=None, terms=None,
                 ask: Optional[Callable[[str], str]] = None, out=None, interactive: Optional[bool] = None,
                 timeout: float = 600.0):
        from ops.batch import RelayControl
        from ops.config import load_llm_config
        from ops.limits import load_limits

        if stage not in TOOL_STAGES:
            raise ValueError(f"unknown tool stage {stage!r}")
        self.batch_id = batch_id
        self.stage = stage
        self.run_id = f"{batch_id}_tools"
        self.relay = relay or RelayControl()
        self.llm = llm or load_llm_config()
        self.limits = limits or load_limits()
        self.terms = terms
        self.ask, self.out, self.interactive = ask, out, interactive
        self.timeout = timeout
        self.stage_model = self.llm.stages[STAGE_GROUP[stage]]
        self.model = self.stage_model.model
        self.calls = 0
        self._active = False
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ----- session
    def begin(self, purpose: str, sends: List[str], max_calls: int) -> None:
        """Checklist, typed confirmation, then mark the tool stage active on the relay."""
        from ops.batch import BatchError
        from ops.privacy import require_checklist

        require_checklist()
        if not self.relay.health():
            raise LLMError("the relay is not answering; start the stack with `python -m ops start`")
        status = (self.relay.status() or {}).get("budget") or {}
        caps = self.limits["caps"]
        host = urlsplit(self.llm.base_url).hostname
        lines = [f"{purpose} for batch {self.batch_id}: up to {max_calls} call(s) to {self.model} "
                 f"(effort {self.stage_model.reasoning_effort or 'model default'})",
                 f"  sends to {host} (Google Gemini API), through the local relay:",
                 *(f"    - {item}" for item in sends),
                 "  nothing else; every prompt passes the leak gate first",
                 f"  counts against the batch caps: spend {status.get('spend', 0.0):.4f} of {caps['max_spend']}, "
                 f"calls {status.get('calls', 0)} of {caps['max_llm_calls']}"]
        typed_phrase(lines, f"{self.stage} {self.batch_id}", "not confirmed; nothing was sent",
                     ask=self.ask, out=self.out, interactive=self.interactive)
        try:
            self.relay.activate(batch_id=self.batch_id, run_id=self.run_id, kind="tools", stage=self.stage,
                                attempt=1, caps={"max_spend": caps["max_spend"], "max_llm_calls": caps["max_llm_calls"]},
                                run_max_spend=None)
        except BatchError as error:
            raise LLMError(str(error)) from None
        self._active = True

    def end(self) -> None:
        if self._active:
            try:
                self.relay.deactivate()
            except Exception:  # the relay may already be gone; nothing is active without it
                pass
            self._active = False

    def __enter__(self) -> "WrapperLLM":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.end()

    # ----- calls
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{self.relay.llm_base_url}/chat/completions", data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.relay.state['session_token']}",
                     "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                detail = (json.loads(error.read() or b"{}").get("error") or {})
            except ValueError:
                detail = {}
            code = detail.get("code") if isinstance(detail, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise LLMError(f"relay refused the call (HTTP {error.code}, {code or 'error'}): {message}") from None
        except (urllib.error.URLError, OSError) as error:
            raise LLMError(f"relay not reachable: {error}") from None

    def chat_json(self, system: str, user: str, *, validate: Callable[[Dict], Optional[str]], seed: int = 0,
                  temperature: float = 0.2, retries: int = 3, max_tokens: Optional[int] = None) -> Dict:
        """Ask for one JSON object; validate(obj) returns an error string or None. Invalid answers are
        asked again, up to `retries` calls. `seed` is accepted for interface compatibility only."""
        if not self._active:
            raise LLMError("begin() was not called; no confirmation was given")
        leakgate.check({f"{self.stage} prompt": f"{system}\n{user}"}, self.terms)
        last_error = "no attempt"
        for _attempt in range(retries):
            self.calls += 1
            payload: Dict[str, Any] = {"model": self.model, "temperature": temperature,
                                       "messages": [{"role": "system", "content": system},
                                                    {"role": "user", "content": user}],
                                       "response_format": {"type": "json_object"}}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            response = self._post(payload)
            try:
                content = response["choices"][0]["message"]["content"]
                value = json.loads(clean_model_text(content))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = f"unparseable response: {type(exc).__name__}"
                continue
            if not isinstance(value, dict):
                last_error = "top-level JSON is not an object"
                continue
            last_error = validate(value) or ""
            if not last_error:
                return value
        raise LLMError(f"no valid answer after {retries} attempts: {last_error}")

    def model_info(self) -> Dict[str, Any]:
        return {"model": self.model, "reasoning_effort": self.stage_model.reasoning_effort,
                "provider": urlsplit(self.llm.base_url).hostname, "via": "relay", "stage": self.stage}


def require_list_of_strings(value: Any, low: int, high: int, max_len: int = 400) -> Optional[str]:
    if not isinstance(value, list):
        return "expected a list"
    items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if len(items) != len(value):
        return "list contains non-string or empty items"
    if len({i.lower() for i in items}) != len(items):
        return "list contains duplicates"
    if not low <= len(items) <= high:
        return f"expected {low}-{high} items, got {len(items)}"
    if any(len(i) > max_len for i in items):
        return f"an item is longer than {max_len} characters"
    return None


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"
