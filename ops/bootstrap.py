"""bootstrap: one-command, idempotent setup on a new machine (macOS, Linux; Windows for the wrapper only).

  1. the wrapper's virtualenv (.venv) from the hash-pinned ops/requirements.lock
  2. upstream MiroFish cloned into ./mirofish at the commit pinned in ops/UPSTREAM.md
  3. ops/patches applied
  4. backend dependencies from upstream's uv.lock (uv sync --frozen); frontend from package-lock.json
     (npm ci --ignore-scripts). Upstream's own setup scripts are not run.
  5. mirofish/.env (owner-only) from .env.example with a random SECRET_KEY; on re-runs only keys that
     a newer template introduced are added, existing values are kept
  6. private (0700) data directories
  7. the simulation's one-time downloads (Twitter/twhin-bert-base, tiktoken encodings) cached in
     ops/cache, then mirofish/.env switched offline so runs only talk to Gemini and Zep
  8. the backend's and the wrapper's test suites

Uses only the standard library, so it runs before .venv exists.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ops import envfile, paths, upstream

ENV_TEMPLATE = paths.REPO_ROOT / ".env.example"
WRAPPER_VENV = paths.REPO_ROOT / ".venv"
CACHE_DIR = paths.OPS_DIR / "cache"
PRIVATE_DIRS = [paths.UPSTREAM_DIR / "backend" / "uploads", paths.UPSTREAM_DIR / "backend" / "logs",
                paths.LOGS_DIR, paths.STATE_DIR, paths.RUNS_DIR, CACHE_DIR, paths.OPS_DIR / "mappings"]
SUPPORTED_PYTHON = ((3, 11), (3, 12))
# Upstream requires Python >=3.11,<3.13.
DEFAULT_UV_PYTHON = "3.12"

ASSET_SCRIPT = r"""
import json, os, sys
from pathlib import Path
import tiktoken
from transformers import AutoModel, AutoTokenizer
MODEL = "Twitter/twhin-bert-base"
AutoTokenizer.from_pretrained(MODEL, model_max_length=512)
AutoModel.from_pretrained(MODEL)
encodings = {name: tiktoken.get_encoding(name).n_vocab for name in ("cl100k_base", "o200k_base")}
ref = Path(os.environ["HF_HOME"], "hub", "models--Twitter--twhin-bert-base", "refs", "main")
print(json.dumps({"twhin_bert_revision": ref.read_text().strip() if ref.exists() else None,
                  "tiktoken_encodings": encodings}))
