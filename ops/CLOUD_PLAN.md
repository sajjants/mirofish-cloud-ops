# Cloud profile plan (Phase 1)

Upstream MiroFish at `7657031` (see `UPSTREAM.md`), run with Google Gemini as the LLM and Zep Cloud as memory. Sources: the local repo's `ops/NOTES.md`, `ops/ZEP_DECISION.md` and `ops/UPSTREAM.md` (commit in `PROVENANCE.md`), and the pinned upstream source, read from the committed tree (unpatched). File references are to upstream paths.

Nothing about Gemini's or Zep's current APIs, prices, quotas or data terms is asserted here. Those are verified from the providers' documentation in later phases.

## 1. Which settings select the LLM and the memory backend

All are read from `mirofish/.env`, loaded with `override=True` (`backend/app/config.py`), so `.env` wins over the process environment.

| Setting | Effect in unmodified upstream |
|---|---|
| `LLM_API_KEY` | Required; startup fails if empty. Also copied to `OPENAI_API_KEY` for the simulation's CAMEL/OASIS agents. |
| `LLM_BASE_URL` | OpenAI-compatible endpoint for every LLM call. Code default `https://api.openai.com/v1`. Copied to `OPENAI_API_BASE_URL` for the simulation. |
| `LLM_MODEL_NAME` | The one model used by every stage (ontology, personas, simulation config, agents, report, interviews). Code default `gpt-4o-mini`. |
| `LLM_BOOST_API_KEY`, `_BASE_URL`, `_MODEL_NAME` | Optional second model; see §2. |
| `ZEP_API_KEY` | Required; startup fails if empty. Selects Zep Cloud, the only memory backend upstream has. |
| `ZEP_API_URL` | Must be unset; startup fails if set (`config.py` `validate`). The SDK client is hard-wired to `https://api.getzep.com/api/v2` (`backend/app/utils/zep.py`). |

So pointing upstream at Gemini needs only `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL_NAME`, provided Gemini's OpenAI-compatible endpoint supports what upstream uses (§6). Zep Cloud is upstream's default path and needs only `ZEP_API_KEY`.

## 2. More than one model?

**Partly, and not by stage.** The only second-model mechanism is `create_model(config, use_boost)` in `backend/scripts/run_parallel_simulation.py` (lines 984–1037):

- In a `parallel` run, the **Twitter** simulation uses `LLM_*` (line 1130) and the **Reddit** simulation uses `LLM_BOOST_*` if `LLM_BOOST_API_KEY` is set (line 1322). Its stated purpose is spreading load across two providers.
- Every other stage (ontology, personas, simulation config, report, interviews) builds its own client from `LLM_*` only (`llm_client.py`, `oasis_profile_generator.py`, `simulation_config_generator.py`, `report_agent.py`, `zep_tools.py`). There is no setting for a report-stage model.
- The boost path also rewrites the process-wide `OPENAI_API_KEY` and `OPENAI_API_BASE_URL` environment variables after the Twitter model is created. That is harmless only if CAMEL captures them at model creation, which is to be confirmed if boost is ever used.

Consequences for your plan ("fast tier for agent turns, stronger tier for the report if upstream allows it"):

- **Upstream does not allow it.** Boost splits by platform, not by role. Using it would put Twitter and Reddit agents on different models, which confounds comparisons between them.
- **A wrapper-level option exists.** The local metering relay (Phase 3) sees every LLM request. While `run` is in the report stage, it could replace the `model` field with the strong model name. No upstream change is needed, because MiroFish never sees the substitution. Decision D1 below.

## 3. How unmodified upstream calls Zep Cloud

- **SDK:** `zep-cloud==3.25.0` from upstream's lockfile. There is one cached client (`get_zep_client()` in `backend/app/utils/zep.py`), and every call site goes through it.
- **17 SDK methods across 6 modules:**
  - graph lifecycle: `graph.create`, `graph.get`, `graph.delete`, `graph.set_ontology`;
  - ingestion: `batch.create`, `batch.add`, `batch.process`, `batch.get`, `batch.list`, `batch.list_items`, `graph.add`, `graph.episode.get`;
  - reads: `graph.node.get`, `graph.node.get_edges`, and paged node/edge listing;
  - search: `graph.search` (scopes `edges` and `nodes`; rerankers `rrf` and `cross_encoder`).
- **Resources created:** one Zep **graph** per project (id `mirofish_<hex>`), plus the episodes and ingestion batches inside it. No Zep users, threads or memory sessions are used (searched `client.user|thread|memory|group`: none).
- **Data sent to Zep:**
  - every 500-character chunk of the uploaded seed text;
  - the ontology (type names and descriptions);
  - the project name;
  - search queries, which contain entity names and the report's questions;
  - if graph-memory update is on, every simulated post, comment, like and follow as text with agent names. Upstream's UI always turns it on (`Step3Simulation.vue:402`); the wrapper controls it per run (decision D3).
