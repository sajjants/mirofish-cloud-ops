"""Five JSON-mode prompts shaped like MiroFish's own structured calls, each with a schema check."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


def clean_model_text(content: str) -> str:
    """Mirror MiroFish's cleanup: drop <think> blocks and an outer ```json fence."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content).strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_ontology(d: dict) -> bool:
    types = d.get("entity_types")
    return (
        isinstance(types, list)
        and len(types) >= 2
        and all(isinstance(t, dict) and _is_str(t.get("name")) and _is_str(t.get("description")) for t in types)
    )


def _check_persona(d: dict) -> bool:
    return (
        _is_str(d.get("bio"))
        and _is_str(d.get("persona"))
        and _is_int(d.get("age"))
        and d.get("gender") in {"male", "female", "other"}
        and isinstance(d.get("mbti"), str)
        and len(d["mbti"]) == 4
    )


def _check_time_config(d: dict) -> bool:
    hours = d.get("total_simulation_hours")
    minutes = d.get("minutes_per_round")
    peaks = d.get("peak_hours")
    return (
        _is_int(hours)
        and 24 <= hours <= 168
        and _is_int(minutes)
        and 30 <= minutes <= 120
        and isinstance(peaks, list)
        and bool(peaks)
        and all(_is_int(h) and 0 <= h <= 23 for h in peaks)
    )


def _check_extraction(d: dict) -> bool:
    entities = d.get("entities")
    relations = d.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        return False
    names = {e.get("name") for e in entities if isinstance(e, dict)}
    return (
        len(entities) >= 2
        and all(isinstance(e, dict) and _is_str(e.get("name")) and _is_str(e.get("type")) for e in entities)
        and len(relations) >= 1
        and all(
            isinstance(r, dict)
            and r.get("source") in names
            and r.get("target") in names
            and _is_str(r.get("type"))
            and _is_str(r.get("fact"))
            for r in relations
        )
    )


def _check_stance(d: dict) -> bool:
    confidence = d.get("confidence")
    return (
        d.get("stance") in {"supportive", "opposing", "neutral"}
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0 <= confidence <= 1
        and _is_str(d.get("reason"))
    )


SYSTEM_PROMPT = "You are a precise assistant. Reply with a single JSON object only."

JSON_TESTS: List[Tuple[str, str, Callable[[dict], bool]]] = [
    (
        "ontology",
        "Text: 'The city council of Riverton voted to close the Elm Street library. "
        "The librarians' union and a parents' group objected.'\n"
        'Return a JSON object {"entity_types": [{"name": str, "description": str}, ...]} '
        "listing at least 3 entity types (PascalCase names) suitable for a social simulation.",
        _check_ontology,
    ),
    (
        "persona",
        "Create a social-media persona for 'a retired schoolteacher in Riverton who uses the "
        'Elm Street library daily\'. Return a JSON object with keys: "bio" (string), '
        '"persona" (string, 2-3 sentences), "age" (integer), "gender" ("male", "female" or '
        '"other"), "mbti" (4-letter string).',
        _check_persona,
    ),
    (
        "time_config",
        "Design the timing of a social-media simulation about a local library closure. Return "
        'a JSON object with keys: "total_simulation_hours" (integer 24-168), '
        '"minutes_per_round" (integer 30-120), "peak_hours" (list of integers 0-23).',
        _check_time_config,
    ),
    (
        "extraction",
        "Text: 'Mayor Dana Ortiz announced on Monday that the Riverton Council will cut the "
        "library budget by 12 percent. Union leader Sam Lee called the plan reckless.'\n"
        'Return a JSON object {"entities": [{"name": str, "type": str}], "relations": '
        '[{"source": str, "target": str, "type": str, "fact": str}]}. Relation source and '
        "target must exactly match entity names.",
        _check_extraction,
    ),
    (
        "stance",
        "Post: 'Closing the library saves money now but costs our kids later. I will be at "
        "the council meeting.'\nClassify the author's stance on the closure. Return a JSON "
        'object with keys: "stance" ("supportive", "opposing" or "neutral"), "confidence" '
        '(number 0-1), "reason" (string).',
        _check_stance,
    ),
]

# One OASIS-style action tool: every simulated agent turn is a single tool-calling request.
TOOL_TEST_PROMPT = ("You are a Riverton resident on a social network. The council plans to close the "
                    "Elm Street library. Use the create_post tool to post your reaction.")
TOOL_TEST_TOOLS = [{
    "type": "function",
    "function": {
        "name": "create_post",
        "description": "Publish a new post on the social network.",
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "The text of the post."}},
            "required": ["content"],
        },
    },
}]


def check_json_answer(content: str, check: Callable[[dict], bool]) -> Optional[str]:
    """None if the content is a JSON object passing the check, else the reason it failed."""
    try:
        parsed = json.loads(clean_model_text(content))
    except ValueError:
        return "not valid JSON"
    if not isinstance(parsed, dict):
        return "top-level JSON is not an object"
    return None if check(parsed) else "JSON parsed but failed the schema check"


def check_tool_call(tool_calls: List[Dict[str, Any]]) -> Optional[str]:
    for call in tool_calls:
        function = call.get("function") or {}
        if function.get("name") != "create_post":
            continue
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            return "create_post arguments are not valid JSON"
        return None if _is_str(arguments.get("content")) else "create_post called without content"
    return "no create_post tool call"