"""


class BootstrapError(RuntimeError):
    pass


def step(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def run(command: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
        capture: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            capture_output=capture)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-800:] if capture else ""
        raise BootstrapError(f"`{' '.join(command[:4])}` failed (exit {result.returncode}) {detail}".strip())
    return result


def need(tool: str, hint: str) -> str:
    found = shutil.which(tool)
    if not found:
        raise BootstrapError(f"{tool} is required: {hint}")
    return found


def node_major(node: str) -> int:
    return int(run([node, "-p", "process.versions.node.split('.')[0]"], capture=True).stdout.strip())


def prerequisites(frontend: bool) -> Dict[str, str]:
    if sys.version_info[:2] not in SUPPORTED_PYTHON:
        raise BootstrapError(f"run bootstrap with Python 3.11 or 3.12 (this is {sys.version.split()[0]})")
    tools = {"git": need("git", "install Git"),
             "uv": need("uv", "install uv: https://docs.astral.sh/uv/getting-started/installation/")}
    if frontend:
        tools["node"] = need("node", "install Node.js 18 or newer (or pass --no-frontend)")
        tools["npm"] = need("npm", "install npm (ships with Node.js)")
        if node_major(tools["node"]) < 18:
            raise BootstrapError("Node.js 18 or newer is required")
    for name, path in tools.items():
        print(f"{name}: {path}")
    return tools


def wrapper_venv(uv: str) -> Path:
    python = upstream.venv_python(WRAPPER_VENV)
    if not python.exists():
        run([uv, "venv", "--quiet", "--python", f"{sys.version_info[0]}.{sys.version_info[1]}", str(WRAPPER_VENV)])
    run([uv, "pip", "install", "--quiet", "--python", str(python), "--require-hashes",
         "-r", str(paths.OPS_DIR / "requirements.lock")])
    print(f"OK: {WRAPPER_VENV} (hash-pinned ops/requirements.lock)")
    return python


def write_env(env_path: Path = envfile.ENV_PATH, template: Path = ENV_TEMPLATE) -> List[str]:
    """Create mirofish/.env, or add keys a newer template introduced. Returns warnings."""
    if not env_path.exists():
        lines = []
        for line in template.read_text(encoding="utf-8").splitlines(keepends=True):
            lines.append(f"SECRET_KEY={secrets.token_hex(32)}\n" if line.strip() == "SECRET_KEY=change-me" else line)
        paths.write_private_text(env_path, "".join(lines))
        print(f"created {env_path} (owner-only, random SECRET_KEY)")
    else:
        current = envfile.values(env_path)
        wanted = envfile.parse(envfile.read_lines(template))
        missing = {k: v for k, v in wanted.items() if k not in current}
        if missing:
            envfile.set_values(env_path, missing)
        print(f"kept {env_path}; added {len(missing)} new key(s) from the template")
        paths.write_private_text(env_path, env_path.read_text(encoding="utf-8"))
    values = envfile.values(env_path)
    warnings = []
    if values.get("SECRET_KEY") in ("", "change-me"):
        warnings.append(f"SECRET_KEY in {env_path} is still the placeholder")
    if values.get("ZEP_API_KEY"):
        warnings.append(f"{env_path} contains ZEP_API_KEY; remove that line (start passes the key in the "
                        "process environment and refuses to run while a key is stored on disk)")
    return warnings


def cache_assets(env_path: Path = envfile.ENV_PATH) -> Dict[str, object]:
    python = upstream.backend_python()
    hf_home, tiktoken_dir = CACHE_DIR / "huggingface", CACHE_DIR / "tiktoken"
    for directory in (hf_home, tiktoken_dir):
        paths.make_private_dir(directory)
    env = {**os.environ, "HF_HOME": str(hf_home), "TIKTOKEN_CACHE_DIR": str(tiktoken_dir),
           "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    print("downloading once (setup-time access to huggingface.co and openaipublic.blob.core.windows.net)")
    result = run([str(python), "-W", "ignore", "-c", ASSET_SCRIPT],
                 env={**env, "HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0"}, capture=True)
    info = json.loads(result.stdout.strip().splitlines()[-1])
    print("verifying the cache loads with downloads disabled (with the ops/runtime shims, as start runs it)")
    run([str(python), "-W", "ignore", "-c", ASSET_SCRIPT],
        env={**env, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
             "PYTHONPATH": str(paths.OPS_DIR / "runtime")}, capture=True)
    paths.write_private_text(paths.STATE_DIR / "assets.json", json.dumps(info, indent=2) + "\n")
    envfile.set_values(env_path, {"HF_HOME": str(hf_home), "TIKTOKEN_CACHE_DIR": str(tiktoken_dir),
                                  "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    print(f"OK: assets cached; {env_path} now runs MiroFish offline for these downloads")
    return info


def main(frontend: bool = True, tests: bool = True, assets: bool = True) -> int:
    try:
        step("Prerequisites")
        tools = prerequisites(frontend)
        if (paths.REPO_ROOT / ".git").exists():
            step("Pre-commit secret scan")
            run(["git", "config", "core.hooksPath", ".githooks"], cwd=paths.REPO_ROOT)
            print("OK: .githooks/pre-commit runs ops/tools/secret_scan.py on every commit")
        step("Wrapper virtualenv")
        wrapper_python = wrapper_venv(tools["uv"])
        step("Upstream MiroFish")
        commit = upstream.ensure_checkout()
        print(f"OK: mirofish at pinned commit {commit}")
        step("Patches")
        if not upstream.apply_patches("apply"):
            raise BootstrapError("a patch does not apply; nothing else was changed")
        step("Backend dependencies (uv sync --frozen)")
        uv_env = {**os.environ, "UV_PYTHON": os.environ.get("UV_PYTHON", DEFAULT_UV_PYTHON)}
        run([tools["uv"], "sync", "--frozen", "--quiet"], cwd=paths.UPSTREAM_DIR / "backend", env=uv_env)
        print("OK: backend dependencies installed")
        if frontend:
            step("Frontend dependencies (npm ci --ignore-scripts)")
            # Only esbuild and fsevents declare install scripts; both work from their prebuilt platform packages.
            frontend_dir = paths.UPSTREAM_DIR / "frontend"
            run([tools["npm"], "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=frontend_dir)
            run([tools["node"], "node_modules/esbuild/bin/esbuild", "--version"], cwd=frontend_dir, capture=True)
            print("OK: frontend dependencies installed")
        step("Configuration (mirofish/.env)")
        warnings = write_env()
        step("Private data directories")
        for directory in PRIVATE_DIRS:
            paths.make_private_dir(directory)
            print(directory)
        if assets:
            step("One-time downloads for offline simulation")
            cache_assets()
        else:
            warnings.append("assets not cached (--skip-assets): the Twitter simulation would download from "
                            "huggingface.co, which netcheck reports as a violation")
        if tests:
            step("Backend tests")
            run([str(upstream.backend_python()), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "-W", "ignore::SyntaxWarning"], cwd=paths.UPSTREAM_DIR / "backend")
            step("Wrapper tests")
            run([str(wrapper_python), "-W", "ignore::SyntaxWarning", "-m", "unittest", "discover",
                 "-s", "ops/tests", "-t", "."], cwd=paths.REPO_ROOT)
    except (BootstrapError, upstream.UpstreamError) as error:
        print(f"\nFAIL: {error}", file=sys.stderr)
        return 1
    step("Done")
    warnings += [f"{problem}; start and prepare refuse to run here. Move the repository to a folder that is not "
                 "synced or backed up to the cloud." for problem in paths.cloud_sync_problems()[:1]]
    for warning in warnings:
        print(f"WARN: {warning}")
    print(f"Next: {upstream.venv_python(WRAPPER_VENV).relative_to(paths.REPO_ROOT)} -m ops doctor, then "
          "privacy_report --confirm, then start (see README.md)")
    return 0