- **Work Zep does with its own models:** entity and relationship extraction constrained by the ontology, deduplication, node summaries, temporal facts, and hybrid search with reranking. So **Zep does its own embedding** and MiroFish never calls an embedding endpoint. No Gemini embeddings are needed (Phase 2 item 3). How Zep processes the data internally, and which subprocessors it uses, is governed by Zep's terms; `privacy_report` will link them, not summarise them from memory.
- **Purge implications:** the wrapper must record `graph_id` (and batch ids, if Zep keeps them separately) at creation. Purge calls `graph.delete`, then verifies with `graph.get` (expecting NotFound) and an empty search. Whether batches outlive their graph is checked in Phase 4.

## 4. Patch 0002 (server hardening, Google Fonts)

- **It still applies.** `git apply --check` succeeds on a clean export of `7657031` without patch 0001. It touches `backend/app/__init__.py`, `frontend/index.html` and one new backend test.
- **It is needed, more than in the local profile.**
  - **CORS and Host allowlists** (`MIROFISH_CORS_ORIGINS`, `MIROFISH_ALLOWED_HOSTS`). Upstream allows CORS from any origin. Any web page open in your browser could then call `127.0.0.1:5001`. In this profile such a call spends money on your Gemini key and can read graph content back. DNS rebinding is the same risk. The patch restricts the API to the wrapper's own frontend origin and Host names.
  - **Request-body logging off** (`MIROFISH_LOG_REQUEST_BODIES=false`). Upstream logs JSON request bodies at debug level into `backend/logs/`, which leaves seed-derived text in rotating log files that purge would otherwise have to find.
  - **Google Fonts removal.** No longer a privacy issue here, since Google already receives the content. It is kept so the browser does not contact two extra hosts, which keeps `netcheck` and the allowed-hosts list to the two providers.
- **Plan:** copy it unchanged as `ops/patches/0001-server-hardening.patch`, renumbered because the local memory patch is excluded. Provenance is recorded.

## 5. What the cloud profile needs that the local one did not

