# Upstream MiroFish pin

| | |
|---|---|
| Repository | https://github.com/666ghj/MiroFish.git |
| Commit | `7657031ac01184afe2cb220f5ee3545573b5e843` |
| Commit date | 2026-10-02T02:20:36+08:00 ("chore: update Star History (#832)") |
| License | AGPL-3.0 (see `NOTICE.md`) |
| Checked | 2026-11-14: upstream `main` was at this exact commit (0 commits ahead) |

This is the same commit the local-profile repo pins and studied (see `PROVENANCE.md`), so its notes on upstream behaviour apply unchanged.

## Rules

- Upstream is cloned into `mirofish/` (git-ignored) by `python -m ops bootstrap`, which checks out this commit and refuses any other. It is never committed to this repository.
- Upstream files are never edited in place. Every change is a patch in `ops/patches/`, applied by bootstrap and listed below.
- Dependencies install only from upstream's lockfiles (`uv sync --frozen` for the backend, `npm ci --ignore-scripts` for the frontend). Upstream's own setup scripts are not run.

## Reproduce by hand

```bash
git clone https://github.com/666ghj/MiroFish.git mirofish
git -C mirofish checkout --detach 7657031ac01184afe2cb220f5ee3545573b5e843
git -C mirofish rev-parse HEAD    # must print the commit above
```

## Patches

Applied in file-name order by `python -m ops bootstrap` (idempotent; a patch that neither applies nor reverses cleanly stops bootstrap before anything else changes). `start` refuses to run unless every patch is applied.

| Patch | sha256 | What it changes |
|---|---|---|
| `0001-server-hardening.patch` | `d6c9afa78f486f74e53ef0e1e546a4a95e188d35e9cccd7507a25f2fa6a4e7b3` | `backend/app/__init__.py`: CORS and Host-header allowlists (`MIROFISH_CORS_ORIGINS`, `MIROFISH_ALLOWED_HOSTS`), request bodies not logged unless `MIROFISH_LOG_REQUEST_BODIES=true`. `frontend/index.html`: Google Fonts links removed, so the browser loads nothing from Google. `backend/tests/test_local_hardening.py`: tests for the above. Copied from the local repo's `0002-local-hardening.patch` (see `PROVENANCE.md`). |
