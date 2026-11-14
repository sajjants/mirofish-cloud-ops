"""Zep Cloud resources a batch created: record them at creation, find them later, purge and verify.

Upstream MiroFish creates one Zep graph per run (id ``mirofish_<16 hex>``) and uploads the seed
chunks through one Zep batch. Both ids are saved in upstream's project file before the upload
starts; the run pipeline copies them into the run manifest and into the batch's resource record
while the graph builds. Purge deletes through the Zep SDK and then checks that the graph is gone.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ops import paths
from ops.budget import batch_state_dir

ZEP_HOST = "api.getzep.com"
# Upstream passes this base URL explicitly (backend/app/utils/zep.py) and refuses to start if ZEP_API_URL is set.
ZEP_BASE_URL = f"https://{ZEP_HOST}/api/v2"
ZEP_TIMEOUT_SECONDS = 60.0
GRAPH_ID = re.compile(r"^mirofish_[0-9a-f]{16}$")
ZEP_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
PROJECTS_DIR = paths.UPSTREAM_DIR / "backend" / "uploads" / "projects"
VERIFY_QUERY = "purge verification"   # fixed text; carries nothing from the run
REMAINING_BATCH_NOTE = ("Zep's API deletes only draft or unprocessed batches (zep-cloud 3.25.0 `batch.delete`), so "
                        "the record of this processed upload batch remains in Zep after its graph is deleted. "
                        "Whether Zep keeps any uploaded text with it is governed by Zep's retention and deletion "
                        "terms; see `python -m ops privacy_report`.")


class ZepError(RuntimeError):
    pass


def make_client(api_key: str):
    """A Zep SDK client that can only reach Zep Cloud."""
    if os.environ.get("ZEP_API_URL"):
        raise ZepError("ZEP_API_URL is set; unset it (the SDK would send data there instead of Zep Cloud)")
    from zep_cloud.client import Zep
    return Zep(api_key=api_key, base_url=ZEP_BASE_URL, timeout=ZEP_TIMEOUT_SECONDS)


def status_of(error: BaseException) -> Optional[int]:
    """HTTP status of a Zep SDK error (zep_cloud.core.api_error.ApiError), else None."""
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _is_api_error(error: BaseException) -> bool:
    try:
        from zep_cloud.core.api_error import ApiError
    except ImportError:
        return status_of(error) is not None
    return isinstance(error, ApiError) or status_of(error) is not None


def _check_ids(graph_ids: Iterable[str], batch_ids: Iterable[str]) -> None:
    for graph_id in graph_ids:
        if not GRAPH_ID.fullmatch(graph_id):
            raise ZepError(f"refusing unexpected graph id {graph_id!r} (upstream ids look like mirofish_<16 hex>)")
    for zep_batch_id in batch_ids:
        if not ZEP_BATCH_ID.fullmatch(zep_batch_id):
            raise ZepError(f"refusing unexpected Zep batch id {zep_batch_id!r}")


class ZepResources:
    """ops/state/batches/<batch>/zep_resources.json: every graph and Zep batch id a batch created."""

    def __init__(self, batch_id: str, state_dir: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.path = (state_dir or batch_state_dir(batch_id)) / "zep_resources.json"
        self.clock = clock

    def load(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if not self.path.exists():
            return {"graphs": {}, "batches": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("graphs", {})
        data.setdefault("batches", {})
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        paths.write_private_text(self.path, json.dumps(data, indent=2) + "\n")

    def record(self, run_id: str, project_id: Optional[str], graph_ids: Iterable[str],
               batch_ids: Iterable[str]) -> None:
        graph_ids, batch_ids = list(graph_ids), list(batch_ids)
        _check_ids(graph_ids, batch_ids)
        data = self.load()
        changed = False
        for kind, ids in (("graphs", graph_ids), ("batches", batch_ids)):
            for resource_id in ids:
                if resource_id not in data[kind]:
                    data[kind][resource_id] = {"run_id": run_id, "project_id": project_id,
                                               "recorded_at": self.clock(), "status": "recorded"}
                    changed = True
        if changed:
            self._save(data)

    def mark(self, kind: str, resource_id: str, outcome: Dict[str, Any]) -> None:
        data = self.load()
        entry = data[kind].setdefault(resource_id, {"recorded_at": None})
        entry.update({k: v for k, v in outcome.items() if k != "id"}, checked_at=self.clock())
        self._save(data)

    def open_ids(self) -> Dict[str, List[str]]:
        """Ids not yet confirmed gone."""
        data = self.load()
        return {kind: sorted(i for i, e in data[kind].items() if e.get("status") not in ("deleted", "gone"))
                for kind in ("graphs", "batches")}


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def discover(batch_id: str, runs_dir: Optional[Path] = None, projects_dir: Path = PROJECTS_DIR,
             resources: Optional[ZepResources] = None) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Every graph and Zep batch id of a batch: the record, the run manifests and upstream's project files."""
    runs_dir = runs_dir or paths.RUNS_DIR
    resources = resources or ZepResources(batch_id)
    found = resources.load()
    batch_state = _read_json(runs_dir / batch_id / "batch.json")
    for run_id in sorted(batch_state.get("runs") or {}):
        manifest = _read_json(runs_dir / run_id / "manifest.json")
        project_id = manifest.get("project_id")
        zep = manifest.get("zep") or {}
        graph_ids = set(zep.get("graph_ids") or []) | ({manifest["graph_id"]} if manifest.get("graph_id") else set())
        batch_ids = set(zep.get("batch_ids") or [])
        if project_id and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
            project = _read_json(projects_dir / project_id / "project.json")
            graph_ids |= {project["graph_id"]} if project.get("graph_id") else set()
            batch_ids |= {project["zep_batch_id"]} if project.get("zep_batch_id") else set()
        _check_ids(graph_ids, batch_ids)
        for kind, ids in (("graphs", graph_ids), ("batches", batch_ids)):
            for resource_id in ids:
                found[kind].setdefault(resource_id, {"run_id": run_id, "project_id": project_id,
                                                     "recorded_at": None, "status": "discovered"})
    return found


