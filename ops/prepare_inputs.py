"""prepare: turn seed.md + questions.md into a batch's cloud-safe inputs (no network).

  python -m ops prepare --batch BATCH_ID --seed seed.md --questions questions.md --terms terms.txt [--keep-flagged]

Writes ops/runs/<BATCH_ID>/ (0700):
  inputs/seed.original.md      the seed after the instruction lint (stays local)
  inputs/seed.md               what is uploaded: the linted seed, pseudonymised (names replaced, money
                               as ranges, counts scaled)
  inputs/questions.md          the pseudonymised questions
  inputs/<Qn>.requirement.txt  the simulation requirement sent with each question
  inputs.json                  facts, UNKNOWNs, questions, lint findings and input hashes
and the pseudonym mapping to ops/mappings/<BATCH_ID>.mapping.json (git-ignored, 0600).

Pseudonymisation is required (--terms, format in ops/pseudonymize.py). The leak gate
(ops/config/sensitive_terms.txt plus email, phone and long-digit patterns) runs on the pseudonymised
seed, questions and requirements before anything is written; on a match nothing is written and the
matched terms are listed, never the text around them.

The lint drops HTML comments and lines that address a model ('ignore previous instructions',
'SYSTEM NOTE', 'begin the report with ...'). It is a heuristic, not a guarantee.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, Optional, Sequence

from ops import leakgate, seedfile
from ops.budget import BATCH_ID
from ops.mirofish_api import sha256_file, write_private_json
from ops import paths
from ops.paths import make_private_dir, write_private_text
from ops.pseudonymize import parse_terms, pseudonymize_texts, suggest


def validate_batch_id(batch_id: str) -> str:
    if not BATCH_ID.fullmatch(batch_id or ""):
        raise ValueError(f"invalid batch id {batch_id!r}: letters, digits and '-' only (max 40)")
    return batch_id


def batch_dir(batch_id: str) -> Path:
    return paths.RUNS_DIR / validate_batch_id(batch_id)


def load_inputs(batch_id: str) -> Dict:
    path = batch_dir(batch_id) / "inputs.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `python -m ops prepare --batch {batch_id} ...` first")
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(batch_id: str, seed_path: Path, questions_path: Path, terms_path: Path, keep_flagged: bool = False,
            rng_seed: Optional[int] = None, gate_terms: Optional[Sequence[leakgate.Term]] = None) -> Dict:
    target = batch_dir(batch_id)
    synced = paths.cloud_sync_problems([paths.RUNS_DIR, paths.MAPPINGS_DIR])
    if synced:
        raise ValueError(f"refusing to write the original seed and the pseudonym mapping where a sync client "
                         f"would upload them: {synced[0]}. Move the repository out of synced folders.")
    if (target / "inputs.json").exists():
        raise FileExistsError(f"{target} already has inputs; choose a new batch id (or purge this one)")
    raw_seed = seed_path.read_text(encoding="utf-8")
    raw_questions = questions_path.read_text(encoding="utf-8")
    original_seed, findings = seedfile.lint_seed(raw_seed, keep_flagged=keep_flagged)
    seedfile.parse_questions(raw_questions)  # fail early on a malformed questions file

    terms = parse_terms(terms_path.read_text(encoding="utf-8"))
    if not terms:
        raise ValueError(f"{terms_path} lists no names to pseudonymise")
    out, mapping = pseudonymize_texts({"seed": original_seed, "questions": raw_questions}, terms, rng_seed)
    seed_text, questions_text = out["seed"], out["questions"]
    facts = seedfile.parse_facts(seed_text)
    questions = seedfile.parse_questions(questions_text)
    if not questions:
        raise ValueError(f"{questions_path}: no '## Q<n>' sections found")
    requirements = {q.id: seedfile.requirement_text(q) for q in questions}
    leakgate.check({"seed (pseudonymised)": seed_text, "questions (pseudonymised)": questions_text,
                    **{f"requirement {qid}": text for qid, text in requirements.items()}}, gate_terms)

    mapping["inputs"] = [{"name": p.name, "sha256": sha256_file(p)} for p in (seed_path, questions_path, terms_path)]
    mapping_path = make_private_dir(paths.MAPPINGS_DIR) / f"{batch_id}.mapping.json"
    write_private_json(mapping_path, mapping)
    make_private_dir(paths.RUNS_DIR)
    inputs_dir = make_private_dir(target / "inputs")
    write_private_text(inputs_dir / "seed.original.md", original_seed)
    write_private_text(inputs_dir / "seed.md", seed_text)
    write_private_text(inputs_dir / "questions.md", questions_text)
    question_entries = []
    for question in questions:
        requirement = requirements[question.id]
        name = f"{question.id}.requirement.txt"
        write_private_text(inputs_dir / name, requirement)
        question_entries.append({"id": question.id, "title": question.title, "requirement_file": f"inputs/{name}",
                                 "requirement_sha256": hashlib.sha256(requirement.encode()).hexdigest()})
    counts = {tag: sum(1 for f in facts if f.tag == tag) for tag in seedfile.TAGS}
    inputs = {
        "batch_id": batch_id,
        "created_at": time.time(),
        "sources": {"seed": {"name": seed_path.name, "sha256": sha256_file(seed_path)},
                    "questions": {"name": questions_path.name, "sha256": sha256_file(questions_path)}},
        "upload_seed_sha256": hashlib.sha256(seed_text.encode()).hexdigest(),
        "pseudonymized": True,
        "mapping_file": str(mapping_path),
        "mapping_version": mapping["version"],
        "lint": {"keep_flagged": keep_flagged, "findings": findings},
        "fact_counts": counts,
        "facts": [f.to_dict() for f in facts],
        "unknowns": [f.to_dict() for f in seedfile.unknowns(facts)],
        "questions": question_entries,
        "unlisted_capitalised_phrases": suggest(seed_text + "\n" + questions_text, [])[:50],
    }
    write_private_json(target / "inputs.json", inputs)
    return inputs


def summary(inputs: Dict) -> str:
    counts = inputs["fact_counts"]
    lines = [f"Batch {inputs['batch_id']}: {len(inputs['questions'])} question(s); facts "
             + ", ".join(f"{tag} {counts[tag]}" for tag in seedfile.TAGS)]
    for finding in inputs["lint"]["findings"]:
        lines.append(f"  LINT {finding['action']}: line {finding['line']} [{finding['rule']}] {finding['excerpt']}")
    if not sum(counts.values()):
        lines.append("  WARNING: no tagged facts found (lines must start with VERIFIED:, ESTIMATE:, BELIEF: or UNKNOWN:)")
    lines.append(f"  pseudonymised (money as ranges); leak gate passed; mapping in {inputs['mapping_file']} "
                 "(keep private)")
    if inputs["unlisted_capitalised_phrases"]:
        lines.append("  review capitalised phrases left in the upload (add real names to the terms file and "
                     "prepare a new batch): " + ", ".join(inputs["unlisted_capitalised_phrases"][:15]))
    lines.append(f"  read what will be uploaded: {batch_dir(inputs['batch_id']) / 'inputs' / 'seed.md'}")
    lines.append(f"  next: python -m ops variants propose --batch {inputs['batch_id']}"
                 if inputs["unknowns"] else f"  next: python -m ops plan --batch {inputs['batch_id']}, then pilot")
    return "\n".join(lines)


def main(batch_id: str, seed: Path, questions: Path, terms: Path, keep_flagged: bool = False) -> int:
    try:
        print(summary(prepare(batch_id, seed, questions, terms, keep_flagged)))
    except leakgate.LeakGateError as error:
        print(str(error))
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"FAIL: {error}")
        return 1
    return 0
