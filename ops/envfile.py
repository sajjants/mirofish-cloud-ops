"""Read and edit mirofish/.env safely (owner-only, atomic replace, existing values preserved)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from ops import paths

# python-dotenv also accepts an optional "export " prefix, and the last occurrence of a key wins.
KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
ENV_PATH = paths.UPSTREAM_DIR / ".env"


def unquote(raw: str) -> str:
    """Value as python-dotenv reads it: quotes removed, or an unquoted ' #' comment dropped."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse(lines: List[str]) -> Dict[str, str]:
    values = {}
    for line in lines:
        match = KEY_LINE.match(line)
        if match:
            values[match.group(1)] = unquote(match.group(2))
    return values


def read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def values(path: Path = ENV_PATH) -> Dict[str, str]:
    return parse(read_lines(path))


def set_values(target: Path, assignments: Dict[str, str]) -> None:
    for key, value in assignments.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value or "\r" in value:
            raise ValueError(f"invalid assignment for {key!r}")
    lines = read_lines(target)
    found = set()
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if match and match.group(1) in assignments:
            key = match.group(1)
            lines[index] = f"{key}={assignments[key]}\n"
            found.add(key)
    lines.extend(f"{key}={value}\n" for key, value in assignments.items() if key not in found)
    paths.write_private_text(target, "".join(lines))
