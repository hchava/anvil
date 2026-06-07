"""Baseline capture (Milestone 1).

Runs the configured project/scope baseline commands inside the run's worktree
and records results in ``baseline_validation.json``. For pytest commands it
additionally normalizes per-test identities (test_id, status,
failure_fingerprint) into ``baseline_tests.json`` so later milestones can diff
new failures at test-identity granularity. Non-pytest command results are also
written to ``validation_results.json`` (deterministic check outputs).

No network. Commands are structured argv arrays (never shell strings), executed
with ``subprocess.run`` and a timeout.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..timeutil import now_iso

# A single pytest result line, e.g. "tests/test_x.py::test_y PASSED".
_PYTEST_LINE = re.compile(
    r"^(?P<test_id>[\w./\\:\[\]-]+::[\w\[\]./-]+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)",
)


def _is_pytest(command_array: list[str]) -> bool:
    return any(part == "pytest" or part.endswith("pytest") for part in command_array[:2])


def _fingerprint(test_id: str, status: str) -> str:
    return "sha256:" + hashlib.sha256(f"{test_id}|{status}".encode()).hexdigest()


def normalize_pytest_identities(stdout: str) -> list[dict[str, str]]:
    """Extract normalized test identities from pytest verbose output."""
    identities: list[dict[str, str]] = []
    for line in stdout.splitlines():
        match = _PYTEST_LINE.match(line.strip())
        if not match:
            continue
        test_id = match.group("test_id")
        status = match.group("status").lower()
        identities.append(
            {
                "test_id": test_id,
                "status": status,
                "failure_fingerprint": _fingerprint(test_id, status) if status in ("failed", "error") else "",
            }
        )
    return identities


@dataclass
class CommandOutcome:
    command_array: list[str]
    label: str | None
    exit_code: int
    passed: bool
    stdout: str = ""
    stderr: str = ""
    test_identities: list[dict[str, str]] = field(default_factory=list)


def run_command(command_array: list[str], cwd: Path, timeout: int = 600) -> CommandOutcome:
    """Run one baseline command inside ``cwd``. Never raises on non-zero exit."""
    label = None
    try:
        proc = subprocess.run(
            command_array,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except FileNotFoundError:
        exit_code, stdout, stderr = 127, "", f"command not found: {command_array[0]}"
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = 124, "", "timed out"

    identities = normalize_pytest_identities(stdout) if _is_pytest(command_array) else []
    return CommandOutcome(
        command_array=command_array,
        label=label,
        exit_code=exit_code,
        passed=exit_code == 0,
        stdout=stdout,
        stderr=stderr,
        test_identities=identities,
    )


def baseline_validation_dict(
    run_id: str, base_commit: str, outcomes: list[CommandOutcome], schema_version: str = "0.1.0"
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    for outcome in outcomes:
        entry: dict[str, Any] = {
            "command_array": outcome.command_array,
            "exit_code": outcome.exit_code,
            "passed": outcome.passed,
        }
        if outcome.label:
            entry["label"] = outcome.label
        commands.append(entry)
    return {
        "run_id": run_id,
        "schema_version": schema_version,
        "captured_at": now_iso(),
        "base_commit": base_commit,
        "baseline_green": all(o.passed for o in outcomes),
        "commands": commands,
    }


def validation_results_dict(
    run_id: str,
    work_order_ref: str,
    outcomes: list[CommandOutcome],
    schema_version: str = "0.1.0",
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for outcome in outcomes:
        entry: dict[str, Any] = {
            "command_array": outcome.command_array,
            "exit_code": outcome.exit_code,
            "passed": outcome.passed,
        }
        if outcome.stdout:
            entry["stdout_excerpt"] = outcome.stdout[:2000]
        if outcome.stderr:
            entry["stderr_excerpt"] = outcome.stderr[:2000]
        results.append(entry)
    return {
        "run_id": run_id,
        "schema_version": schema_version,
        "generated_at": now_iso(),
        "work_order_ref": work_order_ref,
        "overall_passed": all(o.passed for o in outcomes),
        "new_failures_vs_baseline": 0,
        "results": results,
    }


def write_baseline_tests(path: Path, outcomes: list[CommandOutcome]) -> None:
    """Persist normalized pytest identities for later baseline diffing."""
    identities: list[dict[str, str]] = []
    for outcome in outcomes:
        identities.extend(outcome.test_identities)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"tests": identities}, handle, indent=2)
        handle.write("\n")
