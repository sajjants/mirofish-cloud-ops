"""start and stop: the relay, the MiroFish backend and frontend, and the socket sampler (any OS, no shell).

start refuses to run unless the checkout is at the pinned commit with the patches applied,
mirofish/.env is safe (no Zep key on disk, no LLM_BOOST_*, no tracing), the provider host is on
the relay's allowlist and both keys are available. It then
  - writes a new relay session token and the relay URL into mirofish/.env (MiroFish's only LLM endpoint),
  - starts every process with a scrubbed environment: the relay gets only the Gemini key, the
    backend only the Zep key (in its process environment, never on disk), the frontend neither,
  - turns on the network audit and the psutil socket sampler that netcheck evaluates,
  - waits for each service, then verifies every listener is on loopback (127.0.0.1).
Nothing is sent to Gemini or Zep by start itself.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import psutil

from ops import envfile, paths, upstream
from ops.keys import get_key
from ops.redact import redact, register_secret

RELAY_PORT = 5011
FRONTEND_PORT = 3000
ROLES = ("relay", "backend", "frontend")
STOP_TIMEOUT_S = 20.0
WAIT_S = {"relay": 30.0, "backend": 180.0, "frontend": 90.0}
SCRUB_PREFIXES = ("LANGCHAIN_", "LANGSMITH_", "AGENTOPS_", "LANGFUSE_", "HELICONE_", "WANDB_", "OTEL_", "LLM_",
                  "OPENAI_", "ZEP_", "LOCAL_GRAPH_", "MIROFISH_", "FLASK_", "HF_", "TRANSFORMERS_", "TIKTOKEN_",
                  "GEMINI_", "GOOGLE_", "VITE_", "PYTHON")
SCRUB_NAMES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy", "requests_ca_bundle",
               "ssl_cert_file", "curl_ca_bundle"}
TRACING_PREFIXES = ("LANGCHAIN_", "LANGSMITH_", "AGENTOPS_", "LANGFUSE_", "HELICONE_", "WANDB_", "OTEL_EXPORTER_",
                    "TRACEROOT_")
OFF_VALUES = {"", "0", "false", "no", "off", "none"}


class StackError(RuntimeError):
    pass


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def container_mode() -> bool:
    """Inside the Docker image only: services listen on all container interfaces so the host's
    127.0.0.1-only port mappings reach them. Ignored outside a container."""
    return os.environ.get("MIROFISH_OPS_CONTAINER") == "1" and in_container()


# ----- state
def stack_file() -> Path:
    return paths.STATE_DIR / "stack.json"


def last_stack_file() -> Path:
    return paths.STATE_DIR / "last_stack.json"


def _load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_stack() -> Optional[Dict[str, Any]]:
    return _load(stack_file())


def evidence_paths() -> Dict[str, Any]:
    stack = load_stack() or _load(last_stack_file())
    if not stack:
        raise ValueError("no stack has been started yet; run `python -m ops start`, a pilot or run, then netcheck")
    return {"audit": Path(stack["audit_log"]), "samples": Path(stack["samples_log"]),
            "started": stack["started_at"], "listen_any": stack.get("mode") == "container"}


def process_of(record: Dict[str, Any]) -> Optional[psutil.Process]:
    """The recorded process if it is still the same one (pid and create time), else None."""
    try:
        process = psutil.Process(int(record["pid"]))
        if abs(process.create_time() - float(record["create_time"])) > 0.01:
            return None
        return process if process.status() != psutil.STATUS_ZOMBIE else None
    except (psutil.Error, KeyError, TypeError, ValueError):
        return None


def running_roles(stack: Optional[Dict[str, Any]]) -> List[str]:
    if not stack:
        return []
    return [role for role, record in stack.get("processes", {}).items() if process_of(record)]


# ----- checks
def scrubbed_env(base: Optional[Dict[str, str]] = None, extra_names: Tuple[str, ...] = ()) -> Dict[str, str]:
    env = {}
    for name, value in (os.environ if base is None else base).items():
        if name.upper().startswith(SCRUB_PREFIXES) or name.lower() in SCRUB_NAMES or name in extra_names:
            continue
        env[name] = value
    env.update({"NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "ANONYMIZED_TELEMETRY": "False",
                "PYTHONUNBUFFERED": "1"})
    return env


def env_problems(values: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """(problems that block start, warnings) for mirofish/.env."""
    problems, warnings = [], []
    if "ZEP_API_KEY" in values:
        problems.append("mirofish/.env contains ZEP_API_KEY; delete that line (start passes the key in the "
                        "process environment only)")
    if values.get("ZEP_API_URL"):
        problems.append("mirofish/.env sets ZEP_API_URL; remove it (only Zep Cloud is allowed)")
    boosted = sorted(k for k in values if k.startswith("LLM_BOOST_") and values[k])
    if boosted:
        problems.append(f"mirofish/.env sets {', '.join(boosted)}; boost calls would bypass the relay")
    tracing = sorted(k for k, v in values.items() if k.startswith(TRACING_PREFIXES) and v.strip().lower()
                     not in OFF_VALUES)
    if tracing:
        problems.append(f"mirofish/.env enables tracing or telemetry export: {', '.join(tracing)}")
    if values.get("SECRET_KEY", "change-me") in ("", "change-me"):
        problems.append("mirofish/.env SECRET_KEY is the placeholder; run bootstrap again")
    if values.get("HF_HUB_OFFLINE") != "1":
        warnings.append("the simulation's downloads are not cached (HF_HUB_OFFLINE is not 1): the Twitter "
                        "simulation would reach huggingface.co and netcheck would fail; run bootstrap without "
                        "--skip-assets")
    return problems, warnings


def readiness(frontend: bool, env_path: Path = envfile.ENV_PATH) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Everything start needs, checked before anything is changed."""
    from ops.config import ConfigError, load_llm_config
    from ops.leakgate import LeakGateError, load_terms
    from ops.limits import load_limits
    from ops.netcheck import load_allowlist
    from ops.pricing import load_prices

    problems: List[str] = []
    warnings: List[str] = []
    found: Dict[str, Any] = {}
    try:
        commit = upstream.pinned_commit()
        if upstream.head() != commit:
            problems.append(f"mirofish/ is missing or not at the pinned commit {commit[:12]}; run bootstrap")
        else:
            states = {p.name: upstream.patch_state(p) for p in upstream.patch_files()}
            not_applied = [name for name, state in states.items() if state != "applied"]
            if not_applied:
                problems.append(f"patches not applied: {', '.join(not_applied)}; run bootstrap")
        found["upstream"] = commit
    except (OSError, upstream.UpstreamError) as error:
        problems.append(str(error))
    if not upstream.backend_python().exists():
        problems.append("backend virtualenv missing; run bootstrap")
    if frontend and not upstream.vite_script().exists():
        problems.append("frontend dependencies missing; run bootstrap (or start with --no-frontend)")
    if not env_path.exists():
        problems.append(f"{env_path} not found; run bootstrap")
    else:
        more, warn = env_problems(envfile.values(env_path))
        problems += more
        warnings += warn
    try:
        llm = load_llm_config()
        found["llm"] = llm
        if llm.placeholders():
            problems.append(f"model names are placeholders for {', '.join(llm.placeholders())} in ops/config/llm.yaml")
        host = (urlsplit(llm.base_url).hostname or "").lower()
        if host not in load_allowlist()["relay"]:
            problems.append(f"the provider host {host} is not on the relay's allowlist (ops/config/allowed_hosts.txt)")
        gemini = get_key(llm.api_key_env, llm.api_key_env)
        if gemini is None:
            problems.append(f"no Gemini key in {llm.api_key_env} or the OS keychain "
                            f"(python -m ops keys set {llm.api_key_env})")
        found["gemini_key"] = gemini
        load_prices()
        load_limits()
    except (ConfigError, OSError, ValueError) as error:
        problems.append(str(error))
    try:
        load_terms()
    except LeakGateError as error:
        problems.append(str(error))
    zep = get_key("ZEP_API_KEY", "ZEP_API_KEY")
    if zep is None:
        problems.append("no Zep key in ZEP_API_KEY or the OS keychain (python -m ops keys set ZEP_API_KEY)")
    found["zep_key"] = zep
    synced = paths.cloud_sync_problems()
    if synced:
        problems.append("run data, mappings and tokens would be uploaded by a sync client: " + synced[0]
                        + ". Move the repository to a folder that is not synced or backed up to the cloud.")
    return problems, warnings, found


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


