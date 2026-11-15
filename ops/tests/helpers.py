"""Shared fixtures for relay and batch tests: configs, prices, a relay on 127.0.0.1, a fake MiroFish."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import yaml

from ops import paths
from ops.config import RetrySettings, parse_llm_config
from ops.leakgate import Term, TermMatcher
from ops.limits import validate_limits
from ops.pricing import ModelPrice, Prices
from ops.provider import ProviderClient
from ops.relay import Relay, make_server
from ops.tests.fake_provider import FAKE_GOOGLE_KEY

SESSION = "session-token-for-tests-only"
CONTROL = "control-token-for-tests-only"
TERMS = [Term("Zebulon", 1), Term("Quixote Holdings", 2)]
GRAPH_ID = "mirofish_00000000000000a1"


def llm_config(base_url: str):
    return parse_llm_config({
        "provider": {"base_url": base_url},
        "stages": {"setup": {"model": "setup-m", "reasoning_effort": "medium"},
                   "simulation": {"model": "sim-m", "reasoning_effort": "low"},
                   "report": {"model": "setup-m", "reasoning_effort": "medium"}},
    })


def prices() -> Prices:
    return Prices("USD", "https://example.invalid/pricing", "2026-11-15",
                  {"setup-m": ModelPrice(1.0, 2.0), "sim-m": ModelPrice(0.5, 1.0)})


def limits(**overrides: Any) -> Dict[str, Any]:
    raw = yaml.safe_load((paths.CONFIG_DIR / "limits.example.yaml").read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        raw[section][key] = value
    return validate_limits(raw)


class TempState:
    """Point ops state and run directories at a temporary folder for one test."""

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("STATE_DIR", self.root / "state"), ("RUNS_DIR", self.root / "runs"),
                            ("MAPPINGS_DIR", self.root / "mappings"), ("LOGS_DIR", self.root / "logs"),
                            ("UPSTREAM_DIR", self.root / "mirofish")):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            test.addCleanup(patch.stop)


def confirm_privacy() -> None:
    """Record the privacy checklist in the (temporary) state dir, as `privacy_report --confirm` would."""
    from ops import privacy
    privacy.confirm_checklist(ask=lambda _prompt: "yes", out=io.StringIO(), interactive=True)


class RelayFixture:
    def __init__(self, test, provider_url: str, reserve_output_tokens: int = 4096):
        client = ProviderClient(provider_url, FAKE_GOOGLE_KEY, retry=RetrySettings(1, 0.01, 1.0), timeout=5,
                                sleep=lambda _s: None)
        self.relay = Relay(llm_config(provider_url), prices(), client, TermMatcher(TERMS), SESSION, CONTROL,
                           reserve_output_tokens, log=lambda _m: None)
        self.server = make_server(self.relay, 0)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(self.close)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def state(self) -> Dict[str, Any]:
        return {"port": self.port, "session_token": SESSION, "control_token": CONTROL}


def post(url: str, payload: Any, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None):
    """(status, headers, parsed body) for a POST to the relay."""
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), json.loads(error.read() or b"{}")


def chat(relay_url: str, content: str = "hello", *, token: str = SESSION, **extra: Any):
    body = {"model": "whatever-mirofish-sends", "messages": [{"role": "user", "content": content}], **extra}
    return post(f"{relay_url}/v1/chat/completions", body, {"Authorization": f"Bearer {token}"})


def control(relay_url: str, path: str, payload: Dict[str, Any], token: str = CONTROL):
    return post(f"{relay_url}/control/{path}", payload, {"X-Ops-Control": token})


class FakeMiroFish:
    """Stands in for the MiroFish REST API. LLM-using steps send real requests through the relay."""

    def __init__(self, relay_url: str, simulations_dir: Path, *, entities: Optional[Dict[str, int]] = None,
                 calls_per_poll: int = 3, report_calls: int = 2):
        self.relay_url = relay_url
        self.simulations_dir = simulations_dir
        self.entities = entities or {"Person": 4, "Organization": 2}
        self.calls_per_poll = calls_per_poll
        self.report_calls = report_calls
        self.runner = "idle"
        self.round = 0
        self.max_rounds = 0
        self.stops = 0
        self.starts: List[Dict[str, Any]] = []
        self.llm_statuses: List[int] = []

    def _llm(self, content: str, **extra: Any) -> None:
        status, _headers, _body = chat(self.relay_url, content, **extra)
        self.llm_statuses.append(status)

    def health(self) -> bool:
        return True

    def entity_counts(self, graph_id: str, entity_types: Optional[List[str]] = None) -> Dict[str, int]:
        return {k: v for k, v in self.entities.items() if not entity_types or k in entity_types}

    def post_multipart(self, path: str, fields: Dict[str, str], files, **kwargs) -> Dict[str, Any]:
        assert path == "/api/graph/ontology/generate"
        self._llm("design an ontology", response_format={"type": "json_object"})
        return {"project_id": "proj_1"}

    def get(self, path: str, **kwargs) -> Dict[str, Any]:
        if path.startswith("/api/graph/project/"):
            return {"project_id": "proj_1", "graph_id": GRAPH_ID, "zep_batch_id": "zb-1"}
        if path.startswith("/api/graph/task/"):
            return {"status": "completed", "result": {"graph_id": GRAPH_ID}}
        if path.endswith("/run-status"):
            if self.runner == "running":
                for _ in range(self.calls_per_poll):
                    self._llm("agent turn", tools=[{"type": "function", "function": {"name": "create_post"}}])
                self.round += 1
                if self.round >= self.max_rounds:
                    self.runner = "completed"
            done = self.runner in ("completed", "stopped")
            return {"runner_status": self.runner, "current_round": self.round, "total_rounds": self.max_rounds,
                    "twitter_completed": done, "reddit_completed": done, "total_actions_count": self.round * 3}
        if path.startswith("/api/report/"):
            return {"markdown_content": "# Report\n"}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        if path == "/api/graph/build":
            return {"task_id": "t1"}
        if path == "/api/simulation/create":
            sim_dir = self.simulations_dir / "sim_abc"
            sim_dir.mkdir(parents=True, exist_ok=True)
            agents = [{"agent_id": i, "active_hours": list(range(0, 24)), "activity_level": 0.8}
                      for i in range(sum(self.entities.values()))]
            config = {"time_config": {"minutes_per_round": 60, "agents_per_hour_min": 2, "agents_per_hour_max": 4},
                      "agent_configs": agents}
            (sim_dir / "simulation_config.json").write_text(json.dumps(config), encoding="utf-8")
            return {"simulation_id": "sim_abc"}
        if path == "/api/simulation/prepare":
            types = payload.get("entity_types")
            count = sum(self.entity_counts("g", types).values())
            for _ in range(count):
                self._llm("write a persona", response_format={"type": "json_object"})
            return {"task_id": "p1", "expected_entities_count": count}
        if path == "/api/simulation/prepare/status":
            return {"status": "completed"}
        if path == "/api/simulation/start":
            self.starts.append(payload)
            if payload.get("force"):
                self.round = 0
            self.runner, self.max_rounds = "running", payload["max_rounds"]
            return {}
        if path == "/api/simulation/stop":
            self.stops += 1
            self.runner = "stopped"
            return {}
        if path == "/api/simulation/close-env":
            return {}
        if path == "/api/report/generate":
            for _ in range(self.report_calls):
                self._llm("draft the report section")
            return {"report_id": "report_1", "task_id": "rt1"}
        if path == "/api/report/generate/status":
            return {"status": "completed"}
        raise AssertionError(f"unexpected POST {path}")
