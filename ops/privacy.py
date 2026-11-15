"""privacy_report: what each provider receives, in plain language, with links to the providers' own
current terms, and the checklist you must confirm before anything is uploaded.

This module deliberately states no retention, deletion or training policy for either provider:
those change, and only the providers' current terms are authoritative. The links were checked to
resolve on LINKS_CHECKED_ON; read the pages themselves.

Your confirmation is stored in ops/state/privacy_checklist.json with the date, the checklist text
and the hosts it covered. pilot, run and the wrapper's analysis tools refuse to send anything until
it exists, and ask again if the hosts or the checklist change.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ops import paths
from ops.confirm import NotConfirmed, data_flows, typed_phrase

LINKS_CHECKED_ON = "2026-11-15"
LINKS = {
    "Google Gemini API": [
        ("Gemini API terms", "https://ai.google.dev/gemini-api/terms"),
        ("Gemini API billing", "https://ai.google.dev/gemini-api/docs/billing"),
        ("Google Privacy Policy", "https://policies.google.com/privacy"),
    ],
    "Zep Cloud": [
        ("Zep Terms of Service", "https://www.getzep.com/legal/terms/"),
        ("Zep Privacy Policy", "https://www.getzep.com/legal/privacy/"),
        ("Zep documentation: deleting data from the graph", "https://help.getzep.com/deleting-data-from-the-graph"),
        ("Zep Trust Center", "https://trust.getzep.com/"),
    ],
}
CHECKLIST = [
    ("gemini-paid-tier",
     "My Gemini API key belongs to a Google Cloud project with billing enabled (paid tier), and I have "
     "read Google's current Gemini API terms on how API data is used, including how the free (unpaid) "
     "tier differs."),
    ("zep-retention",
     "I have read Zep Cloud's current terms and privacy policy, including how long it keeps graph data "
     "and how deletion works, and I accept them for this data. (`python -m ops purge` deletes this "
     "wrapper's graphs and verifies they are gone from the API; what Zep keeps beyond that is set by its terms.)"),
    ("content-leaves",
     "I understand that the pseudonymised seed, the requirements and everything the simulation writes "
     "leave this machine for the hosts above. Pseudonymisation, money ranges and the leak gate remove "
     "the listed names, figures and patterns; they do not hide what the scenario is about."),
]
WRAPPER_FLOWS = [
    "The wrapper's own analysis tools (only when you run them, each after its own typed confirmation;",
    "they go through the same relay, leak gate and budget):",
    "    - variants propose: the pseudonymised seed and each UNKNOWN fact",
    "    - factguard: sentences from a run's report (pseudonymised), the UNKNOWN and the run's premise",
    "    - compare: a digest of each run's simulated discussion (pseudonymised) and the question",
]
OTHER_FLOWS = [
    "doctor: fixed synthetic test prompts to Gemini and a project lookup to Zep; no seed or run content.",
    "bootstrap (setup time only): source code and packages from github.com, PyPI and the npm registry;",
    "    model and tokenizer files from huggingface.co and openaipublic.blob.core.windows.net.",
    "    These downloads send none of your data.",
    "Stays on this machine: the original seed and questions, the pseudonym mapping (ops/mappings),",
    "    sensitive_terms.txt, de-pseudonymised reports, keys in the OS keychain, and all logs.",
]


def checklist_file() -> Path:
    return paths.STATE_DIR / "privacy_checklist.json"


def hosts() -> Dict[str, List[str]]:
    from ops.netcheck import load_allowlist
    return {group: sorted(names) for group, names in load_allowlist().items() if names}


def checklist_digest() -> str:
    return hashlib.sha256(json.dumps(CHECKLIST).encode("utf-8")).hexdigest()


def checklist_status() -> Tuple[bool, str]:
    try:
        record = json.loads(checklist_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "the privacy checklist has not been confirmed; run `python -m ops privacy_report --confirm`"
    except (OSError, ValueError):
        return False, f"{checklist_file()} is unreadable; run `python -m ops privacy_report --confirm` again"
    if record.get("checklist_sha256") != checklist_digest():
        return False, "the privacy checklist changed since you confirmed it; run `python -m ops privacy_report --confirm`"
    if record.get("hosts") != hosts():
        return False, ("the hosts that receive data changed since you confirmed the privacy checklist; "
                       "run `python -m ops privacy_report --confirm`")
    return True, f"privacy checklist confirmed on {record.get('confirmed_on')}"


def require_checklist() -> None:
    ok, message = checklist_status()
    if not ok:
        raise NotConfirmed(message)


def report_lines(llm_base_url: str) -> List[str]:
    lines = ["WHAT LEAVES THIS MACHINE, AND TO WHOM", "",
             "During a pilot or run (after the leak gate and your typed confirmation):"]
    lines += [f"  {line}" for line in data_flows(llm_base_url)]
    lines += ["", *WRAPPER_FLOWS, "", *OTHER_FLOWS, "",
              "WHAT THIS TOOL DOES NOT TELL YOU",
              "  How long Google or Zep keep this data, whether it is used to train or improve their",
              "  services, and who can read it are set by their current terms, which change. This tool does",
              "  not summarise them. Read them here (links checked to resolve on " + LINKS_CHECKED_ON + "):"]
    for provider, links in LINKS.items():
        lines.append(f"  {provider}:")
        lines += [f"    - {label}: {url}" for label, url in links]
    ok, status = checklist_status()
    lines += ["", f"CHECKLIST ({'confirmed' if ok else 'NOT confirmed'}): {status}"]
    lines += [f"  [{'x' if ok else ' '}] {text}" for _, text in CHECKLIST]
    return lines


def confirm_checklist(ask: Optional[Callable[[str], str]] = None, out=None,
                      interactive: Optional[bool] = None) -> Dict[str, object]:
    out = out or sys.stdout
    print("\nConfirm each item by typing 'yes' (anything else stops; nothing is recorded):", file=out)
    for item_id, text in CHECKLIST:
        typed_phrase([f"\n- {text}"], "yes", f"checklist item '{item_id}' not confirmed; nothing was recorded",
                     ask=ask, out=out, interactive=interactive)
    record = {"confirmed_on": time.strftime("%Y-%m-%d"), "confirmed_at": time.time(),
              "checklist_sha256": checklist_digest(), "items": [item_id for item_id, _ in CHECKLIST],
              "hosts": hosts(), "links_checked_on": LINKS_CHECKED_ON}
    paths.write_private_text(checklist_file(), json.dumps(record, indent=2) + "\n")
    print(f"\nRecorded in {checklist_file()}.", file=out)
    return record


def run(confirm: bool = False, ask: Optional[Callable[[str], str]] = None, out=None,
        interactive: Optional[bool] = None) -> int:
    from ops.config import ConfigError, load_llm_config
    out = out or sys.stdout
    try:
        base_url = load_llm_config().base_url
    except (ConfigError, OSError):
        from ops.netcheck import load_allowlist
        relay_hosts = sorted(load_allowlist()["relay"])
        base_url = f"https://{relay_hosts[0]}/" if relay_hosts else "the configured LLM provider"
    print("\n".join(report_lines(base_url)), file=out)
    if not confirm:
        return 0
    try:
        confirm_checklist(ask=ask, out=out, interactive=interactive)
    except NotConfirmed as error:
        print(str(error), file=out)
        return 2
    return 0
