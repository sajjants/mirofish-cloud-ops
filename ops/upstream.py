"""The upstream MiroFish checkout: pinned commit, patches, and paths to its interpreters (any OS)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from ops import paths

UPSTREAM_URL = "https://github.com/666ghj/MiroFish.git"
PATCHES_DIR = paths.OPS_DIR / "patches"
PIN_LINE = re.compile(r"^\| Commit \| `([0-9a-f]{40})` \|", re.MULTILINE)


class UpstreamError(RuntimeError):
    pass


def pinned_commit(upstream_md: Optional[Path] = None) -> str:
    upstream_md = upstream_md or paths.OPS_DIR / "UPSTREAM.md"
    match = PIN_LINE.search(upstream_md.read_text(encoding="utf-8"))
    if not match:
        raise UpstreamError(f"no pinned commit in {upstream_md}")
    return match.group(1)


def git(args: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check, timeout=600)


def head(upstream: Optional[Path] = None) -> Optional[str]:
    upstream = upstream or paths.UPSTREAM_DIR
    if not (upstream / ".git").exists():
        return None
    result = git(["rev-parse", "HEAD"], cwd=upstream, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_checkout(upstream: Optional[Path] = None, commit: Optional[str] = None,
                    log: Callable[[str], None] = print) -> str:
    """Clone at the pinned commit if missing; refuse a checkout at any other commit."""
    upstream = upstream or paths.UPSTREAM_DIR
    commit = commit or pinned_commit()
    if not upstream.exists():
        log(f"cloning {UPSTREAM_URL} into {upstream}")
        # LF as committed upstream, so ops/patches apply on Windows too (core.autocrlf would convert).
        git(["clone", "--quiet", "--no-checkout", "--config", "core.autocrlf=false", UPSTREAM_URL, str(upstream)])
        git(["checkout", "--quiet", "--detach", commit], cwd=upstream)
    elif not (upstream / ".git").exists():
        raise UpstreamError(f"{upstream} exists but is not a git checkout; move it aside and run bootstrap again")
    current = head(upstream)
    if current != commit:
        raise UpstreamError(f"mirofish is at {current}, expected the pinned {commit}")
    return commit


def patch_files() -> List[Path]:
    return sorted(PATCHES_DIR.glob("*.patch"))


def patch_state(patch: Path, upstream: Optional[Path] = None) -> str:
    """'applied', 'pending' (applies cleanly) or 'conflict'."""
    upstream = upstream or paths.UPSTREAM_DIR
    if git(["apply", "--reverse", "--check", str(patch)], cwd=upstream, check=False).returncode == 0:
        return "applied"
    if git(["apply", "--check", str(patch)], cwd=upstream, check=False).returncode == 0:
        return "pending"
    return "conflict"


def apply_patches(mode: str = "apply", upstream: Optional[Path] = None,
                  log: Callable[[str], None] = print) -> bool:
    """mode: apply, check or revert. Idempotent. Returns False on a conflict."""
    if mode not in ("apply", "check", "revert"):
        raise ValueError(mode)
    upstream = upstream or paths.UPSTREAM_DIR
    patches = patch_files()
    if mode == "revert":
        patches = list(reversed(patches))
    for patch in patches:
        state = patch_state(patch, upstream)
        if state == "conflict":
            log(f"CONFLICT  {patch.name}: neither applies nor reverses cleanly; inspect `git -C mirofish status`")
            return False
        if state == "applied" and mode == "revert":
            git(["apply", "--reverse", str(patch)], cwd=upstream)
            log(f"REVERTED  {patch.name}")
        elif state == "pending" and mode == "apply":
            git(["apply", str(patch)], cwd=upstream)
            log(f"APPLIED   {patch.name}")
        else:
            log(f"{state.upper():9} {patch.name}")
    return True


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def backend_python(upstream: Optional[Path] = None) -> Path:
    return venv_python((upstream or paths.UPSTREAM_DIR) / "backend" / ".venv")


def vite_script(upstream: Optional[Path] = None) -> Path:
    return (upstream or paths.UPSTREAM_DIR) / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
