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
  - *Checked in Phase 4:* upstream saves `graph_id` and `zep_batch_id` in its project file before creating them, and the run pipeline copies both into the manifest and `ops/state/batches/<batch>/zep_resources.json` while the graph builds. The SDK's `batch.delete` "deletes a draft or invalid unprocessed batch. Processed batches cannot be deleted" (zep-cloud 3.25.0 docstring). So after purge the record of each processed upload batch remains in Zep; purge reports it by id and points to Zep's retention and deletion terms rather than claiming it is gone.

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

**Reviewed 2026-11-14: all recommendations D1–D8 accepted.**

**D1 revised 2026-11-15: per-stage routing in the relay.**
- `ops/config/llm.yaml` maps three stage groups to a model and a `reasoning_effort`:
  - `setup`: ontology, personas, simulation config;
  - `simulation`: agent turns;
  - `report`: report agent and interviews.
- `run` tells the relay which stage is active. The relay rewrites each request's `model` and adds `reasoning_effort` (upstream never sends it). MiroFish code is unchanged, and all agents inside the simulation still share one model.
- Defaults: every group uses the same model at the provider's default effort until you fill in the table.
- Graph extraction is done by Zep's own models, so no Gemini setting affects it.
- `doctor` tests every configured model. `plan` and the meter price each stage with its own model.

**Key handling that follows from the relay:** MiroFish sends its requests to the relay with a random per-session token in place of a key. The relay checks that token and swaps in the real Gemini key, so the Gemini key is never written to `mirofish/.env`. The token also stops other local programs from spending through the relay. The Zep key still has to be in `mirofish/.env`, because upstream's Zep SDK connects directly.

## 9. Phase 3 design: spend metering, kill switch, plan and pilot (for review)

Nothing in this section is built yet. Facts marked *checked* were read from the pinned upstream commit or the library versions it installs.

### 9.1 The pieces

- **Relay** (`ops/relay.py`): the only LLM endpoint MiroFish knows. It listens on 127.0.0.1, meters, routes and forwards to Gemini using `ops/provider.py` (Phase 2).
- **Ledger:** `ops/state/batches/<batch>/ledger.jsonl` (one line per call) and `budget.json` (running totals). Both are owner-only and git-ignored, and they never contain prompt or response text.
- **Commands:** `plan`, `pilot` and `run`. They drive MiroFish through its REST API, the same way the local repo's `run_batch.py` does.

### 9.2 What the relay does with each request, in order

