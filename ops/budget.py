"""A batch's spend and call budget: reservations, the 90% warning, the 100% kill switch, and the ledger.

State lives in ops/state/batches/<batch>/ (owner-only): budget.json holds the running totals and
ledger.jsonl one line per call. Neither ever holds prompt or response text. Totals persist across
resumes; a cap trip clears only when the caps in force are raised above the totals (C1). A leak-gate
trip never clears: that batch is finished.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ops import paths

WARN_FRACTION = 0.9
BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
LEAK_TRIP = "leak_gate"


def batch_state_dir(batch_id: str) -> Path:
    if not BATCH_ID.fullmatch(batch_id or ""):
        raise ValueError(f"invalid batch id {batch_id!r}: letters, digits and '-' only (max 40)")
    return paths.STATE_DIR / "batches" / batch_id


class Budget:
    def __init__(self, batch_id: str, state_dir: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.batch_id = batch_id
        self.dir = paths.make_private_dir(state_dir or batch_state_dir(batch_id))
        self.state_path = self.dir / "budget.json"
        self.ledger_path = self.dir / "ledger.jsonl"
        self._clock = clock
        self._lock = threading.RLock()
        self.reserved = 0.0
        self.run_reserved: Dict[str, float] = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self.state = {"batch_id": batch_id, "spend": 0.0, "calls": 0, "attempts": 0, "runs": {},
                          "warnings": [], "trip": None}
        self.caps = {"max_spend": float("inf"), "max_llm_calls": float("inf")}
        self.run_caps: Dict[str, float] = {}

    # ----- caps
    def set_caps(self, max_spend: float, max_llm_calls: int, run_id: Optional[str] = None,
                 run_max_spend: Optional[float] = None) -> None:
        """Apply the caps now in force; clears a cap trip if they now allow more."""
        with self._lock:
            self.caps = {"max_spend": float(max_spend), "max_llm_calls": int(max_llm_calls)}
            if run_id is not None:
                if run_max_spend is None:
                    self.run_caps.pop(run_id, None)
                else:
                    self.run_caps[run_id] = float(run_max_spend)
            trip = self.state.get("trip")
            if trip and trip["reason"] != LEAK_TRIP and self._cap_reached(run_id) is None:
                self.state["trip"] = None
                self._event({"event": "trip_cleared", "previous": trip["reason"]})
            self._save()

    def _run(self, run_id: str) -> Dict[str, Any]:
        return self.state["runs"].setdefault(run_id, {"spend": 0.0, "calls": 0})

    def _cap_reached(self, run_id: Optional[str]) -> Optional[str]:
        if self.state["calls"] >= self.caps["max_llm_calls"]:
            return "max_llm_calls"
        if self.state["spend"] >= self.caps["max_spend"]:
            return "max_spend"
        if run_id in self.run_caps and self._run(run_id)["spend"] >= self.run_caps[run_id]:
            return "pilot.max_spend"
        return None

    # ----- per call
    def admit(self, run_id: str, reservation: float) -> Optional[str]:
        """Reserve a call's worst-case cost; return the refusal reason if it would break a cap."""
        with self._lock:
            trip = self.state.get("trip")
            if trip:
                return trip["reason"]
            reason = None
            run = self._run(run_id)
            run_reserved = self.run_reserved.get(run_id, 0.0)
            if self.state["calls"] + 1 > self.caps["max_llm_calls"]:
                reason = "max_llm_calls"
            elif self.state["spend"] + self.reserved + reservation > self.caps["max_spend"]:
                reason = "max_spend"
            elif run_id in self.run_caps and run["spend"] + run_reserved + reservation > self.run_caps[run_id]:
                reason = "pilot.max_spend"
            if reason:
                self.trip(reason)
                return reason
            self.state["calls"] += 1
            run["calls"] += 1
            self.reserved += reservation
            self.run_reserved[run_id] = run_reserved + reservation
            return None

    def settle(self, run_id: str, reservation: float, cost: float, record: Dict[str, Any]) -> None:
        """Replace a reservation by the real cost, write the ledger line, apply the thresholds."""
        with self._lock:
            self.reserved = max(0.0, self.reserved - reservation)
            self.run_reserved[run_id] = max(0.0, self.run_reserved.get(run_id, 0.0) - reservation)
            run = self._run(run_id)
            self.state["spend"] += cost
            self.state["attempts"] += int(record.get("attempts") or 0)
            run["spend"] += cost
            self.record(dict(record, run_id=run_id, cost=round(cost, 8)))
            self._thresholds(run_id)
            self._save()

    def record(self, line: Dict[str, Any]) -> None:
        with self._lock:
            entry = {"ts": round(self._clock(), 3), "batch_id": self.batch_id, **line}
            paths.append_private_line(self.ledger_path, json.dumps(entry, sort_keys=True))

    def _thresholds(self, run_id: str) -> None:
        checks = [("max_spend", self.state["spend"], self.caps["max_spend"]),
                  ("max_llm_calls", self.state["calls"], self.caps["max_llm_calls"])]
        if run_id in self.run_caps:
            checks.append(("pilot.max_spend", self._run(run_id)["spend"], self.run_caps[run_id]))
        for name, used, cap in checks:
            if cap == float("inf") or cap <= 0:
                continue
            if used >= cap:
                self.trip(name)
            elif used >= WARN_FRACTION * cap and name not in {w["limit"] for w in self.state["warnings"]}:
                warning = {"limit": name, "used": round(used, 6), "cap": cap, "at": round(self._clock(), 3)}
                self.state["warnings"].append(warning)
                self._event({"event": "warning_90", **warning})

    # ----- trips
    def trip(self, reason: str) -> None:
        with self._lock:
            if self.state.get("trip"):
                return
            self.state["trip"] = {"reason": reason, "at": round(self._clock(), 3)}
            self._event({"event": "trip", "reason": reason})
            self._save()

    def tripped(self) -> Optional[str]:
        with self._lock:
            trip = self.state.get("trip")
            return trip["reason"] if trip else None

    def _event(self, line: Dict[str, Any]) -> None:
        self.record(line)

    # ----- persistence and reporting
    def _save(self) -> None:
        paths.write_private_text(self.state_path, json.dumps(self.state, indent=2, sort_keys=True) + "\n")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            caps = {k: (None if v == float("inf") else v) for k, v in self.caps.items()}
            return {**json.loads(json.dumps(self.state)), "reserved": round(self.reserved, 8),
                    "caps": caps, "run_caps": dict(self.run_caps)}

    def ledger(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        rows = []
        with open(self.ledger_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if "event" not in row and (run_id is None or row.get("run_id") == run_id):
                    rows.append(row)
        return rows
