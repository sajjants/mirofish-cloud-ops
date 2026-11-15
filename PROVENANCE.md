# Provenance

Shared code in this repository is copied from the local-profile repository, which is read-only for this work and was not modified.

| | |
|---|---|
| Source repository (local path) | `/Users/sajjant/Documents/Personal Project/Mirofish Implementation` |
| Source remote | https://github.com/sajjants/mirofish-implementation |
| Source commit | `e0154850dfb1bddde5e8dd5338a0866163852436` (working tree clean when read) |
| Upstream commit both repos pin | `7657031ac01184afe2cb220f5ee3545573b5e843` |

## Copied

Each phase that copies a file adds a row here: destination, source path, and what was changed for the cloud profile.

| Destination | Source | Changes |
|---|---|---|
| `ops/json_checks.py` (`clean_model_text`, the five `JSON_TESTS` prompts and their check functions, `SYSTEM_PROMPT`) | `ops/scripts/doctor_llm.py` | Prompts and checks unchanged. The Ollama request code was not copied; requests go through `ops/provider.py`. Added a tool-calling test (`create_post`) and its check, which the local doctor did not have. |
| `ops/schedule.py` | `ops/scripts/sim_schedule.py` | Functions copied unchanged (the port of upstream's agent-selection arithmetic, the clock shift). Added `scale_schedule` for estimating a larger agent count. Dropped the command-line `main`. |
| `ops/mirofish_api.py` | `ops/scripts/mirofish_api.py` | REST client, polling and the staged pipeline copied. Added `StopRun`, `RunHooks` (stage admission, kill-switch checks, agent cap before personas, clock shift), `entity_counts`, stopping the simulation on an interrupted rounds stage, attempt history per stage, `entity_types`. Graph-memory update is always off. Dropped `main`. |
| `ops/envfile.py` | `ops/scripts/envfile.py` | `unquote`, `parse`, `read_lines`, `set_values` copied. `sync` and `main` dropped; added `values`. Writes use `ops/paths.py`. |
| `ops/plan.py` (`run_turns`, `make_plan` structure, `totals`, `fits`, `over`, `format_plan`) | `ops/scripts/plan.py` | Rewritten for money: per-group models and prices, low/high estimates, prompt growth per round, spend already used in the batch. Limit validation moved to `ops/limits.py` (rewritten for the cloud limits). |
| `ops/batch.py` (structure of `Batch`, `BatchLock`, `export_transcript`, `format_pilot`, `format_status`) | `ops/scripts/run_batch.py` | Rewritten around the relay: per-run budget activation, stage admission, wall-clock limit, preflight against the leak gate and the relay settings, typed data-flow confirmation, spend per stage in the manifest, pilot gates. `BatchLock` uses `psutil` with process start time. |
| `ops/relay.py` (`analyse`) | `ops/scripts/llm_meter.py` | Same JSON and tool-call checks. The rest of the relay (authentication, leak gate, budget, stage routing, retries to Gemini) is new; no streaming support. |
| `ops/patches/0001-server-hardening.patch` | `ops/patches/0002-local-hardening.patch` | Byte-for-byte copy, renamed (sha256 `d6c9afa78f486f74e53ef0e1e546a4a95e188d35e9cccd7507a25f2fa6a4e7b3`). It applies on its own to the pinned commit; the local memory-backend patch it followed was not copied. (Phase 5) |
| `ops/runtime/sitecustomize.py` | `ops/runtime/sitecustomize.py` | Same audit hook. Each record now also carries the thread id and the process group (`MIROFISH_NETAUDIT_ROLE`), which netcheck needs to attribute connections per group; docstring updated. (Phase 5) |
| `ops/netcheck.py` (`LOOPBACK_NAMES`, `SIMULATION_PROCESS`, `is_loopback`, `read_jsonl`, `missing_evidence`, `evaluate_audit` structure) | `ops/scripts/netcheck.py` | Rewritten: per-group allowlists (`ops/config/allowed_hosts.txt`) instead of loopback only, IP connects attributed to the lookup in the same process and thread, a `psutil` sampler instead of `lsof`, relay and heartbeat evidence. (Phase 5) |
| `ops/local_purge.py` (`checked_child`, `shred_file`, `shred_tree`, `batch_run_ids`, the upstream-directory discovery in `upstream_dirs`) | `ops/scripts/purge.py` | Shredding copied with `getattr` for `O_NOFOLLOW`/`O_BINARY` (Windows). The local memory-store deletion was not copied (Zep Cloud deletion is in `ops/purge.py`). Path defaults are read at call time. (Phase 5) |
| `ops/seedfile.py` | `ops/scripts/seedfile.py` | Unchanged except one comment that pointed at the local repo's `INJECTION_TEST.md`. (Phase 5) |
| `ops/pseudonymize.py` | `ops/scripts/pseudonymize.py` | Money (a currency symbol or code, or dollars/euros/pounds) becomes a 1-2-5 range ("between $2 million and $5 million") instead of a scaled figure; other numbers are scaled as before. Mapping version 2 records each figure's kind; mappings go to `ops/mappings/`. Package imports. (Phase 5) |
| `ops/depseudonymize.py` | `ops/scripts/depseudonymize.py` | Reads mapping versions 1 and 2. A money range that several seed amounts share is left as a range; money amounts that are not the seed's are reported instead of rescaled. (Phase 5) |
| `ops/prepare_inputs.py` | `ops/scripts/prepare_inputs.py` | Pseudonymisation is mandatory (a terms file is required), the leak gate runs before anything is written, and it refuses to write into a cloud-synced folder. The local `submit` step was dropped: uploads only happen through `pilot`/`run`, after the typed confirmation. (Phase 5) |
| `ops/unknown_variants.py` | `ops/scripts/unknown_variants.py` | Proposals go to Gemini through the relay (`ops/wrapper_llm.py`) after the privacy checklist and a typed confirmation; variant seeds pass the leak gate before they are written. (Phase 5) |
| `ops/factguard.py` | `ops/scripts/factguard.py` | Checks run against the pseudonymised seed (what MiroFish saw); outputs are de-pseudonymised locally and list money amounts that are not the seed's. The optional UNKNOWN judge uses `ops/wrapper_llm.py`. (Phase 5) |
| `ops/compare.py` | `ops/scripts/compare.py` | The classifier uses `ops/wrapper_llm.py`; results are de-pseudonymised locally. (Phase 5) |
| `ops/config/rubric.yaml` | `ops/config/rubric.yaml` | Header comment updated to the new command name. (Phase 5) |
| `ops/tests/fixtures/synthetic_seed.md`, `synthetic_requirement.txt` | `ops/tests/fixtures/` | Unchanged (fictional test data). (Phase 5) |

## Deliberately not copied

Local-only parts: Ollama setup (`ollama_serve.sh`, `build_model.sh`, `.tools/ollama`), the local memory backend (`patches/0001-local-memory-backend.patch`), local model configuration (`config/model.conf`, `config/Modelfile.template`), and the local LLM doctor checks that depend on them.

## Written from scratch here

`ops/tools/secret_scan.py`, `ops/tools/install_gitleaks.py`, `.githooks/pre-commit` (Phase 0).

`ops/provider.py`, `ops/stages.py`, `ops/doctor.py`, `ops/cli.py`, `ops/config.py`, `ops/keys.py`, `ops/redact.py`, `ops/paths.py` and the tests in `ops/tests/` (Phase 2).

`ops/limits.py`, `ops/pricing.py`, `ops/leakgate.py`, `ops/budget.py`, `ops/confirm.py`, `ops/config/limits.example.yaml`, `ops/config/prices.example.yaml` and the Phase 3 tests in `ops/tests/` (Phase 3).

`ops/zep.py`, `ops/zep_usage.py`, `ops/purge.py` (Zep part; the local repo's `ops/scripts/purge.py` deleted from the local memory store, which this profile does not have), `ops/config/zep_limits.example.yaml`, `ops/tests/fake_zep.py`, `ops/tests/test_zep.py` (Phase 4).

`ops/bootstrap.py`, `ops/upstream.py`, `ops/stack.py`, `ops/privacy.py`, `ops/wrapper_llm.py`, `ops/config/allowed_hosts.txt`, `.env.example` (rewritten as the template for `mirofish/.env`), `ops/docker/`, `.dockerignore`, the CLI commands added to `ops/cli.py`, and `ops/tests/test_netcheck_stack.py`, `test_privacy_llm.py`, `test_tools_purge.py` (Phase 5). The local repo's bash scripts (`bootstrap.sh`, `start.sh`, `stop.sh`, `netcheck.sh`, `purge.sh`, `apply_patches.sh`, `cache_assets.sh`) were not copied; their jobs are done in Python here.

`ops/SMOKE_TEST.md`, `.gitattributes`, `ops/tests/fixtures/synthetic_terms.txt` and `synthetic_questions.md` (written for the fictional seed above) (Phase 6).
