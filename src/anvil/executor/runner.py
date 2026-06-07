"""Validation command runner (Milestone 3).

Executes structured command arrays inside the worktree. Every command goes
through the Milestone 2 CommandPolicy before execution:
  - blocked binaries are rejected
  - blocked arg patterns are rejected
  - network commands are rejected
  - timeout_seconds is enforced

All output is captured. Stdout/stderr are truncated to MAX_OUTPUT_BYTES so
large test suites cannot exhaust memory. Test identities are extracted from
pytest verbose output so the executor can do per-test baseline diffs.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..controller.baseline import normalize_pytest_identities
from ..controller.policy import CommandPolicy, PolicyResult
from ..timeutil import now_iso

MAX_OUTPUT_BYTES = 200_000


@dataclass
class CommandResult:
    command_id: str
    command_array: list[str]
    exit_code: int
    passed: bool
    stdout_excerpt: str
    stderr_excerpt: str
    started_at: str
    finished_at: str
    duration_seconds: float
    timed_out: bool
    policy_allowed: bool
    policy_reason: str | None
    test_identities: list[dict[str, Any]] = field(default_factory=list)
    baseline_new_failures: list[str] = field(default_factory=list)


class ValidationRunner:
    def __init__(self, policy: CommandPolicy | None = None) -> None:
        self._policy = policy or CommandPolicy.from_dict({})

    def run_command(
        self,
        command_id: str,
        command_array: list[str],
        cwd: Path,
        timeout_seconds: int = 60,
    ) -> CommandResult:
        policy_result: PolicyResult = self._policy.validate(command_array)
        started_at = now_iso()

        if not policy_result.allowed:
            return CommandResult(
                command_id=command_id,
                command_array=command_array,
                exit_code=-1,
                passed=False,
                stdout_excerpt="",
                stderr_excerpt=f"[policy blocked] {policy_result.reason}",
                started_at=started_at,
                finished_at=now_iso(),
                duration_seconds=0.0,
                timed_out=False,
                policy_allowed=False,
                policy_reason=policy_result.reason,
            )

        timed_out = False
        start = time.monotonic()
        try:
            proc = subprocess.run(
                command_array,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = proc.returncode
            stdout = proc.stdout[:MAX_OUTPUT_BYTES]
            stderr = proc.stderr[:MAX_OUTPUT_BYTES]
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = (
                (exc.output or b"").decode(errors="replace")[:MAX_OUTPUT_BYTES]
                if isinstance(exc.output, bytes)
                else str(exc.output or "")[:MAX_OUTPUT_BYTES]
            )
            stderr = (
                (exc.stderr or b"").decode(errors="replace")[:MAX_OUTPUT_BYTES]
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "")[:MAX_OUTPUT_BYTES]
            )

        duration = time.monotonic() - start
        passed = exit_code == 0 and not timed_out

        is_pytest = any("pytest" in p for p in command_array[:2])
        identities = normalize_pytest_identities(stdout) if is_pytest else []

        return CommandResult(
            command_id=command_id,
            command_array=command_array,
            exit_code=exit_code,
            passed=passed,
            stdout_excerpt=stdout,
            stderr_excerpt=stderr,
            started_at=started_at,
            finished_at=now_iso(),
            duration_seconds=round(duration, 3),
            timed_out=timed_out,
            policy_allowed=True,
            policy_reason=None,
            test_identities=identities,
        )

    def run_all(
        self,
        commands: list[dict[str, Any]],
        cwd: Path,
        default_timeout: int = 60,
    ) -> list[CommandResult]:
        results: list[CommandResult] = []
        for i, cmd_spec in enumerate(commands):
            command_array = cmd_spec["command_array"]
            command_id = cmd_spec.get("label", f"CMD-{i + 1:03d}")
            timeout = cmd_spec.get("timeout_seconds", default_timeout)
            results.append(self.run_command(command_id, command_array, cwd, timeout))
        return results
