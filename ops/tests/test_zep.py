import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from ops import keys, paths, zep
from ops.purge import purge_command
from ops.tests.fake_zep import FAKE_ZEP_KEY, FakeZep
from ops.tests.helpers import TempState
from ops.zep_usage import ZepLimitsError, episode_range, estimate, example_limits, validate_zep_limits

G1, G2 = "mirofish_0123456789abcdef", "mirofish_fedcba9876543210"


class PurgeTests(unittest.TestCase):
    def test_deletes_graphs_and_verifies_with_get_and_search(self):
        client = FakeZep(graphs={G1})
        report = zep.purge(client, [G1], [])
        self.assertEqual(report["graphs"][0]["status"], "deleted")
        self.assertTrue(zep.purge_ok(report))
        self.assertEqual([c[0] for c in client.calls], ["graph.delete", "graph.get", "graph.search"])
        self.assertEqual(client.queries, [zep.VERIFY_QUERY])

    def test_already_gone_is_verified_and_failures_are_reported(self):
        client = FakeZep(graphs={G2})
        client.sticky_graphs.add(G2)
        report = zep.purge(client, [G1, G2], [])
        by_id = {o["id"]: o for o in report["graphs"]}
        self.assertEqual(by_id[G1]["status"], "gone")
        self.assertEqual(by_id[G2]["status"], "failed")
        self.assertIn("still exists", by_id[G2]["error"])
        self.assertFalse(zep.purge_ok(report))

    def test_server_errors_on_delete_fail_without_raising(self):
        client = FakeZep(graphs={G1})
        client.undeletable_graphs.add(G1)
        outcome = zep.purge_graph(client, G1)
        self.assertEqual((outcome["status"], outcome["error"]), ("failed", "delete failed (HTTP 500)"))

    def test_processed_upload_batches_remain_and_are_explained(self):
        client = FakeZep(batches={"b-done": "completed", "b-draft": "draft"})
        report = zep.purge(client, [], ["b-done", "b-draft", "b-gone"])
        by_id = {o["id"]: o for o in report["batches"]}
        self.assertEqual(by_id["b-done"]["status"], "remains")
        self.assertIn("retention and deletion terms", by_id["b-done"]["note"])
        self.assertEqual(by_id["b-draft"]["status"], "deleted")
        self.assertEqual(by_id["b-gone"]["status"], "gone")
        self.assertTrue(zep.purge_ok(report))

    def test_unexpected_ids_are_refused(self):
        with self.assertRaises(zep.ZepError):
            zep.purge_graph(FakeZep(), "someone_elses_graph")

    def test_client_refuses_a_redirected_zep_url(self):
        with mock.patch.dict(os.environ, {"ZEP_API_URL": "https://example.invalid"}):
            with self.assertRaises(zep.ZepError):
                zep.make_client(FAKE_ZEP_KEY)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)

    def write_run(self, run_id, manifest):
        run_dir = paths.RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text(json.dumps(manifest))

    def test_discover_merges_record_manifests_and_upstream_projects(self):
        zep.ZepResources("b1").record("b1_pilot", "proj_a", [G1], ["zb-1"])
        (paths.RUNS_DIR / "b1").mkdir(parents=True)
        (paths.RUNS_DIR / "b1" / "batch.json").write_text(json.dumps({"runs": {"b1_pilot": {}, "b1_Q1_v0_r1": {}}}))
        self.write_run("b1_pilot", {"project_id": "proj_a", "graph_id": G1})
        self.write_run("b1_Q1_v0_r1", {"project_id": "proj_b"})
        projects = self.state.root / "projects"
        (projects / "proj_b").mkdir(parents=True)
        (projects / "proj_b" / "project.json").write_text(json.dumps({"graph_id": G2, "zep_batch_id": "zb-2"}))
        found = zep.discover("b1", projects_dir=projects)
        self.assertEqual(sorted(found["graphs"]), [G1, G2])
        self.assertEqual(sorted(found["batches"]), ["zb-1", "zb-2"])
        self.assertEqual(found["graphs"][G2]["status"], "discovered")
        self.assertEqual(found["graphs"][G2]["run_id"], "b1_Q1_v0_r1")

    def test_purge_command_confirms_deletes_and_records(self):
        zep.ZepResources("b1").record("b1_pilot", "proj_a", [G1], ["zb-1"])
        client = FakeZep(graphs={G1}, batches={"zb-1": "completed"})
        out = io.StringIO()
        with mock.patch.object(keys, "_from_keyring", return_value=None), \
                mock.patch.dict(os.environ, {"ZEP_API_KEY": FAKE_ZEP_KEY}):
            refused = purge_command("b1", client_factory=lambda _k: client, ask=lambda _p: "yes", out=out)
            self.assertEqual((refused, client.calls), (2, []))
            code = purge_command("b1", client_factory=lambda _k: client, ask=lambda _p: "purge b1", out=out)
            again = purge_command("b1", client_factory=lambda _k: client, ask=lambda _p: "purge b1", out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(again, 0)
        record = zep.ZepResources("b1").load()
        self.assertEqual(record["graphs"][G1]["status"], "deleted")
        self.assertEqual(record["batches"]["zb-1"]["status"], "remains")
        self.assertNotIn(FAKE_ZEP_KEY, out.getvalue())
        self.assertTrue((paths.STATE_DIR / "batches" / "b1" / "purge.jsonl").exists())

    def test_purge_command_needs_a_terminal(self):
        zep.ZepResources("b1").record("b1_pilot", None, [G1], [])
        with mock.patch.dict(os.environ, {"ZEP_API_KEY": FAKE_ZEP_KEY}):
            code = purge_command("b1", client_factory=lambda _k: FakeZep(), interactive=False, out=io.StringIO())
        self.assertEqual(code, 2)


class UsageEstimateTests(unittest.TestCase):
    def test_episode_range_brackets_upstream_chunking(self):
        self.assertEqual(episode_range(0, 500, 50), {"low": 0, "high": 0})
        self.assertEqual(episode_range(400, 500, 50), {"low": 1, "high": 1})
        self.assertEqual(episode_range(5000, 500, 50), {"low": 11, "high": 46})

    def test_estimate_scales_with_runs_and_agents_and_warns_on_quotas(self):
        limits = example_limits()
        limits["quotas"]["searches"] = 100
        limits.update(source_url="https://example.invalid/plan", checked_on="2026-11-15")
        result = estimate(5000, [10, 12], limits, open_graphs=1)
        self.assertEqual(result["total"]["episodes"], {"low": 22, "high": 92})
        self.assertEqual(result["total"]["searches"]["high"], 2 * 22 + 2 * 60)
        self.assertEqual(result["total"]["graphs"]["high"], 3)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("searches", result["warnings"][0])

    def test_without_a_config_quotas_are_not_checked(self):
        result = estimate(1000, [5], None)
        self.assertIn("not checked", result["warnings"][0])

    def test_quotas_need_a_source_and_date(self):
        raw = {"source_url": "SET_ME", "checked_on": "SET_ME", "quotas": {"episodes": 1000},
               "usage": example_limits()["usage"]}
        with self.assertRaises(ZepLimitsError):
            validate_zep_limits(raw)
        with self.assertRaises(ZepLimitsError):
            validate_zep_limits({**raw, "quotas": {"credits": 5}})


if __name__ == "__main__":
    unittest.main()
