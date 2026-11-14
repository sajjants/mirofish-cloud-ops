"""Map MiroFish pipeline stages to the stage groups in llm.yaml, and rewrite requests accordingly."""

from __future__ import annotations

import copy
from typing import Any, Dict

from ops.config import LLMConfig, StageModel

# Stage names are those of the run pipeline (run manifest stages) plus "interview".
STAGE_GROUP = {
    "ontology": "setup",
    "graph": "setup",          # Zep extracts the graph itself; only MiroFish's own calls are routed
    "simulation": "setup",
    "prepare": "setup",        # personas and simulation config
    "schedule": "setup",
    "rounds": "simulation",
    "close_env": "simulation",
    "report": "report",
    "interview": "report",
}


def group_for(stage: str) -> str:
    try:
        return STAGE_GROUP[stage]
    except KeyError:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(STAGE_GROUP)}") from None


def model_for(stage: str, config: LLMConfig) -> StageModel:
    return config.stages[group_for(stage)]


def route_request(body: Dict[str, Any], stage_model: StageModel) -> Dict[str, Any]:
    """The request MiroFish sent, with the stage's model and reasoning effort substituted."""
    routed = copy.deepcopy(body)
    routed["model"] = stage_model.model
    if stage_model.reasoning_effort is not None:
        routed["reasoning_effort"] = stage_model.reasoning_effort
    return routed
