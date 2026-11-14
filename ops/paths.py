"""Repository paths and private-directory creation (pathlib only, any OS)."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
CONFIG_DIR = OPS_DIR / "config"
STATE_DIR = OPS_DIR / "state"
LOGS_DIR = OPS_DIR / "logs"
RUNS_DIR = OPS_DIR / "runs"
UPSTREAM_DIR = REPO_ROOT / "mirofish"


def make_private_dir(path: Path) -> Path:
    """Create a directory readable only by the current user (0700 where the OS supports modes)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def write_private_text(path: Path, text: str) -> None:
    """Write a file atomically with owner-only permissions (0600 where the OS supports modes)."""
    make_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)
    if os.name != "nt":
        path.chmod(0o600)