# ----- processes
def _open_log(path: Path):
    paths.make_private_dir(path.parent)
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8")


def spawn(command: List[str], cwd: Path, env: Dict[str, str], log_path: Path) -> psutil.Process:
    with _open_log(log_path) as log:
        log.write(f"\n--- started {datetime.now(timezone.utc).isoformat()} ---\n")
        log.flush()
        kwargs: Dict[str, Any] = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": log,
                                  "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen(command, **kwargs)
    return psutil.Process(child.pid)


def http_ok(url: str, timeout: float = 3.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, ValueError):
        return False


def wait_until(check: Callable[[], bool], process: psutil.Process, seconds: float,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.Error:
            return False
        sleep(0.5)
    return check()


def listeners(process: psutil.Process) -> List[Tuple[str, int]]:
    found = []
    for member in [process, *process.children(recursive=True)]:
        try:
            for conn in member.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN:
                    found.append((conn.laddr.ip, conn.laddr.port))
        except psutil.Error:
            continue
    return found


def terminate_tree(process: psutil.Process, timeout: float = STOP_TIMEOUT_S) -> int:
    """SIGTERM (TerminateProcess on Windows) the process and its descendants; kill what is left."""
    try:
        members = [process, *process.children(recursive=True)]
    except psutil.Error:
        return 0
    for member in members:
        try:
            member.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(members, timeout=timeout)
    for member in alive:
        try:
            member.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)
    return len(members)


