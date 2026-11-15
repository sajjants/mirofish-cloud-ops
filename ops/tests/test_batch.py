import io
import json
import os
import unittest

from ops import paths, privacy
from ops.batch import Batch, BatchError, BatchLock, RelayControl
from ops.budget import Budget
from ops.confirm import NotConfirmed, confirm
from ops.mirofish_api import StopRun
from ops.tests.fake_provider import FakeProvider, completion
from ops.tests.fake_zep import FAKE_ZEP_KEY
from ops.tests.helpers import GRAPH_ID, SESSION, TERMS, FakeMiroFish, RelayFixture, TempState, \
    confirm_privacy, limits, llm_config, prices
from ops.zep import ZepResources

TINY_ESTIMATES = {"estimates__setup": {"fixed_calls": 1, "calls_per_agent": 1, "prompt_tokens_per_call": 100,
                                       "output_tokens_per_call": 10},
                  "estimates__simulation": {"calls_per_turn": 1, "prompt_tokens_per_call": 100,
                                            "output_tokens_per_call": 10},
                  "estimates__report": {"fixed_calls": 2, "prompt_tokens_per_call": 100, "output_tokens_per_call": 10}}


class BatchTestCase(unittest.TestCase):
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}

    def setUp(self):
        self.state = TempState(self)
        root = self.state.root
        self.provider = FakeProvider(lambda m, p, b: (200, {}, completion("{}", usage=self.usage)))
        self.provider.__enter__()
        self.addCleanup(self.provider.__exit__)
        self.relay = RelayFixture(self, self.provider.base_url, reserve_output_tokens=100)
        self.sims = root / "simulations"
        self.mirofish = FakeMiroFish(self.relay.url, self.sims)
        self.env = root / "mirofish.env"
        self.env.write_text(f"LLM_BASE_URL={self.relay.url}/v1\nLLM_API_KEY={SESSION}\nFLASK_PORT=5001\n")
        confirm_privacy()
        self.batch_dir = paths.RUNS_DIR / "b1"
        (self.batch_dir / "inputs").mkdir(parents=True)
        (self.batch_dir / "inputs" / "seed.md").write_text("VERIFIED: Person A runs Org B.\n")
        (self.batch_dir / "inputs" / "Q1.requirement.txt").write_text("How will people react?\n")
        self.write_inputs(pseudonymized=True)
        self.answers = []
        self.logs = []
        self.out = io.StringIO()

    def write_inputs(self, pseudonymized: bool) -> None:
        (self.batch_dir / "inputs.json").write_text(json.dumps({
            "batch_id": "b1", "sources": {"seed": {"sha256": "abc"}}, "pseudonymized": pseudonymized,
            "questions": [{"id": "Q1", "requirement_file": "inputs/Q1.requirement.txt"}], "unknowns": []}))

    def make_batch(self, lim=None, phrase="send b1") -> Batch:
        return Batch("b1", client=self.mirofish, relay=RelayControl(state=self.relay.state()),
                     limits=lim or limits(**TINY_ESTIMATES), llm=llm_config(self.provider.base_url), prices=prices(),
                     log=self.logs.append, ask=lambda _prompt: phrase, interactive=True, poll_interval_s=0,
                     sleep=lambda _s: None, simulations_dir=self.sims, terms=TERMS, env_path=self.env,
                     out=self.out, zep_limits=None)


class PilotTests(BatchTestCase):
    def test_pilot_end_to_end_meters_every_stage_and_plans(self):
        summary = self.make_batch().pilot()
        self.assertEqual((summary["rounds"], summary["agents"], summary["gates"]), (10, 6, []))
        self.assertEqual(set(summary["groups"]), {"setup", "simulation", "report"})
        self.assertEqual(summary["groups"]["simulation"]["requests"], 30)
        self.assertEqual(summary["groups"]["setup"]["requests"], 7)
        self.assertEqual(summary["cost_model"]["source"], "pilot")
        manifest = json.loads((paths.RUNS_DIR / "b1_pilot" / "manifest.json").read_text())
        spend = manifest["batch"]["spend"]
        self.assertAlmostEqual(spend["total"], Budget("b1").status()["spend"])
        self.assertEqual(spend["wasted"], 0)
        self.assertEqual(manifest["batch"]["models"]["simulation"], {"model": "sim-m", "reasoning_effort": "low"})
        self.assertEqual(self.mirofish.starts[0]["enable_graph_memory_update"], False)
        plan = json.loads((self.batch_dir / "plan.json").read_text())
        self.assertEqual(plan["cost_source"], "pilot")
        self.assertEqual(plan["zep"]["runs"], len(plan["runs"]))
        self.assertTrue(all(status == 200 for status in self.mirofish.llm_statuses))
        self.assertIn("Zep (estimated, not metered) for 1 run(s)", self.out.getvalue())
        self.assertEqual(manifest["zep"], {"graph_ids": [GRAPH_ID], "batch_ids": ["zb-1"]})
        record = ZepResources("b1").load()
        self.assertEqual(record["graphs"][GRAPH_ID]["run_id"], "b1_pilot")
        self.assertEqual(list(record["batches"]), ["zb-1"])

    def test_too_many_agents_stops_before_personas_and_entity_types_resume(self):
        self.mirofish.entities = {"Person": 9, "Organization": 5}
        batch = self.make_batch()
        with self.assertRaises(StopRun) as caught:
            batch.pilot()
        self.assertEqual(caught.exception.reason, "agent_cap")
        self.assertIn("Person 9", str(caught.exception))
        self.assertFalse(any(r["stage"] == "prepare" for r in Budget("b1").ledger()))
        batch.state["entity_types"] = ["Person"]
        summary = batch.pilot()
        self.assertEqual(summary["agents"], 9)

    def test_kill_switch_stops_rounds_cleanly_and_resume_restarts_them(self):
        self.usage = {"prompt_tokens": 20000, "completion_tokens": 100, "total_tokens": 20100}
        batch = self.make_batch(limits(pilot__max_spend=0.15, **TINY_ESTIMATES))
        with self.assertRaises(StopRun) as caught:
            batch.pilot()
        self.assertEqual(caught.exception.reason, "pilot.max_spend")
        self.assertEqual(self.mirofish.stops, 1)
        manifest = json.loads((paths.RUNS_DIR / "b1_pilot" / "manifest.json").read_text())
        self.assertEqual(manifest["stages"]["rounds"]["status"], "interrupted")
        self.assertIn(402, self.mirofish.llm_statuses)
        self.assertEqual(batch.state["runs"]["b1_pilot"]["status"], "stopped")

        resumed = self.make_batch(limits(pilot__max_spend=2.0, caps__max_spend=4.0, **TINY_ESTIMATES))
        summary = resumed.pilot()
        manifest = json.loads((paths.RUNS_DIR / "b1_pilot" / "manifest.json").read_text())
        self.assertEqual(manifest["stages"]["rounds"]["attempt"], 2)
        self.assertEqual(manifest["stages"]["rounds"]["history"][0]["status"], "interrupted")
        self.assertTrue(self.mirofish.starts[-1]["force"])
        self.assertGreater(manifest["batch"]["spend"]["wasted"], 0)
        self.assertEqual(summary["rounds"], 10)

    def test_admission_stops_before_a_stage_that_would_not_fit(self):
        batch = self.make_batch(limits(pilot__max_spend=0.01))
        with self.assertRaises(StopRun) as caught:
            batch.pilot()
        self.assertEqual(caught.exception.reason, "admission")
        self.assertEqual(Budget("b1").ledger(), [])


