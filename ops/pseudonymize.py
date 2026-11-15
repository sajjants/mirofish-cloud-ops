"""Replace names, counterparties and figures in a seed with consistent pseudonyms.

  python -m ops pseudonymize --terms TERMS.txt [--mapping FILE] IN.md [IN2.md ...] --out-dir DIR
  python -m ops pseudonymize --suggest IN.md   # list capitalised phrases to review for the terms file

TERMS file, one entity per line ('#' comments allowed), aliases separated by '|':
    person: Elena Duarte | Duarte
    org:    Riverton Transit Workers Union | the union's leadership
    place:  Riverton
List atoms: 'place: Riverton' also rewrites 'Riverton City Council'. A person's last name is
mapped on its own automatically ('Mayor Duarte' -> 'Mayor Quill').

Money (an amount with a currency symbol or code, or followed by dollars/euros/pounds) becomes a
range on a 1-2-5 scale: '$3.2m' -> 'between $2 million and $5 million', '€450' -> 'between €200
and €500'. Other numbers >= 100 (counts; not years 1900-2100 or percentages) are multiplied by a
secret factor between 0.7 and 1.4 and rounded to 3 significant digits. depseudonymize restores a
range only when exactly one original amount produced it.

The mapping file holds the originals, so it is written 0600 under ops/mappings/ (git-ignored).
Output is refused if any original term is still present.
"""

from __future__ import annotations

import argparse
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ops.mirofish_api import sha256_file, write_private_json
from ops import paths
from ops.paths import make_private_dir, write_private_text

MAPPING_VERSION = 2

CATEGORIES = ("person", "org", "place", "name")
FIRST_NAMES = ["Mara", "Joel", "Ines", "Tobin", "Lena", "Arlo", "Priya", "Cosmo", "Wren", "Dario",
               "Hana", "Felix", "Noor", "Otto", "Sabine", "Rafe", "Yara", "Emil", "Tessa", "Kofi",
               "Livia", "Bram", "Selin", "Ivo", "Maeve", "Teo", "Anouk", "Jasper", "Rhea", "Malik"]
LAST_NAMES = ["Quill", "Ashby", "Marlow", "Venn", "Okafor", "Lindqvist", "Brandt", "Castell", "Hale",
              "Ferreira", "Novak", "Pryor", "Sato", "Whitlock", "Arden", "Kovacs", "Dunmore", "Ilves",
              "Mercer", "Oyelaran", "Petrov", "Rasmussen", "Strand", "Tamsin", "Upton", "Voss",
              "Wexley", "Yilmaz", "Zeller", "Corbin"]
ORG_WORDS = ["Alder", "Bracken", "Cobalt", "Driftwood", "Ember", "Fenwick", "Granite", "Heron",
             "Ironbridge", "Juniper", "Kestrel", "Larch", "Meridian", "Northwind", "Oakhurst",
             "Pinecrest", "Quarry", "Redfern", "Saltmarsh", "Thornbury", "Umber", "Vantage",
             "Willowby", "Yarrow"]
ORG_SUFFIXES = ["Group", "Partners", "Holdings", "Collective", "Alliance", "Associates", "Trust", "Works"]
PLACE_NAMES = ["Elmford", "Brackwater", "Coldharbour", "Dunmere", "Eastwick", "Fallowfield", "Glenrock",
               "Hollins", "Kingsmere", "Lowbridge", "Marchmont", "Netherby", "Oldcastle", "Penhallow",
               "Ravensby", "Stonegate", "Thistleton", "Westmoor"]

CURRENCY = r"(?:[$€£¥]|USD|EUR|GBP|CHF|AUD|CAD)"
SCALE_WORDS = r"(?:thousand|million|billion|trillion|[kKmMbB]n?\b)"
# Not inside identifiers such as 'CASE-7731' or 'v2.10', and not part of an ISO date.
NUMBER = re.compile(
    rf"(?<![\w.\-/:])(?P<cur>{CURRENCY}\s?)?(?P<num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    rf"(?![\-/:]|\.\d)(?:(?P<scale>\s?{SCALE_WORDS})|(?!\w))"
    rf"(?P<unit>\s?(?:dollars|euros|pounds|percent\b|per\s?cent\b|%))?"
)
CAPITALISED_PHRASE = re.compile(r"\b[A-Z][a-zA-Z'’-]+(?:\s+(?:of|the|and|for|de|van|von)?\s*[A-Z][a-zA-Z'’-]+)*")
CURRENCY_WORD = re.compile(r"dollars|euros|pounds")
SCALE_VALUES = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mn": 1e6, "billion": 1e9, "b": 1e9,
                "bn": 1e9, "trillion": 1e12}
WORD_SCALES = ((1e12, "trillion"), (1e9, "billion"), (1e6, "million"))


