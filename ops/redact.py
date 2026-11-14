"""Remove API keys from any text before it is logged, printed or raised."""

from __future__ import annotations

import re
import threading
from typing import Iterable, Set

REDACTED = "[redacted]"

# Key-shaped strings, redacted even when the value is not registered.
KEY_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9_\-.=]{8,}"),
    re.compile(r"(?i)([?&](?:key|api_key)=)[^&\s]+"),
)

_lock = threading.Lock()
_registered: Set[str] = set()


def register_secret(value: str) -> None:
    """Remember a secret so every later redact() call removes it verbatim."""
    if value and len(value) >= 6:
        with _lock:
            _registered.add(value)


def redact(text: object, extra: Iterable[str] = ()) -> str:
    out = str(text)
    with _lock:
        secrets = sorted(_registered | {s for s in extra if s}, key=len, reverse=True)
    for secret in secrets:
        out = out.replace(secret, REDACTED)
    for pattern in KEY_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, out)
    return out
