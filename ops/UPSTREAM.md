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

None yet. Planned (see `ops/CLOUD_PLAN.md` §4): the local repo's `0002-local-hardening.patch` (CORS and Host allowlists, optional request-body logging, Google Fonts removal), which applies cleanly to this commit on its own.
