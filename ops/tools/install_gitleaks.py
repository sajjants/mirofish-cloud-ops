#!/usr/bin/env python3
"""Download the pinned gitleaks release into .tools/gitleaks/ and verify its checksum.

  python ops/tools/install_gitleaks.py [--force]

The archive's SHA-256 must match the release's published checksums file, otherwise nothing is
installed. Standard library only; macOS, Linux and Windows on x64 or arm64.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "8.30.1"
RELEASES = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}"
REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_DIR = REPO_ROOT / ".tools" / "gitleaks"


def asset_name() -> str:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if system is None or arch is None:
        raise SystemExit(f"unsupported platform {platform.system()}/{platform.machine()}; install gitleaks manually")
    return f"gitleaks_{VERSION}_{system}_{arch}." + ("zip" if system == "windows" else "tar.gz")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 (fixed https URL)
        return response.read()


def expected_sha256(checksums: str, name: str) -> str:
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == name:
            return parts[0].lower()
    raise SystemExit(f"{name} is not listed in the release checksums")


def extract(archive: bytes, name: str, exe: str) -> bytes:
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            return zf.read(exe)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        member = tf.extractfile(exe)
        if member is None:
            raise SystemExit(f"{exe} not found in {name}")
        return member.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="reinstall even if present")
    args = parser.parse_args()

    exe = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    target = TARGET_DIR / exe
    if target.is_file() and not args.force:
        print(f"gitleaks already installed: {target}")
        return 0
    name = asset_name()
    checksums = fetch(f"{RELEASES}/gitleaks_{VERSION}_checksums.txt").decode("utf-8")
    archive = fetch(f"{RELEASES}/{name}")
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected_sha256(checksums, name):
        raise SystemExit(f"checksum mismatch for {name}; nothing installed")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    target.write_bytes(extract(archive, name, exe))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed gitleaks {VERSION} ({name}, sha256 {actual[:16]}...) at {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
