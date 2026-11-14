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

## Deliberately not copied

Local-only parts: Ollama setup (`ollama_serve.sh`, `build_model.sh`, `.tools/ollama`), the local memory backend (`patches/0001-local-memory-backend.patch`), local model configuration (`config/model.conf`, `config/Modelfile.template`), and the local LLM doctor checks that depend on them.

## Written from scratch here

`ops/tools/secret_scan.py`, `ops/tools/install_gitleaks.py`, `.githooks/pre-commit` (Phase 0).

`ops/provider.py`, `ops/stages.py`, `ops/doctor.py`, `ops/cli.py`, `ops/config.py`, `ops/keys.py`, `ops/redact.py`, `ops/paths.py` and the tests in `ops/tests/` (Phase 2).

`ops/limits.py`, `ops/pricing.py`, `ops/leakgate.py`, `ops/budget.py`, `ops/confirm.py`, `ops/config/limits.example.yaml`, `ops/config/prices.example.yaml` and the Phase 3 tests in `ops/tests/` (Phase 3).
