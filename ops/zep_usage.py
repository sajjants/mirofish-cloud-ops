"""Estimate a batch's Zep Cloud usage and warn against the quotas in zep_limits.yaml.

Zep calls go from upstream's SDK straight to Zep, so nothing here is measured; the numbers come
from the seed length, the agent counts and the usage figures in the config.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ops import paths

METRICS = ("episodes", "episode_characters", "searches", "graphs")
USAGE_KEYS = ("chunk_size", "chunk_overlap", "searches_per_agent", "report_searches")
# Upstream splits at a sentence end found past 30% of the chunk, so a chunk is never shorter than that.
MIN_CHUNK_FRACTION = 0.3


class ZepLimitsError(ValueError):
    pass


def _placeholder(value: Any) -> bool:
    return value is None or str(value).strip() in ("", "SET_ME")


def validate_zep_limits(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ZepLimitsError("zep_limits.yaml must be a mapping")
    quotas = raw.get("quotas") or {}
    usage = raw.get("usage") or {}
    unknown = set(quotas) - set(METRICS)
    if unknown:
        raise ZepLimitsError(f"unknown quotas {sorted(unknown)}; known: {', '.join(METRICS)}")
    for name, value in quotas.items():
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0):
            raise ZepLimitsError(f"quotas.{name} must be a positive number or null")
    for key in USAGE_KEYS:
        if not isinstance(usage.get(key), (int, float)) or isinstance(usage.get(key), bool) or usage[key] < 0:
            raise ZepLimitsError(f"usage.{key} must be a number >= 0")
    if not 0 <= usage["chunk_overlap"] < usage["chunk_size"] * MIN_CHUNK_FRACTION:
        raise ZepLimitsError("usage.chunk_overlap must be smaller than 30% of usage.chunk_size")
    fraction = raw.get("warn_fraction", 0.8)
    if not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        raise ZepLimitsError("warn_fraction must be between 0 and 1")
    if any(v is not None for v in quotas.values()) and \
            (_placeholder(raw.get("source_url")) or _placeholder(raw.get("checked_on"))):
        raise ZepLimitsError("quotas are set but source_url or checked_on is not; record where and when you read them")
    if not _placeholder(raw.get("checked_on")) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw["checked_on"])):
        raise ZepLimitsError("checked_on must be a date (YYYY-MM-DD)")
    return {"source_url": raw.get("source_url"), "checked_on": str(raw.get("checked_on")),
            "period": raw.get("period"), "warn_fraction": float(fraction),
            "quotas": {m: quotas.get(m) for m in METRICS}, "usage": {k: usage[k] for k in USAGE_KEYS}}


def example_limits() -> Dict[str, Any]:
    raw = yaml.safe_load((paths.CONFIG_DIR / "zep_limits.example.yaml").read_text(encoding="utf-8"))
    return validate_zep_limits(raw)


def load_zep_limits(config_dir: Path = paths.CONFIG_DIR) -> Optional[Dict[str, Any]]:
    """The validated config, or None if zep_limits.yaml does not exist (quotas are then not checked)."""
    path = config_dir / "zep_limits.yaml"
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ZepLimitsError(f"{path} is not valid YAML: {error}") from None
    return validate_zep_limits(raw)


def episode_range(characters: int, chunk_size: int, overlap: int) -> Dict[str, int]:
    """Fewest and most chunks upstream's splitter can make from this many characters."""
    if characters <= 0:
        return {"low": 0, "high": 0}
    if characters <= chunk_size:
        return {"low": 1, "high": 1}

    def chunks(step: float) -> int:
        return 1 + math.ceil((characters - chunk_size) / step)

    return {"low": chunks(chunk_size - overlap),
            "high": chunks(max(1.0, chunk_size * MIN_CHUNK_FRACTION - overlap))}


def estimate(seed_characters: int, agents_per_run: List[int], zep_limits: Optional[Dict[str, Any]],
             open_graphs: int = 0) -> Dict[str, Any]:
    """Usage for the given runs (one graph each), as low/high, with warnings against the quotas."""
    config = zep_limits or example_limits()
    usage = config["usage"]
    episodes = episode_range(seed_characters, usage["chunk_size"], usage["chunk_overlap"])
    runs = len(agents_per_run)
    total: Dict[str, Dict[str, int]] = {
        "episodes": {k: v * runs for k, v in episodes.items()},
        "episode_characters": {"low": seed_characters * runs,
                               "high": (seed_characters + usage["chunk_overlap"] * episodes["high"]) * runs},
        "searches": {k: int(sum(usage["searches_per_agent"] * a + usage["report_searches"] for a in agents_per_run))
                     for k in ("low", "high")},
        "graphs": {"low": runs + open_graphs, "high": runs + open_graphs},
    }
    warnings: List[str] = []
    if zep_limits is None:
        warnings.append("no ops/config/zep_limits.yaml: Zep quotas are not checked")
    else:
        for metric, quota in zep_limits["quotas"].items():
            if quota is None:
                continue
            high = total[metric]["high"]
            if high > quota * zep_limits["warn_fraction"]:
                warnings.append(f"Zep {metric}: this batch may use up to {high:,} of your {quota:,} per "
                                f"{zep_limits['period'] or 'period'} (quota from {zep_limits['source_url']}, "
                                f"checked {zep_limits['checked_on']}); usage outside this batch is not counted")
    return {"runs": runs, "seed_characters": seed_characters, "per_run_episodes": episodes, "total": total,
            "warnings": warnings}


def format_estimate(zep: Dict[str, Any]) -> List[str]:
    t = zep["total"]

    def span(metric: str) -> str:
        low, high = t[metric]["low"], t[metric]["high"]
        return f"{low:,}" if low == high else f"{low:,} - {high:,}"

    lines = [f"Zep (estimated, not metered) for {zep['runs']} run(s): {span('episodes')} episodes, "
             f"{span('episode_characters')} characters, about {span('searches')} searches, "
             f"{span('graphs')} graph(s) until purged"]
    lines += [f"WARNING: {w}" for w in zep["warnings"]]
    return lines
