"""Minimal client for the MiroFish REST API (standard library only, loopback only), and one resumable run.

A run: ontology -> graph -> agents (count check, no LLM) -> simulation -> prepare (file count check) -> schedule
-> rounds -> close_env -> report. Each finished stage is recorded in ops/runs/<RUN_ID>/manifest.json,
so running the same RUN_ID again resumes after the last finished stage. A stage stopped by the
kill switch or a failed check is marked 'interrupted' and starts again from its beginning on
resume; upstream cannot continue a stopped simulation, so an interrupted rounds stage restarts
at round 0 (its spend is recorded as wasted by the batch).
"""

from __future__ import annotations

import functools
import hashlib
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ops import paths
from ops.config import LOOPBACK_HOSTS

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Upstream's single-platform scripts never write <platform>/actions.jsonl or its simulation_end
# event, which is how the backend detects the end of the rounds; only the parallel script does.
PLATFORMS = ("parallel",)
STAGES = ("ontology", "graph", "agents", "simulation", "prepare", "schedule", "rounds", "close_env", "report")
PREPARE_PATH = "/api/simulation/prepare"
# Must match the header and routes patch 0003 guards with MIROFISH_WRAPPER_TOKEN.
WRAPPER_TOKEN_HEADER = "X-MiroFish-Wrapper-Token"
WRAPPER_ONLY_PATHS = frozenset({"/api/simulation/create", PREPARE_PATH, "/api/report/generate"})


class ApiError(RuntimeError):
    """The backend answered with success=false, an HTTP error, or a failed task."""


class StopRun(RuntimeError):
    """A clean stop requested by the wrapper (kill switch, wall clock, admission, agent cap)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id or "") or ".." in run_id:
        raise ValueError(f"invalid run id {run_id!r}: use letters, digits, '_', '-', '.' (max 64)")
    return run_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_private_json(path: Path, payload: Dict[str, Any]) -> None:
    paths.write_private_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


class MiroFishClient:
    def __init__(self, base_url: str = "http://127.0.0.1:5001", timeout: float = 60.0,
                 wrapper_token: Optional[str] = None):
        host = urllib.parse.urlparse(base_url).hostname
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"refusing non-loopback MiroFish URL: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.wrapper_token = wrapper_token
        # An empty ProxyHandler ignores HTTP(S)_PROXY so requests can never leave the machine.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: Optional[bytes] = None,
                 content_type: Optional[str] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Accept-Language", "en")
        if self.wrapper_token and method == "POST" and path in WRAPPER_ONLY_PATHS:
            request.add_header(WRAPPER_TOKEN_HEADER, self.wrapper_token)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ApiError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path} -> {exc.reason}") from None
        if not payload.get("success", False):
            raise ApiError(f"{method} {path} -> {payload.get('error') or payload}")
        return payload.get("data") or {}

    def get(self, path: str, **kwargs) -> Dict[str, Any]:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        return self._request("POST", path, json.dumps(payload).encode("utf-8"), "application/json", **kwargs)

    def delete(self, path: str, **kwargs) -> Dict[str, Any]:
        return self._request("DELETE", path, **kwargs)

    def post_multipart(self, path: str, fields: Dict[str, str], files: Iterable[Path], **kwargs) -> Dict[str, Any]:
        boundary = f"mirofish-{uuid.uuid4().hex}"
        parts: List[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value.encode("utf-8") + b"\r\n"
            )
        for file_path in files:
            mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            filename = file_path.name.replace('"', "")
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n".encode() + file_path.read_bytes() + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request("POST", path, b"".join(parts), f"multipart/form-data; boundary={boundary}", **kwargs)

    def health(self) -> bool:
        try:
            with self._opener.open(self.base_url + "/health", timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def entity_counts(self, graph_id: str, entity_types: Optional[List[str]] = None) -> Dict[str, int]:
        """Agents prepare would create, by entity type. Reads the graph from Zep; no LLM call."""
        query = {"enrich": "false"}
        if entity_types:
            query["entity_types"] = ",".join(entity_types)
        data = self.get(f"/api/simulation/entities/{urllib.parse.quote(graph_id)}?{urllib.parse.urlencode(query)}")
        counts: Dict[str, int] = {}
        for entity in data.get("entities") or []:
            labels = [label for label in (entity.get("labels") or []) if label not in ("Entity", "Node")]
            kind = str(labels[0]) if labels else "unknown"
            counts[kind] = counts.get(kind, 0) + 1
        return counts


def poll(fetch: Callable[[], Dict[str, Any]], is_done: Callable[[Dict[str, Any]], bool], label: str,
         timeout_s: float, interval_s: float = 5.0, log: Callable[[str], None] = print,
         check: Callable[[], None] = lambda: None, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Call fetch() until is_done(result) or the timeout; check() runs every round and may raise StopRun."""
    deadline = time.monotonic() + timeout_s
    last_message = None
    while True:
        check()
        state = fetch()
        if str(state.get("status", "")).lower() == "failed" or state.get("runner_status") == "failed":
            raise ApiError(f"{label} failed: {state.get('error') or state.get('message') or state}")
        if is_done(state):
            return state
        message = state.get("message") or state.get("runner_status")
        progress = state.get("progress", state.get("progress_percent"))
        summary = f"{label}: {progress}% {message or ''}".strip()
        if summary != last_message:
            log(summary)
            last_message = summary
        if time.monotonic() > deadline:
            raise ApiError(f"{label} did not finish within {int(timeout_s)}s (last state: {summary})")
        sleep(interval_s)