def _log_tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    return redact("\n".join(text))


def start(frontend: bool = True, relay_port: int = RELAY_PORT, foreground: bool = False, out=None) -> int:
    from ops.relay import rotate_relay_state

    out = out or sys.stdout

    def say(message: str) -> None:
        print(message, file=out, flush=True)

    existing = load_stack()
    alive = running_roles(existing)
    if alive:
        say(f"FAIL: the stack is already running ({', '.join(alive)}); run `python -m ops stop` first")
        return 1
    if existing:
        say("note: the previous stack was not stopped cleanly (its processes are gone); starting fresh")
        paths.write_private_text(last_stack_file(), json.dumps(existing, indent=2) + "\n")
        stack_file().unlink(missing_ok=True)

    problems, warnings, found = readiness(frontend)
    for warning in warnings:
        say(f"WARN: {warning}")
    if problems:
        say("FAIL: start refused; nothing was started or changed:")
        for problem in problems:
            say(f"  - {problem}")
        return 1

    container = container_mode()
    bind = "0.0.0.0" if container else "127.0.0.1"
    backend_port = int(envfile.values().get("FLASK_PORT") or 5001)
    ports = {"relay": relay_port, "backend": backend_port, "frontend": FRONTEND_PORT if frontend else None}
    busy = [f"{role} {port}" for role, port in ports.items() if port and not port_free(port)]
    if busy:
        say(f"FAIL: port(s) already in use: {', '.join(busy)}; stop whatever is using them")
        return 1

    llm, gemini, zep = found["llm"], found["gemini_key"], found["zep_key"]
    relay_state = rotate_relay_state(relay_port)
    register_secret(relay_state["session_token"])
    register_secret(relay_state["control_token"])
    envfile.set_values(envfile.ENV_PATH, {
        "LLM_BASE_URL": f"http://127.0.0.1:{relay_port}/v1", "LLM_API_KEY": relay_state["session_token"],
        "LLM_MODEL_NAME": llm.stages["setup"].model, "FLASK_HOST": bind, "FLASK_DEBUG": "False"})

    stamp = time.strftime("%Y%m%d-%H%M%S")
    audit_log = paths.LOGS_DIR / f"netaudit-{stamp}.jsonl"
    samples_log = paths.LOGS_DIR / f"netsamples-{stamp}.jsonl"
    runtime = str(paths.OPS_DIR / "runtime")
    base = scrubbed_env(extra_names=(llm.api_key_env,))
    base.update({"PYTHONPATH": runtime, "MIROFISH_NETAUDIT_LOG": str(audit_log)})
    stack: Dict[str, Any] = {"mode": "container" if container else "host",
                             "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "upstream": found.get("upstream"), "env_path": str(envfile.ENV_PATH),
                             "audit_log": str(audit_log), "samples_log": str(samples_log), "ports": ports,
                             "processes": {}}
    started: List[psutil.Process] = []

    def launch(role: str, command: List[str], cwd: Path, env: Dict[str, str]) -> psutil.Process:
        log_path = paths.LOGS_DIR / f"{role}.log"
        process = spawn(command, cwd, env, log_path)
        started.append(process)
        stack["processes"][role] = {"pid": process.pid, "create_time": process.create_time(),
                                    "log": str(log_path)}
        paths.write_private_text(stack_file(), json.dumps(stack, indent=2) + "\n")
        return process

    def fail(role: str, message: str) -> int:
        say(f"FAIL: {message}")
        tail = _log_tail(paths.LOGS_DIR / f"{role}.log")
        if tail:
            say(f"last lines of ops/logs/{role}.log (keys redacted):\n{tail}")
        stop(out=out)
        return 1

    relay_env = {**base, "MIROFISH_NETAUDIT_ROLE": "relay"}
    if gemini.source.startswith("environment"):
        relay_env[llm.api_key_env] = gemini.value
    say(f"starting the relay on 127.0.0.1:{relay_port} (forwards to {urlsplit(llm.base_url).hostname})")
    relay = launch("relay", [sys.executable, "-m", "ops", "relay", "--port", str(relay_port)], paths.REPO_ROOT,
                   relay_env)
    if not wait_until(lambda: http_ok(f"http://127.0.0.1:{relay_port}/meter/health"), relay, WAIT_S["relay"]):
        return fail("relay", "the relay did not come up")

    backend_env = {**base, "MIROFISH_NETAUDIT_ROLE": "mirofish", "ZEP_API_KEY": zep.value}
    say(f"starting the MiroFish backend on {bind}:{backend_port}")
    backend = launch("backend", [str(upstream.backend_python()), "run.py"], paths.UPSTREAM_DIR / "backend",
                     backend_env)
    if not wait_until(lambda: http_ok(f"http://127.0.0.1:{backend_port}/health"), backend, WAIT_S["backend"]):
        return fail("backend", "the backend did not come up")

    if frontend:
        node = shutil.which("node")
        if node is None:
            return fail("frontend", "node not found on PATH")
        frontend_env = {**base, "MIROFISH_NETAUDIT_ROLE": "frontend", "BROWSER": "none",
                        "VITE_API_BASE_URL": f"http://127.0.0.1:{backend_port}"}
        frontend_env.pop("MIROFISH_NETAUDIT_LOG", None)
        say(f"starting the frontend on {bind}:{FRONTEND_PORT}")
        web = launch("frontend", [node, str(upstream.vite_script()), "--host", bind, "--port", str(FRONTEND_PORT),
                                  "--strictPort"], paths.UPSTREAM_DIR / "frontend", frontend_env)
        if not wait_until(lambda: http_ok(f"http://127.0.0.1:{FRONTEND_PORT}/"), web, WAIT_S["frontend"]):
            return fail("frontend", "the frontend did not come up")

    roots = [f"{role}:{r['pid']}:{r['create_time']}" for role, r in stack["processes"].items()]
    sampler = spawn([sys.executable, "-m", "ops", "_netsample", str(samples_log), *roots], paths.REPO_ROOT,
                    scrubbed_env(), paths.LOGS_DIR / "sampler.log")
    stack["processes"]["sampler"] = {"pid": sampler.pid, "create_time": sampler.create_time(),
                                     "log": str(paths.LOGS_DIR / "sampler.log")}
    paths.write_private_text(stack_file(), json.dumps(stack, indent=2) + "\n")

    exposed = []
    for role in ROLES:
        record = stack["processes"].get(role)
        process = process_of(record) if record else None
        for ip, port in (listeners(process) if process else []):
            loopback = ip in ("127.0.0.1", "::1") or ip.startswith("127.")
            if not loopback and not (container and role != "relay"):
                exposed.append(f"{role} listens on {ip}:{port}")
    if exposed:
        return fail("backend", "a service listens beyond loopback: " + "; ".join(exposed))

    say("")
    say(f"Running. Frontend http://127.0.0.1:{FRONTEND_PORT}/" if frontend else "Running (no frontend).")
    say(f"  backend API http://127.0.0.1:{backend_port}  relay http://127.0.0.1:{relay_port} (session token rotated)")
    if container:
        say("  container mode: services listen on the container's interfaces; publish them on the host "
            "as 127.0.0.1 only (compose.yml does)")
    say(f"  network audit: {audit_log.name}; socket samples: {samples_log.name} (evaluate with netcheck)")
    say(f"  logs: {paths.LOGS_DIR}")
    say("Nothing has been sent to Gemini or Zep. Uploads happen only in `pilot`/`run`, after the leak gate "
        "and your typed confirmation; the relay refuses LLM calls outside a confirmed run.")
    if foreground:
        return wait_foreground(out)
    return 0


