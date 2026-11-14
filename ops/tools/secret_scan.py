#!/usr/bin/env python3
"""Block secrets and sensitive terms from entering git (pre-commit hook and CI).

  secret_scan.py --staged            # what is about to be committed (the pre-commit hook)
  secret_scan.py --tracked           # every tracked file in the working tree
  secret_scan.py --history           # every commit reachable from any ref
  options: --require-gitleaks        # fail if gitleaks is unavailable (CI)

Two layers, both must pass:
  1. gitleaks (ops/tools/install_gitleaks.py puts it in .tools/gitleaks/), if installed.
  2. Built-in rules: key-shaped strings, private files that must never be committed, and every
     term in the local ops/config/sensitive_terms.txt (git-ignored, so only checked locally).
Findings name the file, line and rule; secrets are never printed, sensitive terms are named so you
know what to remove. Exit status: 0 clean, 1 findings, 2 the scan could not run.
Standard library only, Python 3.9+, any OS.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
TERMS_FILE = REPO_ROOT / "ops" / "config" / "sensitive_terms.txt"

# Key-shaped strings. Kept specific to avoid noise; gitleaks covers the long tail.
KEY_RULES: Sequence[Tuple[str, re.Pattern]] = (
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("assigned-secret", re.compile(
        r"(?i)\b[A-Z0-9_]*(?:api[_-]?key|secret|token|password|passwd)[A-Z0-9_]*\s*[:=]\s*['\"]?"
        r"(?=[A-Za-z0-9_\-./+=]*\d)(?=[A-Za-z0-9_\-./+=]*[A-Za-z])[A-Za-z0-9_\-./+=]{20,}")),
)

# Paths that must never be committed, whatever .gitignore says (git add -f bypasses it).
FORBIDDEN_PATHS: Sequence[Tuple[str, re.Pattern]] = (
    ("env-file", re.compile(r"(^|/)\.env(\.(?!example$)[^/]+)?$")),
    ("key-file", re.compile(r"\.(key|pem|p12)$")),
    ("sensitive-terms-file", re.compile(r"(^|/)ops/config/sensitive_terms\.txt$")),
    ("private-config", re.compile(r"(^|/)ops/config/(limits|prices|zep_limits|llm)\.yaml$")),
    ("mapping-file", re.compile(r"\.mapping\.(json|ya?ml)$|(^|/)(ops/)?mappings/")),
    ("seed-file", re.compile(r"(^|/)(ops/)?seeds/|\.seed\.md$")),
    ("run-output", re.compile(r"(^|/)ops/(runs|outputs|logs|state)/|(^|/)(outputs|transcripts)/")),
    ("upstream-clone", re.compile(r"^mirofish/")),
)


@dataclass(frozen=True)
class Finding:
    where: str
    line: int
    rule: str
    detail: str = ""

    def format(self) -> str:
        location = f"{self.where}:{self.line}" if self.line else self.where
        return f"  {location}: {self.rule}" + (f" ({self.detail})" if self.detail else "")


def load_terms(path: Path = TERMS_FILE) -> List[str]:
    if not path.is_file():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return terms


def term_pattern(terms: Sequence[str]) -> Optional[re.Pattern]:
    if not terms:
        return None
    alternatives = sorted({re.escape(t) for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE)


def scan_text(where: str, text: str, terms: Optional[re.Pattern]) -> List[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in KEY_RULES:
            if pattern.search(line):
                findings.append(Finding(where, number, rule))
        if terms is not None:
            for match in sorted({m.group(0).lower() for m in terms.finditer(line)}):
                findings.append(Finding(where, number, "sensitive-term", f"'{match}'"))
    return findings


def scan_path(path: str) -> List[Finding]:
    posix = PurePosixPath(path.replace("\\", "/")).as_posix()
    return [Finding(posix, 0, rule, "this file must never be committed")
            for rule, pattern in FORBIDDEN_PATHS if pattern.search(posix)]


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True, capture_output=True).stdout


def _decode(blob: bytes) -> Optional[str]:
    if b"\0" in blob[:8192]:
        return None  # binary
    return blob.decode("utf-8", errors="replace")


def staged_items() -> Iterator[Tuple[str, Optional[str]]]:
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    for name in filter(None, names):
        path = name.decode("utf-8", errors="replace")
        yield path, _decode(_git("show", f":{path}"))


def tracked_items() -> Iterator[Tuple[str, Optional[str]]]:
    for name in filter(None, _git("ls-files", "-z").split(b"\0")):
        path = name.decode("utf-8", errors="replace")
        file = REPO_ROOT / path
        yield path, _decode(file.read_bytes()) if file.is_file() else None


def history_items() -> Iterator[Tuple[str, Optional[str]]]:
    """Every file version ever committed, as '<commit>:<path>'."""
    seen = set()
    commits = [c for c in _git("rev-list", "--all").decode().split() if c]
    for commit in commits:
        listing = _git("ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in filter(None, listing):
            meta, _, name = entry.partition(b"\t")
            kind, blob_id = meta.split()[1], meta.split()[2].decode()
            path = name.decode("utf-8", errors="replace")
            if kind != b"blob" or (blob_id, path) in seen:
                continue
            seen.add((blob_id, path))
            yield f"{commit[:12]}:{path}", _decode(_git("cat-file", "-p", blob_id))


def scan_items(items: Iterable[Tuple[str, Optional[str]]], terms: Optional[re.Pattern]) -> List[Finding]:
    findings: List[Finding] = []
    for where, text in items:
        path = where.split(":", 1)[1] if re.match(r"^[0-9a-f]{12}:", where) else where
        findings += scan_path(path)
        if text is not None:
            findings += scan_text(where, text, terms)
    return findings


def find_gitleaks() -> Optional[str]:
    exe = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    local = REPO_ROOT / ".tools" / "gitleaks" / exe
    return str(local) if local.is_file() else shutil.which("gitleaks")


def run_gitleaks(binary: str, mode: str) -> int:
    base = [binary, "git", "--redact", "--no-banner", "--exit-code", "1"]
    if mode == "staged":
        args = base + ["--pre-commit", "--staged", str(REPO_ROOT)]
    elif mode == "history":
        args = base + ["--log-opts=--all", str(REPO_ROOT)]
    else:
        args = [binary, "dir", "--redact", "--no-banner", "--exit-code", "1", str(REPO_ROOT)]
    return subprocess.run(args).returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_const", const="staged", dest="mode")
    mode.add_argument("--tracked", action="store_const", const="tracked", dest="mode")
    mode.add_argument("--history", action="store_const", const="history", dest="mode")
    parser.add_argument("--require-gitleaks", action="store_true")
    args = parser.parse_args(argv)

    terms = load_terms()
    items = {"staged": staged_items, "tracked": tracked_items, "history": history_items}[args.mode]()
    try:
        findings = scan_items(items, term_pattern(terms))
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"secret-scan: could not read the repository: {exc}", file=sys.stderr)
        return 2

    failed = False
    if findings:
        failed = True
        print(f"secret-scan: {len(findings)} finding(s); remove them before committing:", file=sys.stderr)
        for finding in findings:
            print(finding.format(), file=sys.stderr)
    if not terms:
        print(f"secret-scan: note: {TERMS_FILE.relative_to(REPO_ROOT)} not found; sensitive terms not checked",
              file=sys.stderr)

    binary = find_gitleaks()
    if binary is None:
        message = "gitleaks not found (run: python ops/tools/install_gitleaks.py)"
        if args.require_gitleaks:
            print(f"secret-scan: {message}", file=sys.stderr)
            return 2
        print(f"secret-scan: warning: {message}; built-in rules only", file=sys.stderr)
    elif run_gitleaks(binary, args.mode) != 0:
        failed = True
        print("secret-scan: gitleaks reported findings (above)", file=sys.stderr)

    if failed:
        return 1
    print(f"secret-scan: clean ({args.mode}{', gitleaks' if binary else ''}"
          f"{f', {len(terms)} sensitive terms' if terms else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
