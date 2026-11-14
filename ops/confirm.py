"""The typed confirmation every pilot and run needs: which hosts receive which classes of data."""

from __future__ import annotations

import sys
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from ops.zep import ZEP_HOST


class NotConfirmed(RuntimeError):
    pass


def data_flows(llm_base_url: str) -> List[str]:
    gemini = urlsplit(llm_base_url).hostname or llm_base_url
    return [
        f"{gemini} (Google Gemini API), through the local relay:",
        "    - the pseudonymised seed and the simulation requirement, inside prompts",
        "    - prompts built from them: the ontology, personas, simulation settings, every simulated",
        "      agent's feed and its posts, the report agent's drafts and interview questions",
        "    - your Gemini API key (as the request credential)",
        f"{ZEP_HOST} (Zep Cloud), directly from MiroFish's Zep SDK:",
        "    - the pseudonymised seed, split into chunks (stored as graph episodes and extracted by Zep)",
        "    - the ontology (entity and relation types) and the run's project name",
        "    - graph search queries made while generating personas and by the report agent's tools",
        "    - your Zep API key (as the request credential)",
        "    - not sent: simulated posts (graph-memory update is off)",
        "Nothing is sent anywhere else; the leak gate has already passed on every uploaded text.",
    ]


def typed_phrase(lines: List[str], phrase: str, refused: str, ask: Optional[Callable[[str], str]] = None,
                 out=None, interactive: Optional[bool] = None) -> None:
    """Print the lines and require the exact phrase; raise NotConfirmed(refused) otherwise."""
    out = out or sys.stdout
    if ask is None:
        if not (sys.stdin.isatty() if interactive is None else interactive):
            raise NotConfirmed("a typed confirmation is required, and no one is at the terminal (stdin is not a TTY)")
        ask = input
    for line in lines:
        print(line, file=out)
    answer = ask(f"\nType '{phrase}' to continue, anything else to stop: ")
    if answer.strip() != phrase:
        raise NotConfirmed(refused)


def confirm(title: str, details: List[str], phrase: str, llm_base_url: str,
            ask: Optional[Callable[[str], str]] = None, out=None, interactive: Optional[bool] = None) -> None:
    """Print what will be sent and require the exact phrase; raise NotConfirmed otherwise."""
    lines = [title, *(f"  {line}" for line in details), "\nThis sends data to:",
             *(f"  {line}" for line in data_flows(llm_base_url))]
    typed_phrase(lines, phrase, "not confirmed; nothing was sent", ask=ask, out=out, interactive=interactive)