def rounds_done(state: Dict[str, Any], platform: str) -> bool:
    if state.get("runner_status") in ("completed", "stopped"):
        return True
    need_twitter = platform in ("twitter", "parallel")
    need_reddit = platform in ("reddit", "parallel")
    return (not need_twitter or bool(state.get("twitter_completed"))) and \
           (not need_reddit or bool(state.get("reddit_completed")))


class RunHooks:
    """What the batch plugs into a run; the defaults do nothing."""

    def before_stage(self, name: str, attempt: int) -> None:
        """Runs before each stage that is not already done; may raise StopRun."""

    def check(self) -> None:
        """Runs on every poll; may raise StopRun (kill switch, wall clock)."""

    def agents(self, graph_id: str, entity_types: Optional[List[str]]) -> Dict[str, Any]:
        """The agent-count check after the graph is built; may raise StopRun."""
        return {}

    def after_prepare(self, simulation_id: str, expected_agents: Optional[int]) -> Dict[str, Any]:
        """Checks the files prepare wrote against the agent count; may raise StopRun."""
        return {}

    def before_rounds(self, simulation_id: str) -> Dict[str, Any]:
        """The 'schedule' stage, after prepare."""
        return {}

    def zep_resources(self, project_id: str, graph_ids: List[str], batch_ids: List[str]) -> None:
        """Called as soon as new Zep graph or batch ids appear in upstream's project."""


