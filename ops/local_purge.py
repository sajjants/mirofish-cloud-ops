"""Securely delete what a batch left on this machine (used by `python -m ops purge` after Zep is clean).

For every run of the batch (batch.json plus any ops/runs/<batch>_* run with a manifest):
  - MiroFish's project, simulation and report directories under mirofish/backend/uploads
    (ids from the run manifest, plus projects named after the run and reports whose metadata names
    the run's simulation, which an interrupted run may have left unrecorded),
  - ops/runs/<run>/ (manifest, report, transcript, factguard output);
then ops/runs/<batch>/ (inputs, original seed, variants, plans) and the pseudonym mapping in
ops/mappings/. With logs=True also every file in ops/logs and mirofish/backend/logs, which are not
separable per batch. ops/state/batches/<batch>/ is kept: it holds only ids, statuses, token counts
and costs, and is the record of what was purged.

Files are overwritten with zeros and fsynced before being unlinked. On SSDs and copy-on-write file
systems that cannot guarantee the old blocks are erased; full-disk encryption is what protects them.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from ops import paths
from ops.mirofish_api import validate_run_id

UPSTREAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def checked_child(base: Path, name: Optional[str]) -> Optional[Path]:
    """base/name if name is a safe single path component that stays inside base, else None."""
    if not name:
        return None
    if not UPSTREAM_ID.fullmatch(name):
        raise ValueError(f"refusing unexpected id {name!r}")
    candidate = base / name
    if candidate.is_symlink():
        raise ValueError(f"refusing to follow symlink {candidate}")
    if candidate.exists() and candidate.resolve().parent != base.resolve():
        raise ValueError(f"{candidate} resolves outside {base}")
    return candidate


def shred_file(path: Path, warnings: Optional[List[str]] = None) -> None:
    """Overwrite a regular file with zeros, fsync, then unlink.

    Symlinks and special files are only unlinked (never opened through). A file with other
    hard links is unlinked without overwriting, because zeroing it would also change the
    other names, which may live outside the tree being purged.
    """
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if path.is_symlink():
        path.unlink()
        return
    try:
        fd = os.open(path, flags)
    except OSError:
        path.unlink(missing_ok=True)
        return
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            block = b"\0" * (1 << 20)
            remaining = info.st_size
            while remaining > 0:
                remaining -= handle.write(block[:min(remaining, len(block))])
            os.fsync(fd)
        elif stat.S_ISREG(info.st_mode) and warnings is not None:
            warnings.append(f"{path}: has {info.st_nlink} hard links; unlinked without overwriting")
    path.unlink()


def shred_tree(root: Path, warnings: Optional[List[str]] = None) -> int:
    """Shred every file under root (without following symlinks) and remove the tree. Returns file count."""
    if root.is_symlink() or root.is_file():
        shred_file(root, warnings)
        return 1
    if not root.exists():
        return 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            shred_file(Path(dirpath) / name, warnings)
            count += 1
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                entry.unlink()
            else:
                entry.rmdir()
    root.rmdir()
    return count


def _read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def batch_run_ids(batch_id: str, runs_dir: Optional[Path] = None) -> List[str]:
    runs_dir = runs_dir or paths.RUNS_DIR
    found: Set[str] = set(_read_json(runs_dir / batch_id / "batch.json").get("runs", {}))
    found.update(p.parent.name for p in runs_dir.glob(f"{batch_id}_*/manifest.json"))
    return sorted(r for r in found if (runs_dir / r / "manifest.json").exists())


def upstream_dirs(run_id: str, manifest: Dict, uploads: Path) -> List[Path]:
    ids = {kind: {manifest[key]} if manifest.get(key) else set()
           for kind, key in (("projects", "project_id"), ("simulations", "simulation_id"), ("reports", "report_id"))}
    projects = uploads / "projects"
    if projects.is_dir():
        for entry in projects.iterdir():
            if _read_json(entry / "project.json").get("name") == run_id:
                ids["projects"].add(entry.name)
    reports = uploads / "reports"
    if reports.is_dir() and ids["simulations"]:
        for entry in reports.iterdir():
            if _read_json(entry / "meta.json").get("simulation_id") in ids["simulations"]:
                ids["reports"].add(entry.name)
    found = [checked_child(uploads / kind, item) for kind in ("projects", "simulations", "reports")
             for item in sorted(ids[kind])]
    return [d for d in found if d is not None and d.exists()]


def plan(batch_id: str, include_logs: bool, runs_dir: Optional[Path] = None, uploads: Optional[Path] = None,
         mappings_dir: Optional[Path] = None, log_dirs: Optional[List[Path]] = None) -> List[Path]:
    """Everything that would be shredded, in order (validates every path first)."""
    runs_dir = runs_dir or paths.RUNS_DIR
    uploads = uploads or paths.UPSTREAM_DIR / "backend" / "uploads"
    mappings_dir = mappings_dir or paths.MAPPINGS_DIR
    log_dirs = log_dirs if log_dirs is not None else [paths.LOGS_DIR, paths.UPSTREAM_DIR / "backend" / "logs"]
    targets: List[Path] = []
    for run_id in batch_run_ids(batch_id, runs_dir):
        run_dir = checked_child(runs_dir, validate_run_id(run_id))
        manifest = _read_json(run_dir / "manifest.json")
        targets += upstream_dirs(run_id, manifest, uploads)
        targets.append(run_dir)
    batch_path = checked_child(runs_dir, batch_id)
    if batch_path is not None and batch_path.exists():
        targets.append(batch_path)
    mapping = mappings_dir / f"{batch_id}.mapping.json"
    if mapping.exists() or mapping.is_symlink():
        targets.append(mapping)
    if include_logs:
        for log_dir in log_dirs:
            if log_dir.is_dir():
                targets += sorted(log_dir.iterdir())
    return targets


def shred(targets: List[Path], log=print) -> List[str]:
    warnings: List[str] = []
    for target in targets:
        log(f"  shredded {shred_tree(target, warnings):4d} file(s): {target}")
    return warnings


def disk_encryption_note() -> str:
    note = ("On SSDs and copy-on-write file systems, overwriting cannot guarantee the old blocks are "
            "physically erased; full-disk encryption is what protects deleted data.")
    if sys.platform == "darwin":
        try:
            status = subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            status = "unknown (fdesetup not available)"
        return f"{note}\nFileVault: {status}"
    return f"{note} Check that it is on (BitLocker on Windows, LUKS or similar on Linux)."