class PreflightTests(BatchTestCase):
    def test_unpseudonymised_batches_are_refused(self):
        self.write_inputs(pseudonymized=False)
        with self.assertRaises(BatchError):
            self.make_batch().pilot()

    def test_leak_gate_blocks_before_any_upload(self):
        (self.batch_dir / "inputs" / "seed.md").write_text("VERIFIED: Zebulon met someone.\n")
        with self.assertRaises(BatchError) as caught:
            self.make_batch().pilot()
        self.assertIn("term 'Zebulon'", str(caught.exception))
        self.assertEqual(self.provider.requests, [])

    def test_mirofish_must_point_at_the_relay(self):
        self.env.write_text("LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\n"
                            f"LLM_API_KEY={SESSION}\n")
        with self.assertRaises(BatchError) as caught:
            self.make_batch().pilot()
        self.assertIn("bypass metering", str(caught.exception))

    def test_zep_key_stays_out_of_the_env_file_and_no_redirected_zep_url(self):
        base = f"LLM_BASE_URL={self.relay.url}/v1\nLLM_API_KEY={SESSION}\n"
        for env, expected in ((base + f"ZEP_API_KEY={FAKE_ZEP_KEY}\n", "contains ZEP_API_KEY"),
                              (base + "ZEP_API_URL=https://example.invalid\n", "ZEP_API_URL")):
            self.env.write_text(env)
            with self.assertRaises(BatchError) as caught:
                self.make_batch().pilot()
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn(FAKE_ZEP_KEY, str(caught.exception))
        self.assertEqual(self.provider.requests, [])

    def test_nothing_is_sent_until_the_privacy_checklist_is_confirmed(self):
        privacy.checklist_file().unlink()
        with self.assertRaises(BatchError) as caught:
            self.make_batch().pilot()
        self.assertIn("privacy_report --confirm", str(caught.exception))
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.mirofish.starts, [])

    def test_wrong_confirmation_sends_nothing(self):
        with self.assertRaises(NotConfirmed):
            self.make_batch(phrase="yes").pilot()
        self.assertEqual(self.provider.requests, [])

    def test_run_refuses_after_a_failed_pilot_gate(self):
        batch = self.make_batch()
        (self.batch_dir / "pilot.json").write_text(json.dumps({"gates": ["refusal rate 9.0% > 2.0%"]}))
        with self.assertRaises(BatchError) as caught:
            batch.run()
        self.assertIn("refusal rate", str(caught.exception))


class SmallPartsTests(unittest.TestCase):
    def test_confirmation_needs_a_terminal_and_the_exact_phrase(self):
        with self.assertRaises(NotConfirmed):
            confirm("t", [], "send b1", "https://x.example/", interactive=False)
        with open(os.devnull, "w") as out:
            confirm("t", [], "send b1", "https://x.example/", ask=lambda _p: "send b1", out=out)
            with self.assertRaises(NotConfirmed):
                confirm("t", [], "send b1", "https://x.example/", ask=lambda _p: "send", out=out)

    def test_batch_lock_is_exclusive_and_stale_locks_are_taken_over(self):
        state = TempState(self)
        lock_path = state.root / "batch.lock"
        with BatchLock(lock_path):
            with self.assertRaises(BatchError):
                with BatchLock(lock_path):
                    pass
        for stale in ("999999999 1.000", f"{os.getpid()} 1.000", "garbage"):
            lock_path.write_text(stale)
            with BatchLock(lock_path):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
