"""purge: delete a batch's Zep Cloud graphs (and upload batches, where Zep allows) and verify they are
gone, then securely delete the batch's local files (ops/local_purge.py).

Zep ids come from the batch's resource record, its run manifests and upstream's project files, so the
local files are only removed after every Zep resource is verified gone; if anything remains, run
purge again. The outcome of every delete and check is stored in ops/state/batches/<batch>/
zep_resources.json and appended to purge.jsonl in the same folder (ids and statuses only).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, Optional, TextIO

from ops import local_purge, paths
from ops.batch import BatchError, BatchLock, batch_dir
from ops.budget import batch_state_dir
from ops.confirm import NotConfirmed, typed_phrase
from ops.keys import get_key
from ops.redact import redact
from ops.zep import ZEP_HOST, ZepError, ZepResources, discover, make_client, purge, purge_ok

ZEP_KEY = "ZEP_API_KEY"


def _stack_running() -> bool:
    from ops.stack import load_stack, running_roles
    return bool(running_roles(load_stack()))


def purge_command(batch_id: str, *, keep_local: bool = False, include_logs: bool = False,
                  client_factory: Callable[[str], Any] = make_client, ask: Optional[Callable[[str], str]] = None,
                  interactive: Optional[bool] = None, out: TextIO = sys.stdout,
                  stack_running: Callable[[], bool] = _stack_running) -> int:
    """0: everything verified gone; 1: something failed or could not be checked; 2: not confirmed."""
    report = None
    try:
        batch_dir(batch_id)
        if not keep_local and stack_running():
            print("purge: stop the stack first (`python -m ops stop`) so no files are in use, or pass "
                  "--keep-local to delete only the Zep data now", file=out)
            return 1
        with BatchLock():
            resources = ZepResources(batch_id)
            found = discover(batch_id, resources=resources)
            graphs = sorted(i for i, e in found["graphs"].items() if e.get("status") not in ("deleted", "gone"))
            batches = sorted(i for i, e in found["batches"].items() if e.get("status") not in ("deleted", "gone"))
            for kind, ids in (("graphs", graphs), ("batches", batches)):
                for resource_id in ids:
                    entry = found[kind][resource_id]
                    if entry.get("status") == "discovered":
                        resources.record(entry.get("run_id") or "unknown", entry.get("project_id"),
                                         [resource_id] if kind == "graphs" else [],
                                         [resource_id] if kind == "batches" else [])
            local = [] if keep_local else local_purge.plan(batch_id, include_logs)
            if not graphs and not batches and not local:
                print(f"purge {batch_id}: nothing left to delete in Zep Cloud or on this machine", file=out)
                return 0
            key = None
            if graphs or batches:
                key = get_key(ZEP_KEY, ZEP_KEY)
                if key is None:
                    print(f"purge: no Zep key; set {ZEP_KEY} or run `python -m ops keys set {ZEP_KEY}`", file=out)
                    return 1
            lines = [f"PURGE {batch_id}: this cannot be undone."]
            if graphs or batches:
                lines += [f"  Permanently delete from Zep Cloud ({ZEP_HOST}); only these ids and your Zep key are sent:",
                          *(f"    graph {g} (run {found['graphs'][g].get('run_id')})" for g in graphs),
                          *(f"    upload batch {b} (run {found['batches'][b].get('run_id')})" for b in batches)]
            if local:
                lines += ["  Then, once Zep is verified clean, overwrite and delete on this machine:",
                          *(f"    {path}" for path in local)]
            typed_phrase(lines, f"purge {batch_id}", "not confirmed; nothing was deleted", ask=ask, out=out,
                         interactive=interactive)
            if graphs or batches:
                report = purge(client_factory(key.value), graphs, batches, resources)
                paths.append_private_line(batch_state_dir(batch_id) / "purge.jsonl",
                                          json.dumps({"at": time.time(), "report": report}))
                for kind, label in (("graphs", "graph"), ("batches", "upload batch")):
                    for outcome in report[kind]:
                        detail = outcome.get("error") or outcome.get("action") or ""
                        print(f"  {label} {outcome['id']}: {outcome['status']}"
                              f"{' (' + detail + ')' if detail else ''}", file=out)
                        if outcome.get("note"):
                            print(f"    {outcome['note']}", file=out)
                if not purge_ok(report):
                    print(f"purge {batch_id}: Zep INCOMPLETE; local files were kept (they hold the ids). "
                          "Run purge again.", file=out)
                    return 1
                print(f"purge {batch_id}: Zep graphs deleted and verified gone", file=out)
            if local:
                warnings = local_purge.shred(local, log=lambda line: print(line, file=out))
                for warning in warnings:
                    print(f"  WARN: {warning}", file=out)
                paths.append_private_line(batch_state_dir(batch_id) / "purge.jsonl",
                                          json.dumps({"at": time.time(), "local": [str(p) for p in local],
                                                      "warnings": warnings}))
                print(f"purge {batch_id}: local files shredded", file=out)
                print(local_purge.disk_encryption_note(), file=out)
            elif keep_local:
                print("Local files were kept (--keep-local).", file=out)
    except NotConfirmed as error:
        print(f"purge: {error}", file=out)
        return 2
    except (BatchError, ZepError, ValueError, OSError) as error:
        print(f"purge: {error}", file=out)
        return 1
    except Exception as error:  # noqa: BLE001 - network failures from the SDK; never print the key
        print(f"purge: could not reach Zep: {redact(f'{type(error).__name__}: {error}')[:300]}", file=out)
        return 1
    return 0
