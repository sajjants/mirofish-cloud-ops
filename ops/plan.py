"""Cost model, low and high estimates, and the run plan.

Before a pilot, rates come from limits.yaml `estimates` ("unmeasured"). After a pilot, they come
from its ledger: calls and tokens per stage, per agent and per agent turn, plus the growth in
prompt size across rounds. Each stage group is priced with its own model.

  low  = measured rates, the agent count the pilot found in the graph, no prompt growth
  high = agents at caps.max_agents, measured prompt growth extrapolated, times estimates.high_factor

The plan checks the HIGH estimate against what is left of max_spend, max_llm_calls and
wall_clock_minutes, cutting rounds per run first (down to rounds.real_min), then runs per question
(down to 1). Every cut is listed with its reason.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, Iterable, List, Optional

from ops.config import LLMConfig
from ops.pricing import Prices
from ops.schedule import expected_turns, scale_schedule
from ops.stages import STAGE_GROUP
from ops.zep_usage import format_estimate

GROUPS = ("setup", "simulation", "report")
COST_STAGES = {"ontology": "setup", "prepare": "setup", "rounds": "simulation", "report": "report"}
PLATFORMS = 2


# ----------------------------------------------------------------------------- ledger summaries
def _p95(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 3)


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [r for r in rows if r.get("outcome") != "refused"]
    latencies = [float(r["latency_s"]) for r in rows if r.get("latency_s") is not None]
    json_rows = [r for r in rows if r.get("json_mode") and r.get("json_ok") is not None]
    tool_rows = [r for r in rows if r.get("tools") and r.get("outcome") == "ok"]
    with_calls = [r for r in tool_rows if r.get("tool_calls")]
    json_fail = sum(1 for r in json_rows if r.get("json_ok") is False)
    args_fail = sum(1 for r in with_calls if r.get("tool_args_ok") is False)
    structured = len(json_rows) + len(with_calls)
    requests = len(rows)
    return {
        "requests": requests,
        "attempts": sum(int(r.get("attempts") or 0) for r in rows),
        "retries": sum(int(r.get("retries") or 0) for r in rows),
        "rate_limited": sum(int(r.get("rate_limited") or 0) for r in rows),
        "server_errors": sum(int(r.get("server_errors") or 0) for r in rows),
        "refused_by_provider": sum(1 for r in rows if int(r.get("refusals") or 0) > 0),
        "empty_answers": sum(1 for r in rows if int(r.get("empty") or 0) > 0),
        "failed": sum(1 for r in rows if r.get("outcome") != "ok"),
        "missing_usage": sum(int(r.get("missing_usage") or 0) for r in rows),
        "truncated": sum(1 for r in rows if r.get("finish_reason") == "length"),
        "spend": round(sum(float(r.get("cost") or 0) for r in rows), 6),
        "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "reasoning_tokens": sum(int(r.get("reasoning_tokens") or 0) for r in rows),
        "latency_median_s": round(statistics.median(latencies), 3) if latencies else None,
        "latency_p95_s": _p95(latencies),
        "json_calls": len(json_rows),
        "json_failures": json_fail,
        "tool_turns": len(tool_rows),
        "tool_turns_without_action": len(tool_rows) - len(with_calls),
        "tool_argument_failures": args_fail,
        "failure_rate": round(sum(1 for r in rows if r.get("outcome") != "ok") / requests, 4) if requests else 0.0,
        "refusal_rate": round(sum(1 for r in rows if int(r.get("refusals") or 0) > 0) / requests, 4)
        if requests else 0.0,
        "structured_failure_rate": round((json_fail + args_fail) / structured, 4) if structured else 0.0,
    }


def prompt_growth_per_round(rows: List[Dict[str, Any]], rounds: int) -> float:
    """Increase in mean prompt tokens per call per round, from the first and second half of the rounds stage."""
    timed = sorted((r for r in rows if r.get("outcome") == "ok" and r.get("prompt_tokens")), key=lambda r: r["ts"])
    if len(timed) < 4 or rounds < 2:
        return 0.0
    middle = (timed[0]["ts"] + timed[-1]["ts"]) / 2
    first = [r["prompt_tokens"] for r in timed if r["ts"] <= middle]
    second = [r["prompt_tokens"] for r in timed if r["ts"] > middle]
    if not first or not second:
        return 0.0
    return round((statistics.mean(second) - statistics.mean(first)) / (rounds / 2), 3)


# ----------------------------------------------------------------------------- cost models
def unmeasured_model(limits: Dict[str, Any]) -> Dict[str, Any]:
    est = limits["estimates"]
    return {
        "source": "estimates",
        "setup": dict(est["setup"]),
        "simulation": dict(est["simulation"], prompt_growth_per_round=0.0, measured_rounds=0),
        "report": dict(est["report"]),
        "seconds": dict(est["seconds"]),
        "turns": {"schedule": None, "turns_per_round": est["agent_turns_per_round"], "at_agents": est["agents"]},
        "agents_low": est["agents"],
    }


def _per_call(summary: Dict[str, Any]) -> tuple:
    calls = summary["requests"] or 1
    return round(summary["prompt_tokens"] / calls, 1), round(summary["output_tokens"] / calls, 1)


def measured_model(pilot: Dict[str, Any], max_agents: int) -> Dict[str, Any]:
    """Rates from a pilot summary (see batch.pilot_summary)."""
    stages = pilot["stages"]
    agents = max(1, int(pilot["agents"] or 1))
    turns = float(pilot["expected_turns"] or 0)
    empty = summarize([])
    setup_rows = [s for name, s in stages.items() if STAGE_GROUP.get(name) == "setup"]
    setup_all = _merge(setup_rows)
    prepare = stages.get("prepare", empty)
    rounds = stages.get("rounds", empty)
    report = stages.get("report", empty)
    s_prompt, s_out = _per_call(setup_all)
    r_prompt, r_out = _per_call(rounds)
    p_prompt, p_out = _per_call(report)
    seconds = pilot["stage_seconds"]
    return {
        "source": "pilot",
        "setup": {"fixed_calls": setup_all["requests"] - prepare["requests"],
                  "calls_per_agent": round(prepare["requests"] / agents, 3),
                  "prompt_tokens_per_call": s_prompt, "output_tokens_per_call": s_out},
        "simulation": {"calls_per_turn": round(rounds["requests"] / turns, 3) if turns else 0.0,
                       "prompt_tokens_per_call": r_prompt, "output_tokens_per_call": r_out,
                       "prompt_growth_per_round": pilot.get("prompt_growth_per_round", 0.0),
                       "measured_rounds": pilot["rounds"]},
        "report": {"fixed_calls": report["requests"], "prompt_tokens_per_call": p_prompt,
                   "output_tokens_per_call": p_out},
        "seconds": {"fixed_per_run": round(sum(v for k, v in seconds.items() if k not in ("prepare", "rounds")), 1),
                    "per_agent": round(seconds.get("prepare", 0.0) / agents, 2),
                    "per_turn": round(seconds.get("rounds", 0.0) / turns, 2) if turns else 0.0},
        "turns": {"schedule": pilot.get("schedule"), "turns_per_round": None, "at_agents": agents},
        "agents_low": min(int(pilot.get("graph_agents") or agents), max_agents),
    }


def _merge(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = summarize([])
    for s in summaries:
        for key in ("requests", "prompt_tokens", "output_tokens"):
            out[key] += s[key]
    return out


def run_turns(model: Dict[str, Any], rounds: int, agents: int) -> float:
    turns = model["turns"]
    if turns.get("schedule"):
        return expected_turns(scale_schedule(turns["schedule"], agents), rounds, PLATFORMS)
    return float(turns["turns_per_round"]) * rounds * agents / max(1, int(turns["at_agents"]))


def run_estimate(model: Dict[str, Any], llm: LLMConfig, prices: Prices, *, rounds: int, agents: int,
                 factor: float = 1.0, growth: bool = False) -> Dict[str, Any]:
    """Calls, tokens, spend and minutes for one run, per cost-bearing stage and per stage group."""
    turns = run_turns(model, rounds, agents)
    sim = model["simulation"]
    sim_prompt = sim["prompt_tokens_per_call"]
    if growth and sim.get("prompt_growth_per_round", 0) > 0:
        sim_prompt += sim["prompt_growth_per_round"] * max(0.0, (rounds - sim.get("measured_rounds", 0)) / 2)
    plan = {
        "ontology": (model["setup"]["fixed_calls"], model["setup"]["prompt_tokens_per_call"],
                     model["setup"]["output_tokens_per_call"]),
        "prepare": (model["setup"]["calls_per_agent"] * agents, model["setup"]["prompt_tokens_per_call"],
                    model["setup"]["output_tokens_per_call"]),
        "rounds": (sim["calls_per_turn"] * turns, sim_prompt, sim["output_tokens_per_call"]),
        "report": (model["report"]["fixed_calls"], model["report"]["prompt_tokens_per_call"],
                   model["report"]["output_tokens_per_call"]),
    }
    stages: Dict[str, Dict[str, float]] = {}
    groups = {g: {"calls": 0.0, "prompt_tokens": 0.0, "output_tokens": 0.0, "spend": 0.0,
                  "model": llm.stages[g].model} for g in GROUPS}
    for stage, (calls, prompt, output) in plan.items():
        group = COST_STAGES[stage]
        price = prices.for_model(llm.stages[group].model)
        input_rate, output_rate = price.rates(int(prompt))
        calls *= factor
        entry = {"calls": calls, "prompt_tokens": calls * prompt, "output_tokens": calls * output,
                 "spend": calls * (prompt * input_rate + output * output_rate) / 1_000_000}
        stages[stage] = entry
        for key in ("calls", "prompt_tokens", "output_tokens", "spend"):
            groups[group][key] += entry[key]
    secs = model["seconds"]
    minutes = factor * (secs["fixed_per_run"] + secs["per_agent"] * agents + secs["per_turn"] * turns) / 60
    return {"rounds": rounds, "agents": agents, "turns": round(turns, 1), "stages": stages, "groups": groups,
            "calls": sum(s["calls"] for s in stages.values()), "spend": sum(s["spend"] for s in stages.values()),
            "minutes": minutes}


def low_high(model: Dict[str, Any], limits: Dict[str, Any], llm: LLMConfig, prices: Prices, rounds: int,
             agents_low: Optional[int] = None, agents_high: Optional[int] = None) -> Dict[str, Any]:
    low = run_estimate(model, llm, prices, rounds=rounds, agents=agents_low or model["agents_low"])
    high = run_estimate(model, llm, prices, rounds=rounds, agents=agents_high or limits["caps"]["max_agents"],
                        factor=limits["estimates"]["high_factor"], growth=True)
    return {"low": low, "high": high}


# ----------------------------------------------------------------------------- the plan
def make_plan(limits: Dict[str, Any], llm: LLMConfig, prices: Prices, model: Dict[str, Any],
              question_ids: List[str], unknown_count: int, used: Optional[Dict[str, float]] = None,
              batch_id: str = "batch") -> Dict[str, Any]:
    if not question_ids:
        raise ValueError("no questions to plan")
    caps, r, runs_cfg = limits["caps"], limits["rounds"], limits["runs"]
    used = used or {}
    remaining = {"spend": caps["max_spend"] - used.get("spend", 0.0),
                 "calls": caps["max_llm_calls"] - used.get("calls", 0),
                 "minutes": caps["wall_clock_minutes"] - used.get("minutes", 0.0)}
    n_questions = len(question_ids)

    def totals(rounds: int, runs: int) -> Dict[str, Any]:
        estimate = low_high(model, limits, llm, prices, rounds)
        scale = n_questions * runs
        return {side: {"calls": round(e["calls"] * scale), "spend": round(e["spend"] * scale, 4),
                       "minutes": round(e["minutes"] * scale, 1)} for side, e in estimate.items()}

    def over(rounds: int, runs: int) -> str:
        high = totals(rounds, runs)["high"]
        parts = [f"high {key} {high[key]} > {remaining[key]:.4g} left" for key in ("spend", "calls", "minutes")
                 if high[key] > remaining[key]]
        return ", ".join(parts)

    def fits(rounds: int, runs: int) -> bool:
        return not over(rounds, runs)

    high_unknown = unknown_count > runs_cfg["unknown_threshold"]
    wanted_runs = runs_cfg["per_question_high_unknown"] if high_unknown else runs_cfg["per_question"]
    wanted_rounds = min(r["real_max"], r["max_rounds"])
    rounds, runs, cuts = wanted_rounds, wanted_runs, []
    if not fits(rounds, runs):
        reason = over(rounds, runs)
        while rounds > r["real_min"] and not fits(rounds, runs):
            rounds -= 1
        if rounds != wanted_rounds:
            cuts.append(f"rounds per run {wanted_rounds} -> {rounds} ({reason})")
    while runs > 1 and not fits(rounds, runs):
        reason = over(rounds, runs)
        runs -= 1
        cuts.append(f"runs per question {runs + 1} -> {runs} ({reason})")
        best = max((x for x in range(r["real_min"], wanted_rounds + 1) if fits(x, runs)), default=rounds)
        if best != rounds:
            cuts.append(f"rounds per run {rounds} -> {best} (budget freed by the dropped run)")
            rounds = best
    feasible = fits(rounds, runs)

    warnings = []
    if model["source"] == "estimates":
        warnings.append("unmeasured: costs come from limits.yaml estimates; the pilot measures them")
    if unknown_count and runs < 2:
        warnings.append("only one run per question: UNKNOWN variants cannot be compared")
    if not feasible:
        warnings.append(f"INFEASIBLE: even {runs} run(s) x {rounds} rounds per question exceeds what is left "
                        f"({over(rounds, runs)}); raise the caps or ask fewer questions")
    planned = [{"run_id": f"{batch_id}_{q.lower()}_r{i + 1}", "question": q, "variant": i, "rounds": rounds}
               for q in question_ids for i in range(runs)]
    per_run = low_high(model, limits, llm, prices, rounds)
    return {
        "batch_id": batch_id,
        "feasible": feasible,
        "questions": list(question_ids),
        "unknown_count": unknown_count,
        "runs_per_question": runs,
        "rounds_per_run": rounds,
        "currency": prices.currency,
        "cost_source": model["source"],
        "remaining": remaining,
        "estimate": totals(rounds, runs),
        "per_run": per_run,
        "cost_model": model,
        "cuts": cuts,
        "warnings": warnings,
        "runs": planned if feasible else [],
    }


def format_plan(plan: Dict[str, Any]) -> str:
    cur = plan["currency"]
    low, high = plan["estimate"]["low"], plan["estimate"]["high"]
    left = plan["remaining"]
    lines = [
        f"Plan for {plan['batch_id']}: {len(plan['questions'])} question(s) x {plan['runs_per_question']} run(s) "
        f"x {plan['rounds_per_run']} rounds ({plan['cost_source']})",
        f"  spend {cur} {low['spend']:.2f} - {high['spend']:.2f}  (left: {cur} {left['spend']:.2f})",
        f"  LLM calls {low['calls']} - {high['calls']}  (left: {left['calls']})",
        f"  time {low['minutes']:.0f} - {high['minutes']:.0f} min  (left: {left['minutes']:.0f} min)",
        "  per run, by stage group (low - high):",
    ]
    per_run = plan["per_run"]
    for group in GROUPS:
        lo, hi = per_run["low"]["groups"][group], per_run["high"]["groups"][group]
        lines.append(f"    {group:10} {lo['model']}: {lo['calls']:.0f} - {hi['calls']:.0f} calls, "
                     f"{cur} {lo['spend']:.3f} - {hi['spend']:.3f}")
    lines.append(f"    agents {per_run['low']['agents']} - {per_run['high']['agents']}, expected agent turns "
                 f"{per_run['low']['turns']:.0f} - {per_run['high']['turns']:.0f}")
    lines += [f"  CUT: {c}" for c in plan["cuts"]]
    lines += [f"  WARNING: {w}" for w in plan["warnings"]]
    if plan.get("zep"):
        lines += [f"  {line}" for line in format_estimate(plan["zep"])]
    return "\n".join(lines)
