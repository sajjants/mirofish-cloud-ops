# Provenance

Shared code in this repository is copied from the local-profile repository, which is read-only for this work and was not modified.

| | |
|---|---|
| Source repository (local path) | `/Users/sajjant/Documents/Personal Project/Mirofish Implementation` |
| Source remote | https://github.com/sajjants/mirofish-implementation |
| Source commit | `e0154850dfb1bddde5e8dd5338a0866163852436` (working tree clean when read) |
| Upstream commit both repos pin | `7657031ac01184afe2cb220f5ee3545573b5e843` |

## Copied

Nothing yet. Each phase that copies a file adds a row here: destination, source path, and what was changed for the cloud profile.

| Destination | Source | Changes |
|---|---|---|

## Deliberately not copied

Local-only parts: Ollama setup (`ollama_serve.sh`, `build_model.sh`, `.tools/ollama`), the local memory backend (`patches/0001-local-memory-backend.patch`), local model configuration (`config/model.conf`, `config/Modelfile.template`), and the local LLM doctor checks that depend on them.

## Written from scratch here

`ops/tools/secret_scan.py`, `ops/tools/install_gitleaks.py`, `.githooks/pre-commit` (Phase 0).
