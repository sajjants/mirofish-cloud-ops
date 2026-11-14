"""Prices from ops/config/prices.yaml, and the cost of one call from its usage block."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ops.config import ConfigError, load_yaml
from ops.paths import CONFIG_DIR

WORST_CASE_OUTPUT_TOKENS = 8192
CHARS_PER_TOKEN_LOW = 3   # reservations and missing-usage pricing assume short tokens, so they err high


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    long_threshold: Optional[int] = None
    long_input_per_million: Optional[float] = None
    long_output_per_million: Optional[float] = None

    def rates(self, prompt_tokens: int) -> tuple:
        if self.long_threshold is not None and prompt_tokens > self.long_threshold:
            return self.long_input_per_million, self.long_output_per_million
        return self.input_per_million, self.output_per_million


@dataclass(frozen=True)
class Prices:
    currency: str
    source_url: str
    checked_on: str
    models: Dict[str, ModelPrice]
    sha256: str = ""

    def missing(self, model_names: Iterable[str]) -> list:
        return sorted({m for m in model_names if m not in self.models})

    def for_model(self, model: str) -> ModelPrice:
        try:
            return self.models[model]
        except KeyError:
            raise ConfigError(f"prices.yaml has no price for model {model!r}") from None


def billable_tokens(usage: Dict[str, int]) -> tuple:
    """(input, output) tokens to bill; output includes thinking tokens however they are reported."""
    prompt = int(usage.get("prompt_tokens") or 0)
    if usage.get("total_tokens") is not None:
        return prompt, max(0, int(usage["total_tokens"]) - prompt)
    return prompt, int(usage.get("completion_tokens") or 0) + int(usage.get("reasoning_tokens") or 0)


def cost(price: ModelPrice, usage: Dict[str, int]) -> float:
    prompt, output = billable_tokens(usage)
    input_rate, output_rate = price.rates(prompt)
    return (prompt * input_rate + output * output_rate) / 1_000_000


def worst_case_cost(price: ModelPrice, request_bytes: int, max_tokens: Optional[int]) -> float:
    """Cost assumed for a call when we cannot know it: before it runs, or if usage is missing."""
    prompt = request_bytes // CHARS_PER_TOKEN_LOW + 1
    output = max_tokens if max_tokens else WORST_CASE_OUTPUT_TOKENS
    input_rate, output_rate = price.rates(prompt)
    return (prompt * input_rate + output * output_rate) / 1_000_000


def _rate(entry: Dict[str, Any], key: str, where: str) -> float:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"prices.yaml: {where}.{key} must be a number >= 0 (got {value!r}); copy it from the "
                          "provider's current price page")
    return float(value)


def parse_prices(raw: Dict[str, Any], sha256: str = "") -> Prices:
    models_raw = raw.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise ConfigError("prices.yaml: 'models' is missing or empty")
    models = {}
    for name, entry in models_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"prices.yaml: models.{name} must be a mapping")
        where = f"models.{name}"
        long = entry.get("long_context")
        if long is not None:
            if not isinstance(long, dict):
                raise ConfigError(f"prices.yaml: {where}.long_context must be a mapping")
            threshold = long.get("threshold_prompt_tokens")
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
                raise ConfigError(f"prices.yaml: {where}.long_context.threshold_prompt_tokens must be a positive integer")
            models[str(name)] = ModelPrice(_rate(entry, "input_per_million", where),
                                           _rate(entry, "output_per_million", where), threshold,
                                           _rate(long, "input_per_million", where + ".long_context"),
                                           _rate(long, "output_per_million", where + ".long_context"))
        else:
            models[str(name)] = ModelPrice(_rate(entry, "input_per_million", where),
                                           _rate(entry, "output_per_million", where))
    for key in ("currency", "source_url", "checked_on"):
        if not str(raw.get(key) or "").strip():
            raise ConfigError(f"prices.yaml: '{key}' is empty; record where and when the prices were checked")
    return Prices(str(raw["currency"]), str(raw["source_url"]), str(raw["checked_on"]), models, sha256)


def load_prices(config_dir: Path = CONFIG_DIR) -> Prices:
    raw = load_yaml("prices", config_dir)
    source = Path(raw.get("_source", ""))
    digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ""
    return parse_prices(raw, digest)