def money_bucket(value: float) -> Tuple[float, float]:
    """The 1-2-5 range [low, high) that holds a positive value, e.g. 3.2e6 -> (2e6, 5e6)."""
    exponent = math.floor(math.log10(value))
    mantissa = value / 10 ** exponent
    if mantissa >= 10:
        exponent, mantissa = exponent + 1, mantissa / 10
    elif mantissa < 1:
        exponent, mantissa = exponent - 1, mantissa * 10
    low, high = (5, 10) if mantissa >= 5 else (2, 5) if mantissa >= 2 else (1, 2)
    return low * 10.0 ** exponent, high * 10.0 ** exponent


def amount_words(value: float) -> str:
    for size, word in WORD_SCALES:
        if value >= size:
            return f"{value / size:g} {word}"
    if value >= 1:
        return f"{round(value):,}"
    return f"{value:.2f}"


@dataclass
class Term:
    category: str
    original: str
    aliases: List[str] = field(default_factory=list)


def parse_terms(text: str) -> List[Term]:
    terms: List[Term] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        category = "name"
        if ":" in line:
            prefix, rest = line.split(":", 1)
            if prefix.strip().lower() in CATEGORIES:
                category, line = prefix.strip().lower(), rest
        names = [n.strip() for n in line.split("|") if n.strip()]
        if not names:
            raise ValueError(f"terms line {number}: no name")
        terms.append(Term(category, names[0], names[1:]))
    return terms


class Pseudonymizer:
    def __init__(self, rng_seed: Optional[int] = None):
        self.rng = random.Random(rng_seed if rng_seed is not None else random.SystemRandom().getrandbits(64))
        self.scale_factor = round(self.rng.uniform(0.7, 1.4), 4)
        self.names: Dict[str, Tuple[str, str]] = {}  # original -> (pseudonym, category)
        self.figures: Dict[str, str] = {}
        self.figure_kinds: Dict[str, str] = {}   # original -> "money_range" | "scaled"
        self.context = ""
        self._used: set = set()

    # ----- names -----
    def _fresh(self, pool: List[str], forbidden_text: str) -> str:
        options = [p for p in pool if p not in self._used and p.lower() not in forbidden_text.lower()]
        if not options:
            options = [f"{p}{len(self._used)}" for p in pool]
        choice = self.rng.choice(options)
        self._used.add(choice)
        return choice

    def add_terms(self, terms: Iterable[Term], context: str) -> None:
        self.context = context
        for term in terms:
            if term.category == "person":
                parts = term.original.split()
                first = self._fresh(FIRST_NAMES, context)
                last = self._fresh(LAST_NAMES, context)
                pseudo = f"{first} {last}" if len(parts) > 1 else last
                self.names[term.original] = (pseudo, "person")
                if len(parts) > 1:
                    self.names.setdefault(parts[-1], (last, "person"))
                for alias in term.aliases:
                    if alias == parts[0]:
                        self.names[alias] = (first, "person")
                    elif alias == parts[-1]:
                        self.names[alias] = (last, "person")
                    else:
                        self.names[alias] = (pseudo, "person")
            else:
                if term.category == "place":
                    pseudo = self._fresh(PLACE_NAMES, context)
                else:
                    pseudo = f"{self._fresh(ORG_WORDS, context)} {self.rng.choice(ORG_SUFFIXES)}"
                for name in [term.original, *term.aliases]:
                    self.names[name] = (pseudo, term.category)

    def _name_pattern(self) -> Optional[re.Pattern]:
        if not self.names:
            return None
        alternatives = sorted(self.names, key=len, reverse=True)
        return re.compile(r"(?<![\w])(" + "|".join(re.escape(a) for a in alternatives) + r")(?![\w])")

    # ----- figures -----
    def _in_source(self, pseudo: str) -> bool:
        """A pseudonymised figure must not equal a number already in the input, or depseudonymize
        would rewrite that unrelated number."""
        return bool(re.search(r"(?<![\w.,])" + re.escape(pseudo) + r"(?![\w]|[.,]\d)", self.context))

    def _scale(self, match: re.Match) -> str:
        whole = match.group(0)
        if match["unit"] and re.search(r"percent|per\s?cent|%", match["unit"]):
            return whole
        raw = match["num"]
        value = float(raw.replace(",", ""))
        is_money = bool(match["cur"] or match["scale"] or (match["unit"] or "").strip())
        is_year = (not is_money and "," not in raw and "." not in raw and 1900 <= value <= 2100)
        if is_year or (not is_money and value < 100):
            return whole
        if whole in self.figures:
            return self.figures[whole]
        if match["cur"] or CURRENCY_WORD.search(match["unit"] or ""):
            return self._money_range(match, value)
        scaled = float(f"{value * self.scale_factor:.3g}")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        text = f"{scaled:,.{decimals}f}" if "," in raw else f"{scaled:.{decimals}f}"
        pseudo = f"{match['cur'] or ''}{text}{match['scale'] or ''}{match['unit'] or ''}"
        while pseudo in self.figures.values() or pseudo == whole or self._in_source(pseudo):
            scaled += 10 ** max(0, len(str(int(scaled))) - 3) if decimals == 0 else 10 ** -decimals
            text = f"{scaled:,.{decimals}f}" if "," in raw else f"{scaled:.{decimals}f}"
            pseudo = f"{match['cur'] or ''}{text}{match['scale'] or ''}{match['unit'] or ''}"
        self.figures[whole] = pseudo
        self.figure_kinds[whole] = "scaled"
        return pseudo

    def _money_range(self, match: re.Match, value: float) -> str:
        whole = match.group(0)
        scale = (match["scale"] or "").strip().lower()
        amount = value * SCALE_VALUES.get(scale, 1.0)
        if amount <= 0:
            return whole
        low, high = money_bucket(amount)
        currency, unit = match["cur"] or "", (match["unit"] or "").rstrip()
        pseudo = f"between {currency}{amount_words(low)}{unit} and {currency}{amount_words(high)}{unit}"
        self.figures[whole] = pseudo
        self.figure_kinds[whole] = "money_range"
        return pseudo

    # ----- apply -----
    def apply(self, text: str) -> str:
        pattern = self._name_pattern()
        if pattern:
            text = pattern.sub(lambda m: self.names[m.group(1)][0], text)
        return NUMBER.sub(self._scale, text)

    def leaks(self, text: str) -> List[str]:
        found = []
        for original in self.names:
            if re.search(r"(?<![\w])" + re.escape(original) + r"(?![\w])", text, re.I):
                found.append(original)
        return found

    def mapping(self) -> Dict:
        return {
            "version": MAPPING_VERSION,
            "scale_factor": self.scale_factor,
            "money": "1-2-5 ranges",
            "names": [{"original": o, "pseudonym": p, "category": c} for o, (p, c) in self.names.items()],
            "figures": [{"original": o, "pseudonym": p, "kind": self.figure_kinds.get(o, "scaled")}
                        for o, p in self.figures.items()],
        }


