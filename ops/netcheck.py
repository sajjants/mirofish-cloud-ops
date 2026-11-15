"""netcheck: prove from evidence that the stack only reached loopback, Gemini (relay) and Zep (MiroFish).

Two independent sources, both written while the stack runs (start turns them on every time):
  audit log   every socket connect, sendto and DNS lookup made from Python code in the relay, the
              backend and the simulation subprocesses (ops/runtime/sitecustomize.py), tagged with the
              process group, pid and thread
  samples     the open internet sockets of every stack process and its descendants, polled with
              psutil, which also sees sockets opened by native code and by the frontend (Node)

A connect to an IP address is attributed to the hostname most recently looked up in the same
process and thread (that is how HTTP clients connect). It FAILS on: a lookup or connection outside
the group's allowlist, a socket the audit log cannot explain, a non-loopback listener, or missing
evidence (no audit lines from a group that must have made calls, no sampler heartbeat), because a
clean result without evidence proves nothing.
"""

from __future__ import annotations

import ipaddress
import json
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import psutil

from ops import paths

ALLOWED_HOSTS_FILE = paths.CONFIG_DIR / "allowed_hosts.txt"
GROUPS = ("relay", "mirofish", "frontend")
LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
LOOKUP_EVENTS = {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}
SEND_EVENTS = {"socket.connect", "socket.sendto", "socket.sendmsg"}
SIMULATION_PROCESS = re.compile(r"^run_\w*simulation\w*\.py$")
HEARTBEAT_SECONDS = 30.0


def load_allowlist(path: Path = ALLOWED_HOSTS_FILE) -> Dict[str, Set[str]]:
    allowed: Dict[str, Set[str]] = {group: set() for group in GROUPS}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        entry = line.split("#", 1)[0].split()
        if not entry:
            continue
        if len(entry) != 2 or entry[0] not in GROUPS:
            raise ValueError(f"{path.name} line {number}: expected '<group> <hostname>' with group in {GROUPS}")
        allowed[entry[0]].add(entry[1].lower().rstrip("."))
    return allowed


