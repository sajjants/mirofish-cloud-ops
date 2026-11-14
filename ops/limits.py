"""Validate ops/config/limits.yaml into a plain dict the planner, relay and batch share."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ops.config import ConfigError, load_yaml
from ops.paths import CONFIG_DIR

ROUNDS_CEILING = 40
PLATFORMS = ("parallel",)


def _number(section: Dict[str, Any], key: str, where: str, *, integer: bool = False, positive: bool = True,
            at_most: float = None) -> Any:
    value = section.get(key)
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ConfigError(f"limits: {where}.{key} must be a{'n integer' if integer else ' number'} (got {value!r})")
    if (value <= 0 if positive else value < 0):
        raise ConfigError(f"limits: {where}.{key} must be {'> 0' if positive else '>= 0'} (got {value!r})")
    if at_most is not None and value > at_most:
        raise ConfigError(f"limits: {where}.{key} must be at most {at_most} (got {value!r})")
    return value if integer else float(value)


def _section(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"limits: section '{name}' is missing")
    return value


def validate_limits(raw: Dict[str, Any]) -> Dict[str, Any]:
    caps, rounds, runs, pilot, gates, provider, simulation, est = (
        _section(raw, n) for n in ("caps", "rounds", "runs", "pilot", "pilot_gates", "provider", "simulation",
                                   "estimates"))
    limits: Dict[str, Any] = {
        "caps": {
            "max_spend": _number(caps, "max_spend", "caps"),
            "max_llm_calls": _number(caps, "max_llm_calls", "caps", integer=True),
            "wall_clock_minutes": _number(caps, "wall_clock_minutes", "caps"),
            "max_agents": _number(caps, "max_agents", "caps", integer=True),
        },
        "rounds": {k: _number(rounds, k, "rounds", integer=True, at_most=ROUNDS_CEILING)
                   for k in ("pilot", "real_min", "real_max", "max_rounds")},
        "runs": {
            "per_question": _number(runs, "per_question", "runs", integer=True),
            "per_question_high_unknown": _number(runs, "per_question_high_unknown", "runs", integer=True),
            "unknown_threshold": _number(runs, "unknown_threshold", "runs", integer=True, positive=False),
        },
        "pilot": {
            "max_agents": _number(pilot, "max_agents", "pilot", integer=True),
            "max_spend": _number(pilot, "max_spend", "pilot"),
        },
        "pilot_gates": {k: _number(gates, k, "pilot_gates", positive=False, at_most=1.0)
                        for k in ("max_failure_rate", "max_refusal_rate", "max_structured_failure_rate")},
        "provider": {
            "max_concurrency": _number(provider, "max_concurrency", "provider", integer=True),
            "requests_per_minute": _number(provider, "requests_per_minute", "provider", integer=True),
            "reserve_output_tokens": _number(provider, "reserve_output_tokens", "provider", integer=True),
        },
        "simulation": {
            "platform": simulation.get("platform", "parallel"),
            "clock_start_hour": _number(simulation, "clock_start_hour", "simulation", integer=True,
                                        positive=False, at_most=23),
        },
        "estimates": _validate_estimates(est),
    }
    r = limits["rounds"]
    if not r["real_min"] <= r["real_max"] <= r["max_rounds"] or r["pilot"] > r["max_rounds"]:
        raise ConfigError("limits: need rounds.real_min <= real_max <= max_rounds and pilot <= max_rounds")
    if limits["simulation"]["platform"] not in PLATFORMS:
        raise ConfigError(f"limits: simulation.platform must be one of {PLATFORMS} (upstream's single-platform "
                          "scripts never report the end of the rounds)")
    if limits["pilot"]["max_spend"] > limits["caps"]["max_spend"]:
        raise ConfigError("limits: pilot.max_spend cannot exceed caps.max_spend")
    if limits["pilot"]["max_agents"] > limits["caps"]["max_agents"]:
        raise ConfigError("limits: pilot.max_agents cannot exceed caps.max_agents")
    return limits


def _validate_estimates(est: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "high_factor": _number(est, "high_factor", "estimates"),
        "agents": _number(est, "agents", "estimates", integer=True),
        "agent_turns_per_round": _number(est, "agent_turns_per_round", "estimates"),
    }
    if out["high_factor"] < 1:
        raise ConfigError("limits: estimates.high_factor must be at least 1")
    fields = {
        "setup": ("fixed_calls", "calls_per_agent", "prompt_tokens_per_call", "output_tokens_per_call"),
        "simulation": ("calls_per_turn", "prompt_tokens_per_call", "output_tokens_per_call"),
        "report": ("fixed_calls", "prompt_tokens_per_call", "output_tokens_per_call"),
        "seconds": ("fixed_per_run", "per_agent", "per_turn"),
    }
    for group, keys in fields.items():
        section = est.get(group)
        if not isinstance(section, dict):
            raise ConfigError(f"limits: estimates.{group} is missing")
        out[group] = {k: _number(section, k, f"estimates.{group}", positive=False) for k in keys}
    return out


def load_limits(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    raw = load_yaml("limits", config_dir)
    limits = validate_limits(raw)
    limits["_source"] = raw.get("_source", "")
    return limits
