"""Read API keys from the environment or the OS keychain; never from tracked files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ops.redact import register_secret

KEYRING_SERVICE = "mirofish-cloud-ops"


@dataclass(frozen=True)
class Key:
    value: str
    source: str  # "environment variable NAME" or "OS keychain"; safe to print


def _from_keyring(name: str) -> Optional[str]:
    try:
        import keyring  # optional at import time so the tests and doctor work without a keychain
        from keyring.errors import KeyringError
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except KeyringError:
        return None


def get_key(name: str, env_var: str) -> Optional[Key]:
    """Environment variable first, then the OS keychain entry (service mirofish-cloud-ops, user NAME)."""
    value = os.environ.get(env_var, "").strip()
    source = f"environment variable {env_var}"
    if not value:
        value = (_from_keyring(name) or "").strip()
        source = "OS keychain"
    if not value:
        return None
    register_secret(value)
    return Key(value, source)