def _clean(host: Optional[str]) -> str:
    host = (host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.split("%", 1)[0].rstrip(".")


def ip_of(host: Optional[str]) -> Optional[ipaddress._BaseAddress]:
    try:
        address = ipaddress.ip_address(_clean(host))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def is_loopback(host: Optional[str]) -> bool:
    if host is None or _clean(host) in LOOPBACK_NAMES or _clean(host) == "":
        return True
    address = ip_of(host)
    return bool(address and address.is_loopback)


def read_jsonl(path: Optional[Path]) -> List[Dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def evaluate_audit(records: Iterable[Dict[str, Any]], allowed: Dict[str, Set[str]]
                   ) -> Tuple[List[Dict[str, Any]], Set[Tuple[int, str]], Dict[str, Set[str]]]:
    """Returns (violations, explained (pid, ip) pairs, reached hostnames per group)."""
    last_lookup: Dict[Tuple[Any, Any], str] = {}
    violations: List[Dict[str, Any]] = []
    explained: Set[Tuple[int, str]] = set()
    reached: Dict[str, Set[str]] = {}
    for record in records:
        if record.get("family") == "unix":
            continue
        role = record.get("role") or "unknown"
        hosts = allowed.get(role, set())
        event, host = record.get("event"), record.get("host")
        key = (record.get("pid"), record.get("tid"))
        if event in LOOKUP_EVENTS:
            if host is None or is_loopback(host):
                continue
            if ip_of(host) is None:
                last_lookup[key] = _clean(host)
                if _clean(host) not in hosts:
                    violations.append({**record, "why": f"looked up a host not allowed for group {role}"})
            continue
        if event not in SEND_EVENTS or is_loopback(host):
            continue
        address = ip_of(host)
        name = _clean(host) if address is None else last_lookup.get(key)
        if name in hosts:
            reached.setdefault(role, set()).add(name)
            if address is not None:
                explained.add((record.get("pid"), str(address)))
        else:
            why = (f"connected to {name}, not allowed for group {role}" if name else
                   "connected to an IP address that no allowed lookup in the same thread explains")
            violations.append({**record, "why": why})
    return violations, explained, reached


def evaluate_samples(samples: Iterable[Dict[str, Any]], explained: Set[Tuple[int, str]],
                     listen_any: bool = False) -> List[Dict[str, Any]]:
    violations, seen = [], set()
    for sample in samples:
        if sample.get("heartbeat"):
            continue
        if sample.get("status") == "LISTEN":
            if not listen_any and not is_loopback(sample.get("laddr")):
                bad = "listens on a non-loopback address"
            else:
                continue
        else:
            remote = sample.get("raddr")
            if not remote or is_loopback(remote):
                continue
            if (sample.get("pid"), str(ip_of(remote) or remote)) in explained:
                continue
            bad = "socket to a remote address the audit log does not explain (native code, Node, or not allowed)"
        key = (sample.get("pid"), sample.get("laddr"), sample.get("lport"), sample.get("raddr"),
               sample.get("rport"), sample.get("status"))
        if key not in seen:
            seen.add(key)
            violations.append({**sample, "why": bad})
    return violations


def missing_evidence(audit: List[Dict[str, Any]], samples: List[Dict[str, Any]], expect_simulation: bool
                     ) -> List[str]:
    def loopback_connects(role: str, proc_ok) -> bool:
        return any(r.get("event") == "socket.connect" and r.get("family") != "unix" and r.get("role") == role
                   and is_loopback(r.get("host")) and proc_ok(r.get("proc") or "") for r in audit)

    missing = []
    if not audit:
        missing.append("the audit log is empty or missing (the audit hook did not run, or nothing ran yet)")
    else:
        if not loopback_connects("mirofish", lambda proc: proc == "run.py"):
            missing.append("no loopback connection logged from the backend (it calls the relay for every LLM step)")
        if expect_simulation and not loopback_connects("mirofish", SIMULATION_PROCESS.match):
            missing.append("no loopback connection logged from a simulation process")
        if not any(r.get("role") == "relay" for r in audit):
            missing.append("no audit lines from the relay (its audit hook did not run)")
    if not any(s.get("heartbeat") and s.get("pids") for s in samples):
        missing.append("no socket-sampler heartbeat (the psutil sampler did not run)")
    return missing


def evaluate(audit_path: Optional[Path], samples_path: Optional[Path], allowed: Dict[str, Set[str]],
             expect_simulation: bool = True, listen_any: bool = False) -> Dict[str, Any]:
    audit, samples = read_jsonl(audit_path), read_jsonl(samples_path)
    audit_violations, explained, reached = evaluate_audit(audit, allowed)
    sample_violations = evaluate_samples(samples, explained, listen_any)
    missing = missing_evidence(audit, samples, expect_simulation)
    return {"allowed": {g: sorted(h) for g, h in allowed.items()},
            "reached": {g: sorted(h) for g, h in reached.items()},
            "audit_events": len(audit), "samples": len([s for s in samples if not s.get("heartbeat")]),
            "violations": {"audit": audit_violations, "samples": sample_violations},
            "missing_evidence": missing,
            "passed": not audit_violations and not sample_violations and not missing}


def format_result(result: Dict[str, Any]) -> str:
    lines = [f"audit events: {result['audit_events']}; socket samples: {result['samples']}"]
    for group in GROUPS:
        allowed = ", ".join(result["allowed"].get(group) or []) or "loopback only"
        reached = ", ".join(result["reached"].get(group) or []) or "-"
        lines.append(f"  {group:9} allowed: {allowed:40} reached: {reached}")
    if result["passed"]:
        lines.append("PASS: every connection went to loopback or to the group's allowed host")
        return "\n".join(lines)
    lines.append("FAIL:")
    lines += [f"  evidence: {reason}" for reason in result["missing_evidence"]]
    for r in result["violations"]["audit"]:
        lines.append(f"  audit:   [{r.get('role')}] {r.get('proc')} pid {r.get('pid')} {r.get('event')} "
                     f"{r.get('host')}:{r.get('port')}: {r['why']}")
    for s in result["violations"]["samples"]:
        lines.append(f"  socket:  [{s.get('role')}] {s.get('name')} pid {s.get('pid')} {s.get('laddr')}:{s.get('lport')}"
                     f" -> {s.get('raddr')}:{s.get('rport')} {s.get('status')}: {s['why']}")
    return "\n".join(lines)


# ----- the sampler (a background process started by `start`)
def _tree(pid: int, create_time: float) -> List[psutil.Process]:
    try:
        root = psutil.Process(pid)
        if abs(root.create_time() - create_time) > 0.01:
            return []
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def _address(addr: Any) -> Optional[str]:
    return f"{addr.ip}:{addr.port}" if addr else None


def sample_once(roots: Dict[str, Tuple[int, float]], seen: Set[Tuple], now: float) -> Tuple[List[Dict], int]:
    rows, count = [], 0
    for role, (pid, create_time) in roots.items():
        for process in _tree(pid, create_time):
            count += 1
            try:
                connections = process.net_connections(kind="inet")
                name = process.name()
            except psutil.Error:
                continue
            for conn in connections:
                if not conn.raddr and conn.status != psutil.CONN_LISTEN:
                    continue
                key = (process.pid, _address(conn.laddr), _address(conn.raddr), conn.status)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"ts": now, "role": role, "pid": process.pid, "name": name,
                             "laddr": conn.laddr.ip if conn.laddr else None,
                             "lport": conn.laddr.port if conn.laddr else None,
                             "raddr": conn.raddr.ip if conn.raddr else None,
                             "rport": conn.raddr.port if conn.raddr else None, "status": conn.status})
    return rows, count


