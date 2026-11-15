# mirofish-cloud-ops

A wrapper around the real [MiroFish](https://github.com/666ghj/MiroFish) social-simulation engine that runs on an ordinary laptop (macOS, Linux or Windows): the LLM is Google Gemini through an API key, and agent memory is Zep Cloud. No GPU, no local model.

MiroFish itself is not modified or included. `bootstrap` clones it at a pinned commit into `mirofish/` (git-ignored) and applies the patches in `ops/patches/`. Everything else (spend metering, the leak gate, pseudonymisation, network evidence, purge) lives in `ops/`.

> **This version deliberately gives up privacy.** The pseudonymised seed, the simulation requirements, and everything the simulation generates from them (personas, posts, reports) are sent to Google and to Zep. Pseudonymisation and the leak gate remove the names, figures and patterns you list; they do not hide what your scenario is about. Simulation results are illustrative samples from a noisy generator and have not been validated against real outcomes.

## Contents

- [What leaves your machine](#what-leaves-your-machine)
- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [A run, step by step](#a-run-step-by-step)
- [Command reference](#command-reference)
- [Spend controls](#spend-controls)
- [Network evidence (netcheck)](#network-evidence-netcheck)
- [Deleting data (purge)](#deleting-data-purge)
- [Docker (untested)](#docker-untested)
- [Development](#development)
- [Limitations](#limitations)
- [Documents in this repository](#documents-in-this-repository)
- [License](#license)

## What leaves your machine

`python -m ops privacy_report` prints this in full, with links to each provider's current terms. In short:

| Host | Receives | When |
|---|---|---|
| `generativelanguage.googleapis.com` (Google Gemini API) | Every LLM prompt MiroFish makes: the pseudonymised seed and requirement, ontology and persona generation, every agent turn, the report. Also the wrapper's own analysis prompts (variants, factguard, compare), each after its own confirmation. | `pilot`, `run`, `variants propose`, `factguard` (unless `--no-llm`), `compare`, and `doctor` (fixed synthetic prompts only) |
| `api.getzep.com` (Zep Cloud) | The pseudonymised seed text, which Zep turns into a knowledge graph; reads and searches of that graph during setup and by the report agent. (Writing the simulation's activity back into the graph is always off in this wrapper.) `purge` sends graph ids to delete them. | `pilot`, `run`, `purge`, `doctor` (a project lookup) |

Nothing is sent until you have:

1. confirmed the **privacy checklist** (`privacy_report --confirm`). It asks you to confirm, one item at a time, that you use a **paid-tier Gemini key with billing enabled** and have read Google's current Gemini API terms (including how unpaid use differs), and that you have read **Zep Cloud's current retention and deletion terms**. This tool does not summarise either provider's data-use, retention or training policy; read their pages, which `privacy_report` links.
2. passed the **leak gate**, which cannot be switched off: before any upload, the seed, every requirement and every variant are checked against your `ops/config/sensitive_terms.txt` and against patterns for email addresses, phone numbers and long digit strings. On a match nothing is uploaded, and only the matched terms and line numbers are shown, never the surrounding text.
3. typed the **confirmation phrase** for that run (for example `send b1`), after a list of the hosts and the classes of data each one will receive.

Stays on your machine: the original seed and questions, the pseudonym mapping (`ops/mappings/`), `sensitive_terms.txt`, de-pseudonymised outputs, logs, and your keys (environment variables or the OS keychain; never written to a file by this tool).

**Do not keep this repository in a cloud-synced or backed-up folder** (Google Drive, iCloud Drive, Dropbox, OneDrive). The sync client would upload your mappings, original seeds and run data. `start` and `prepare` refuse to run when they detect one (path markers, Google Drive's `.tmp.driveupload` folder in a parent directory, or private files that have gained a second hard link), but detection is best effort. Google Drive for desktop can back up `~/Documents` without the path saying so.

## Requirements

- Python **3.11 or 3.12**
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs the locked dependencies)
- git
- [Node.js](https://nodejs.org/en/download) **18 or newer**, with npm (for the MiroFish web UI; skip with `--no-frontend`)
- A Google Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) on a project with billing enabled, and a Zep Cloud API key from your Zep Cloud dashboard

No `bash`, `ss` or `lsof` is needed; the wrapper is pure Python (`pathlib`, `psutil`).

## Install

Clone into a folder that is **not** synced to the cloud, then run bootstrap with Python 3.11 or 3.12:

```bash
git clone https://github.com/sajjants/mirofish-cloud-ops.git
cd mirofish-cloud-ops
python3 -m ops bootstrap          # Windows: py -3.12 -m ops bootstrap
```

bootstrap, in order:

1. checks the prerequisites and enables the pre-commit secret scan (`git config core.hooksPath .githooks`);
2. creates `.venv/` and installs `ops/requirements.lock` (hash-checked);
3. clones upstream MiroFish into `mirofish/` at the commit in `ops/UPSTREAM.md` (refusing any other commit) and applies `ops/patches/`;
4. installs upstream's backend (`uv sync --frozen`) and frontend (`npm ci --ignore-scripts`) from upstream's own lockfiles;
5. writes `mirofish/.env` from `.env.example` (owner-only, random `SECRET_KEY`, no keys);
6. downloads, once, the model and tokenizer files the Twitter simulation needs, so later runs work offline from those hosts;
7. runs upstream's backend tests and the wrapper's tests.

Options: `--no-frontend`, `--skip-tests`, `--skip-assets` (then the simulation would contact huggingface.co and netcheck fails). bootstrap is safe to run again.

From here on, use the wrapper's interpreter: `.venv/bin/python -m ops ...` on macOS and Linux, `.venv\Scripts\python -m ops ...` on Windows. The examples below write `python -m ops` for short.

Install gitleaks for the pre-commit scan (pinned and checksum-verified, into the git-ignored `.tools/`):

```bash
python ops/tools/install_gitleaks.py
```

## Configure

### Keys

Either set environment variables:

```bash
export GEMINI_API_KEY=...   # PowerShell: $env:GEMINI_API_KEY = "..."
export ZEP_API_KEY=...
```

or store them in the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux), prompted with hidden input:

```bash
python -m ops keys set GEMINI_API_KEY
python -m ops keys set ZEP_API_KEY
python -m ops keys status           # where each key was found; never the value
```

Keys are never written to `mirofish/.env`. `start` passes the Zep key to the MiroFish backend only through its process environment, and MiroFish talks to Gemini only through the local relay, using a session token that changes on every `start`. Logs and error messages pass through redaction, and a unit test fails if any tracked file contains a key-shaped string or a private file (the pre-commit hook and CI check the same with gitleaks).

### Configuration files

Copy each template to its git-ignored name and fill it in:

| Template | Copy to | What to fill in |
|---|---|---|
| `ops/config/llm.example.yaml` | `ops/config/llm.yaml` | A Gemini model and reasoning effort for each stage group: setup (ontology, personas, config), simulation (every agent turn), report. Check Google's current [model list](https://ai.google.dev/gemini-api/docs/models). |
| `ops/config/prices.example.yaml` | `ops/config/prices.yaml` | Input and output price per million tokens for each model you use, from Google's current [pricing page](https://ai.google.dev/gemini-api/docs/pricing), with `source_url` and `checked_on`. |
| `ops/config/limits.example.yaml` | `ops/config/limits.yaml` | Spend, call, time, agent, round and run limits (see [Spend controls](#spend-controls)). |
| `ops/config/sensitive_terms.example.txt` | `ops/config/sensitive_terms.txt` | Your real sensitive terms, one per line: people, companies, counterparties, project names, internal places. The leak gate and the pre-commit hook block them. |
| `ops/config/zep_limits.example.yaml` | `ops/config/zep_limits.yaml` (optional) | Your Zep plan's quotas, to have `plan` warn before you exceed them. |

Then check everything with real but synthetic calls:

```bash
python -m ops doctor
```

doctor verifies the key, each configured model, JSON mode, tool calling, latency and token-usage reporting, and that Zep accepts its key. It warns if Gemini rate-limits a handful of tiny requests, which suggests a low-quota (possibly unpaid) key.

Finally, read the report and confirm the checklist:

```bash
python -m ops privacy_report
python -m ops privacy_report --confirm
```

## A run, step by step

### 1. Write the seed, the questions and a terms file

The **seed** is a Markdown file of facts, one per line, each tagged:

```markdown
- VERIFIED: The council published Proposal 14 on 3 March.
- ESTIMATE: The finance office estimates savings of 4.2 million dollars over five years.
- BELIEF: Many residents believe the closure will lengthen night journeys.
- UNKNOWN: Whether the union will call a strike if Proposal 14 passes.
```

UNKNOWN facts become variants: each run assumes one resolution of each UNKNOWN, so the runs can be compared. Lines that address the model rather than describe the world ("ignore previous instructions", "you are now ...") are removed by a lint step and reported.

The **questions** file has one section per question; the text under each heading becomes MiroFish's simulation requirement:

```markdown
## Q1: Reaction before the vote
How will residents, the union, local businesses and officials react on social media in the four
weeks before the vote, and which groups are most likely to resist?
```

The **terms file** for pseudonymisation lists each entity with its category and aliases. It is separate from `sensitive_terms.txt`, which only blocks uploads:

```text
person: Elena Duarte | Duarte
org:    Riverton Transit Workers Union | the union's leadership
place:  Riverton
```

`python -m ops pseudonymize --suggest seed.md` lists capitalised phrases you may have missed. A complete fictional example is in `ops/tests/fixtures/`.

### 2. Prepare a batch (no network)

```bash
python -m ops prepare --batch b1 --seed path/to/seed.md --questions path/to/questions.md --terms path/to/terms.txt
```

prepare lints the seed, replaces every listed name with a consistent pseudonym, turns money amounts into ranges ("4.2 million dollars" becomes "between 2 million dollars and 5 million dollars"), scales other large counts by a secret factor, and runs the leak gate on the result before writing anything. Read `ops/runs/b1/inputs/seed.md`: that is exactly what will be uploaded. The mapping that reverses the pseudonyms is written to `ops/mappings/b1.mapping.json` (owner-only).

### 3. Start the stack (no network)

```bash
python -m ops start
```

This starts, on 127.0.0.1 only: the metering relay (port 5011), the MiroFish backend (5001), the web UI (http://127.0.0.1:3000), and a socket sampler for netcheck. `start --foreground` stays attached until Ctrl-C; otherwise stop it later with `python -m ops stop`.

### 4. Variants, plan, pilot

```bash
python -m ops variants propose --batch b1   # if the seed has UNKNOWNs: Gemini proposes 3-5 resolutions each
python -m ops variants sample --batch b1 --n 3
python -m ops plan --batch b1               # low and high estimates of spend, calls and time
python -m ops pilot --batch b1              # 10 rounds, at most 12 agents, then it stops for review
```

Review and edit `ops/runs/b1/alternatives.json` before sampling if you like. The pilot measures real token use per stage and replaces the estimates in the plan; `run` refuses to start if the pilot exceeded the failure, refusal or malformed-output gates. If the graph would create more agents than the cap, the pilot stops and asks you to choose entity types (`--entity-types Person,Organization`). Every run builds its own ontology, so type names can differ between runs; a run whose graph lacks one of the chosen types stops before generating personas and lists the types it has (`--entity-types ""` clears the filter).

### 5. The real runs

```bash
python -m ops run --batch b1                # resumable; rerun the same command after any stop
python -m ops status --batch b1             # spend, calls, time and run states
```

### 6. Check and compare the results

```bash
python -m ops netcheck                      # proof that only the two allowed hosts were contacted
python -m ops factguard --batch b1          # flags report claims the seed does not support
python -m ops compare --batch b1            # classifies each run's discussion against ops/config/rubric.yaml
```

factguard and compare read the pseudonymised outputs and write de-pseudonymised copies next to them (`report.depseudo.md`, `factguard.md`, `compare.md`), all owner-only. Money amounts that are not the seed's own are listed, so you can tell the model's figures from yours. `factguard --no-llm` sends nothing and lists the sentences that touch an UNKNOWN for you to review. To restore names in any other MiroFish output: `python -m ops depseudonymize --mapping ops/mappings/b1.mapping.json FILE`.

### 7. Clean up

```bash
python -m ops stop
python -m ops purge --batch b1
```

## Command reference

| Command | Network | What it does |
|---|---|---|
| `bootstrap` | GitHub, PyPI, npm, Hugging Face, tokenizer files | Set up this machine (see [Install](#install)). Sends none of your data. |
| `doctor` | Gemini, Zep | Synthetic checks of keys, models and features. |
| `privacy_report [--confirm]` | none | What each provider receives; provider links; the checklist. |
| `keys set NAME` / `keys status` | none | Store a key in the OS keychain; show where keys are found. |
| `prepare --batch --seed --questions --terms [--keep-flagged]` | none | Lint, pseudonymise, leak-gate and store a batch's inputs. |
| `pseudonymize` / `depseudonymize` | none | The pseudonymiser on its own (`--help` for options). |
| `start [--no-frontend] [--foreground]` / `stop` | none | Start or stop the relay, MiroFish and the socket sampler. |
| `variants propose --batch [--redo]` | Gemini | Alternatives for each UNKNOWN fact. |
| `variants sample --batch --n [--rng-seed]` | none | Write N variant seeds (leak-gated). |
| `plan --batch` / `status --batch` | none | Estimates; progress and spend. |
| `pilot --batch [--entity-types]` | Gemini, Zep | Measured pilot run. |
| `run --batch [--entity-types] [--replan]` | Gemini, Zep | The planned runs. |
| `netcheck [--no-simulation]` | none | Evaluate the network evidence of the last `start`. |
| `factguard (--batch \| --run) [--include-pilot] [--no-llm]` | Gemini (optional) | Unsupported numbers, dates, entities and UNKNOWN claims. |
| `compare --batch [--rubric] [--include-pilot] [--passes]` | Gemini | Trajectories and resistors across runs. |
| `purge --batch [--keep-local] [--logs]` | Zep | Delete Zep data and verify it is gone, then shred local files. |

## Spend controls

Every LLM call MiroFish makes goes through the local relay, which belongs to exactly one confirmed run and stage at a time and refuses calls otherwise (including calls from the web UI outside a run). The relay:

- routes each stage to the model and reasoning effort set in `llm.yaml`;
- reserves the worst-case price of each call before sending it and records its actual token use and cost in the batch ledger (no prompt or answer content is logged);
- warns at 90% of `caps.max_spend` and stops the run cleanly at 100%: MiroFish receives a refusal, the wrapper stops the simulation, and the stage can be resumed after you raise the limit;
- counts calls against `caps.max_llm_calls`, and active time against `caps.wall_clock_minutes`;
- admits a stage only if its estimate still fits the remaining budget.

Budgets belong to a batch and persist across resumes; nothing resets silently. Costs are computed from `prices.yaml`, which you keep current; they are estimates of what Google will bill, not a copy of your invoice. Zep usage is estimated, not metered.

## Network evidence (netcheck)

While the stack runs, two independent sources are recorded under `ops/logs/`:

- an **audit hook** inside the relay, the backend and every simulation process logs each DNS lookup and socket connection, with process and thread;
- a **socket sampler** (`psutil`) records every internet socket of every stack process and its children, including sockets opened by native code or by Node.

`netcheck` passes only if the relay reached nothing but `generativelanguage.googleapis.com`, MiroFish reached nothing but `api.getzep.com`, the web UI reached nothing at all, every listener was on loopback, and every sampled socket is explained by an allowed lookup. Missing evidence (no audit lines, no sampler heartbeat) is a failure, not a pass. The allowlist is `ops/config/allowed_hosts.txt`. netcheck sees the stack's processes, not your browser.

## Deleting data (purge)

`python -m ops purge --batch b1` finds every Zep graph and upload batch the batch created (recorded when created, and recovered from run manifests and MiroFish's project files), deletes them after you type `purge b1`, and verifies each graph is gone. Only when Zep is verified clean does it overwrite and delete the local files: MiroFish's project, simulation and report folders for those runs, `ops/runs/b1*`, and the mapping. `--logs` also shreds all logs. If anything remains in Zep, local files are kept (they hold the ids) and you can run purge again.

Two limits: Zep may keep data it has already processed according to its own retention terms (purge reports such cases instead of claiming deletion), and on SSDs and copy-on-write file systems overwriting cannot guarantee old blocks are erased; full-disk encryption (FileVault, BitLocker, LUKS) is what protects deleted local data.

## Docker (untested)

`ops/docker/` runs the same stack in one container, with ports published on the host's 127.0.0.1 only and keys passed by name from your shell. **It was written without a Docker engine available and has not been run.** From the repository root:

```bash
mkdir -p docker-data/inputs docker-data/state docker-data/runs docker-data/logs docker-data/mappings docker-data/uploads
export GEMINI_API_KEY=... ZEP_API_KEY=...
docker compose -f ops/docker/compose.yml up -d --build
docker compose -f ops/docker/compose.yml exec mirofish .venv/bin/python -m ops privacy_report --confirm
```

Put your seed, questions and terms in `docker-data/inputs/` and refer to them as `/app/inputs/...`. Configuration is mounted read-only from `ops/config/`. Anyone who can run `docker inspect` on your machine can read the keys while the container exists. `docker-data/` is git-ignored but is subject to the same cloud-sync warning, which the container cannot detect.

## Development

```bash
.venv/bin/python -W ignore::SyntaxWarning -m unittest discover -s ops/tests -t .
```

The tests use local fakes for Gemini, Zep and MiroFish and never touch the network. CI (`.github/workflows/`) runs them on Ubuntu, macOS and Windows with Python 3.11 and 3.12, and scans the full history for secrets. Before a release, work through `ops/SMOKE_TEST.md` by hand.

The pre-commit hook blocks commits that contain key-shaped strings, private files or any term from your local `sensitive_terms.txt`. To change MiroFish, add a patch to `ops/patches/` and record it in `ops/UPSTREAM.md`; never edit `mirofish/` in place.

## Limitations

- MiroFish's internals, its social-media model and its single-model-per-stage design are unchanged; every simulated agent uses the same simulation model.
- The relay does not support streaming responses.
- Pseudonymisation replaces only what your terms file lists, and the leak gate catches only your listed terms and the built-in patterns. Neither hides the substance of the scenario.
- Detection of cloud-synced folders is best effort.
- A handful of runs per question cannot support probabilities; `compare` says so in its output.

## Documents in this repository

| File | Contents |
|---|---|
| `NOTICE.md` | Upstream's license and what it means for the patches |
| `PROVENANCE.md` | What was copied from the local-model version of this wrapper, from which commit, and what changed |
| `ops/UPSTREAM.md` | The pinned upstream commit and the patches |
| `ops/CLOUD_PLAN.md` | Design notes and the decisions behind them |
| `ops/SMOKE_TEST.md` | Manual end-to-end checklist |

## License

Upstream MiroFish is AGPL-3.0, and `ops/patches/` is derived from it; see `NOTICE.md`.

**TODO:** the license for the wrapper code has not been chosen. Until the owner chooses one, no license is granted; there is deliberately no `LICENSE` file.
