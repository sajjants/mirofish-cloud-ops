"""Repository paths and private-directory creation (pathlib only, any OS)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
CONFIG_DIR = OPS_DIR / "config"
STATE_DIR = OPS_DIR / "state"
LOGS_DIR = OPS_DIR / "logs"
RUNS_DIR = OPS_DIR / "runs"
MAPPINGS_DIR = OPS_DIR / "mappings"
UPSTREAM_DIR = REPO_ROOT / "mirofish"

CLOUD_SYNC_MARKERS = ("Google Drive", "GoogleDrive", "Dropbox", "OneDrive", "Mobile Documents", "iCloud")
# Google Drive for desktop keeps these at the root of every folder it backs up (e.g. ~/Documents) and
# hard-links files into them while uploading; Dropbox keeps .dropbox at its root.
SYNC_STAGING_NAMES = (".tmp.driveupload", ".tmp.drivedownload", ".dropbox")
LINK_SCAN_LIMIT = 200


def private_dirs() -> List[Path]:
    """Where seeds, mappings, run outputs, tokens and logs are written."""
    return [STATE_DIR, RUNS_DIR, MAPPINGS_DIR, LOGS_DIR, UPSTREAM_DIR]


def cloud_sync_problems(directories: Optional[Iterable[Path]] = None) -> List[str]:
    """Signs that a sync client uploads what is written to these directories (best effort: a sync
    client that leaves none of these signs, such as some iCloud setups, is not detected)."""
    problems: List[str] = []
    for directory in directories if directories is not None else private_dirs():
        absolute = Path(os.path.abspath(directory))
        marker = next((m for m in CLOUD_SYNC_MARKERS if m in str(absolute)), None)
        staging = next((parent / name for parent in (absolute, *absolute.parents) for name in SYNC_STAGING_NAMES
                        if (parent / name).exists()), None)
        linked = 0
        if absolute.is_dir():
            for count, path in enumerate(absolute.rglob("*")):
                if count >= LINK_SCAN_LIMIT:
                    break
                try:
                    linked += path.is_file() and not path.is_symlink() and path.stat().st_nlink > 1
                except OSError:
                    continue
        if marker:
            problems.append(f"{absolute} is inside a cloud-synced folder ({marker})")
        elif staging:
            problems.append(f"{absolute} is inside a folder a sync client uploads ({staging} exists)")
        elif linked:
            problems.append(f"{linked} file(s) in {absolute} have a second hard link (a sync or backup client "
                            "staging them for upload)")
    return problems


def make_private_dir(path: Path) -> Path:
    """Create a directory readable only by the current user (0700 where the OS supports modes)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def append_private_line(path: Path, line: str) -> None:
    """Append one line to a file created with owner-only permissions."""
    make_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


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