def sample_loop(out: Path, roots: Dict[str, Tuple[int, float]], interval: float = 1.0,
                stop_after: Optional[float] = None) -> int:
    """Poll until SIGTERM (or stop_after seconds), appending new sockets and periodic heartbeats."""
    stop = {"flag": False}
    if stop_after is None:
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    seen: Set[Tuple] = set()
    started = last_beat = time.time()
    first = True
    while not stop["flag"]:
        now = time.time()
        rows, count = sample_once(roots, seen, now)
        if first or now - last_beat >= HEARTBEAT_SECONDS:
            rows.append({"ts": now, "heartbeat": True, "pids": count})
            last_beat, first = now, False
        for row in rows:
            paths.append_private_line(out, json.dumps(row))
        if stop_after is not None and now - started >= stop_after:
            break
        time.sleep(interval)
    return 0


def run(expect_simulation: bool = True) -> int:
    from ops.stack import evidence_paths
    try:
        evidence = evidence_paths()
        allowed = load_allowlist()
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    result = evaluate(evidence["audit"], evidence["samples"], allowed, expect_simulation, evidence["listen_any"])
    print(f"evidence from the stack started at {evidence['started']} ({evidence['audit']})")
    print(format_result(result))
    report = paths.LOGS_DIR / f"netcheck-{time.strftime('%Y%m%d-%H%M%S')}.json"
    paths.write_private_text(report, json.dumps(result, indent=2, default=str) + "\n")
    print(f"report: {report}")
    return 0 if result["passed"] else 1


def sampler_main(argv: List[str]) -> int:
    """`python -m ops _netsample OUT ROLE:PID:CREATE_TIME ...` (internal)."""
    out, specs = Path(argv[0]), argv[1:]
    roots = {}
    for spec in specs:
        role, pid, create_time = spec.split(":", 2)
        roots[role] = (int(pid), float(create_time))
    return sample_loop(out, roots)


if __name__ == "__main__":
    sys.exit(sampler_main(sys.argv[1:]))
