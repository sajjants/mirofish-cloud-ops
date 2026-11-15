"""Restore original names and figures in MiroFish outputs produced from a pseudonymised seed.

  python -m ops depseudonymize --mapping ops/mappings/NAME.mapping.json FILE [FILE ...] [--out-dir DIR | --stdout]

Pseudonyms and pseudonymised figures that appear verbatim are replaced with the originals. A money
range ('between $2 million and $5 million') is restored only if exactly one original amount produced
it; otherwise it stays a range, which is still true. Figures the model derived itself (sums, amounts
inside a range) cannot be mapped back; remaining money amounts are listed so a reader knows they are
the model's, not the seed's. Output files are written 0600 as <name>.depseudo<ext>.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ops.paths import write_private_text
from ops.pseudonymize import CURRENCY_WORD, NUMBER

SUPPORTED_VERSIONS = (1, 2)


def load_mapping(path: Path) -> Dict:
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if mapping.get("version") not in SUPPORTED_VERSIONS:
        raise ValueError(f"{path}: unsupported mapping version {mapping.get('version')!r}")
    return mapping


def reverse_table(mapping: Dict) -> Dict[str, str]:
    """pseudonym -> original. The first original listed for a pseudonym (the canonical name) wins;
    a money range that several different amounts fall into is left as it is."""
    table: Dict[str, str] = {}
    for entry in mapping.get("names", []):
        table.setdefault(entry["pseudonym"], entry["original"])
    shared = Counter(e["pseudonym"] for e in mapping.get("figures", []) if e.get("kind") == "money_range")
    for entry in mapping.get("figures", []):
        if entry.get("kind") == "money_range" and shared[entry["pseudonym"]] > 1:
            continue
        table.setdefault(entry["pseudonym"], entry["original"])
    return table


def depseudonymize(text: str, mapping: Dict) -> Tuple[str, List[str]]:
    """Return (restored_text, money amounts in the text that are not the seed's own)."""
    table = reverse_table(mapping)
    if table:
        alternatives = sorted(table, key=len, reverse=True)
        # Not inside a longer word or number ('451' must not match within '1,451' or '451.5').
        pattern = re.compile(r"(?<![\w.,])(" + "|".join(re.escape(a) for a in alternatives) + r")(?![\w]|[.,]\d)")
        restored = pattern.sub(lambda m: table[m.group(1)], text)
    else:
        restored = text
    originals = {e["original"] for e in mapping.get("figures", [])}
    seed_ranges = [(m.start(), m.end()) for e in mapping.get("figures", []) if e.get("kind") == "money_range"
                   for m in re.finditer(re.escape(e["pseudonym"]), restored)]
    unmapped = []
    for match in NUMBER.finditer(restored):
        is_money = match["cur"] or CURRENCY_WORD.search(match["unit"] or "")
        inside_seed_range = any(start <= match.start() < end for start, end in seed_ranges)
        if is_money and match.group(0) not in originals and not inside_seed_range:
            unmapped.append(match.group(0).strip())
    return restored, sorted(set(unmapped))


def unmapped_note(mapping: Dict, unmapped: List[str]) -> str:
    if mapping.get("version", 1) >= 2:
        return ("money amounts that are not the seed's (the seed's money was sent only as ranges, so these "
                f"are the model's own): {', '.join(unmapped)}")
    return f"amounts still on the pseudonymised scale (divide by {mapping['scale_factor']}): {', '.join(unmapped)}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ops depseudonymize", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out-dir", type=Path)
    target.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    mapping = load_mapping(args.mapping)
    for path in args.files:
        restored, unmapped = depseudonymize(path.read_text(encoding="utf-8"), mapping)
        if args.stdout:
            sys.stdout.write(restored)
        else:
            out = (args.out_dir or path.parent) / f"{path.stem}.depseudo{path.suffix}"
            write_private_text(out, restored)
            print(f"wrote {out}")
        if unmapped:
            print(f"{path.name}: {unmapped_note(mapping, unmapped)}", file=sys.stderr)
    return 0
