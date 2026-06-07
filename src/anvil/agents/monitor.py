"""Agent monitor (Milestone 2).

Polls agent workspace files to determine completion. Completion is determined
entirely from the filesystem (agent_status.json + agent_output.json) — never
from tmux messages.

Completion criteria (delegated to AgentWorkspace.is_complete):
  1. agent_status.json schema-valid AND phase == "completed"
  2. agent_output.md exists
  3. agent_output.json exists
  4. agent_output.json attempt_id == task.attempt_id (stale guard)
  5. agent_output.json agent_id / task_type / run_id match the task
  6. agent_output.json passes agent_output schema
  7. agent_output.json "output" passes the task's output_schema

This module is intentionally I/O-only: it reads files and returns structured
results. Retry logic and event logging live in ContractLoopRunner.
"""

from __future__ import annotations

from dataclasses import dataclass

from .io import AgentTask, AgentWorkspace


@dataclass
class MonitorResult:
    """Outcome of a single completion check."""

    complete: bool
    reasons: list[str]

    @property
    def schema_errors(self) -> list[str]:
        return [r for r in self.reasons if "schema" in r or "payload" in r]


class AgentMonitor:
    """Stateless completion checker for an agent workspace."""

    def check(self, workspace: AgentWorkspace, task: AgentTask) -> MonitorResult:
        """Return the current completion status of the agent."""
        ok, reasons = workspace.is_complete(task)
        return MonitorResult(complete=ok, reasons=reasons)
