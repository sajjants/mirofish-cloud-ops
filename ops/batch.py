"""Batches: a measured pilot first, then (after a typed confirmation) the planned real runs.

  pilot: the first question, variant v0, rounds.pilot rounds, at most pilot.max_agents agents, inside
         pilot.max_spend. Prints measurements, the gate results and the real-run plan, then stops.
  run:   refuses unless the pilot passed its gates; plans (high estimate within what is left of the
         caps), asks for confirmation, runs. Re-running resumes; finished runs are skipped.

Before every stage the relay is told which run and stage is active, and the stage's high
estimate must fit in what is left of the budget. During a stage, every poll checks the relay for
a kill-switch trip and the batch's active time against wall_clock_minutes. A stop marks the stage
'interrupted'; the next invocation resumes there. Each run's manifest records spend per stage,
wasted spend from interrupted attempts, the models and efforts used, and the price file used.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

import psutil

from ops import envfile, leakgate, paths, zep_usage
from ops.budget import BATCH_ID, Budget
from ops.config import LLMConfig, load_llm_config
from ops.confirm import NotConfirmed, confirm
from ops.limits import load_limits
from ops.mirofish_api import ApiError, MiroFishClient, RunHooks, RunPipeline, StopRun, sha256_file, \
    write_private_json
from ops.plan import COST_STAGES, format_plan, low_high, make_plan, measured_model, prompt_growth_per_round, \
    run_estimate, summarize, unmeasured_model
from ops.pricing import Prices, load_prices
from ops.relay import load_relay_state
from ops.schedule import SIMULATIONS_DIR, apply_clock_shift
from ops.zep import ZepResources
from ops.zep_usage import format_estimate, load_zep_limits

_LOAD = object()   # "read ops/config/zep_limits.yaml"; None means "no quotas configured"

PILOT = "pilot"
REAL = "real"
WARN_FRACTION = 0.9


class BatchError(RuntimeError):
    pass


def batch_dir(batch_id: str) -> Path:
    if not BATCH_ID.fullmatch(batch_id or ""):
        raise BatchError(f"invalid batch id {batch_id!r}: letters, digits and '-' only (max 40)")
    return paths.RUNS_DIR / batch_id


def git_commit(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


class BatchLock:
    """Only one batch at a time (calls are attributed to the active run). Works on any OS."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.STATE_DIR / "batch.lock"

    @staticmethod
    def _owner(pid: int) -> str:
        return f"{pid} {psutil.Process(pid).create_time():.3f}"

    def _holder(self) -> Optional[int]:
        """The pid holding the lock, or None if the lock is stale (its process is gone or the pid was reused)."""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            pid = int(text.split()[0])
            return pid if self._owner(pid) == text else None
        except (OSError, ValueError, IndexError, psutil.Error):
            return None

    def __enter__(self) -> "BatchLock":
        paths.make_private_dir(self.path.parent)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pid = self._holder()
                if pid is not None:
                    raise BatchError(f"another batch command is running (pid {pid}); batches must not overlap")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._owner(os.getpid()))
            return self
        raise BatchError(f"could not take the batch lock {self.path}")

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)