1. **Checks the session token.** If it is wrong, the relay answers 401.
2. **Refuses streaming requests**, because token usage cannot be metered reliably on a stream. *Checked:* upstream's backend and simulation scripts never set `stream=True`.
3. **Leak gate 2 (D4).** The request text is scanned against `sensitive_terms.txt`. On a match, the call is refused and the run is stopped. The ledger records which term entries matched (by line number), never the text.
4. **Checks there is an active run.** Calls arriving with no active run (for example, from clicking in MiroFish's web UI) are refused with 409. Every paid call then belongs to a run you confirmed (C3).
5. **Budget admission** (§9.4).
6. **Routes by stage:** substitutes the stage group's model and `reasoning_effort` (D1 revised).
7. **Forwards** with retries and the concurrency and requests-per-minute caps (Phase 2 code).
8. **Meters** tokens and price (§9.3) and appends a ledger line.
9. **Checks thresholds:** a warning at 90%, the kill switch at 100%.
10. **Returns Gemini's response unchanged.** When the relay's own retries are used up, or it refuses a call, it adds `x-should-retry: false`. *Checked:* the OpenAI SDK upstream installs (1.109.1) honours that header. Without it, the SDK retries 408, 409, 429 and 5xx up to 2 more times on top of the relay's retries.

Each ledger line holds: time, batch, run, stage, stage group, model, effort, HTTP status, outcome (ok, refusal, empty or error), attempts, latency, token counts (prompt, completion, reasoning and total), cost, whether JSON mode or tools were requested, whether the JSON parsed, and whether tool arguments parsed.

### 9.3 Pricing

- **Price file.** `prices.yaml` lists each model's `input_per_million` and `output_per_million`. It may also give an optional long-context tier (`threshold_prompt_tokens` with its own two prices). It must also give `currency`, `source_url` and `checked_on`. `plan`, `pilot` and `run` refuse to start if any configured model has no price.
- **Billable output** is `total_tokens − prompt_tokens` when the response reports a total, and `completion_tokens + reasoning_tokens` otherwise. This counts thinking tokens whether or not Gemini includes them in `completion_tokens`. I am not asserting how Google bills thinking: the pilot report shows the thinking share so you can check it against the pricing page.
- **Cached-input discounts are ignored**, so estimates err high.
- **A response with no usage block** is priced at a worst case (prompt characters ÷ 3 as input tokens, plus `max_tokens` or 8,192 output tokens) and flagged. `doctor` already fails if usage is missing.

### 9.4 Limits and the kill switch

The budget belongs to the **batch** and persists across resumes. Raising a limit in `limits.yaml` lets a stopped batch continue (C1).

| Limit | Counted by | At 90% | At 100% |
|---|---|---|---|
| `max_spend` | Relay, from priced tokens | Warning printed and logged | Kill switch |
| `max_llm_calls` | Relay; requests from MiroFish (retry attempts are recorded separately) | Warning | Kill switch |
| `wall_clock_minutes` | `run`; active time only (time while stopped does not count) | Warning | Kill switch |
| `max_rounds` (≤ 40) | Config validation and `plan`; passed to upstream as `max_rounds` | n/a | Never exceeded, so no runtime trip |

**Admission before every call.** The relay refuses a call if this sum would pass `max_spend`:
- the spend so far;
- the reservations of calls already in flight;
- this call's reservation (prompt characters ÷ 3 × input price, plus `max_tokens` or `provider.reserve_output_tokens` (default 4,096) × output price).

The worst overshoot is therefore one reservation per concurrent call (`max_concurrency`, default 4).

**Admission before every stage.** Before starting a stage (most importantly rounds and report), `run` requires the remaining budget to cover that stage's **high** estimate. Otherwise it stops at the stage boundary, wasting nothing, and says how much more budget the stage needs. This keeps mid-stage trips rare.

**What the kill switch does:**
1. The relay answers every further call with HTTP 402 `budget_exhausted` (with `x-should-retry: false`).
2. `run` notices within one poll interval.
3. If the rounds stage is running, it calls upstream's `/api/simulation/stop`.
4. It marks the stage `interrupted` with the reason, saves the run manifest and batch state, and exits with a message saying how to resume.

**What "resumable" means, honestly:**
- Completed stages are never repeated.
- An interrupted stage starts again from its beginning. For the rounds stage, that means round 0. *Checked:* upstream cannot continue a stopped simulation. Its only restart is `start` with `force`, which discards the attempt; the only `start_round` parameter is on a read-only timeline endpoint.
- The spend of the discarded attempt is recorded in the manifest as wasted.

### 9.5 Stage signalling

- Before each stage, `run` tells the relay the batch, run and stage through a control endpoint. The control endpoint uses a second random token, kept in an owner-only state file.
- The relay attributes ledger lines to that stage and routes the request by the stage's group.
- As in the local repo, only one batch runs at a time (a lock file).

### 9.6 `plan`: low and high estimates

**Before a pilot** the estimates come from `limits.yaml` `estimates` (calls per stage, plus tokens per call at a low and a high value). They are labelled "unmeasured".

**After a pilot**, each stage group (priced with its own model) gets two estimates:
- **Low:** the pilot's measured rates (calls and tokens per stage, per round and per agent turn), scaled to the planned rounds and agent count. Expected agent turns come from upstream's activity schedule, as in the local repo.
- **High:** the low estimate with three adjustments:
  - the pilot's measured growth in prompt tokens per call across rounds, extrapolated to the planned round count;
  - agents at `caps.max_agents` instead of the pilot's count;
  - the result multiplied by `estimates.high_factor` (default 1.5) (C4).

The **high** estimate is checked against `max_spend`, `max_llm_calls` and `wall_clock_minutes`. Cuts are made in this order:
1. Rounds per run, down to `rounds.real_min`.
2. Runs per question, down to 1.

Every cut is printed with its reason. If even one minimum run per question does not fit, `plan` stops and says by how much. The output shows a per-stage breakdown (model, calls, tokens, low and high spend) and the Zep usage estimate (Phase 4).

### 9.7 The pilot

**Shape:**
- One run: the first question, variant v0.
- `rounds.pilot` rounds (10).
- At most `pilot.max_agents` agents (12).
- Its own spend ceiling, `pilot.max_spend`. This is counted inside the batch budget (C6).

**Agent cap, enforced before any persona is generated:**
- After the graph is built, `pilot` counts entities by type with `GET /api/simulation/entities/<graph_id>?enrich=false`. *Checked:* this endpoint reads from Zep and makes no LLM call.
- If there are more than 12, it stops and lists the counts per type. You then choose entity types (`--entity-types`), which upstream's `prepare` accepts as a filter.
- A single type with more than 12 entities cannot be capped without trimming the seed, and the message says so (C5).
- Real runs get the same check against `caps.max_agents`. The local repo only warned, and only after the money was spent.

**Before it starts:**
- It prints the typed confirmation (D5): hosts, data classes, and the pilot's low and high spend.
- Then it starts the relay stage-by-stage as above.

**Pilot report:**
- **Per stage group and model:**
  - calls and tokens (input, output, thinking);
  - spend;
  - latency (median and 95th percentile);
  - retries, 429s and 5xx responses;
  - refusals, empty answers and failed calls, with their rates;
  - JSON failures, missing tool calls and truncated answers.
- **Overall:**
  - agents and rounds completed;
  - the Zep estimate;
  - the path to `report.md`, so you can judge report quality (the original D1 question);
  - the real-run plan with low and high estimates and any cuts.

**Pilot gates (C6).** `run` refuses to start if any of these hold:
- failure rate above `pilot_gates.max_failure_rate` (proposed 5%);
- refusal rate above `max_refusal_rate` (proposed 2%);
- structured-output failure rate above `max_structured_failure_rate` (proposed 10%);
- fewer rounds completed than planned.

These are settings, not safety rails: you can change them in `limits.yaml`. The leak gate stays unbypassable.

`run` then asks for its own typed confirmation of the plan.

### 9.8 Added to each run's manifest

- Spend per stage and in total.
- Tokens and calls.
- The model and effort used for each stage group.
- The `prices.yaml` hash and its `checked_on` date.
- Wasted spend from interrupted attempts.
- Any 90% warnings and kill-switch trips, with reasons.

### 9.9 Tests (fakes only)

These use the fake provider from Phase 2 plus a fake MiroFish API. They cover:
- **Relay:** wrong token, streaming refused, leak gate, no active run, admission with calls in flight, the 90% warning, the 100% trip, `x-should-retry` on refusals, routing per stage, no content in the ledger, pricing from total versus completion tokens, and worst-case pricing without usage.
- **`plan`:** cut order and the infeasible case.
- **`pilot`:** the agent-cap check against a fake entities endpoint.
- **`run`:** a trip calls stop and marks the stage interrupted; resume restarts only that stage; the wall-clock trip.

### 9.10 Decisions for your review

| # | Decision | Recommendation |
|---|---|---|
| C1 | Budget scope | **Per batch, persisted across resumes.** Raising a limit continues the batch; it never silently resets. |
| C2 | Hitting 100% mid-stage | **402 from the relay, then a clean stop.** Mid-rounds trips restart the rounds stage on resume (an upstream limit), so stage admission checks are used to make that rare. |
| C3 | LLM calls with no active run (for example from the web UI) | **Refused.** Every paid call belongs to a confirmed run. |
| C4 | How "high" is computed | **Measured prompt growth, agents at the cap, × 1.5** (`estimates.high_factor`). |
| C5 | Pilot finds more than 12 agents | **Stop and ask you to choose entity types**, rather than the wrapper picking which parts of your scenario to drop. |
| C6 | Pilot spend ceiling and gates | **`pilot.max_spend` you set** (template value 1.00 in your currency). Gates at 5% failures, 2% refusals, 10% structured failures. |

**Reviewed 2026-11-15: C1–C6 accepted as recommended.**