def _count_results(results: Any) -> int:
    return sum(len(getattr(results, field, None) or []) for field in ("edges", "nodes", "episodes", "observations"))


def purge_graph(client: Any, graph_id: str) -> Dict[str, Any]:
    """Delete one graph, then verify: get must answer 404 and a search must find nothing."""
    _check_ids([graph_id], [])
    outcome: Dict[str, Any] = {"id": graph_id}
    try:
        client.graph.delete(graph_id=graph_id)
        outcome["action"] = "deleted"
    except Exception as error:  # noqa: BLE001 - only Zep API errors are expected; others re-raise below
        if not _is_api_error(error):
            raise
        if status_of(error) != 404:
            return {**outcome, "status": "failed", "error": f"delete failed (HTTP {status_of(error)})"}
        outcome["action"] = "already gone"
    try:
        client.graph.get(graph_id=graph_id)
        return {**outcome, "status": "failed", "error": "graph still exists after delete"}
    except Exception as error:  # noqa: BLE001
        if not _is_api_error(error):
            raise
        if status_of(error) != 404:
            return {**outcome, "status": "failed", "error": f"could not verify (HTTP {status_of(error)} on get)"}
    try:
        results = client.graph.search(graph_id=graph_id, query=VERIFY_QUERY, limit=1)
        if _count_results(results):
            return {**outcome, "status": "failed", "error": "a search still returns results from this graph"}
    except Exception as error:  # noqa: BLE001
        if not _is_api_error(error):
            raise
        if status_of(error) != 404:
            return {**outcome, "status": "failed", "error": f"could not verify (HTTP {status_of(error)} on search)"}
    return {**outcome, "status": "deleted" if outcome["action"] == "deleted" else "gone", "verified": True}


def purge_batch_record(client: Any, zep_batch_id: str) -> Dict[str, Any]:
    """Delete a Zep upload batch if Zep allows it; report it plainly if it remains."""
    _check_ids([], [zep_batch_id])
    outcome: Dict[str, Any] = {"id": zep_batch_id}
    try:
        summary = client.batch.get(batch_id=zep_batch_id)
    except Exception as error:  # noqa: BLE001
        if not _is_api_error(error):
            raise
        if status_of(error) == 404:
            return {**outcome, "status": "gone", "verified": True}
        return {**outcome, "status": "failed", "error": f"could not read the batch (HTTP {status_of(error)})"}
    batch_status = str(getattr(summary, "status", "") or "unknown")
    try:
        client.batch.delete(batch_id=zep_batch_id)
    except Exception as error:  # noqa: BLE001
        if not _is_api_error(error):
            raise
        return {**outcome, "status": "remains", "batch_status": batch_status,
                "error": f"delete refused (HTTP {status_of(error)})", "note": REMAINING_BATCH_NOTE}
    try:
        client.batch.get(batch_id=zep_batch_id)
        return {**outcome, "status": "failed", "error": "batch still exists after delete"}
    except Exception as error:  # noqa: BLE001
        if not _is_api_error(error):
            raise
        if status_of(error) != 404:
            return {**outcome, "status": "failed", "error": f"could not verify (HTTP {status_of(error)})"}
    return {**outcome, "status": "deleted", "verified": True}


def purge(client: Any, graph_ids: Iterable[str], batch_ids: Iterable[str],
          resources: Optional[ZepResources] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Graphs first (they hold the extracted data), then the upload batches. Each outcome is recorded."""
    report: Dict[str, List[Dict[str, Any]]] = {"graphs": [], "batches": []}
    for kind, ids, action in (("graphs", graph_ids, purge_graph), ("batches", batch_ids, purge_batch_record)):
        for resource_id in sorted(ids):
            outcome = action(client, resource_id)
            report[kind].append(outcome)
            if resources is not None:
                resources.mark(kind, resource_id, outcome)
    return report


def purge_ok(report: Dict[str, List[Dict[str, Any]]]) -> bool:
    """Every graph verified gone and no batch in an unknown state ('remains' is reported, not a failure)."""
    return all(o.get("verified") for o in report["graphs"]) and \
        all(o["status"] in ("deleted", "gone", "remains") for o in report["batches"])
