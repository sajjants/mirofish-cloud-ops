"""The leak gate: nothing is uploaded if it contains a sensitive term, an email, a phone number or a long digit string.

There is no flag to skip it. A missing or empty terms file, or the unchanged example file, also
blocks uploads, because the gate could not do its job. Reports name the matched terms (the user's
own list), the input and the line number, never the surrounding text. The relay uses only the term
matcher (D4) and reports terms by their line number in the terms file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from ops.paths import CONFIG_DIR

TERMS_FILE = CONFIG_DIR / "sensitive_terms.txt"
EXAMPLE_TERMS_FILE = CONFIG_DIR / "sensitive_terms.example.txt"

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
PHONE_CANDIDATE = re.compile(r"(?<![\w.])\+?\(?\d[\d\s().\-]{6,}\d(?![\w.])")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
LONG_DIGITS = re.compile(r"(?<!\d)\d{8,}(?!\d)")


class LeakGateError(RuntimeError):
    def __init__(self, message: str, matches: Sequence["Match"] = ()):
        super().__init__(message)
        self.matches = list(matches)


@dataclass(frozen=True)
class Term:
    text: str
    line: int


@dataclass(frozen=True)
class Match:
    kind: str           # term | email | phone | long_digits
    label: str          # the term itself, or the pattern kind
    source: str         # which input, e.g. "seed (variant v1)" or "requirement Q2"
    line: int           # line number in that input
    term_line: Optional[int] = None


def parse_terms(text: str) -> List[Term]:
    terms = []
    for number, raw in enumerate(text.splitlines(), start=1):
        value = raw.strip()
        if value and not value.startswith("#"):
            terms.append(Term(value, number))
    return terms


def load_terms(path: Path = TERMS_FILE, example: Path = EXAMPLE_TERMS_FILE) -> List[Term]:
    if not path.is_file():
        raise LeakGateError(f"{path.name} not found: copy {example.name} to {path.name} and list your real "
                            "sensitive terms; uploads stay blocked until it exists")
    terms = parse_terms(path.read_text(encoding="utf-8"))
    if not terms:
        raise LeakGateError(f"{path.name} lists no terms; uploads stay blocked until it does")
    if example.is_file() and {t.text for t in terms} == {t.text for t in parse_terms(example.read_text("utf-8"))}:
        raise LeakGateError(f"{path.name} still holds the fictional examples; replace them with your real terms")
    return terms


def _term_pattern(term: str) -> str:
    return r"(?<!\w)" + r"\s+".join(re.escape(part) for part in term.split()) + r"(?!\w)"


class TermMatcher:
    """All terms in one case-insensitive regex; finds which terms occur in a text."""

    def __init__(self, terms: Sequence[Term]):
        self.terms = list(terms)
        self._patterns = [re.compile(_term_pattern(t.text), re.IGNORECASE) for t in self.terms]
        combined = "|".join(f"(?:{p.pattern})" for p in self._patterns)
        self._any = re.compile(combined, re.IGNORECASE) if combined else None

    def find(self, text: str) -> List[Term]:
        if self._any is None or not self._any.search(text):
            return []
        return [t for t, p in zip(self.terms, self._patterns) if p.search(text)]

    def term_lines(self, text: str) -> List[int]:
        return sorted({t.line for t in self.find(text)})


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _phones(text: str) -> Iterator[re.Match]:
    for match in PHONE_CANDIDATE.finditer(text):
        value = match.group(0)
        digits = sum(ch.isdigit() for ch in value)
        if 9 <= digits <= 15 and not ISO_DATE.fullmatch(value.strip()):
            yield match


def scan(text: str, source: str, matcher: TermMatcher) -> List[Match]:
    found: List[Match] = []
    for term, pattern in zip(matcher.terms, matcher._patterns):
        for hit in pattern.finditer(text):
            found.append(Match("term", term.text, source, _line_of(text, hit.start()), term.line))
    for kind, hits in (("email", EMAIL.finditer(text)), ("phone", _phones(text)),
                       ("long_digits", LONG_DIGITS.finditer(text))):
        for hit in hits:
            found.append(Match(kind, kind, source, _line_of(text, hit.start())))
    return found


def check(sources: Dict[str, str], terms: Optional[Sequence[Term]] = None) -> None:
    """Raise LeakGateError listing every match across the named inputs; return quietly if all are clean."""
    matcher = TermMatcher(load_terms() if terms is None else terms)
    matches = [m for name, text in sources.items() for m in scan(text, name, matcher)]
    if matches:
        raise LeakGateError(format_matches(matches), matches)


def format_matches(matches: Iterable[Match]) -> str:
    grouped: Dict[tuple, List[int]] = {}
    for m in matches:
        grouped.setdefault((m.source, m.kind, m.label, m.term_line), []).append(m.line)
    lines = ["LEAK GATE: nothing was uploaded. Matches (the surrounding text is not shown):"]
    for (source, kind, label, term_line), at in sorted(grouped.items(), key=lambda item: str(item[0])):
        where = ", ".join(str(n) for n in sorted(set(at))[:10]) + (" ..." if len(set(at)) > 10 else "")
        what = f"term '{label}' (terms file line {term_line})" if kind == "term" else {
            "email": "an email address", "phone": "a phone number", "long_digits": "a long digit string"}[kind]
        lines.append(f"  {source}: {what} on line(s) {where}")
    lines.append("Pseudonymise or remove these, then prepare the batch again.")
    return "\n".join(lines)


def request_strings(body: Any) -> Iterator[str]:
    """Every string value in a JSON request body (messages, tool descriptions), for the relay's gate."""
    if isinstance(body, str):
        yield body
    elif isinstance(body, dict):
        for value in body.values():
            yield from request_strings(value)
    elif isinstance(body, list):
        for value in body:
            yield from request_strings(value)


def request_text(body: Dict[str, Any]) -> str:
    return "\n".join(request_strings(body))