def wait_foreground(out, poll_s: float = 2.0) -> int:
    """Keep the stack in the foreground (the container's main process) until SIGTERM or Ctrl-C."""
    def interrupt(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    code = 0
    try:
        while True:
            stack = load_stack() or {}
            gone = [role for role in ("relay", "backend") if role in stack.get("processes", {})
                    and role not in running_roles(stack)]
            if gone:
                print(f"FAIL: {', '.join(gone)} exited; stopping the stack (see ops/logs)", file=out, flush=True)
                code = 1
                break
            time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
    stop(out=out)
    return code


def stop(out=None) -> int:
    out = out or sys.stdout
    stack = load_stack()
    if not stack:
        print("not running (no ops/state/stack.json)", file=out)
        return 0
    lock = paths.STATE_DIR / "batch.lock"
    if lock.exists():
        from ops.batch import BatchLock
        holder = BatchLock(lock)._holder()
        if holder is not None:
            print(f"FAIL: a batch command is running (pid {holder}); stop it first with Ctrl-C in its terminal "
                  "(the run is marked interrupted and resumes later)", file=out)
            return 1
    for role in ("frontend", "backend", "relay", "sampler"):
        record = stack.get("processes", {}).get(role)
        process = process_of(record) if record else None
        if process is None:
            continue
        count = terminate_tree(process)
        print(f"stopped {role} (pid {record['pid']}, {count} process(es))", file=out)
    paths.write_private_text(last_stack_file(), json.dumps(stack, indent=2) + "\n")
    stack_file().unlink(missing_ok=True)
    print("stopped. Evaluate the network evidence with `python -m ops netcheck`.", file=out)
    return 0
