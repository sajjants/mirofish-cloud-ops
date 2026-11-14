"""doctor: check the Gemini key, every configured model, JSON mode, tool calling, latency and usage,
and that the Zep key is accepted.

Only the fixed synthetic prompts in ops/json_checks.py are sent; no seed or run content. The
results file (ops/state/doctor.json) holds counts and timings, never prompts, answers or keys.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO
from urllib.parse import urlsplit

from ops import paths
from ops.redact import redact
from ops.zep import ZEP_HOST, make_client, status_of
from ops.config import ConfigError, LLMConfig, StageModel, load_llm_config
from ops.limits import load_limits
from ops.json_checks import (JSON_TESTS, SYSTEM_PROMPT, TOOL_TEST_PROMPT, TOOL_TEST_TOOLS,
                             check_json_answer, check_tool_call)
from ops.keys import Key, get_key
from ops.provider import ChatResult, ProviderClient, ProviderError, RateLimiter, Stats

JSON_PASS_REQUIRED = 4   # of 5; MiroFish retries malformed JSON a few times itself
ZEP_KEY = "ZEP_API_KEY"

ClientFactory = Callable[[LLMConfig, Key, Stats], ProviderClient]


class Report:
    def __init__(self, out: TextIO):
        self.out = out
        self.failures = 0
        self.warnings = 0

    def line(self, status: str, message: str) -> None:
        if status == "FAIL":
            self.failures += 1
        elif status == "WARN":
            self.warnings += 1
        print(f"  [{status}] {message}", file=self.out)

    def info(self, message: str) -> None:
        print(f"         {message}", file=self.out)


def default_client(config: LLMConfig, key: Key, stats: Stats) -> ProviderClient:
    provider = load_limits()["provider"]
    limiter = RateLimiter(provider["max_concurrency"], provider["requests_per_minute"])
    return ProviderClient(config.base_url, key.value, retry=config.retry, timeout=config.timeout_seconds,
                          limiter=limiter, stats=stats)


def _usage_summary(results: List[ChatResult]) -> Dict[str, bool]:
    with_usage = [r.usage for r in results if r.usage]
    return {
        "usage_reported": bool(results) and len(with_usage) == len(results),
        "reasoning_tokens_reported": any("reasoning_tokens" in u for u in with_usage),
    }


def check_model(client: ProviderClient, stage: StageModel, groups: List[str],
                report: Report) -> Optional[Dict[str, object]]:
    """Run the model checks; None means stop (the key itself was rejected)."""
    label = f"{stage.model} (effort {stage.reasoning_effort or 'model default'}; used for {', '.join(groups)})"
    print(f"\nModel {label}", file=report.out)
    response = client.get_model(stage.model)
    if response.status in (401, 403):
        report.line("FAIL", f"the API key was rejected (HTTP {response.status}); check it in Google AI Studio")
        return None
    if response.status == 404:
        report.line("FAIL", f"model '{stage.model}' not found; check the name in ops/config/llm.yaml")
        return {"model": stage.model, "reachable": False}
    if response.status != 200:
        report.line("FAIL", f"model lookup failed with HTTP {response.status}")
        return {"model": stage.model, "reachable": False}
    report.line("PASS", "key accepted and model reachable")

    results: List[ChatResult] = []
    passed = 0
    for name, prompt, check in JSON_TESTS:
        result = client.chat(stage.model, [{"role": "system", "content": SYSTEM_PROMPT},
                                           {"role": "user", "content": prompt}],
                             json_mode=True, reasoning_effort=stage.reasoning_effort, temperature=0.3)
        results.append(result)
        if result.outcome != "ok":
            reason = result.error or f"{result.outcome} ({result.finish_reason or 'no reason given'})"
        else:
            reason = check_json_answer(result.content, check)
        if reason is None:
            passed += 1
        else:
            report.info(f"JSON test '{name}' failed: {reason}")
    total = len(JSON_TESTS)
    if passed == total:
        report.line("PASS", f"JSON mode: {passed}/{total} prompts returned valid, schema-correct JSON")
    elif passed >= JSON_PASS_REQUIRED:
        report.line("WARN", f"JSON mode: {passed}/{total}; MiroFish retries bad JSON, but expect some slowdowns")
    else:
        report.line("FAIL", f"JSON mode: only {passed}/{total}; this model is unlikely to complete a run")

    tool_ok: Optional[bool] = None
    if "simulation" in groups:
        result = client.chat(stage.model, [{"role": "user", "content": TOOL_TEST_PROMPT}],
                             tools=TOOL_TEST_TOOLS, reasoning_effort=stage.reasoning_effort)
        results.append(result)
        reason = (result.error or f"{result.outcome} ({result.finish_reason})") if result.outcome != "ok" \
            else check_tool_call(result.tool_calls)
        tool_ok = reason is None
        if tool_ok:
            report.line("PASS", "tool calling works (simulated agents act through tool calls)")
        else:
            report.line("FAIL", f"tool calling: {reason}")

    timings = [r.seconds for r in results if r.outcome == "ok"]
    median = round(statistics.median(timings), 2) if timings else None
    slowest = round(max(timings), 2) if timings else None
    if timings:
        report.line("PASS", f"latency: median {median}s, slowest {slowest}s over {len(timings)} calls")
    usage = _usage_summary(results)
    if usage["usage_reported"]:
        report.line("PASS", "token usage is reported on every response (needed for spend metering)")
    else:
        report.line("FAIL", "token usage missing from some responses; spend metering cannot work")
    report.info("thinking tokens are " + ("reported separately" if usage["reasoning_tokens_reported"]
                                          else "not reported separately (they may be inside completion tokens)"))
    return {
        "model": stage.model,
        "reasoning_effort": stage.reasoning_effort,
        "stage_groups": groups,
        "reachable": True,
        "json_passed": passed,
        "json_total": total,
        "tool_calling_ok": tool_ok,
        "latency_median_seconds": median,
        "latency_max_seconds": slowest,
        **usage,
    }


def check_zep(report: Report, zep_factory: Callable[[str], Any]) -> Dict[str, object]:
    """One authenticated read (the Zep project info); sends only the key."""
    print(f"\nZep Cloud ({ZEP_HOST})", file=report.out)
    key = get_key(ZEP_KEY, ZEP_KEY)
    if key is None:
        report.line("FAIL", f"no Zep key: set {ZEP_KEY} or run `python -m ops keys set {ZEP_KEY}`; runs need it")
        return {"key": False}
    try:
        zep_factory(key.value).project.get()
    except Exception as error:  # noqa: BLE001 - SDK API errors carry a status; network errors do not
        status = status_of(error)
        if status in (401, 403):
            report.line("FAIL", f"the Zep key was rejected (HTTP {status})")
        else:
            report.line("FAIL", f"could not reach Zep: {redact(type(error).__name__ + ' ' + str(status or ''))}")
        return {"key": True, "reachable": False, "status": status}
    report.line("PASS", f"Zep key accepted ({key.source})")
    report.info("Before a real run: read Zep's current retention and deletion terms (`python -m ops privacy_report`)")
    return {"key": True, "reachable": True}


def run(out: TextIO = sys.stdout, client_factory: ClientFactory = default_client,
        config_dir: Path = paths.CONFIG_DIR, zep_factory: Callable[[str], Any] = make_client) -> int:
    report = Report(out)
    print("doctor: checking the Gemini setup", file=out)
    try:
        config = load_llm_config(config_dir)
    except ConfigError as error:
        report.line("FAIL", str(error))
        return 1
    missing = config.placeholders()
    if missing:
        report.line("FAIL", f"model names are still placeholders for: {', '.join(missing)}; "
                            "set them in ops/config/llm.yaml (copy llm.example.yaml)")
        return 1
    key = get_key(config.api_key_env, config.api_key_env)
    if key is None:
        report.line("FAIL", f"no API key: set the {config.api_key_env} environment variable, or store it in "
                            f"the OS keychain with `python -m ops keys set {config.api_key_env}`")
        return 1
    report.line("PASS", f"API key found ({key.source})")
    host = urlsplit(config.base_url).hostname
    report.info(f"sending only fixed synthetic test prompts to {host}; no seed or run content")

    stats = Stats()
    client = client_factory(config, key, stats)
    models: List[Dict[str, object]] = []
    started = time.monotonic()
    try:
        for _group, stage in config.distinct_models():
            groups = [g for g, s in config.stages.items() if s == stage]
            result = check_model(client, stage, groups, report)
            if result is None:
                break
            models.append(result)
    except ProviderError as error:
        report.line("FAIL", str(error))

    counters = stats.to_dict()
    print("\nProvider behaviour", file=out)
    report.info(f"{counters['requests']} requests, {counters['retries']} retries, "
                f"{counters['refusals']} refusals, {counters['empty']} empty answers, {counters['failed']} failed")
    if counters["rate_limited"]:
        report.line("WARN", f"{counters['rate_limited']} rate-limit (HTTP 429) responses during a handful of tiny "
                            "requests: this looks like a low-quota key, possibly free tier. A run makes thousands "
                            "of calls. Use a paid-tier key with billing enabled.")
    report.info("Before a real run: use a paid-tier key with billing enabled, and read Google's current "
                "data-use terms for paid and unpaid use: https://ai.google.dev/gemini-api/terms")
    zep = check_zep(report, zep_factory)

    paths.write_private_text(paths.STATE_DIR / "doctor.json", json.dumps({
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url_host": host,
        "key_source": key.source,
        "models": models,
        "provider_stats": counters,
        "zep": zep,
        "seconds": round(time.monotonic() - started, 1),
        "failures": report.failures,
        "warnings": report.warnings,
    }, indent=2) + "\n")

    verdict = "FAILED" if report.failures else ("passed with warnings" if report.warnings else "passed")
    print(f"\ndoctor {verdict}: {report.failures} failure(s), {report.warnings} warning(s)", file=out)
    return 1 if report.failures else 0
