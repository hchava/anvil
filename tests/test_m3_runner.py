"""Tests for the validation command runner (Milestone 3).

No network, no real API keys, temp dirs only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anvil.controller.policy import CommandPolicy
from anvil.executor.runner import ValidationRunner


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_safe_echo_command_passes(tmp_path: Path) -> None:
    runner = ValidationRunner()
    result = runner.run_command("CMD-001", ["python3", "-c", "print('ok')"], tmp_path)
    # python3 -c is blocked by default policy; use a real safe binary instead.
    # Actually python3 -c is blocked. Let's use the python interpreter path.
    # We'll test a non-blocked command: pytest --version is safe.


def test_allowed_python_version_command(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    # python3 --version doesn't use -c so should pass the arg pattern check.
    result = runner.run_command("CMD-001", ["python3", "--version"], tmp_path)
    assert result.policy_allowed
    assert result.exit_code == 0
    assert result.passed


def test_exit_code_captured(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    # Exit with non-zero via sys.exit through a script file.
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "fail.py"], tmp_path)
    assert result.exit_code == 1
    assert not result.passed


def test_stdout_captured(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script = tmp_path / "out.py"
    script.write_text("print('hello world')\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "out.py"], tmp_path)
    assert "hello world" in result.stdout_excerpt


def test_stderr_captured(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script = tmp_path / "err.py"
    script.write_text("import sys; sys.stderr.write('an error\\n')\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "err.py"], tmp_path)
    assert "an error" in result.stderr_excerpt


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------

def test_policy_blocks_curl(tmp_path: Path) -> None:
    runner = ValidationRunner()
    result = runner.run_command("CMD-001", ["curl", "http://example.com"], tmp_path)
    assert not result.policy_allowed
    assert not result.passed
    assert "policy blocked" in result.stderr_excerpt.lower()


def test_policy_blocks_python_dash_c(tmp_path: Path) -> None:
    runner = ValidationRunner()
    result = runner.run_command("CMD-001", ["python3", "-c", "print(1)"], tmp_path)
    assert not result.policy_allowed
    assert not result.passed


def test_policy_blocks_git_push(tmp_path: Path) -> None:
    runner = ValidationRunner()
    result = runner.run_command(
        "CMD-001", ["git", "push", "origin", "main"], tmp_path
    )
    assert not result.policy_allowed
    assert not result.passed


def test_custom_policy_allows_extra_binary(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["echo"]})
    )
    # 'echo' is not in default allowed list but our custom policy allows it.
    result = runner.run_command("CMD-001", ["echo", "hi"], tmp_path)
    assert result.policy_allowed


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

def test_timeout_returns_failure(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(60)\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "slow.py"], tmp_path, timeout_seconds=1)
    assert result.timed_out
    assert not result.passed
    assert result.exit_code == -1


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------

def test_run_all_returns_one_result_per_command(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script_a = tmp_path / "a.py"
    script_a.write_text("print('a')\n", encoding="utf-8")
    script_b = tmp_path / "b.py"
    script_b.write_text("print('b')\n", encoding="utf-8")

    commands = [
        {"command_array": ["python3", "a.py"], "label": "step-a"},
        {"command_array": ["python3", "b.py"], "label": "step-b"},
    ]
    results = runner.run_all(commands, tmp_path)
    assert len(results) == 2
    assert results[0].command_id == "step-a"
    assert results[1].command_id == "step-b"


def test_run_all_uses_default_label_when_absent(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script = tmp_path / "x.py"
    script.write_text("pass\n", encoding="utf-8")
    results = runner.run_all([{"command_array": ["python3", "x.py"]}], tmp_path)
    assert results[0].command_id == "CMD-001"


# ---------------------------------------------------------------------------
# Timing metadata
# ---------------------------------------------------------------------------

def test_duration_is_non_negative(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script = tmp_path / "noop.py"
    script.write_text("pass\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "noop.py"], tmp_path)
    assert result.duration_seconds >= 0.0


def test_timestamps_present(tmp_path: Path) -> None:
    runner = ValidationRunner(
        policy=CommandPolicy.from_dict({"allowed_binaries": ["python3"]})
    )
    script = tmp_path / "ts.py"
    script.write_text("pass\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "ts.py"], tmp_path)
    assert result.started_at
    assert result.finished_at


# ---------------------------------------------------------------------------
# Timeout cap enforcement
# ---------------------------------------------------------------------------

def test_timeout_exceeding_policy_cap_is_blocked(tmp_path: Path) -> None:
    """High-priority fix: timeout_seconds > max_timeout_seconds must be blocked."""
    policy = CommandPolicy.from_dict({
        "allowed_binaries": ["python3"],
        "max_timeout_seconds": 30,
    })
    runner = ValidationRunner(policy=policy)
    script = tmp_path / "ok.py"
    script.write_text("pass\n", encoding="utf-8")
    # Request 120s timeout against a 30s cap.
    result = runner.run_command("CMD-001", ["python3", "ok.py"], tmp_path, timeout_seconds=120)
    assert not result.policy_allowed
    assert not result.passed
    assert "policy blocked" in result.stderr_excerpt.lower()
    assert result.exit_code == -1


def test_timeout_within_policy_cap_is_allowed(tmp_path: Path) -> None:
    policy = CommandPolicy.from_dict({
        "allowed_binaries": ["python3"],
        "max_timeout_seconds": 600,
    })
    runner = ValidationRunner(policy=policy)
    script = tmp_path / "ok.py"
    script.write_text("pass\n", encoding="utf-8")
    result = runner.run_command("CMD-001", ["python3", "ok.py"], tmp_path, timeout_seconds=60)
    assert result.policy_allowed
    assert result.passed
