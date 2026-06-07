"""Agent launcher abstraction (Milestone 2).

Defines the AgentLauncher interface and a FakeAgentLauncher for automated
tests. The fake launcher writes the expected workspace files synchronously so
tests need no tmux, no network, and no real LLM.

Future launchers (TmuxAgentLauncher for real Claude/Codex) implement the same
interface. Completion is detected by AgentMonitor reading agent_status.json and
agent_output.json — never by tmux messages.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..timeutil import now_iso
from .io import AgentTask, AgentWorkspace


class AgentLauncher(ABC):
    """Interface for launching an agent into a workspace."""

    @abstractmethod
    def launch(self, workspace: AgentWorkspace, task: AgentTask) -> None:
        """Start the agent. The launcher is responsible for ensuring the agent
        will eventually write agent_status.json and agent_output.json.

        This method must return quickly (non-blocking). Completion is polled
        by AgentMonitor, not by waiting inside launch().
        """


class FakeAgentLauncher(AgentLauncher):
    """Test double: synchronously writes agent workspace files.

    ``response_fn`` receives the AgentTask and returns the dict that will be
    placed in agent_output.json's "output" field. If omitted, an empty dict
    is used (which will fail schema validation for most output schemas).

    To simulate a failure or invalid output, pass a response_fn that returns
    a dict that won't validate against the task's output_schema, OR set
    ``fail_phase`` to a non-"completed" phase string.
    """

    def __init__(
        self,
        response_fn: Callable[[AgentTask], dict[str, Any]] | None = None,
        fail_phase: str | None = None,
    ) -> None:
        self._response_fn = response_fn
        self._fail_phase = fail_phase

    def launch(self, workspace: AgentWorkspace, task: AgentTask) -> None:
        phase = self._fail_phase if self._fail_phase else "completed"

        # Write agent_status.json.
        status: dict[str, Any] = {
            "agent_id": task.agent_id,
            "task": task.task_type,
            "phase": phase,
            "last_checkpoint": now_iso(),
        }
        with workspace.status_path.open("w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
            f.write("\n")

        # Write stdout.log and stderr.log.
        workspace.stdout_path.write_text(
            f"fake agent: attempt {task.attempt_id} phase={phase}\n",
            encoding="utf-8",
        )
        workspace.stderr_path.write_text("", encoding="utf-8")

        if phase != "completed":
            # Simulate a failed run: no output file written.
            return

        output_payload = self._response_fn(task) if self._response_fn else {}

        output: dict[str, Any] = {
            "agent_id": task.agent_id,
            "attempt_id": task.attempt_id,
            "task_type": task.task_type,
            "run_id": task.run_id,
            "output": output_payload,
            "produced_at": now_iso(),
        }
        with workspace.output_path.open("w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
            f.write("\n")

        # Human-readable summary.
        workspace.output_md_path.write_text(
            f"# Agent Output\n\nattempt_id: {task.attempt_id}\ntask_type: {task.task_type}\n",
            encoding="utf-8",
        )


class StaleFakeAgentLauncher(AgentLauncher):
    """Test double that writes output with a WRONG attempt_id (stale simulation)."""

    def __init__(self, stale_attempt_id: str, output_payload: dict[str, Any]) -> None:
        self._stale_id = stale_attempt_id
        self._payload = output_payload

    def launch(self, workspace: AgentWorkspace, task: AgentTask) -> None:
        status: dict[str, Any] = {
            "agent_id": task.agent_id,
            "task": task.task_type,
            "phase": "completed",
            "last_checkpoint": now_iso(),
        }
        with workspace.status_path.open("w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
            f.write("\n")

        workspace.stdout_path.write_text(
            f"stale-fake agent: attempt {self._stale_id} (stale)\n", encoding="utf-8"
        )
        workspace.stderr_path.write_text("", encoding="utf-8")

        # Output uses the stale attempt_id, not the current one.
        output: dict[str, Any] = {
            "agent_id": task.agent_id,
            "attempt_id": self._stale_id,
            "task_type": task.task_type,
            "run_id": task.run_id,
            "output": self._payload,
            "produced_at": now_iso(),
        }
        with workspace.output_path.open("w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
            f.write("\n")
