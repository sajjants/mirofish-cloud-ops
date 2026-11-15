import json
import os
import socket
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import psutil

from ops import bootstrap, netcheck, paths, stack, upstream
from ops.tests.helpers import TempState

ALLOWED = {"relay": {"generativelanguage.googleapis.com"}, "mirofish": {"api.getzep.com"}, "frontend": set()}


def audit(role, event, host, pid=10, tid=1, proc="run.py", port=443, family="inet"):
    return {"role": role, "event": event, "host": host, "pid": pid, "tid": tid, "proc": proc, "port": port,
            "family": family}


def healthy_audit():
    return [audit("relay", "socket.getaddrinfo", "generativelanguage.googleapis.com", pid=1, proc="ops"),
            audit("relay", "socket.connect", "142.250.1.1", pid=1, proc="ops"),
            audit("mirofish", "socket.connect", "127.0.0.1", pid=10, port=5011),
            audit("mirofish", "socket.getaddrinfo", "api.getzep.com", pid=10),
            audit("mirofish", "socket.connect", "34.1.2.3", pid=10),
            audit("mirofish", "socket.connect", "127.0.0.1", pid=20, proc="run_parallel_simulation.py", port=5011)]


class AuditTests(unittest.TestCase):
    def test_allowed_hosts_per_group_pass_and_are_reported(self):
        violations, explained, reached = netcheck.evaluate_audit(healthy_audit(), ALLOWED)
        self.assertEqual(violations, [])
        self.assertEqual(reached, {"relay": {"generativelanguage.googleapis.com"}, "mirofish": {"api.getzep.com"}})
        self.assertIn((1, "142.250.1.1"), explained)

    def test_a_host_allowed_for_another_group_is_a_violation(self):
        records = [audit("mirofish", "socket.getaddrinfo", "generativelanguage.googleapis.com"),
                   audit("mirofish", "socket.connect", "142.250.1.1")]
        violations, _, _ = netcheck.evaluate_audit(records, ALLOWED)
        self.assertEqual(len(violations), 2)
        self.assertIn("not allowed for group mirofish", violations[0]["why"])

    def test_unexplained_ip_and_lookups_in_another_thread_fail(self):
        records = [audit("relay", "socket.getaddrinfo", "generativelanguage.googleapis.com", tid=1),
                   audit("relay", "socket.connect", "142.250.1.1", tid=2),
                   audit("mirofish", "socket.getaddrinfo", "huggingface.co")]
        violations, explained, _ = netcheck.evaluate_audit(records, ALLOWED)
        self.assertEqual([v["why"][:30] for v in violations],
                         ["connected to an IP address tha", "looked up a host not allowed f"])
        self.assertEqual(explained, set())

    def test_loopback_and_unix_sockets_are_ignored(self):
        records = [audit("mirofish", "socket.connect", "::ffff:127.0.0.1"),
                   audit("mirofish", "socket.connect", "localhost"),
                   audit("mirofish", "socket.connect", "/tmp/sock", family="unix")]
        self.assertEqual(netcheck.evaluate_audit(records, ALLOWED)[0], [])

    def test_allowlist_file_format(self):
        self.assertEqual(netcheck.load_allowlist()["relay"], {"generativelanguage.googleapis.com"})
        self.assertEqual(netcheck.load_allowlist()["mirofish"], {"api.getzep.com"})
        state = TempState(self)
        bad = state.root / "hosts.txt"
        bad.write_text("everyone example.com\n")
        with self.assertRaises(ValueError):
            netcheck.load_allowlist(bad)


