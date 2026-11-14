"""An in-memory stand-in for the parts of the zep-cloud SDK client the wrapper uses."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional, Set

from zep_cloud.core.api_error import ApiError

FAKE_ZEP_KEY = "zep-fake-key-for-tests-only"


class FakeZepError(ApiError):
    def __init__(self, status: int):
        super().__init__(status_code=status, body={"message": f"fake {status}"})


class _Graphs:
    def __init__(self, zep: "FakeZep"):
        self.zep = zep

    def delete(self, *, graph_id: str):
        self.zep.calls.append(("graph.delete", graph_id))
        if graph_id in self.zep.undeletable_graphs:
            raise FakeZepError(500)
        if graph_id not in self.zep.graphs:
            raise FakeZepError(404)
        if graph_id not in self.zep.sticky_graphs:
            self.zep.graphs.discard(graph_id)

    def get(self, *, graph_id: str):
        self.zep.calls.append(("graph.get", graph_id))
        if graph_id not in self.zep.graphs:
            raise FakeZepError(404)
        return SimpleNamespace(graph_id=graph_id)

    def search(self, *, graph_id: str, query: str, limit: int):
        self.zep.calls.append(("graph.search", graph_id))
        self.zep.queries.append(query)
        if graph_id not in self.zep.graphs:
            raise FakeZepError(404)
        return SimpleNamespace(edges=[object()], nodes=[], episodes=[])


class _Batches:
    def __init__(self, zep: "FakeZep"):
        self.zep = zep

    def get(self, *, batch_id: str):
        self.zep.calls.append(("batch.get", batch_id))
        if batch_id not in self.zep.batches:
            raise FakeZepError(404)
        return SimpleNamespace(batch_id=batch_id, status=self.zep.batches[batch_id])

    def delete(self, *, batch_id: str):
        self.zep.calls.append(("batch.delete", batch_id))
        if self.zep.batches.get(batch_id) == "completed":
            raise FakeZepError(409)
        self.zep.batches.pop(batch_id, None)


class FakeZep:
    def __init__(self, graphs: Optional[Set[str]] = None, batches: Optional[Dict[str, str]] = None):
        self.graphs: Set[str] = set(graphs or ())
        self.batches: Dict[str, str] = dict(batches or {})
        self.sticky_graphs: Set[str] = set()       # delete "succeeds" but the graph stays
        self.undeletable_graphs: Set[str] = set()  # delete answers HTTP 500
        self.fail_with: Optional[Exception] = None
        self.calls: List[tuple] = []
        self.queries: List[str] = []
        self.api_keys: List[str] = []
        self.graph = _Graphs(self)
        self.batch = _Batches(self)
        self.project = SimpleNamespace(get=self._project_get)

    def _project_get(self):
        self.calls.append(("project.get",))
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(project=SimpleNamespace(name="test"))