| Need | Why | Where |
|---|---|---|
| **Mandatory leak gate** | Seed content now leaves the machine | A gate module called unconditionally by every upload path (seed, requirement text, variants, and any text the wrapper's own tools send to the LLM). No flag disables it. It names matched terms and rule types, never the text. |
| **Second gate in the relay** (proposed, D4) | Catches anything sent to the LLM that bypassed the first gate | The relay scans each outbound request body in memory for sensitive terms (not regexes, which would trip on agent chatter) and rejects the request. |
| **Money bucketing** | Your rule for the cloud-safe variant | `pseudonymize` maps money to ranges (e.g. "between 1 and 5 million") instead of the local version's secret scaling. `factguard` and `depseudonymize` must treat ranges, not exact figures, as the reference. |
| **Typed run confirmation** | Each invocation must list exactly which hosts receive which data classes | `run` and `pilot` print the table and require you to type a confirmation phrase. Proposed once per invocation, covering every run it will start (D5). |
| **`privacy_report` and checklist** | You read the providers' current terms yourself | Plain-language "who receives what", links to Google's and Zep's current terms pages, and a checklist you confirm (paid-tier key with billing; free-tier data-use terms checked; Zep retention and deletion terms checked). The confirmation is stored with a date and required before the first run. |
| **Spend metering and kill switch** | Real money | The relay counts input and output tokens from response `usage` fields, prices them from `prices.yaml`, warns at 90% of `max_spend`, and at 100% rejects further calls. The run then stops cleanly at a checkpoint and resumes after you raise the limit. |
| **Retry, backoff, rate limits** | Provider 429s, 5xx and quotas | Also in the relay, so upstream's code is unchanged: retries with exponential backoff and jitter, honouring `Retry-After`, plus concurrency and requests-per-minute caps. Safety refusals and empty responses are counted, retried once, then recorded as failed. The pilot reports the rates. |
| **Secrets** | API keys exist now | Read from the environment or the OS keychain (`keyring`). Written only to the generated, git-ignored, owner-only `mirofish/.env`. Redacted in all logs and errors. A test fails on any key-shaped string in tracked files or logs. |
| **Zep purge through the SDK** | Data lives in Zep, not only on disk | Resource ids recorded in the manifest at creation; delete, then verify. |
| **Zep usage estimate** | Quotas in `zep_limits.yaml` | Zep calls go directly from upstream's SDK to the hard-wired host, so the relay cannot see them. Usage is **estimated** (seed characters ÷ 500 for episodes, 2 searches per agent, report searches) and warned against the quotas. Exact metering would need a patch. |
| **Cross-platform CLI** | Windows, macOS, Linux | `python -m ops <command>` with `pathlib` and `psutil`, no bash, `ss` or `lsof`. **Caveat:** upstream stops simulations with `os.killpg` (`simulation_runner.py:954`), which does not exist on Windows. On Windows, MiroFish itself should run under WSL2 or the Docker route (D6). |
| **Netcheck with two allowed hosts** | Non-loopback traffic is now expected | Hostname-level evidence from a Python audit hook in the backend and simulation processes (carried over), plus `psutil` socket sampling of all MiroFish processes. IPs must belong to resolutions of the allowed hosts seen during the run. Exact attribution for Google's many front-end IPs is designed in Phase 5. |
| **One-time downloads kept off the allowlist** | Keep `allowed_hosts.txt` to Gemini and Zep | Pre-cache the Twitter recommender model and tiktoken files at bootstrap, then run offline. This needs the local repo's offline-tokenizer fix (`runtime/sitecustomize.py`) (D8). |
| **Graph-memory update off** | Less data sent to Zep, lower cost | The wrapper always sends `enable_graph_memory_update: false` (D3). |
| **Free-tier warning** | Free-tier keys may have different data-use terms and low quotas | `doctor` warns on signals such as quota errors in a short burst, or limits in headers if the provider sends them. A key's tier cannot be read reliably through the API, so the checklist confirmation is the real control. |

## 6. Gemini features upstream relies on (to verify in Phase 2)

From the code:
- chat completions;
- JSON mode (`response_format={"type": "json_object"}` in personas, simulation config and the JSON helper);
- tool or function calling (every OASIS agent turn is one tool-calling request through CAMEL 0.2.78);
- `max_tokens`;
- a `usage` block in responses (needed by the relay for spend).

CAMEL leaves unset parameters out of requests, so only `model`, `messages` and `tools` are sent by agents. Each of these is checked against Google's current documentation for its OpenAI-compatible endpoint, and exercised by `doctor` and the pilot. If JSON mode or tool calling is not supported, upstream cannot run on Gemini without changes, and I will stop and report.

## 7. Carried over from the local repo (planned copies)

To copy and adapt, with each copy recorded in `PROVENANCE.md`:
- **Inputs:** `seedfile.py`, `pseudonymize.py` (adding money bucketing), `depseudonymize.py`, `prepare_inputs.py`.
- **Planning and runs:** `unknown_variants.py` (gate before the LLM call), `plan.py` (spend-based, with low and high estimates), `run_batch.py`, `mirofish_api.py`, `sim_schedule.py`.
- **Checks:** `factguard.py` and `compare.py`. Their LLM calls must send pseudonymised text. Today's `factguard` de-pseudonymises before judging, which in this profile would send real names to Google.
- **Metering:** `llm_meter.py`, extended to relay to HTTPS with spend, kill switch and retries.
- **Shared code:** `local_llm.py` (becomes a provider client), `opsconfig.py`, the purge logic (plus Zep), the netcheck evaluation (plus the allowlist), and `runtime/sitecustomize.py`.
- **Config and docs:** `config/rubric.yaml`, and the relevant tests and fixtures, using fake providers only.

Not copied: Ollama scripts, the local memory patch, `model.conf`, `Modelfile.template`, the local LLM doctor, and the bash scripts (rewritten as Python CLI commands).

New dependencies for the wrapper (spec-mandated): `psutil` and `keyring`, plus `PyYAML` and `zep-cloud==3.25.0`, the same version upstream locks. They live in the wrapper's own virtualenv with a hash-pinned lockfile, separate from upstream's (D7).

## 8. Decisions for your review

| # | Decision | Recommendation |
|---|---|---|
| D1 | Report-stage model | Start with **one fast-tier model** (your stated fallback) and judge report quality in the pilot. Add relay-level report routing only if quality is poor; it is a wrapper feature and touches no upstream code. |
| D2 | Use upstream's `LLM_BOOST_*`? | **No.** It splits Twitter and Reddit across models, not stages. |
| D3 | Graph-memory update during simulation | **Off.** Less data to Zep and lower cost; matches how the local runs were made. |
| D4 | Second leak gate inside the relay, on every outbound LLM request (sensitive terms only) | **Yes.** It is cheap, and it also covers the wrapper's own LLM calls. |
| D5 | Typed confirmation granularity | **Once per `pilot` or `run` invocation**, listing every run it will start, the hosts and the data classes. |
| D6 | Windows | Wrapper CLI native on all three OSes; MiroFish under **WSL2 or Docker** on Windows. CI matrix tests the wrapper's paths and command construction on all three. |
| D7 | Wrapper dependencies | Own virtualenv with hash-pinned `psutil`, `keyring`, `PyYAML`, `zep-cloud==3.25.0`. |
| D8 | One-time downloads | **Pre-cache and run offline**, so `allowed_hosts.txt` lists only the Gemini and Zep hosts. |

Also pending from Phase 0: the CI workflow file (`.github/workflows/secret-scan.yml`) awaits your approval.
