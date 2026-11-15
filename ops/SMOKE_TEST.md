# Manual smoke test

Run this after bootstrap on a new machine, after changing the upstream pin or a patch, and before tagging a release. It uses only the fictional scenario in `ops/tests/fixtures/` and your real keys. One pilot is sent to Gemini and Zep Cloud, capped by `pilot.max_spend` in `ops/config/limits.yaml`; everything it creates is purged at the end.

Commands are written for macOS and Linux (`.venv/bin/python`). On Windows use `.venv\Scripts\python`. Tick each box only if the **Expect** line holds; if one fails, stop and keep the output (it contains no keys).

## 0. Before you start

- [ ] The repository is not in a folder that is synced or backed up to the cloud (Google Drive, iCloud Drive, Dropbox, OneDrive). **Expect:** `start` and `prepare` refuse to run if they detect one; detection is best effort, so check yourself too.
- [ ] `ops/config/llm.yaml` names a model for each stage, `ops/config/prices.yaml` has the current prices with `source_url` and `checked_on`, and `ops/config/limits.yaml` has caps you accept.
- [ ] `ops/config/sensitive_terms.txt` exists with your real terms. For this test, temporarily add the line `Riverton` (a fictional name the pseudonymiser replaces) at the end.

## 1. Setup and checks (sends nothing about your scenario)

- [ ] `python3 -m ops bootstrap` (or `py -3.12 -m ops bootstrap` on Windows). **Expect:** every step OK; upstream at the commit in `ops/UPSTREAM.md`; `APPLIED` for `0001-server-hardening.patch`; backend and wrapper tests pass; `mirofish/.env` exists and is owner-only (`ls -l` shows `-rw-------`).
- [ ] `.venv/bin/python -m ops keys set GEMINI_API_KEY` and `... keys set ZEP_API_KEY` (or export both variables), then `.venv/bin/python -m ops keys status`. **Expect:** both found, and no key value is printed anywhere.
- [ ] `.venv/bin/python -m ops doctor`. **Expect:** PASS for each configured model (JSON mode, tool calling) and for the Zep key; only synthetic prompts are sent.
- [ ] `.venv/bin/python -m ops privacy_report`. **Expect:** the hosts `generativelanguage.googleapis.com` and `api.getzep.com`, the provider links (open each one and read it), and "NOT confirmed".
- [ ] `.venv/bin/python -m ops privacy_report --confirm`. Type `yes` to each item only if it is true. **Expect:** "Recorded in ops/state/privacy_checklist.json".

## 2. Leak gate and pseudonymisation (no network)

- [ ] Add a second temporary line `Proposal 14` to `sensitive_terms.txt` (the pseudonymiser does not replace it), then run `.venv/bin/python -m ops prepare --batch smoke0 --seed ops/tests/fixtures/synthetic_seed.md --questions ops/tests/fixtures/synthetic_questions.md --terms ops/tests/fixtures/synthetic_terms.txt`. **Expect:** `LEAK GATE: nothing was uploaded`, the term `Proposal 14` named with line numbers but no surrounding text, and no `ops/runs/smoke0` folder. Remove the `Proposal 14` line again.
- [ ] Same command with `--batch smoke1`. **Expect:** "pseudonymised (money as ranges); leak gate passed"; lint findings for the injected instructions in the public-comment section.
- [ ] Open `ops/runs/smoke1/inputs/seed.md` (what will be uploaded). **Expect:** no Elena Duarte, Tomas Reyes, Riverton, Harbor Street or Northgate; the savings appear as "between 2 million dollars and 5 million dollars"; the injected instructions are gone.
- [ ] `.venv/bin/python -m ops variants sample --batch smoke1 --n 1`. **Expect:** FAIL asking for `variants propose` (the seed has two UNKNOWN facts, and a pilot needs variants).

## 3. Stack, variants, pilot and network evidence

- [ ] `.venv/bin/python -m ops start`. **Expect:** relay, backend and frontend listening on 127.0.0.1 only; "sends nothing to Gemini or Zep"; `mirofish/.env` contains no `ZEP_API_KEY`.
- [ ] Open http://127.0.0.1:3000. **Expect:** the MiroFish UI loads; in the browser's developer tools (Network tab) no request goes to any host other than 127.0.0.1.
- [ ] `.venv/bin/python -m ops variants propose --batch smoke1` (up to 6 calls to Gemini, after their own typed confirmation). **Expect:** 3 to 5 pseudonymised alternatives per UNKNOWN. Then `.venv/bin/python -m ops variants sample --batch smoke1 --n 1`. **Expect:** `v0: ...` and `ops/runs/smoke1/variants/v0.seed.md`.
- [ ] `.venv/bin/python -m ops plan --batch smoke1`. **Expect:** low and high estimates in your currency, inside your caps.
- [ ] `.venv/bin/python -m ops pilot --batch smoke1`, and at the confirmation type something other than the phrase. **Expect:** "nothing was sent"; `.venv/bin/python -m ops status --batch smoke1` shows zero calls and zero spend.
- [ ] Run the pilot again and type the phrase it shows. **Expect:** the data-flow list names only the two hosts; the pilot finishes or stops at a gate with a clear reason; spend stays under `pilot.max_spend`. If it stops because of too many agents, rerun with `--entity-types` as it suggests.
- [ ] `.venv/bin/python -m ops netcheck`. **Expect:** PASS; relay reached `generativelanguage.googleapis.com`, mirofish reached `api.getzep.com`, frontend reached nothing; no missing evidence.
- [ ] `.venv/bin/python -m ops factguard --batch smoke1 --include-pilot --no-llm`. **Expect:** `ops/runs/smoke1_pilot/report.depseudo.md` shows the real fictional names and "4.2 million dollars" where the report used the range; any money amount the model made up is listed as not the seed's.
- [ ] `.venv/bin/python -m ops stop`. **Expect:** every process stopped; `.venv/bin/python -m ops status --batch smoke1` still works.

## 4. Purge

- [ ] `.venv/bin/python -m ops purge --batch smoke1`, and type `purge smoke1`. **Expect:** each Zep graph "deleted" and verified gone (an upload batch Zep already processed may be listed as "remains", with a note pointing to Zep's terms); then the upstream project, simulation and report folders, `ops/runs/smoke1*` and `ops/mappings/smoke1.mapping.json` shredded; the disk-encryption note.
- [ ] Run the same purge again. **Expect:** "nothing left to delete in Zep Cloud or on this machine".
- [ ] Remove the temporary `Riverton` line from `sensitive_terms.txt`.
- [ ] `git status`. **Expect:** nothing new or changed (private files are git-ignored).

## 5. Docker route (optional; not yet tested by the maintainer)

- [ ] `mkdir -p docker-data/{inputs,state,runs,logs,mappings,uploads}`, export both keys in the shell, then `docker compose -f ops/docker/compose.yml up -d --build`. **Expect:** the build runs bootstrap without errors; `docker compose -f ops/docker/compose.yml ps` shows the service running; `docker port` lists only `127.0.0.1` bindings.
- [ ] Repeat sections 1 (from `privacy_report`) to 4 with `docker compose -f ops/docker/compose.yml exec mirofish .venv/bin/python -m ops ...`, copying the fixtures into `docker-data/inputs/` and using `/app/inputs/...` paths. **Expect:** the same results; netcheck reports the container's listeners as allowed (they are reachable only through the 127.0.0.1 port mappings).
- [ ] `docker compose -f ops/docker/compose.yml down`. Note that `docker inspect` showed your keys while the container existed.
