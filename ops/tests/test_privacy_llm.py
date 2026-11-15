import io
import json
import unittest
from unittest import mock

from ops import privacy
from ops.batch import RelayControl
from ops.budget import Budget
from ops.confirm import NotConfirmed
from ops.depseudonymize import depseudonymize, load_mapping, reverse_table
from ops.leakgate import LeakGateError
from ops.pseudonymize import Pseudonymizer, Term, amount_words, money_bucket
from ops.tests.fake_provider import FakeProvider, completion
from ops.tests.helpers import TERMS, RelayFixture, TempState, confirm_privacy, limits, llm_config
from ops.wrapper_llm import LLMError, WrapperLLM, require_list_of_strings


class PrivacyChecklistTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)

    def test_unconfirmed_then_confirmed(self):
        ok, message = privacy.checklist_status()
        self.assertFalse(ok)
        self.assertIn("privacy_report --confirm", message)
        with self.assertRaises(NotConfirmed):
            privacy.require_checklist()
        confirm_privacy()
        self.assertTrue(privacy.checklist_status()[0])
        privacy.require_checklist()

    def test_any_answer_but_yes_records_nothing(self):
        answers = iter(["yes", "no"])
        code = privacy.run(confirm=True, ask=lambda _p: next(answers), out=io.StringIO(), interactive=True)
        self.assertEqual(code, 2)
        self.assertFalse(privacy.checklist_file().exists())

    def test_a_changed_checklist_or_host_list_asks_again(self):
        confirm_privacy()
        with mock.patch.object(privacy, "CHECKLIST", privacy.CHECKLIST + [("new", "a new item")]):
            self.assertIn("changed", privacy.checklist_status()[1])
        with mock.patch.object(privacy, "hosts", return_value={"relay": ["other.example"]}):
            self.assertIn("hosts", privacy.checklist_status()[1])

    def test_report_links_every_provider_page_and_asserts_no_policy(self):
        text = "\n".join(privacy.report_lines("https://generativelanguage.googleapis.com/v1beta/openai/"))
        for links in privacy.LINKS.values():
            for _label, url in links:
                self.assertIn(url, text)
                self.assertTrue(url.startswith("https://"))
        self.assertIn("generativelanguage.googleapis.com", text)
        self.assertIn("api.getzep.com", text)
        self.assertIn("NOT confirmed", text)
        self.assertIn("does\n  not summarise them", text.replace("does not", "does\n  not"))
        for item_id, _text in privacy.CHECKLIST:
            self.assertTrue(item_id)
        self.assertIn("paid tier", privacy.CHECKLIST[0][1])


class WrapperLLMTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)
        self.answers = []
        self.provider = FakeProvider(lambda m, p, b: (200, {}, completion(self.answers.pop(0))))
        self.provider.__enter__()
        self.addCleanup(self.provider.__exit__)
        self.relay = RelayFixture(self, self.provider.base_url)
        self.out = io.StringIO()

    def tool(self, stage="variants", phrase="variants b1"):
        return WrapperLLM("b1", stage, relay=RelayControl(state=self.relay.state()),
                          llm=llm_config(self.provider.base_url), limits=limits(), terms=TERMS,
                          ask=lambda _p: phrase, out=self.out, interactive=True, timeout=10)

    def test_nothing_is_sent_without_the_checklist_or_the_typed_phrase(self):
        llm = self.tool()
        with self.assertRaises(NotConfirmed):
            llm.begin("Propose", ["the seed"], max_calls=3)
        confirm_privacy()
        with self.assertRaises(NotConfirmed):
            self.tool(phrase="yes").begin("Propose", ["the seed"], max_calls=3)
        with self.assertRaises(LLMError):
            llm.chat_json("s", "u", validate=lambda _v: None)
        self.assertEqual(self.provider.requests, [])

    def test_calls_are_metered_as_the_tool_stage_with_the_stage_group_model(self):
        confirm_privacy()
        self.answers = ['{"alternatives": ["a", "b"]}', '```json\n{"alternatives": ["a", "b", "c"]}\n```']
        with self.tool() as llm:
            llm.begin("Propose", ["the pseudonymised seed"], max_calls=3)
            value = llm.chat_json("system", "user text", validate=lambda v: require_list_of_strings(
                v.get("alternatives"), 3, 5))
        self.assertEqual(value["alternatives"], ["a", "b", "c"])
        self.assertEqual(llm.calls, 2)
        self.assertEqual([r["body"]["model"] for r in self.provider.requests], ["setup-m", "setup-m"])
        self.assertEqual(self.provider.requests[0]["body"]["response_format"], {"type": "json_object"})
        ledger = Budget("b1").ledger("b1_tools")
        self.assertEqual([(r["stage"], r["run_id"]) for r in ledger], [("variants", "b1_tools")] * 2)
        self.assertIn("the pseudonymised seed", self.out.getvalue())
        self.assertIn("leak gate", self.out.getvalue())

    def test_leak_gate_blocks_the_prompt_before_the_relay(self):
        confirm_privacy()
        with self.tool() as llm:
            llm.begin("Propose", ["the seed"], max_calls=3)
            for text in ("Zebulon said so", "write to someone@example.com", "call +1 415 555 0100"):
                with self.assertRaises(LeakGateError) as caught:
                    llm.chat_json("system", text, validate=lambda _v: None)
                self.assertNotIn(text, str(caught.exception))
        self.assertEqual(self.provider.requests, [])

    def test_report_stage_tools_use_the_report_model_and_invalid_answers_fail(self):
        confirm_privacy()
        self.answers = ["not json", "[1]", '{"x": 1}']
        with self.tool("compare", "compare b1") as llm:
            llm.begin("Compare", ["a digest"], max_calls=3)
            with self.assertRaises(LLMError) as caught:
                llm.chat_json("s", "u", validate=lambda v: "missing trajectory")
        self.assertIn("missing trajectory", str(caught.exception))
        self.assertEqual(len(self.provider.requests), 3)

    def test_unknown_tool_stage_is_refused(self):
        with self.assertRaises(ValueError):
            self.tool(stage="simulation")


