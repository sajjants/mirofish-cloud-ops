import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from ops import cli, compare, factguard, keys, local_purge, paths, prepare_inputs, unknown_variants, zep
from ops.leakgate import LeakGateError
from ops.purge import purge_command
from ops.tests.fake_zep import FAKE_ZEP_KEY, FakeZep
from ops.tests.helpers import TERMS, TempState

FIXTURES = Path(__file__).parent / "fixtures"
RUN = "b1_Q1_v0_r1"
G1 = "mirofish_0123456789abcdef"


class PreparedBatch(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)
        self.root = self.state.root
        self.terms = FIXTURES / "synthetic_terms.txt"
        self.questions = FIXTURES / "synthetic_questions.md"
        self.seed = FIXTURES / "synthetic_seed.md"

    def prepare(self, seed=None):
        return prepare_inputs.prepare("b1", seed or self.seed, self.questions, self.terms, rng_seed=7,
                                      gate_terms=TERMS)

    def mapping(self):
        return json.loads((paths.MAPPINGS_DIR / "b1.mapping.json").read_text())

    def pseudonym(self, original):
        return next(n["pseudonym"] for n in self.mapping()["names"] if n["original"] == original)


class PrepareTests(PreparedBatch):
    def test_prepare_pseudonymises_buckets_money_and_keeps_the_mapping_apart(self):
        inputs = self.prepare()
        self.assertTrue(inputs["pseudonymized"])
        self.assertEqual(inputs["mapping_version"], 2)
        upload = (paths.RUNS_DIR / "b1" / "inputs" / "seed.md").read_text()
        for original in ("Elena Duarte", "Riverton", "Harbor Street", "4.2 million"):
            self.assertNotIn(original, upload)
        self.assertIn("between 2 million dollars and 5 million dollars", upload)
        self.assertNotIn("IGNORE ALL PREVIOUS INSTRUCTIONS", upload)
        self.assertTrue((paths.MAPPINGS_DIR / "b1.mapping.json").exists())
        self.assertEqual(len(inputs["unknowns"]), 2)
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_leak_gate_aborts_before_anything_is_written(self):
        seed = self.root / "seed.md"
        seed.write_text(self.seed.read_text() + "\n- VERIFIED: Contact pat.lee@example.com or Zebulon.\n")
        with self.assertRaises(LeakGateError) as caught:
            self.prepare(seed)
        message = str(caught.exception)
        self.assertIn("Zebulon", message)
        self.assertIn("email", message)
        self.assertNotIn("pat.lee@example.com", message)
        self.assertFalse((paths.RUNS_DIR / "b1").exists())
        self.assertFalse(paths.MAPPINGS_DIR.exists() and any(paths.MAPPINGS_DIR.iterdir()))

    def test_cli_main_reports_a_leak_and_returns_1(self):
        seed = self.root / "seed.md"
        seed.write_text("- VERIFIED: call 0044 20 7946 0958 now.\n")
        out = io.StringIO()
        with mock.patch("ops.leakgate.load_terms", return_value=TERMS), contextlib.redirect_stdout(out):
            code = prepare_inputs.main("b1", seed, self.questions, self.terms)
        self.assertEqual(code, 1)
        self.assertIn("LEAK GATE", out.getvalue())


class VariantTests(PreparedBatch):
    def write_alternatives(self, extra):
        inputs = prepare_inputs.load_inputs("b1")
        unknowns = [{"id": u["id"], "text": u["text"], "alternatives": ["It happens.", "It does not.", extra]}
                    for u in inputs["unknowns"]]
        (paths.RUNS_DIR / "b1" / "alternatives.json").write_text(json.dumps({"unknowns": unknowns}))

    def test_sampled_variant_seeds_pass_the_leak_gate_before_they_are_written(self):
        self.prepare()
        self.write_alternatives("Ask Zebulon at z@example.com.")
        with self.assertRaises(LeakGateError):
            unknown_variants.sample("b1", 3, rng_seed=1, terms=TERMS)
        self.assertFalse((paths.RUNS_DIR / "b1" / "variants").exists())
        self.write_alternatives("It is postponed.")
        result = unknown_variants.sample("b1", 3, rng_seed=1, terms=TERMS)
        self.assertEqual(len(result["variants"]), 3)
        self.assertTrue((paths.RUNS_DIR / "b1" / "variants" / "v2.seed.md").exists())


class FakeLLM:
    model = "fake-model"

    def __init__(self, answer):
        self.answer, self.prompts, self.begun, self.ended = answer, [], None, False

    def begin(self, purpose, sends, max_calls):
        self.begun = max_calls

    def end(self):
        self.ended = True

    def chat_json(self, system, user, *, validate, seed=0, temperature=0.2, **_):
        self.prompts.append(system + user)
        error = validate(self.answer)
        assert error is None, error
        return self.answer