class RelayControl:
    def __init__(self, port: Optional[int] = None, state: Optional[Dict[str, Any]] = None, timeout: float = 30.0):
        self.state = state or load_relay_state()
        self.port = int(port or self.state.get("port") or 0)
        if not self.port:
            raise BatchError("the relay port is unknown; start the stack with `python -m ops start`")
        self.base = f"http://127.0.0.1:{self.port}"
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def llm_base_url(self) -> str:
        return f"{self.base}/v1"

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"X-Ops-Control": self.state["control_token"],
                                                  "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read() or b"{}").get("error")
            except ValueError:
                detail = None
            raise BatchError(f"relay {path}: HTTP {error.code} {detail or ''}".strip()) from None
        except (urllib.error.URLError, OSError) as error:
            raise BatchError(f"relay not reachable on 127.0.0.1:{self.port} ({error})") from None

    def health(self) -> bool:
        try:
            with self._opener.open(self.base + "/meter/health", timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def activate(self, **payload: Any) -> Dict[str, Any]:
        return self._call("POST", "/control/activate", payload)

    def deactivate(self) -> None:
        self._call("POST", "/control/deactivate", {})

    def status(self) -> Dict[str, Any]:
        return self._call("GET", "/control/status")

    def trip(self, batch_id: str, reason: str) -> None:
        self._call("POST", "/control/trip", {"batch_id": batch_id, "reason": reason})


def export_transcript(simulation_id: str, out_path: Path, simulations_dir: Path = SIMULATIONS_DIR) -> Dict:
    """Copy posts, comments, per-agent action counts and agent stances out of the simulation dir."""
    sim_dir = simulations_dir / simulation_id
    if not sim_dir.is_dir():
        raise BatchError(f"simulation directory {sim_dir} not found")
    config_path = sim_dir / "simulation_config.json"
    agents = []
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        keep = ("agent_id", "entity_name", "entity_type", "stance", "sentiment_bias", "activity_level",
                "influence_weight")
        agents = [{k: a.get(k) for k in keep} for a in config.get("agent_configs", [])]
    transcript: Dict[str, Any] = {"simulation_id": simulation_id, "agents": agents, "platforms": {}}
    for platform in ("reddit", "twitter"):
        db = sim_dir / f"{platform}_simulation.db"
        if not db.exists():
            continue
        connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            users = {r["user_id"]: dict(r) for r in connection.execute(
                "SELECT user_id, agent_id, user_name, name FROM user")}
            posts = [dict(r) for r in connection.execute(
                "SELECT post_id, user_id, original_post_id, content, quote_content, created_at, num_likes, "
                "num_dislikes, num_shares FROM post ORDER BY post_id")]
            comments = [dict(r) for r in connection.execute(
                "SELECT comment_id, post_id, user_id, content, created_at, num_likes, num_dislikes "
                "FROM comment ORDER BY comment_id")]
            actions = [dict(r) for r in connection.execute(
                "SELECT user_id, action, COUNT(*) AS n FROM trace GROUP BY user_id, action")]
        finally:
            connection.close()
        transcript["platforms"][platform] = {"users": list(users.values()), "posts": posts,
                                             "comments": comments, "actions": actions}
    write_private_json(out_path, transcript)
    return {"posts": sum(len(p["posts"]) for p in transcript["platforms"].values()),
            "comments": sum(len(p["comments"]) for p in transcript["platforms"].values()),
            "agents": len(agents)}


class BatchHooks(RunHooks):
    def __init__(self, batch: "Batch", run_id: str, kind: str, rounds: int):
        self.batch = batch
        self.run_id = run_id
        self.kind = kind
        self.rounds = rounds
        self.agent_count: Optional[int] = None

    @property
    def agent_cap(self) -> int:
        limits = self.batch.limits
        return limits["pilot"]["max_agents"] if self.kind == PILOT else limits["caps"]["max_agents"]

    def before_stage(self, name: str, attempt: int) -> None:
        b = self.batch
        b.save_time()
        self._check_wall_clock()
        try:
            status = b.relay.activate(batch_id=b.batch_id, run_id=self.run_id, kind=self.kind, stage=name,
                                      attempt=attempt, caps={"max_spend": b.limits["caps"]["max_spend"],
                                                             "max_llm_calls": b.limits["caps"]["max_llm_calls"]},
                                      run_max_spend=b.limits["pilot"]["max_spend"] if self.kind == PILOT else None)
        except BatchError as error:
            raise StopRun("relay", f"the relay refused to start stage {name}: {error}") from None
        if name in COST_STAGES:
            self._admit(name, status["budget"])

    def _admit(self, stage: str, budget: Dict[str, Any]) -> None:
        b = self.batch
        agents = self.agent_count or self.agent_cap
        estimate = run_estimate(b.cost_model(), b.llm, b.prices, rounds=self.rounds, agents=agents,
                                factor=b.limits["estimates"]["high_factor"], growth=True)["stages"][stage]
        caps = b.limits["caps"]
        spend_left = caps["max_spend"] - budget["spend"] - budget.get("reserved", 0.0)
        if self.kind == PILOT:
            run_spend = (budget.get("runs", {}).get(self.run_id) or {}).get("spend", 0.0)
            spend_left = min(spend_left, b.limits["pilot"]["max_spend"] - run_spend)
        calls_left = caps["max_llm_calls"] - budget["calls"]
        cur = b.prices.currency
        if estimate["spend"] > spend_left or estimate["calls"] > calls_left:
            raise StopRun("admission", f"stage {stage} could cost up to {cur} {estimate['spend']:.3f} and "
                          f"{estimate['calls']:.0f} calls (high estimate), but only {cur} {max(spend_left, 0):.3f} "
                          f"and {max(calls_left, 0)} calls are left. Stopped before starting it, so nothing is "
                          "wasted; raise the limits in limits.yaml and run the same command to resume.")

    def check(self) -> None:
        b = self.batch
        self._check_wall_clock()
        budget = (b.relay.status() or {}).get("budget") or {}
        for warning in budget.get("warnings", []):
            if warning["limit"] not in b.state["warnings"]:
                b.state["warnings"].append(warning["limit"])
                b.log(f"WARNING: 90% of {warning['limit']} used ({warning['used']} of {warning['cap']})")
        trip = budget.get("trip")
        if trip:
            raise StopRun(trip["reason"], f"kill switch: {trip['reason']} reached; the run stopped cleanly")

    def _check_wall_clock(self) -> None:
        b = self.batch
        minutes = b.active_minutes()
        cap = b.limits["caps"]["wall_clock_minutes"]
        if minutes >= cap:
            try:
                b.relay.trip(b.batch_id, "wall_clock_minutes")
            except BatchError:
                pass
            raise StopRun("wall_clock_minutes", f"the batch has used its {cap:.0f} minutes of active time")
        if minutes >= WARN_FRACTION * cap and "wall_clock_minutes" not in b.state["warnings"]:
            b.state["warnings"].append("wall_clock_minutes")
            b.log(f"WARNING: 90% of wall_clock_minutes used ({minutes:.0f} of {cap:.0f} min)")

    def agents(self, graph_id: str, entity_types: Optional[List[str]]) -> Dict[str, Any]:
        client = self.batch.client
        all_counts = client.entity_counts(graph_id)
        counts = client.entity_counts(graph_id, entity_types) if entity_types else all_counts
        total = sum(counts.values())
        cap = self.agent_cap
        listing = ", ".join(f"{kind} {n}" for kind, n in sorted(all_counts.items(), key=lambda kv: -kv[1]))
        if total == 0:
            raise StopRun("no_agents", f"no agents for entity types {entity_types}; types in the graph: {listing}")
        if total > cap:
            single = [kind for kind, n in counts.items() if n > cap]
            hint = (f" The type(s) {', '.join(single)} alone exceed the cap; trimming the seed is the only way "
                    "to get under it." if single else
                    " Choose entity types with --entity-types TYPE,TYPE (MiroFish creates one agent per entity "
                    "of the chosen types), then run the same command again.")
            raise StopRun("agent_cap", f"{total} agents would be created, more than the cap of {cap}. "
                          f"Entities by type: {listing}.{hint}")
        self.agent_count = total
        return {"agents": total, "graph_agents": sum(all_counts.values()), "counts": all_counts,
                "entity_types": entity_types}

    def before_rounds(self, simulation_id: str) -> Dict[str, Any]:
        b = self.batch
        result = apply_clock_shift(simulation_id, b.limits["simulation"]["clock_start_hour"], self.rounds, 2,
                                   b.simulations_dir)
        b.log(f"schedule: clock starts at {result['clock_start_hour']:02d}:00; expected agent turns in "
              f"{self.rounds} rounds: {result['expected_agent_turns']:.0f}")
        return result

    def zep_resources(self, project_id: str, graph_ids: List[str], batch_ids: List[str]) -> None:
        ZepResources(self.batch.batch_id).record(self.run_id, project_id, graph_ids, batch_ids)
        self.batch.log(f"[graph] recorded Zep resources for purge: graphs {', '.join(graph_ids) or '-'}; "
                       f"upload batches {', '.join(batch_ids) or '-'}")


class Batch:
    def __init__(self, batch_id: str, *, client: Optional[MiroFishClient] = None,
                 relay: Optional[RelayControl] = None, limits: Optional[Dict[str, Any]] = None,
                 llm: Optional[LLMConfig] = None, prices: Optional[Prices] = None,
                 log: Callable[[str], None] = print, ask: Optional[Callable[[str], str]] = None,
                 interactive: Optional[bool] = None, poll_interval_s: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 simulations_dir: Path = SIMULATIONS_DIR, terms: Optional[list] = None,
                 env_path: Path = envfile.ENV_PATH, out: Optional[TextIO] = None,
                 zep_limits: Any = _LOAD):
        self.batch_id = batch_id
        self.out = out
        self.zep_limits = load_zep_limits() if zep_limits is _LOAD else zep_limits
        self.dir = batch_dir(batch_id)
        inputs_path = self.dir / "inputs.json"
        if not inputs_path.exists():
            raise BatchError(f"{inputs_path} not found; prepare the batch inputs first")
        self.inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        self.limits = limits or load_limits()
        self.llm = llm or load_llm_config()
        self.prices = prices or load_prices()
        self.client = client or MiroFishClient(f"http://127.0.0.1:{envfile.values(env_path).get('FLASK_PORT') or 5001}")
        self.relay = relay or RelayControl()
        self.log = log
        self.ask = ask
        self.interactive = interactive
        self.poll_interval_s = poll_interval_s
        self.sleep = sleep
        self.clock = clock
        self.simulations_dir = simulations_dir
        self.terms = terms
        self.env_path = env_path
        self.state_path = self.dir / "batch.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else \
            {"batch_id": batch_id, "runs": {}, "active_seconds": 0.0, "warnings": [], "entity_types": None}
        self._session_start = clock()

    # ----- bookkeeping
    def _save_state(self) -> None:
        write_private_json(self.state_path, self.state)

    def save_time(self) -> None:
        now = self.clock()
        self.state["active_seconds"] = round(self.state["active_seconds"] + now - self._session_start, 1)
        self._session_start = now
        self._save_state()

    def active_minutes(self) -> float:
        return (self.state["active_seconds"] + self.clock() - self._session_start) / 60

    def budget(self) -> Budget:
        return Budget(self.batch_id)

    def pilot_run_id(self) -> str:
        return f"{self.batch_id}_pilot"

    def cost_model(self) -> Dict[str, Any]:
        pilot = self.dir / "pilot.json"
        if pilot.exists():
            return json.loads(pilot.read_text(encoding="utf-8"))["cost_model"]
        return unmeasured_model(self.limits)

    def used(self) -> Dict[str, float]:
        status = self.budget().status()
        return {"spend": status["spend"], "calls": status["calls"], "minutes": self.active_minutes()}

    # ----- inputs
    def variants(self) -> List[Dict[str, Any]]:
        path = self.dir / "variants.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["variants"]
        if self.inputs.get("unknowns"):
            raise BatchError("the seed has UNKNOWN facts but no variants.json; generate the variants first")
        seed = self.dir / "inputs" / "seed.md"
        return [{"index": 0, "choices": {}, "seed_file": "inputs/seed.md", "seed_sha256": sha256_file(seed)}]

    def zep_estimate(self, agents_per_run: List[int]) -> Dict[str, Any]:
        seed_characters = max(len((self.dir / v["seed_file"]).read_text(encoding="utf-8")) for v in self.variants())
        open_graphs = len(ZepResources(self.batch_id).open_ids()["graphs"])
        return zep_usage.estimate(seed_characters, agents_per_run, self.zep_limits, open_graphs)

    def requirement(self, question_id: str) -> str:
        question = next(q for q in self.inputs["questions"] if q["id"] == question_id)
        return (self.dir / question["requirement_file"]).read_text(encoding="utf-8")

    # ----- checks before anything is uploaded
    def preflight(self, specs: List[Dict[str, Any]]) -> None:
        missing = self.llm.placeholders()
        if missing:
            raise BatchError(f"model names are placeholders for {', '.join(missing)} in ops/config/llm.yaml")
        unpriced = self.prices.missing(s.model for s in self.llm.stages.values())
        if unpriced:
            raise BatchError(f"prices.yaml has no price for {', '.join(unpriced)}")
        if not self.inputs.get("pseudonymized"):
            raise BatchError("this batch is not pseudonymised; the cloud profile only uploads pseudonymised seeds")
        variants = self.variants()
        sources: Dict[str, str] = {}
        for spec in specs:
            if spec["variant"] >= len(variants):
                raise BatchError(f"{spec['run_id']} needs variant v{spec['variant']}, but only {len(variants)} exist")
            variant = variants[spec["variant"]]
            sources[f"seed (variant v{variant['index']})"] = (self.dir / variant["seed_file"]).read_text("utf-8")
            sources[f"requirement {spec['question']}"] = self.requirement(spec["question"])
        try:
            leakgate.check(sources, self.terms)
        except leakgate.LeakGateError as error:
            raise BatchError(str(error)) from None
        if not self.client.health():
            raise BatchError("MiroFish is not answering; start it with `python -m ops start`")
        if not self.relay.health():
            raise BatchError("the relay is not answering; start the stack with `python -m ops start`")
        self.relay.status()
        env = envfile.values(self.env_path)
        if env.get("LLM_BASE_URL", "").rstrip("/") != self.relay.llm_base_url:
            raise BatchError(f"mirofish/.env LLM_BASE_URL must be {self.relay.llm_base_url} (the relay), or calls "
                             "would bypass metering and the kill switch")
        if env.get("LLM_API_KEY") != self.relay.state["session_token"]:
            raise BatchError("mirofish/.env LLM_API_KEY is not the relay's session token; restart with "
                             "`python -m ops start`")
        boosted = [k for k in ("LLM_BOOST_API_KEY", "LLM_BOOST_BASE_URL", "LLM_BOOST_MODEL_NAME") if env.get(k)]
        if boosted:
            raise BatchError(f"mirofish/.env sets {', '.join(boosted)}; boost calls would bypass the relay (D2)")
        if not env.get("ZEP_API_KEY"):
            raise BatchError("mirofish/.env has no ZEP_API_KEY; MiroFish needs it for Zep Cloud "
                             "(`python -m ops start` writes it from the environment or the OS keychain)")
        if env.get("ZEP_API_URL"):
            raise BatchError("mirofish/.env sets ZEP_API_URL; remove it (only Zep Cloud is allowed)")

    # ----- one run
    def execute(self, spec: Dict[str, Any], kind: str) -> Dict[str, Any]:
        run_id = spec["run_id"]
        record = self.state["runs"].setdefault(run_id, {"question": spec["question"], "variant": spec["variant"],
                                                        "rounds": spec["rounds"], "kind": kind})
        if record.get("status") == "done":
            self.log(f"{run_id}: already done, skipping")
            return record
        variant = self.variants()[spec["variant"]]
        hooks = BatchHooks(self, run_id, kind, spec["rounds"])
        pipeline = RunPipeline(self.client, run_id, log=self.log, poll_interval_s=self.poll_interval_s,
                               hooks=hooks, sleep=self.sleep)
        agents_stage = (pipeline.manifest.get("stages", {}).get("agents") or {})
        if agents_stage.get("status") == "done":
            hooks.agent_count = agents_stage["result"].get("agents")
        models = {group: {"model": s.model, "reasoning_effort": s.reasoning_effort}
                  for group, s in self.llm.stages.items()}
        existing = pipeline.manifest.get("batch")
        if existing and existing.get("models") != models:
            raise BatchError(f"{run_id} started with different models or efforts ({existing.get('models')}); "
                             "a run must not mix them. Restore llm.yaml or start a new batch.")
        if not existing:
            pipeline.manifest["batch"] = {
                "batch_id": self.batch_id, "kind": kind, "question": spec["question"],
                "variant": {"index": variant["index"], "choices": variant.get("choices")},
                "seed": {"source_sha256": self.inputs["sources"]["seed"]["sha256"],
                         "variant_seed_sha256": variant["seed_sha256"], "pseudonymized": True},
                "models": models,
                "prices": {"sha256": self.prices.sha256, "checked_on": self.prices.checked_on,
                           "currency": self.prices.currency},
                "provenance": {"upstream": git_commit(paths.UPSTREAM_DIR), "ops": git_commit(paths.REPO_ROOT),
                               "patches": [{"name": p.name, "sha256": sha256_file(p)}
                                           for p in sorted((paths.OPS_DIR / "patches").glob("*.patch"))]},
            }
        pipeline.manifest["batch"]["limits"] = {k: v for k, v in self.limits.items() if not k.startswith("_")}
        record.update(status="running", started_at=record.get("started_at") or time.time())
        self._save_state()
        try:
            manifest = pipeline.run([self.dir / variant["seed_file"]], self.requirement(spec["question"]),
                                    max_rounds=spec["rounds"], platform=self.limits["simulation"]["platform"],
                                    entity_types=self.state.get("entity_types"), project_name=run_id)
        except StopRun as stop:
            record.update(status="stopped", reason=stop.reason)
            self._record_spend(pipeline)
            raise
        except BaseException as exc:
            record.update(status="failed", error=str(exc)[:500])
            self._record_spend(pipeline)
            raise
        finally:
            try:
                self.relay.deactivate()
            except BatchError:
                pass
            self.save_time()
        spend = self._record_spend(pipeline)
        exported = export_transcript(manifest["simulation_id"], pipeline.run_dir / "transcript.json",
                                     self.simulations_dir)
        pipeline.manifest["batch"]["transcript"] = exported
        pipeline._save()
        record.update(status="done", ended_at=time.time(), spend=spend["total"], wasted=spend["wasted"],
                      calls=spend["calls"])
        self._save_state()
        return record

    def _record_spend(self, pipeline: RunPipeline) -> Dict[str, Any]:
        rows = self.budget().ledger(pipeline.run_id)
        stages = pipeline.manifest.get("stages", {})
        per_stage: Dict[str, Any] = {}
        wasted = 0.0
        for name, stage in stages.items():
            final = int(stage.get("attempt") or 1)
            mine = [r for r in rows if r.get("stage") == name]
            current = [r for r in mine if int(r.get("attempt") or 1) == final]
            earlier = sum(float(r.get("cost") or 0) for r in mine if int(r.get("attempt") or 1) != final)
            if stage.get("status") != "done":
                earlier += sum(float(r.get("cost") or 0) for r in current)
            wasted += earlier
            summary = summarize(current)
            per_stage[name] = {"spend": summary["spend"], "calls": summary["requests"],
                               "prompt_tokens": summary["prompt_tokens"], "output_tokens": summary["output_tokens"],
                               "reasoning_tokens": summary["reasoning_tokens"], "wasted_spend": round(earlier, 6)}
        total = round(sum(float(r.get("cost") or 0) for r in rows), 6)
        spend = {"currency": self.prices.currency, "total": total, "wasted": round(wasted, 6),
                 "calls": len([r for r in rows if r.get("outcome") != "refused"]), "stages": per_stage,
                 "warnings": list(self.state["warnings"])}
        pipeline.manifest.setdefault("batch", {})["spend"] = spend
        pipeline._save()
        return spend

    # ----- pilot
    def pilot_summary(self, run_id: str) -> Dict[str, Any]:
        manifest = json.loads((paths.RUNS_DIR / run_id / "manifest.json").read_text(encoding="utf-8"))
        stages = manifest["stages"]
        rows = self.budget().ledger(run_id)
        final = {name: [r for r in rows if r.get("stage") == name and int(r.get("attempt") or 1) ==
                        int(stage.get("attempt") or 1)] for name, stage in stages.items()}
        schedule_result = (stages.get("schedule") or {}).get("result") or {}
        rounds_result = (stages.get("rounds") or {}).get("result") or {}
        agents_result = (stages.get("agents") or {}).get("result") or {}
        rounds = int(rounds_result.get("current_round") or manifest["inputs"]["max_rounds"])
        summary = {
            "run_id": run_id,
            "rounds_planned": manifest["inputs"]["max_rounds"],
            "rounds": rounds,
            "agents": agents_result.get("agents"),
            "graph_agents": agents_result.get("graph_agents"),
            "schedule": schedule_result.get("schedule"),
            "expected_turns": schedule_result.get("expected_agent_turns"),
            "stage_seconds": {name: float(s.get("seconds") or 0) for name, s in stages.items()},
            "stages": {name: summarize(r) for name, r in final.items() if r},
            "groups": {g: summarize([r for r in rows if r.get("group") == g]) for g in ("setup", "simulation", "report")},
            "total": summarize(rows),
            "prompt_growth_per_round": prompt_growth_per_round(final.get("rounds", []), rounds),
            "spend": manifest.get("batch", {}).get("spend"),
            "report_file": str(paths.RUNS_DIR / run_id / "report.md"),
            "measured_at": time.time(),
        }
        summary["cost_model"] = measured_model(summary, self.limits["caps"]["max_agents"])
        summary["gates"] = self.gates(summary)
        return summary

    def gates(self, summary: Dict[str, Any]) -> List[str]:
        g, total = self.limits["pilot_gates"], summary["total"]
        failures = []
        for key, limit_key, label in (("failure_rate", "max_failure_rate", "failure rate"),
                                      ("refusal_rate", "max_refusal_rate", "refusal rate"),
                                      ("structured_failure_rate", "max_structured_failure_rate",
                                       "structured-output failure rate")):
            if total[key] > g[limit_key]:
                failures.append(f"{label} {total[key]:.1%} > {g[limit_key]:.1%}")
        if summary["rounds"] < summary["rounds_planned"]:
            failures.append(f"only {summary['rounds']} of {summary['rounds_planned']} rounds completed")
        return failures

    def pilot(self) -> Dict[str, Any]:
        spec = {"run_id": self.pilot_run_id(), "question": self.inputs["questions"][0]["id"], "variant": 0,
                "rounds": self.limits["rounds"]["pilot"]}
        self.preflight([spec])
        estimate = low_high(unmeasured_model(self.limits), self.limits, self.llm, self.prices, spec["rounds"],
                            agents_low=self.limits["pilot"]["max_agents"], agents_high=self.limits["pilot"]["max_agents"])
        cur = self.prices.currency
        confirm(f"PILOT {spec['run_id']}: question {spec['question']}, variant v0, {spec['rounds']} rounds, "
                f"at most {self.limits['pilot']['max_agents']} agents",
                [f"estimated spend {cur} {estimate['low']['spend']:.2f} - {estimate['high']['spend']:.2f} "
                 "(unmeasured estimate)",
                 f"hard ceilings: pilot {cur} {self.limits['pilot']['max_spend']:.2f}, batch {cur} "
                 f"{self.limits['caps']['max_spend']:.2f}, {self.limits['caps']['max_llm_calls']} calls, "
                 f"{self.limits['caps']['wall_clock_minutes']:.0f} min",
                 *format_estimate(self.zep_estimate([self.limits["pilot"]["max_agents"]]))],
                f"send {self.batch_id}", self.llm.base_url, ask=self.ask, out=self.out, interactive=self.interactive)
        self.execute(spec, PILOT)
        summary = self.pilot_summary(spec["run_id"])
        write_private_json(self.dir / "pilot.json", summary)
        plan = self.make_plan()
        write_private_json(self.dir / "plan.json", plan)
        return summary

    # ----- real runs
    def make_plan(self) -> Dict[str, Any]:
        plan = make_plan(self.limits, self.llm, self.prices, self.cost_model(),
                         [q["id"] for q in self.inputs["questions"]], len(self.inputs.get("unknowns") or []),
                         used=self.used(), batch_id=self.batch_id)
        pending = [s for s in plan["runs"] if self.state["runs"].get(s["run_id"], {}).get("status") != "done"]
        plan["zep"] = self.zep_estimate([plan["per_run"]["high"]["agents"]] * len(pending))
        return plan

    def run(self, replan: bool = False) -> List[Dict[str, Any]]:
        pilot_path = self.dir / "pilot.json"
        if not pilot_path.exists():
            raise BatchError(f"no pilot for {self.batch_id}; run `python -m ops pilot --batch {self.batch_id}` first")
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
        if pilot.get("gates"):
            raise BatchError("the pilot failed its gates: " + "; ".join(pilot["gates"]) +
                             ". Fix the cause (or change pilot_gates in limits.yaml) and run the pilot again.")
        plan_path = self.dir / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() and not replan else None
        if plan is None or not plan.get("confirmed_at"):
            plan = self.make_plan()
        if not plan["feasible"]:
            raise BatchError(format_plan(plan))
        pending = [s for s in plan["runs"] if self.state["runs"].get(s["run_id"], {}).get("status") != "done"]
        if not pending:
            self.log("all planned runs are done")
            return []
        self.preflight(pending)
        confirm(f"RUN batch {self.batch_id}: {len(pending)} run(s) to start or resume",
                format_plan(plan).splitlines(), f"run {self.batch_id}", self.llm.base_url,
                ask=self.ask, out=self.out, interactive=self.interactive)
        plan.setdefault("confirmed_at", time.time())
        write_private_json(plan_path, plan)
        results = []
        for spec in pending:
            self.log(f"=== {spec['run_id']}: {spec['question']}, variant v{spec['variant']}, {spec['rounds']} rounds")
            results.append(self.execute(spec, REAL))
        return results


def format_pilot(summary: Dict[str, Any], plan: Dict[str, Any], currency: str) -> str:
    total = summary["total"]
    lines = [
        f"PILOT {summary['run_id']}: {summary['rounds']} of {summary['rounds_planned']} rounds, "
        f"{summary['agents']} agents ({summary['graph_agents']} in the graph)",
        f"  spend {currency} {total['spend']:.4f}; {total['requests']} calls; tokens in {total['prompt_tokens']}, "
        f"out {total['output_tokens']} (thinking reported: {total['reasoning_tokens']})",
        f"  latency median {total['latency_median_s']}s, p95 {total['latency_p95_s']}s; retries {total['retries']}, "
        f"429s {total['rate_limited']}, 5xx {total['server_errors']}",
        f"  refusal rate {total['refusal_rate']:.1%}, failure rate {total['failure_rate']:.1%}, "
        f"structured-output failure rate {total['structured_failure_rate']:.1%} "
        f"({total['json_failures']} bad JSON of {total['json_calls']}, {total['tool_argument_failures']} bad tool "
        f"calls; no action in {total['tool_turns_without_action']} of {total['tool_turns']} agent turns); "
        f"truncated {total['truncated']}",
        "  by stage group:",
    ]
    for group, g in summary["groups"].items():
        lines.append(f"    {group:10} {g['requests']} calls, {currency} {g['spend']:.4f}, "
                     f"median {g['latency_median_s']}s, refusals {g['refused_by_provider']}, failed {g['failed']}")
    lines.append("  gates: " + ("passed" if not summary["gates"] else "FAILED: " + "; ".join(summary["gates"])))
    lines.append(f"  judge the report yourself: {summary['report_file']}")
    lines += ["", format_plan(plan)]
    return "\n".join(lines)


def format_status(batch: Batch) -> str:
    status = batch.budget().status()
    cur = batch.prices.currency
    lines = [f"Batch {batch.batch_id}: spend {cur} {status['spend']:.4f} of {batch.limits['caps']['max_spend']:.2f}, "
             f"{status['calls']} of {batch.limits['caps']['max_llm_calls']} calls, "
             f"{batch.active_minutes():.0f} of {batch.limits['caps']['wall_clock_minutes']:.0f} min"
             + (f"; STOPPED: {status['trip']['reason']}" if status.get("trip") else "")]
    for run_id, record in sorted(batch.state["runs"].items()):
        lines.append(f"  {run_id:40} {record.get('kind', ''):6} {record.get('status', ''):8} "
                     f"{record.get('question')} v{record.get('variant')} {record.get('rounds')} rounds "
                     f"{cur} {record.get('spend', 0) or 0:.4f}" + (f" ({record['reason']})" if record.get("reason") else ""))
    return "\n".join(lines)


def run_command(command: str, batch_id: str, replan: bool = False, entity_types: Optional[List[str]] = None) -> int:
    """Entry point for `python -m ops pilot|run|status|plan`."""
    try:
        if command in ("status", "plan"):
            batch = Batch(batch_id, relay=_OfflineRelay(), client=_OfflineClient())
            print(format_status(batch) if command == "status" else format_plan(batch.make_plan()))
            return 0
        with BatchLock():
            batch = Batch(batch_id)
            if entity_types is not None:
                batch.state["entity_types"] = entity_types or None
                batch._save_state()
            if command == "pilot":
                summary = batch.pilot()
                plan = json.loads((batch.dir / "plan.json").read_text(encoding="utf-8"))
                print(format_pilot(summary, plan, batch.prices.currency))
                print(f"\nSTOP. Review the pilot, then run: python -m ops run --batch {batch_id}")
                return 0
            batch.run(replan=replan)
            print(format_status(batch))
            return 0
    except StopRun as stop:
        print(f"STOPPED ({stop.reason}): {stop}\nRun the same command to resume after fixing the cause.")
        return 3
    except NotConfirmed as exc:
        print(str(exc))
        return 2
    except (BatchError, ApiError, leakgate.LeakGateError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 1


class _OfflineRelay(RelayControl):
    """status and plan read local files only; they never contact the relay."""

    def __init__(self) -> None:  # noqa: D401 - no network state needed
        self.state, self.port, self.base = {}, 0, ""


class _OfflineClient(MiroFishClient):
    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1"