class MoneyRangeTests(unittest.TestCase):
    def test_buckets_are_1_2_5_ranges(self):
        self.assertEqual(money_bucket(3.2e6), (2e6, 5e6))
        self.assertEqual(money_bucket(1e6), (1e6, 2e6))
        self.assertEqual(money_bucket(5e5), (5e5, 1e6))
        self.assertEqual(money_bucket(999), (500, 1000))
        self.assertEqual(money_bucket(0.5), (0.5, 1.0))
        self.assertEqual((amount_words(2e6), amount_words(2e5), amount_words(1e9), amount_words(0.5)),
                         ("2 million", "200,000", "1 billion", "0.50"))

    def pseudo(self, text):
        engine = Pseudonymizer(rng_seed=1)
        engine.add_terms([Term("person", "Elena Duarte")], text)
        return engine, engine.apply(text)

    def test_money_becomes_ranges_and_other_numbers_do_not(self):
        text = ("Budget $2.5 million; grant €450,000; savings 4.2 million dollars; 30 percent; in 2026; "
                "62 buses; 1,800 signatures.")
        engine, out = self.pseudo(text)
        self.assertIn("between $2 million and $5 million", out)
        self.assertIn("between €200,000 and €500,000", out)
        self.assertIn("between 2 million dollars and 5 million dollars", out)
        for kept in ("30 percent", "2026", "62 buses"):
            self.assertIn(kept, out)
        self.assertNotIn("1,800", out)
        kinds = {f["original"]: f["kind"] for f in engine.mapping()["figures"]}
        self.assertEqual(kinds["$2.5 million"], "money_range")
        self.assertEqual(kinds["1,800"], "scaled")
        self.assertEqual(engine.mapping()["version"], 2)

    def test_round_trip_restores_unique_ranges_and_flags_the_models_own_amounts(self):
        engine, out = self.pseudo("Elena Duarte asked for $2.5 million.")
        mapping = json.loads(json.dumps(engine.mapping()))
        report = out + " A rival plan costs $7 million."
        restored, unmapped = depseudonymize(report, mapping)
        self.assertIn("Elena Duarte asked for $2.5 million.", restored)
        self.assertEqual(unmapped, ["$7 million"])

    def test_a_range_shared_by_two_amounts_is_left_as_a_range(self):
        engine, out = self.pseudo("Costs were $2.5 million and $3 million.")
        mapping = engine.mapping()
        self.assertEqual(len({f["pseudonym"] for f in mapping["figures"]}), 1)
        self.assertNotIn("between $2 million and $5 million", reverse_table(mapping))
        restored, unmapped = depseudonymize(out, mapping)
        self.assertEqual(restored, out)
        self.assertEqual(unmapped, [])

    def test_mapping_versions(self):
        state = TempState(self)
        path = state.root / "m.json"
        path.write_text(json.dumps({"version": 1, "scale_factor": 1.1, "names": [], "figures": []}))
        self.assertEqual(load_mapping(path)["version"], 1)
        path.write_text(json.dumps({"version": 9}))
        with self.assertRaises(ValueError):
            load_mapping(path)


if __name__ == "__main__":
    unittest.main()