class SampleAndEvidenceTests(unittest.TestCase):
    def test_sockets_need_an_audit_explanation_and_listeners_loopback(self):
        samples = [{"pid": 1, "raddr": "142.250.1.1", "status": "ESTABLISHED"},
                   {"pid": 7, "raddr": "93.184.216.34", "status": "ESTABLISHED", "name": "node"},
                   {"pid": 7, "laddr": "0.0.0.0", "lport": 3000, "status": "LISTEN"},
                   {"pid": 8, "laddr": "127.0.0.1", "lport": 5001, "status": "LISTEN"},
                   {"heartbeat": True, "pids": 3}]
        violations = netcheck.evaluate_samples(samples, {(1, "142.250.1.1")})
        self.assertEqual([v["pid"] for v in violations], [7, 7])
        self.assertEqual(len(netcheck.evaluate_samples(samples, {(1, "142.250.1.1")}, listen_any=True)), 1)

    def test_a_clean_result_without_evidence_fails(self):
        self.assertEqual(len(netcheck.missing_evidence([], [], True)), 2)
        missing = netcheck.missing_evidence(healthy_audit()[:2], [{"heartbeat": True, "pids": 3}], True)
        self.assertEqual(len(missing), 2)
        self.assertEqual(netcheck.missing_evidence(healthy_audit(), [{"heartbeat": True, "pids": 3}], True), [])
        self.assertEqual(len(netcheck.missing_evidence(healthy_audit()[:5], [{"heartbeat": True, "pids": 3}], True)), 1)
        self.assertEqual(netcheck.missing_evidence(healthy_audit()[:5], [{"heartbeat": True, "pids": 3}], False), [])

    def test_evaluate_reads_logs_and_formats_a_pass(self):
        state = TempState(self)
        audit_log, samples_log = state.root / "a.jsonl", state.root / "s.jsonl"
        audit_log.write_text("\n".join(json.dumps(r) for r in healthy_audit()) + "\nnot json\n")
        samples_log.write_text(json.dumps({"heartbeat": True, "pids": 4}) + "\n")
        result = netcheck.evaluate(audit_log, samples_log, ALLOWED)
        self.assertTrue(result["passed"], result)
        self.assertIn("PASS", netcheck.format_result(result))

    def test_sampler_sees_this_process_listening_on_loopback(self):
        state = TempState(self)
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            me = psutil.Process()
            rows, count = netcheck.sample_once({"relay": (me.pid, me.create_time())}, set(), 0.0)
            self.assertGreaterEqual(count, 1)
            self.assertIn(("127.0.0.1", port, "LISTEN"), {(r["laddr"], r["lport"], r["status"]) for r in rows})
            out = state.root / "samples.jsonl"
            netcheck.sample_loop(out, {"relay": (me.pid, me.create_time())}, interval=0, stop_after=0)
        rows = netcheck.read_jsonl(out)
        self.assertTrue(any(r.get("heartbeat") and r["pids"] >= 1 for r in rows))
        self.assertEqual(netcheck.evaluate_samples(rows, set()), [])

    def test_a_reused_pid_is_not_sampled(self):
        me = psutil.Process()
        self.assertEqual(netcheck.sample_once({"relay": (me.pid, me.create_time() - 100)}, set(), 0.0), ([], 0))


class StackTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)

    def test_env_file_rules(self):
        good = {"SECRET_KEY": "x" * 64, "HF_HUB_OFFLINE": "1", "LANGFUSE_ENABLED": "False"}
        self.assertEqual(stack.env_problems(good), ([], []))
        problems, warnings = stack.env_problems({**good, "ZEP_API_KEY": "", "ZEP_API_URL": "https://example.invalid",
                                                 "LLM_BOOST_API_KEY": "k", "LANGSMITH_TRACING": "true",
                                                 "SECRET_KEY": "change-me", "HF_HUB_OFFLINE": "0"})
        self.assertEqual(len(problems), 5)
        self.assertEqual(len(warnings), 1)

    def test_child_environment_drops_keys_proxies_and_tracing(self):
        base = {"PATH": "/bin", "HOME": "/home/x", "GEMINI_API_KEY": "g", "ZEP_API_KEY": "z", "HTTPS_PROXY": "p",
                "https_proxy": "p", "LANGSMITH_API_KEY": "l", "OPENAI_API_KEY": "o", "PYTHONPATH": "/x",
                "REQUESTS_CA_BUNDLE": "/ca", "MY_SECRET": "s"}
        env = stack.scrubbed_env(base, extra_names=("MY_SECRET",))
        self.assertEqual(set(env) - {"NO_PROXY", "no_proxy", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK",
                                     "ANONYMIZED_TELEMETRY", "PYTHONUNBUFFERED"}, {"PATH", "HOME"})

    def test_evidence_paths_use_the_running_or_last_stack(self):
        with self.assertRaises(ValueError):
            stack.evidence_paths()
        record = {"audit_log": "/a", "samples_log": "/s", "started_at": "t", "mode": "container", "processes": {}}
        paths.write_private_text(stack.last_stack_file(), json.dumps(record))
        self.assertEqual(stack.evidence_paths(), {"audit": Path("/a"), "samples": Path("/s"), "started": "t",
                                                  "listen_any": True})

    def test_running_roles_match_pid_and_create_time(self):
        me = psutil.Process()
        live = {"pid": me.pid, "create_time": me.create_time()}
        stale = {"pid": me.pid, "create_time": me.create_time() - 50}
        self.assertEqual(stack.running_roles({"processes": {"relay": live, "backend": stale}}), ["relay"])
        self.assertEqual(stack.running_roles(None), [])

    def test_stop_with_nothing_running_is_quiet(self):
        out = __import__("io").StringIO()
        self.assertEqual(stack.stop(out=out), 0)

    def test_container_mode_needs_both_the_flag_and_a_container(self):
        with mock.patch.dict(os.environ, {"MIROFISH_OPS_CONTAINER": "1"}), \
                mock.patch.object(stack, "in_container", return_value=False):
            self.assertFalse(stack.container_mode())
        with mock.patch.dict(os.environ, {"MIROFISH_OPS_CONTAINER": "0"}), \
                mock.patch.object(stack, "in_container", return_value=True):
            self.assertFalse(stack.container_mode())


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.root = TempState(self).root / "home"
        self.private = self.root / "Documents" / "repo" / "ops" / "runs"
        self.private.mkdir(parents=True)

    def test_a_clean_folder_passes(self):
        self.assertEqual(paths.cloud_sync_problems([self.private]), [])

    def test_a_drive_backed_up_ancestor_is_detected(self):
        (self.root / "Documents" / ".tmp.driveupload").mkdir()
        problems = paths.cloud_sync_problems([self.private, self.root / "Documents" / "repo" / "ops" / "missing"])
        self.assertEqual(len(problems), 2)
        self.assertIn(".tmp.driveupload", problems[0])

    def test_sync_paths_and_hard_linked_files_are_detected(self):
        self.assertIn("Dropbox", paths.cloud_sync_problems([self.root / "Dropbox" / "repo"])[0])
        (self.private / "a.json").write_text("{}")
        os.link(self.private / "a.json", self.root / "staged")
        self.assertIn("second hard link", paths.cloud_sync_problems([self.private])[0])

    def test_prepare_refuses_to_write_into_a_synced_folder(self):
        from ops import prepare_inputs
        with mock.patch.object(paths, "cloud_sync_problems", return_value=["X is synced"]):
            with self.assertRaises(ValueError) as caught:
                prepare_inputs.prepare("b1", Path("unused"), Path("unused"), Path("unused"))
        self.assertIn("X is synced", str(caught.exception))


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True)


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)
        self.repo = paths.UPSTREAM_DIR
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        (self.repo / "a.txt").write_text("one\n")
        git("add", "a.txt", cwd=self.repo)
        git("commit", "-q", "-m", "c1", cwd=self.repo)
        self.commit = upstream.head()
        self.patches = self.state.root / "patches"
        self.patches.mkdir()
        (self.patches / "0001-change.patch").write_text(
            "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
        patch = mock.patch.object(upstream, "PATCHES_DIR", self.patches)
        patch.start()
        self.addCleanup(patch.stop)

    def test_checkout_must_be_at_the_pin(self):
        self.assertEqual(upstream.ensure_checkout(commit=self.commit, log=lambda _m: None), self.commit)
        with self.assertRaises(upstream.UpstreamError):
            upstream.ensure_checkout(commit="0" * 40, log=lambda _m: None)

    def test_patches_apply_idempotently_and_revert(self):
        log = []
        self.assertEqual(upstream.patch_state(self.patches / "0001-change.patch"), "pending")
        self.assertTrue(upstream.apply_patches("apply", log=log.append))
        self.assertTrue(upstream.apply_patches("apply", log=log.append))
        self.assertEqual((self.repo / "a.txt").read_text(), "two\n")
        self.assertTrue(upstream.apply_patches("revert", log=log.append))
        self.assertEqual((self.repo / "a.txt").read_text(), "one\n")
        self.assertEqual([line.split()[0] for line in log], ["APPLIED", "APPLIED", "REVERTED"])

    def test_a_conflicting_checkout_is_reported(self):
        (self.repo / "a.txt").write_text("something else\n")
        log = []
        self.assertFalse(upstream.apply_patches("apply", log=log.append))
        self.assertTrue(log[0].startswith("CONFLICT"))

    def test_pin_is_read_from_upstream_md(self):
        self.assertRegex(upstream.pinned_commit(), r"^[0-9a-f]{40}$")


class WriteEnvTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState(self)
        self.template = self.state.root / "template"
        self.template.write_text("FLASK_HOST=127.0.0.1\nSECRET_KEY=change-me\nHF_HUB_OFFLINE=0\n")
        self.env = self.state.root / "mirofish.env"

    def quiet(self):
        return mock.patch("sys.stdout", new=__import__("io").StringIO())

    def test_creates_a_private_file_with_a_random_secret(self):
        with self.quiet():
            self.assertEqual(bootstrap.write_env(self.env, self.template), [])
        text = self.env.read_text()
        self.assertNotIn("change-me", text)
        self.assertRegex(text, r"SECRET_KEY=[0-9a-f]{64}\n")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)

    def test_keeps_existing_values_adds_new_keys_and_warns_about_a_stored_zep_key(self):
        self.env.write_text("FLASK_HOST=0.0.0.0\nSECRET_KEY=change-me\nZEP_API_KEY=abc\n")
        with self.quiet():
            warnings = bootstrap.write_env(self.env, self.template)
        text = self.env.read_text()
        self.assertIn("FLASK_HOST=0.0.0.0", text)
        self.assertIn("HF_HUB_OFFLINE=0", text)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("abc" not in w for w in warnings))

    def test_supported_python_matches_the_running_interpreter(self):
        self.assertIn(sys.version_info[:2], bootstrap.SUPPORTED_PYTHON)


if __name__ == "__main__":
    unittest.main()
