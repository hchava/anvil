"""Agent IO contract (Milestone 2).

Each agent workspace lives at agents/{agent_id}/ inside the run directory:
  agent_task.json       - written by controller; immutable for that attempt
  agent_status.json     - written by agent; phase must reach "completed"
  agent_output.json     - written by agent; attempt_id must match current task
  agent_output.md       - written by agent; human-readable summary (required)
  agent_attempts.jsonl  - appended by controller; one record per attempt
  stdout.log            - written by launcher
  stderr.log            - written by launcher

Completion is defined as:
  agent_status.json schema-valid AND phase == "completed"
  AND agent_output.md exists
  AND agent_output.json exists
  AND agent_output.json attempt_id == current agent_task.json attempt_id
  AND agent_output.json agent_id/task_type/run_id match the current task
  AND agent_output.json passes agent_output schema validation
  AND agent_output.json "output" payload passes the task's output_schema validation

Stale outputs (attempt_id mismatch) are always rejected without further checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas_util import assert_valid, validate_artifact
from ..timeutil import now_iso


@dataclass
class AgentTask:
    """Task written by the controller into agent_task.json."""

    agent_id: str
    attempt_id: str
    task_type: str
    run_id: str
    prompt: str
    output_schema: str
    created_at: str
    context: dict[str, Any] = field(default_factory=dict)
    redaction_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "agent_id": self.agent_id,
            "attempt_id": self.attempt_id,
            "task_type": self.task_type,
            "run_id": self.run_id,
            "prompt": self.prompt,
            "output_schema": self.output_schema,
            "created_at": self.created_at,
        }
        if self.context:
            d["context"] = self.context
        if self.redaction_count:
            d["redaction_count"] = self.redaction_count
        return d


class AgentWorkspace:
    """File layout manager for one agent workspace."""

    def __init__(self, agents_root: Path, agent_id: str) -> None:
        self.agent_id = agent_id
        self.dir = agents_root / agent_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # file paths
    # ------------------------------------------------------------------ #

    @property
    def task_path(self) -> Path:
        return self.dir / "agent_task.json"

    @property
    def status_path(self) -> Path:
        return self.dir / "agent_status.json"

    @property
    def output_path(self) -> Path:
        return self.dir / "agent_output.json"

    @property
    def output_md_path(self) -> Path:
        return self.dir / "agent_output.md"

    @property
    def attempts_path(self) -> Path:
        return self.dir / "agent_attempts.jsonl"

    @property
    def stdout_path(self) -> Path:
        return self.dir / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.dir / "stderr.log"

    # ------------------------------------------------------------------ #
    # controller-side write helpers
    # ------------------------------------------------------------------ #

    def write_task(self, task: AgentTask) -> None:
        """Write agent_task.json; schema-validates before writing."""
        payload = task.to_dict()
        assert_valid("agent_task", payload)
        with self.task_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")

    def record_attempt(
        self,
        attempt_id: str,
        outcome: str,
        schema_errors: list[str] | None = None,
    ) -> None:
        """Append one attempt record to agent_attempts.jsonl."""
        record: dict[str, Any] = {
            "attempt_id": attempt_id,
            "timestamp": now_iso(),
            "outcome": outcome,
        }
        if schema_errors:
            record["schema_errors"] = schema_errors
        with self.attempts_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------------ #
    # read helpers (used by monitor / controller)
    # ------------------------------------------------------------------ #

    def read_task(self) -> dict[str, Any] | None:
        if not self.task_path.exists():
            return None
        with self.task_path.open(encoding="utf-8") as f:
            return json.load(f)

    def read_status(self) -> dict[str, Any] | None:
        if not self.status_path.exists():
            return None
        with self.status_path.open(encoding="utf-8") as f:
            return json.load(f)

    def read_output(self) -> dict[str, Any] | None:
        if not self.output_path.exists():
            return None
        with self.output_path.open(encoding="utf-8") as f:
            return json.load(f)

    def read_attempts(self) -> list[dict[str, Any]]:
        if not self.attempts_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.attempts_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    # ------------------------------------------------------------------ #
    # completion check
    # ------------------------------------------------------------------ #

    def is_complete(self, task: "AgentTask") -> tuple[bool, list[str]]:
        """Check completion; return (ok, list-of-reasons-if-not-ok).

        Checks (in order):
          1. agent_status.json schema-valid and phase == "completed"
          2. agent_output.md exists
          3. agent_output.json exists
          4. attempt_id matches (stale guard — stops further output checks if stale)
          5. agent_id / task_type / run_id match the task (identity binding)
          6. agent_output.json schema-valid
          7. inner output payload valid against task.output_schema
        """
        reasons: list[str] = []

        # --- Status checks ---
        status = self.read_status()
        if status is None:
            reasons.append("agent_status.json missing")
        else:
            status_errors = validate_artifact("agent_status", status)
            if status_errors:
                reasons.append(f"agent_status.json schema invalid: {status_errors[0]}")
            elif status.get("phase") != "completed":
                reasons.append(
                    f"agent_status.json phase is '{status.get('phase')}', expected 'completed'"
                )

        # --- agent_output.md required ---
        if not self.output_md_path.exists():
            reasons.append("agent_output.md missing")

        # --- Output checks ---
        output = self.read_output()
        if output is None:
            reasons.append("agent_output.json missing")
        else:
            out_attempt = output.get("attempt_id")
            if out_attempt != task.attempt_id:
                # Stale: unconditionally reject; skip identity and schema checks.
                reasons.append(
                    f"stale output: attempt_id '{out_attempt}' != expected '{task.attempt_id}'"
                )
            else:
                # Identity binding: output must belong to this exact task.
                if output.get("agent_id") != task.agent_id:
                    reasons.append(
                        f"output agent_id '{output.get('agent_id')}' != task agent_id '{task.agent_id}'"
                    )
                if output.get("task_type") != task.task_type:
                    reasons.append(
                        f"output task_type '{output.get('task_type')}' != task task_type '{task.task_type}'"
                    )
                if output.get("run_id") != task.run_id:
                    reasons.append(
                        f"output run_id '{output.get('run_id')}' != task run_id '{task.run_id}'"
                    )
                # Schema validation of wrapper envelope.
                env_errors = validate_artifact("agent_output", output)
                if env_errors:
                    reasons.append(f"agent_output.json schema invalid: {env_errors[0]}")
                elif task.output_schema:
                    # Validate the nested payload against the task-specific schema.
                    inner_errors = validate_artifact(task.output_schema, output.get("output", {}))
                    if inner_errors:
                        reasons.append(
                            f"output payload fails {task.output_schema} schema: {inner_errors[0]}"
                        )

        return len(reasons) == 0, reasons