class RunOutputTests(PreparedBatch):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.mayor = self.pseudonym("Elena Duarte")
        run_dir = paths.RUNS_DIR / RUN
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "inputs": {"max_rounds": 10},
            "batch": {"batch_id": "b1", "question": "Q1", "variant": {"index": 0, "choices": {}}}}))
        (run_dir / "report.md").write_text(
            f"# Report\n\n{self.mayor} said the closure saves between 2 million dollars and 5 million dollars. "
            "A later poll found 77,000 supporters and a cost of $9 million.\n")
        (run_dir / "transcript.json").write_text(json.dumps({
            "agents": [{"agent_id": 1, "entity_name": self.mayor, "entity_type": "Person", "stance": "support"}],
            "platforms": {"twitter": {"users": [{"user_id": 1, "agent_id": 1, "name": self.mayor}],
                                      "posts": [{"user_id": 1, "content": "Proposal 14 is good.", "created_at": "1"}],
                                      "comments": []}}}))
        (paths.RUNS_DIR / "b1" / "batch.json").write_text(json.dumps({"runs": {RUN: {"status": "done",
                                                                                     "kind": "real"}}}))

    def test_factguard_checks_in_pseudonymised_space_and_restores_locally(self):
        result = factguard.guard_run(RUN, None)
        values = json.dumps(result["flags"])
        self.assertIn("77,000", values)
        self.assertNotIn(self.mayor, values)
        self.assertEqual(result["unmapped_amounts"], ["$9 million"])
        restored = (paths.RUNS_DIR / RUN / "report.depseudo.md").read_text()
        self.assertIn("Elena Duarte said the closure saves 4.2 million dollars", restored)
        annotated = (paths.RUNS_DIR / RUN / "report.annotated.md").read_text()
        self.assertIn("Elena Duarte", annotated)
        self.assertNotIn(self.mayor, annotated)

    def test_factguard_without_llm_needs_no_confirmation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(factguard.main(batch_id="b1", no_llm=True), 0)

    def test_compare_sends_pseudonymised_text_and_restores_the_result(self):
        llm = FakeLLM({"trajectory": "stalemate", "resistors": [self.mayor, "Nobody"], "confidence": 0.5,
                       "rationale": f"{self.mayor} held firm."})
        result = compare.compare("b1", llm, compare.load_rubric(), passes=2)
        self.assertEqual((llm.begun, llm.ended, len(llm.prompts)), (6, True, 2))
        self.assertTrue(all("Elena Duarte" not in p and self.mayor in p for p in llm.prompts))
        label = result["runs"][0]["labels"][0]
        self.assertEqual((label["resistors"], label["unlisted_resistors"]), (["Elena Duarte"], ["Nobody"]))
        self.assertIn("Elena Duarte held firm.", (paths.RUNS_DIR / "b1" / "compare.md").read_text())


class LocalPurgeTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)
        self.uploads = paths.UPSTREAM_DIR / "backend" / "uploads"

    def write(self, path, text="data"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def make_batch_files(self, project_id="proj_1"):
        self.write(paths.RUNS_DIR / "b1" / "batch.json", json.dumps({"runs": {"b1_pilot": {}}}))
        self.write(paths.RUNS_DIR / "b1_pilot" / "manifest.json",
                   json.dumps({"project_id": project_id, "simulation_id": "sim_1"}))
        self.write(self.uploads / "projects" / "proj_1" / "project.json", "{}")
        self.write(self.uploads / "projects" / "proj_2" / "project.json", json.dumps({"name": "b1_pilot"}))
        self.write(self.uploads / "simulations" / "sim_1" / "state.json")
        self.write(self.uploads / "reports" / "rep_1" / "meta.json", json.dumps({"simulation_id": "sim_1"}))
        self.write(self.uploads / "projects" / "other" / "project.json", json.dumps({"name": "b2_pilot"}))
        self.write(paths.MAPPINGS_DIR / "b1.mapping.json")
        self.write(paths.STATE_DIR / "batches" / "b1" / "zep_resources.json", "{}")

    def test_plan_covers_upstream_dirs_runs_batch_and_mapping_only(self):
        self.make_batch_files()
        names = [p.name for p in local_purge.plan("b1", include_logs=False)]
        self.assertEqual(names, ["proj_1", "proj_2", "sim_1", "rep_1", "b1_pilot", "b1", "b1.mapping.json"])

    def test_plan_refuses_unexpected_ids(self):
        self.make_batch_files(project_id="../escape")
        with self.assertRaises(ValueError):
            local_purge.plan("b1", include_logs=False)

    def test_shred_does_not_zero_other_hard_links(self):
        outside = self.write(self.state.root / "outside.txt", "keep me")
        tree = self.state.root / "tree"
        self.write(tree / "file.txt", "secret")
        os.link(outside, tree / "hard.txt")
        warnings = []
        self.assertEqual(local_purge.shred_tree(tree, warnings), 2)
        self.assertFalse(tree.exists())
        self.assertEqual(outside.read_text(), "keep me")
        self.assertEqual(len(warnings), 1)

    @unittest.skipIf(os.name == "nt", "creating symlinks needs extra rights on Windows")
    def test_shred_does_not_follow_symlinks(self):
        outside = self.write(self.state.root / "outside.txt", "keep me")
        tree = self.state.root / "tree"
        self.write(tree / "file.txt", "secret")
        os.symlink(outside, tree / "link.txt")
        (tree / "sub").mkdir()
        os.symlink(self.state.root, tree / "sub" / "dirlink")
        self.assertEqual(local_purge.shred_tree(tree, []), 2)
        self.assertFalse(tree.exists())
        self.assertEqual(outside.read_text(), "keep me")
        self.assertTrue(self.state.root.is_dir())

    def purge(self, client, **kwargs):
        out = io.StringIO()
        with mock.patch.object(keys, "_from_keyring", return_value=None), \
                mock.patch.dict(os.environ, {"ZEP_API_KEY": FAKE_ZEP_KEY}):
            code = purge_command("b1", client_factory=lambda _k: client, ask=lambda _p: "purge b1", out=out,
                                 interactive=True, **kwargs)
        self.assertNotIn(FAKE_ZEP_KEY, out.getvalue())
        return code, out.getvalue()

    def test_local_files_are_kept_until_zep_is_verified_clean(self):
        self.make_batch_files()
        zep.ZepResources("b1").record("b1_pilot", "proj_1", [G1], [])
        client = FakeZep(graphs={G1})
        client.sticky_graphs.add(G1)
        code, out = self.purge(client)
        self.assertEqual(code, 1)
        self.assertIn("local files were kept", out)
        self.assertTrue((paths.MAPPINGS_DIR / "b1.mapping.json").exists())
        client.sticky_graphs.clear()
        code, out = self.purge(client)
        self.assertEqual(code, 0, out)
        self.assertFalse((paths.MAPPINGS_DIR / "b1.mapping.json").exists())
        self.assertFalse((paths.RUNS_DIR / "b1").exists())
        self.assertFalse((self.uploads / "projects" / "proj_1").exists())
        self.assertTrue((self.uploads / "projects" / "other").exists())
        self.assertTrue((paths.STATE_DIR / "batches" / "b1" / "purge.jsonl").exists())
        self.assertIn("nothing left", self.purge(client)[1])

    def test_a_running_stack_blocks_local_deletion_but_not_keep_local(self):
        self.make_batch_files()
        zep.ZepResources("b1").record("b1_pilot", "proj_1", [G1], [])
        code, out = self.purge(FakeZep(graphs={G1}), stack_running=lambda: True)
        self.assertEqual(code, 1)
        self.assertIn("stop the stack first", out)
        code, out = self.purge(FakeZep(graphs={G1}), stack_running=lambda: True, keep_local=True)
        self.assertEqual(code, 0, out)
        self.assertTrue((paths.MAPPINGS_DIR / "b1.mapping.json").exists())


class CliTests(unittest.TestCase):
    def test_every_spec_command_parses(self):
        parser = cli.build_parser()
        for argv in (["bootstrap", "--skip-assets"], ["doctor"], ["start", "--no-frontend", "--foreground"],
                     ["stop"], ["plan", "--batch", "b1"], ["run", "--batch", "b1", "--replan"], ["netcheck"],
                     ["purge", "--batch", "b1", "--keep-local"], ["privacy_report", "--confirm"],
                     ["variants", "sample", "--batch", "b1", "--n", "3"], ["factguard", "--run", RUN, "--no-llm"],
                     ["compare", "--batch", "b1", "--passes", "1"], ["keys", "status"]):
            self.assertTrue(callable(parser.parse_args(argv).func), argv)

    def test_help_hides_the_internal_sampler_and_pass_through_tools_parse_their_own_options(self):
        text = cli.build_parser().format_help()
        self.assertNotIn("_netsample", text)
        self.assertNotIn("SUPPRESS", text)
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as caught:
            cli.main(["pseudonymize", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--terms", out.getvalue())


if __name__ == "__main__":
    unittest.main()