def suggest(text: str, terms: List[Term]) -> List[str]:
    """Capitalised phrases not covered by the terms file (sentence-initial words are noisy)."""
    covered = {n.lower() for t in terms for n in [t.original, *t.aliases]}
    candidates: Dict[str, int] = {}
    for match in CAPITALISED_PHRASE.finditer(text):
        phrase = match.group(0).strip()
        if phrase.isupper() and len(phrase) <= 9 or phrase.lower() in covered:
            continue
        if phrase.split()[0] in {"The", "A", "An", "In", "On", "If", "It", "This", "Whether", "How", "All"}:
            phrase = " ".join(phrase.split()[1:])
        if len(phrase) > 2 and not any(phrase.lower() in c or c in phrase.lower() for c in covered):
            candidates[phrase] = candidates.get(phrase, 0) + 1
    return sorted(candidates, key=lambda p: (-candidates[p], p))


def pseudonymize_texts(texts: Dict[str, str], terms: List[Term], rng_seed: Optional[int] = None
                       ) -> Tuple[Dict[str, str], Dict]:
    """Pseudonymise several texts with one consistent mapping; raise if anything leaks."""
    engine = Pseudonymizer(rng_seed)
    engine.add_terms(terms, "\n".join(texts.values()))
    out = {name: engine.apply(text) for name, text in texts.items()}
    leaked = sorted({leak for text in out.values() for leak in engine.leaks(text)})
    if leaked:
        raise ValueError(f"original terms survived pseudonymisation: {leaked}")
    return out, engine.mapping()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ops pseudonymize", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--terms", type=Path)
    parser.add_argument("--mapping", type=Path, help="default ops/mappings/<first input stem>.mapping.json")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--suggest", action="store_true")
    parser.add_argument("--seed", type=int, help="RNG seed (default: random); only for tests")
    args = parser.parse_args(argv)

    terms = parse_terms(args.terms.read_text(encoding="utf-8")) if args.terms else []
    texts = {str(p): p.read_text(encoding="utf-8") for p in args.inputs}
    if args.suggest:
        for phrase in suggest("\n".join(texts.values()), terms):
            print(phrase)
        return 0
    if not terms or not args.out_dir:
        parser.error("--terms and --out-dir are required unless --suggest")
    out, mapping = pseudonymize_texts(texts, terms, args.seed)
    mapping["inputs"] = [{"name": p.name, "sha256": sha256_file(p)} for p in args.inputs]
    mapping_path = args.mapping or paths.MAPPINGS_DIR / f"{args.inputs[0].stem}.mapping.json"
    make_private_dir(mapping_path.parent)
    write_private_json(mapping_path, mapping)
    for path in args.inputs:
        write_private_text(args.out_dir / path.name, out[str(path)])
    remaining = suggest("\n".join(out.values()), [])
    print(f"wrote {len(args.inputs)} file(s) to {args.out_dir}; mapping {mapping_path} (keep it private)")
    print(f"{len(mapping['names'])} names, {len(mapping['figures'])} figures replaced (money as ranges, "
          "counts scaled by the secret factor kept in the mapping)")
    if remaining:
        print("review these capitalised phrases that were not replaced:\n  " + "\n  ".join(remaining[:30]))
    return 0
