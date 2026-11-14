"""Load and validate the wrapper's YAML configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import yaml

from ops.paths import CONFIG_DIR

PLACEHOLDER = "SET_ME"
STAGE_GROUPS = ("setup", "simulation", "report")
REASONING_EFFORTS = {None, "none", "minimal", "low", "medium", "high", "xhigh"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ConfigError(ValueError):
    pass


def load_yaml(name: str, config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    """Load ops/config/<name>.yaml, falling back to the committed <name>.example.yaml template."""
    for path in (config_dir / f"{name}.yaml", config_dir / f"{name}.example.yaml"):
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ConfigError(f"{path} must contain a mapping")
            data["_source"] = str(path)
            return data
    raise ConfigError(f"{config_dir / (name + '.yaml')} not found")


@dataclass(frozen=True)
class StageModel:
    model: str
    reasoning_effort: Optional[str]


@dataclass(frozen=True)
class RetrySettings:
    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key_env: str
    timeout_seconds: float
    stages: Dict[str, StageModel]
    retry: RetrySettings = field(default_factory=RetrySettings)
    source: str = ""

    def placeholders(self) -> list:
        return [group for group, stage in self.stages.items() if stage.model == PLACEHOLDER]

    def distinct_models(self) -> list:
        """(first stage group, StageModel) for each distinct model/effort pair, in stage order."""
        seen, out = set(), []
        for group in STAGE_GROUPS:
            stage = self.stages[group]
            if stage not in seen:
                seen.add(stage)
                out.append((group, stage))
        return out


def check_endpoint(url: str) -> str:
    """Only HTTPS may leave the machine; plain HTTP is allowed for loopback (tests, the local relay)."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
        return url
    raise ConfigError(f"endpoint must be https (or http on loopback): {url!r}")


def parse_llm_config(raw: Dict[str, Any]) -> LLMConfig:
    provider = raw.get("provider") or {}
    stages_raw = raw.get("stages") or {}
    stages = {}
    for group in STAGE_GROUPS:
        entry = stages_raw.get(group) or {}
        model = str(entry.get("model") or "").strip()
        effort = entry.get("reasoning_effort")
        if not model:
            raise ConfigError(f"stages.{group}.model is missing")
        if effort not in REASONING_EFFORTS:
            raise ConfigError(f"stages.{group}.reasoning_effort must be one of "
                              f"{sorted(e for e in REASONING_EFFORTS if e)} or null, got {effort!r}")
        stages[group] = StageModel(model, effort)
    unknown = set(stages_raw) - set(STAGE_GROUPS)
    if unknown:
        raise ConfigError(f"unknown stage groups: {sorted(unknown)} (expected {list(STAGE_GROUPS)})")
    retry_raw = raw.get("retry") or {}
    retry = RetrySettings(
        max_retries=int(retry_raw.get("max_retries", 5)),
        base_delay_seconds=float(retry_raw.get("base_delay_seconds", 1.0)),
        max_delay_seconds=float(retry_raw.get("max_delay_seconds", 60.0)),
    )
    if retry.max_retries < 0 or retry.base_delay_seconds <= 0 or retry.max_delay_seconds < retry.base_delay_seconds:
        raise ConfigError("retry: need max_retries >= 0 and 0 < base_delay_seconds <= max_delay_seconds")
    return LLMConfig(
        base_url=check_endpoint(str(provider.get("base_url") or "")),
        api_key_env=str(provider.get("api_key_env") or "GEMINI_API_KEY"),
        timeout_seconds=float(provider.get("timeout_seconds", 120)),
        stages=stages,
        retry=retry,
        source=str(raw.get("_source", "")),
    )


def load_llm_config(config_dir: Path = CONFIG_DIR) -> LLMConfig:
    return parse_llm_config(load_yaml("llm", config_dir))


def load_limits(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return load_yaml("limits", config_dir)
