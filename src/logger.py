"""
Structured Run Logger
---------------------
Emits newline-delimited JSON (NDJSON) run logs with:
- Every agent action and its outcome
- LLM prompts (truncated) and responses
- Timing information
- Screenshot paths on failure

Design: logs are append-only, one JSON object per line.
On failure, the replay engine writes a screenshot alongside the log.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class RunLogger:
    def __init__(self, run_id: str, log_dir: Path, run_type: str = "discovery"):
        self.run_id = run_id
        self.run_type = run_type
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{run_type}_{run_id}.jsonl"
        self._file = open(self.log_path, "a", encoding="utf-8")
        self._step_start: Optional[float] = None

    def _write(self, event: str, data: Dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "run_type": self.run_type,
            "event": event,
            **data,
        }
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def run_start(self, goal: str, target: str, capability_name: Optional[str] = None) -> None:
        self._write("run_start", {
            "goal": goal,
            "target": target,
            "capability_name": capability_name,
        })

    def step_start(self, sequence: int, description: str, action_type: str) -> None:
        self._step_start = time.monotonic()
        self._write("step_start", {
            "sequence": sequence,
            "description": description,
            "action_type": action_type,
        })

    def step_success(self, sequence: int, description: str,
                     extracted: Optional[Dict[str, Any]] = None) -> None:
        duration_ms = int((time.monotonic() - self._step_start) * 1000) if self._step_start else None
        self._write("step_success", {
            "sequence": sequence,
            "description": description,
            "duration_ms": duration_ms,
            "extracted": extracted or {},
        })

    def step_failure(self, sequence: int, description: str,
                     error: str, screenshot_path: Optional[str] = None) -> None:
        duration_ms = int((time.monotonic() - self._step_start) * 1000) if self._step_start else None
        self._write("step_failure", {
            "sequence": sequence,
            "description": description,
            "duration_ms": duration_ms,
            "error": error,
            "screenshot_path": screenshot_path,
        })

    def step_skipped(self, sequence: int, description: str, reason: str) -> None:
        self._write("step_skipped", {
            "sequence": sequence,
            "description": description,
            "reason": reason,
        })

    def llm_call(self, prompt_summary: str, response_summary: str,
                 action_decided: Optional[str] = None) -> None:
        """Log LLM interaction without persisting full prompt/response (may contain PII)."""
        self._write("llm_call", {
            "prompt_summary": prompt_summary[:500],
            "response_summary": response_summary[:500],
            "action_decided": action_decided,
        })

    def business_outcome(self, outcome_code: str, message: str) -> None:
        self._write("business_outcome", {
            "outcome_code": outcome_code,
            "message": message,
        })

    def recoverable_condition(self, condition_id: str, recovery_action: str,
                               attempt: int) -> None:
        self._write("recoverable_condition", {
            "condition_id": condition_id,
            "recovery_action": recovery_action,
            "attempt": attempt,
        })

    def safety_violation(self, violation: str) -> None:
        self._write("safety_violation", {"violation": violation})

    def escalation(self, request_id: str, reason: str, reason_code: str) -> None:
        self._write("escalation", {
            "request_id": request_id,
            "reason": reason,
            "reason_code": reason_code,
        })

    def run_end(self, outcome: str, outcome_code: Optional[str] = None,
                message: str = "", outputs: Optional[Dict[str, Any]] = None) -> None:
        self._write("run_end", {
            "outcome": outcome,
            "outcome_code": outcome_code,
            "message": message,
            "outputs": outputs or {},
        })

    def close(self) -> None:
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def path(self) -> Path:
        return self.log_path