class RunPipeline:
    """One MiroFish run with a resumable manifest in ops/runs/<RUN_ID>/."""

    def __init__(self, client: MiroFishClient, run_id: str, runs_dir: Optional[Path] = None,
                 log: Callable[[str], None] = print, poll_interval_s: float = 5.0,
                 hooks: Optional[RunHooks] = None, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.run_id = validate_run_id(run_id)
        self.run_dir = (runs_dir or paths.RUNS_DIR) / run_id
        self.manifest_path = self.run_dir / "manifest.json"
        self.log = log
        self.hooks = hooks or RunHooks()
        self._poll = functools.partial(poll, log=log, interval_s=poll_interval_s, check=self.hooks.check, sleep=sleep)
        self.manifest: Dict[str, Any] = {}
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        write_private_json(self.manifest_path, self.manifest)

    def _stage(self, name: str, action: Callable[[], Dict[str, Any]],
               on_stop: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        stages = self.manifest.setdefault("stages", {})
        previous = stages.get(name, {})
        if previous.get("status") == "done":
            self.log(f"[{name}] already done, skipping")
            return previous["result"]
        attempt = int(previous.get("attempt") or 0) + 1
        history = list(previous.get("history") or [])
        if previous.get("status"):
            history.append({k: previous.get(k) for k in ("attempt", "status", "reason", "error",
                                                         "started_at", "ended_at")})
        stages[name] = {"status": "pending", "attempt": attempt, "history": history}
        self._save()
        self.hooks.before_stage(name, attempt)
        started = time.time()
        stages[name].update(status="running", started_at=started)
        self._save()
        try:
            result = action()
        except StopRun as stop:
            if on_stop is not None:
                try:
                    on_stop()
                except ApiError as exc:
                    self.log(f"[{name}] could not stop cleanly: {exc}")
            stages[name].update(status="interrupted", reason=stop.reason, error=str(stop), ended_at=time.time())
            self._save()
            raise
        except BaseException as exc:
            stages[name].update(status="failed", error=str(exc)[:500], ended_at=time.time())
            self._save()
            raise
        stages[name].update(status="done", result=result, ended_at=time.time(),
                            seconds=round(time.time() - started, 1))
        self._save()
        self.log(f"[{name}] done in {stages[name]['seconds']}s")
        return result

    def run(self, seed_files: List[Path], requirement: str, *, max_rounds: int, platform: str = "parallel",
            entity_types: Optional[List[str]] = None, project_name: Optional[str] = None,
            stage_timeout_s: float = 4 * 3600, stall_timeout_s: float = 1800) -> Dict[str, Any]:
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}: upstream's single-platform scripts never "
                             "report the end of the rounds, so the run would wait forever")
        if not 1 <= int(max_rounds) <= 40:
            raise ValueError("max_rounds must be between 1 and 40 (hard ceiling)")
        inputs = {
            "seed_files": [{"name": p.name, "sha256": sha256_file(p)} for p in seed_files],
            "requirement_sha256": hashlib.sha256(requirement.encode("utf-8")).hexdigest(),
            "max_rounds": int(max_rounds),
            "platform": platform,
            "enable_graph_memory_update": False,
        }
        if self.manifest.get("inputs") and self.manifest["inputs"] != inputs:
            raise ApiError(f"run {self.run_id} exists with different inputs; use a new run id")
        prepare_started = (self.manifest.get("stages", {}).get("prepare") or {}).get("status") in ("running", "done")
        if prepare_started and self.manifest.get("entity_types") != entity_types:
            raise ApiError(f"run {self.run_id}: personas were already generated for entity types "
                           f"{self.manifest.get('entity_types')}; use a new run id to change them")
        self.manifest.setdefault("run_id", self.run_id)
        self.manifest.setdefault("created_at", time.time())
        self.manifest["inputs"] = inputs
        self.manifest["entity_types"] = entity_types
        paths.make_private_dir(self.run_dir)
        self._save()
        c = self.client

        def ontology():
            data = c.post_multipart("/api/graph/ontology/generate",
                                    {"simulation_requirement": requirement,
                                     "project_name": project_name or self.run_id},
                                    seed_files, timeout=1800)
            return {"project_id": data["project_id"]}

        project_id = self._stage("ontology", ontology)["project_id"]
        self.manifest["project_id"] = project_id

        def remember_zep() -> None:
            """Upstream saves the graph and Zep batch ids in its project before creating them; copy them now."""
            try:
                project = c.get(f"/api/graph/project/{urllib.parse.quote(project_id)}")
            except ApiError as exc:
                self.log(f"[graph] could not read the project to record Zep ids yet: {exc}")
                return
            zep = self.manifest.setdefault("zep", {"graph_ids": [], "batch_ids": []})
            changed = False
            for field, key in (("graph_id", "graph_ids"), ("zep_batch_id", "batch_ids")):
                value = project.get(field)
                if value and value not in zep[key]:
                    zep[key].append(value)
                    changed = True
            if changed:
                self._save()
                self.hooks.zep_resources(project_id, list(zep["graph_ids"]), list(zep["batch_ids"]))

        def graph():
            task_id = c.post("/api/graph/build", {"project_id": project_id})["task_id"]

            def fetch():
                remember_zep()
                return c.get(f"/api/graph/task/{task_id}")

            try:
                state = self._poll(fetch, lambda s: s.get("status") == "completed", "graph build", stage_timeout_s)
            finally:
                remember_zep()
            return dict(state.get("result") or {})

        graph_id = self._stage("graph", graph)["graph_id"]
        self.manifest["graph_id"] = graph_id
        self._save()

        if (self.manifest["stages"].get("agents") or {}).get("status") == "done" and \
                self.manifest["stages"]["agents"]["result"].get("entity_types") != entity_types:
            self.manifest["stages"]["agents"]["status"] = "superseded"
        agents = self._stage("agents", lambda: self.hooks.agents(graph_id, entity_types))

        def simulation():
            data = c.post("/api/simulation/create", {
                "project_id": project_id, "graph_id": graph_id,
                "enable_twitter": platform in ("twitter", "parallel"),
                "enable_reddit": platform in ("reddit", "parallel"),
            })
            return {"simulation_id": data["simulation_id"]}

        simulation_id = self._stage("simulation", simulation)["simulation_id"]
        self.manifest["simulation_id"] = simulation_id

        def prepare():
            payload: Dict[str, Any] = {"simulation_id": simulation_id, "use_llm_for_profiles": True}
            if entity_types:
                payload["entity_types"] = entity_types
            history = self.manifest["stages"]["prepare"].get("history") or []
            if history and history[-1].get("reason") == "prepare_mismatch":
                # Upstream would otherwise return the mismatched files as already prepared.
                payload["force_regenerate"] = True
            data = c.post(PREPARE_PATH, payload, timeout=300)
            if not data.get("already_prepared"):
                task_id = data.get("task_id")
                self._poll(lambda: c.post("/api/simulation/prepare/status",
                                          {"task_id": task_id, "simulation_id": simulation_id}),
                           lambda s: s.get("status") in ("completed", "ready"), "prepare", stage_timeout_s)
            checked = self.hooks.after_prepare(simulation_id, agents.get("agents"))
            return {"expected_entities_count": data.get("expected_entities_count"), **checked}

        self._stage("prepare", prepare)
        self._stage("schedule", lambda: self.hooks.before_rounds(simulation_id))

        def stop_simulation():
            c.post("/api/simulation/stop", {"simulation_id": simulation_id}, timeout=120)

        def rounds():
            status_path = f"/api/simulation/{simulation_id}/run-status"
            current = c.get(status_path).get("runner_status")
            if current in ("starting", "running", "paused", "completed"):
                self.log(f"rounds: simulation already {current}; not restarting it")
            else:
                # force=True makes upstream discard a stopped/failed attempt and start clean (round 0).
                c.post("/api/simulation/start", {
                    "simulation_id": simulation_id, "platform": platform, "max_rounds": int(max_rounds),
                    "enable_graph_memory_update": False,
                    "force": current in ("stopped", "failed"),
                }, timeout=120)
            progress = {"key": None, "since": time.monotonic()}

            def fetch_with_stall_check() -> Dict[str, Any]:
                state = c.get(status_path)
                key = (state.get("current_round"), state.get("total_actions_count"), state.get("runner_status"))
                if key != progress["key"]:
                    progress.update(key=key, since=time.monotonic())
                elif not rounds_done(state, platform) and time.monotonic() - progress["since"] > stall_timeout_s:
                    raise ApiError(f"rounds: no progress for {int(stall_timeout_s)}s (round {key[0]}, "
                                   f"{key[1]} actions, {key[2]}); check the simulation directory's logs")
                return state

            state = self._poll(fetch_with_stall_check, lambda s: rounds_done(s, platform), "rounds", stage_timeout_s)
            keys = ("current_round", "total_rounds", "twitter_actions_count", "reddit_actions_count",
                    "total_actions_count", "runner_status", "started_at")
            return {k: state.get(k) for k in keys}

        self._stage("rounds", rounds, on_stop=stop_simulation)

        def close_env():
            try:
                c.post("/api/simulation/close-env", {"simulation_id": simulation_id, "timeout": 60}, timeout=120)
            except ApiError as exc:
                self.log(f"close-env: {exc} (continuing; the environment may already be closed)")
            state = self._poll(lambda: c.get(f"/api/simulation/{simulation_id}/run-status"),
                               lambda s: s.get("runner_status") in ("completed", "stopped", "idle"),
                               "close-env", 600)
            return {"runner_status": state.get("runner_status"), "completed_at": state.get("completed_at")}

        self._stage("close_env", close_env)

        def report():
            data = c.post("/api/report/generate", {"simulation_id": simulation_id}, timeout=120)
            report_id = data.get("report_id")
            task_id = data.get("task_id")
            if report_id:
                self.manifest["report_id"] = report_id  # recorded now so purge finds a half-built report
                self._save()
            if not data.get("already_generated") and task_id:
                state = self._poll(lambda: c.post("/api/report/generate/status",
                                                  {"task_id": task_id, "simulation_id": simulation_id}),
                                   lambda s: s.get("status") == "completed", "report", stage_timeout_s)
                report_id = report_id or (state.get("result") or {}).get("report_id") or state.get("report_id")
            content = c.get(f"/api/report/{report_id}").get("markdown_content") or ""
            paths.write_private_text(self.run_dir / "report.md", content)
            return {"report_id": report_id, "report_file": "report.md", "chars": len(content)}

        report_result = self._stage("report", report)
        self.manifest["report_id"] = report_result["report_id"]
        self._save()
        return self.manifest
