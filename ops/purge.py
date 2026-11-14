"""purge: delete a batch's Zep Cloud graphs (and upload batches, where Zep allows) and verify.

Ids come from the batch's resource record, its run manifests and upstream's project files. The
outcome of every delete and check is stored in ops/state/batches/<batch>/zep_resources.json and
appended to purge.jsonl in the same folder (ids and statuses only).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, Optional, TextIO

from ops import paths
from ops.batch import BatchError, BatchLock, batch_dir
from ops.budget import batch_state_dir
from ops.confirm import NotConfirmed, typed_phrase
from ops.keys import get_key
from ops.redact import redact
from ops.zep import ZEP_HOST, ZepError, ZepResources, discover, make_client, purge, purge_ok

ZEP_KEY = "ZEP_API_KEY"


def purge_command(batch_id: str, *, client_factory: Callable[[str], Any] = make_client,
                  ask: Optional[Callable[[str], str]] = None, interactive: Optional[bool] = None,
                  out: TextIO = sys.stdout) -> int:
    """0: everything verified gone; 1: something failed or could not be checked; 2: not confirmed."""
    try:
        batch_dir(batch_id)
        with BatchLock():
            resources = ZepResources(batch_id)
            found = discover(batch_id, resources=resources)
            graphs = sorted(i for i, e in found["graphs"].items() if e.get("status") not in ("deleted", "gone"))
            batches = sorted(i for i, e in found["batches"].items() if e.get("status") not in ("deleted", "gone"))
            if not graphs and not batches:
                print(f"purge {batch_id}: no Zep resources left to delete", file=out)
                return 0
            for kind, ids in (("graphs", graphs), ("batches", batches)):
                for resource_id in ids:
                    entry = found[kind][resource_id]
                    if entry.get("status") == "discovered":
                        resources.record(entry.get("run_id") or "unknown", entry.get("project_id"),
                                         [resource_id] if kind == "graphs" else [],
                                         [resource_id] if kind == "batches" else [])
            key = get_key(ZEP_KEY, ZEP_KEY)
            if key is None:
                print(f"purge: no Zep key; set {ZEP_KEY} or run `python -m ops keys set {ZEP_KEY}`", file=out)
                return 1
            lines = [f"PURGE {batch_id}: permanently delete from Zep Cloud ({ZEP_HOST})",
                     *(f"  graph {g} (run {found['graphs'][g].get('run_id')})" for g in graphs),
                     *(f"  upload batch {b} (run {found['batches'][b].get('run_id')})" for b in batches),
                     f"This sends only these ids and your Zep key to {ZEP_HOST}. It cannot be undone."]
            typed_phrase(lines, f"purge {batch_id}", "not confirmed; nothing was deleted", ask=ask, out=out,
                         interactive=interactive)
            report = purge(client_factory(key.value), graphs, batches, resources)
    except NotConfirmed as error:
        print(f"purge: {error}", file=out)
        return 2
    except (BatchError, ZepError) as error:
        print(f"purge: {error}", file=out)
        return 1
    except Exception as error:  # noqa: BLE001 - network failures from the SDK; never print the key
        print(f"purge: could not reach Zep: {redact(f'{type(error).__name__}: {error}')[:300]}", file=out)
        return 1
    paths.append_private_line(batch_state_dir(batch_id) / "purge.jsonl",
                              json.dumps({"at": time.time(), "report": report}))
    for kind, label in (("graphs", "graph"), ("batches", "upload batch")):
        for outcome in report[kind]:
            detail = outcome.get("error") or outcome.get("action") or ""
            print(f"  {label} {outcome['id']}: {outcome['status']}{' (' + detail + ')' if detail else ''}", file=out)
            if outcome.get("note"):
                print(f"    {outcome['note']}", file=out)
    ok = purge_ok(report)
    print(f"purge {batch_id}: {'Zep graphs deleted and verified gone' if ok else 'INCOMPLETE; run it again'}",
          file=out)
    print("Local files (ops/runs, MiroFish uploads and logs) are not removed by this version of purge.", file=out)
    return 0 if ok else 1
