"""Tests for agent IO contract (Milestone 2).

Covers: workspace layout, task write/read, attempt-id mismatch rejection,
stale output rejection, schema retry detection, is_complete logic, attempts log,
agent_status schema validation, output identity binding, agent_output.md required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.agents.io import AgentTask, AgentWorkspace
from anvil.agents.launcher import FakeAgentLauncher, StaleFakeAgentLauncher
from anvil.timeutil import now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    run_id: str = "RUN-20260606-001",
    agent_id: str = "claude-test",
    attempt_id: str = "ATT-001",
    task_type: str = "task_contract_generation",
    output_schema: str = "task_contract",
    prompt: str = "Generate a contract.",
) -> AgentTask:
    return AgentTask(
        agent_id=agent_id,
        attempt_id=attempt_id,
        task_type=task_type,
        run_id=run_id,
        prompt=prompt,
        output_schema=output_schema,
        created_at=now_iso(),
    )


def _minimal_contract(run_id: str = "RUN-20260606-001") -> dict:
    return {
        "run_id": run_id,
        "task_summary": "Add input validation to the config parser module",
        "goals": ["Validate all config fields on load"],
        "acceptance_criteria": ["All tests pass with validation enabled"],
        "status": "proposed",
    }


def _valid_status(agent_id: str = "claude-test", task_type: str = "task_contract_generation") -> dict:
    return {
        "agent_id": agent_id,
        "task": task_type,
        "phase": "completed",
        "last_checkpoint": now_iso(),
    }


def _valid_output(
    agent_id: str = "claude-test",
    attempt_id: str = "ATT-001",
    task_type: str = "task_contract_generation",
    run_id: str = "RUN-20260606-001",
) -> dict:
    return {
        "agent_id": agent_id,
        "attempt_id": attempt_id,
        "task_type": task_type,
        "run_id": run_id,
        "output": _minimal_contract(run_id),
        "produced_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Workspace creation
# ---------------------------------------------------------------------------

def test_workspace_dirs_created(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    assert ws.dir.is_dir()


def test_workspace_paths(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    assert ws.task_path == ws.dir / "agent_task.json"
    assert ws.status_path == ws.dir / "agent_status.json"
    assert ws.output_path == ws.dir / "agent_output.json"
    assert ws.attempts_path == ws.dir / "agent_attempts.jsonl"
    assert ws.stdout_path == ws.dir / "stdout.log"
    assert ws.stderr_path == ws.dir / "stderr.log"


# ---------------------------------------------------------------------------
# Task write / read
# ---------------------------------------------------------------------------

def test_write_task_creates_file(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    task = _make_task()
    ws.write_task(task)
    assert ws.task_path.exists()


def test_write_task_is_schema_valid(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    task = _make_task()
    ws.write_task(task)  # would raise ValidationError if invalid
    doc = json.loads(ws.task_path.read_text())
    assert doc["attempt_id"] == "ATT-001"
    assert doc["task_type"] == "task_contract_generation"


def test_read_task_returns_none_when_missing(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    assert ws.read_task() is None


def test_read_task_roundtrip(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    task = _make_task()
    ws.write_task(task)
    doc = ws.read_task()
    assert doc is not None
    assert doc["agent_id"] == task.agent_id
    assert doc["attempt_id"] == task.attempt_id


# ---------------------------------------------------------------------------
# is_complete — status missing / invalid / wrong phase
# ---------------------------------------------------------------------------

def test_is_complete_false_when_status_missing(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    task = _make_task()
    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("agent_status.json missing" in r for r in reasons)


def test_is_complete_false_when_status_schema_invalid(tmp_path: Path) -> None:
    """agent_status.json with missing required fields must be rejected."""
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    # phase only — missing agent_id, task, last_checkpoint
    ws.status_path.write_text(json.dumps({"phase": "completed"}) + "\n")
    task = _make_task()
    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("agent_status.json schema invalid" in r for r in reasons)


def test_is_complete_false_when_output_missing(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    task = _make_task()
    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("agent_output.json missing" in r for r in reasons)


def test_is_complete_false_when_phase_not_completed(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    status = _valid_status()
    status["phase"] = "analyzing"
    ws.status_path.write_text(json.dumps(status) + "\n")
    task = _make_task()
    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("analyzing" in r for r in reasons)


# ---------------------------------------------------------------------------
# is_complete — agent_output.md required
# ---------------------------------------------------------------------------

def test_is_complete_false_when_md_missing(tmp_path: Path) -> None:
    """agent_output.md must exist for completion."""
    run_id = "RUN-20260606-001"
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id)

    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    ws.output_path.write_text(json.dumps(_valid_output(run_id=run_id)) + "\n")
    # No agent_output.md written.

    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("agent_output.md missing" in r for r in reasons)


# ---------------------------------------------------------------------------
# is_complete — stale attempt_id
# ---------------------------------------------------------------------------

def test_is_complete_false_on_stale_attempt_id(tmp_path: Path) -> None:
    """Output from ATT-001 must be rejected when current attempt is ATT-002."""
    run_id = "RUN-20260606-001"
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id, attempt_id="ATT-002")

    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    # Output has OLD attempt_id ATT-001, but current attempt is ATT-002.
    ws.output_path.write_text(json.dumps(_valid_output(run_id=run_id)) + "\n")
    ws.output_md_path.write_text("# Agent Output\n")

    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("stale output" in r for r in reasons)


# ---------------------------------------------------------------------------
# is_complete — output identity binding
# ---------------------------------------------------------------------------

def test_is_complete_false_when_output_agent_id_wrong(tmp_path: Path) -> None:
    """Output agent_id must match the task agent_id."""
    run_id = "RUN-20260606-001"
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id, agent_id="claude-test")

    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    # Output has WRONG agent_id.
    bad_output = _valid_output(agent_id="some-other-agent", run_id=run_id)
    ws.output_path.write_text(json.dumps(bad_output) + "\n")
    ws.output_md_path.write_text("# Agent Output\n")

    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("agent_id" in r for r in reasons)


def test_is_complete_false_when_output_task_type_wrong(tmp_path: Path) -> None:
    """Output task_type must match the task task_type."""
    run_id = "RUN-20260606-001"
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id)

    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    bad_output = _valid_output(task_type="task_contract_review", run_id=run_id)
    ws.output_path.write_text(json.dumps(bad_output) + "\n")
    ws.output_md_path.write_text("# Agent Output\n")

    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("task_type" in r for r in reasons)


def test_is_complete_false_when_output_run_id_wrong(tmp_path: Path) -> None:
    """Output run_id must match the task run_id."""
    run_id = "RUN-20260606-001"
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id)

    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    bad_output = _valid_output(run_id="RUN-20260606-999")  # wrong run
    ws.output_path.write_text(json.dumps(bad_output) + "\n")
    ws.output_md_path.write_text("# Agent Output\n")

    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("run_id" in r for r in reasons)


# ---------------------------------------------------------------------------
# is_complete — positive case
# ---------------------------------------------------------------------------

def test_is_complete_true_with_valid_output(tmp_path: Path) -> None:
    run_id = "RUN-20260606-001"
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id)

    ws.status_path.write_text(json.dumps(_valid_status()) + "\n")
    ws.output_path.write_text(json.dumps(_valid_output(run_id=run_id)) + "\n")
    ws.output_md_path.write_text("# Agent Output\n")

    ok, reasons = ws.is_complete(task)
    assert ok, reasons


# ---------------------------------------------------------------------------
# attempts log
# ---------------------------------------------------------------------------

def test_record_attempt_appends_jsonl(tmp_path: Path) -> None:
    ws = AgentWorkspace(tmp_path / "agents", "agent-001")
    ws.record_attempt("ATT-001", "success")
    ws.record_attempt("ATT-002", "schema_failure", schema_errors=["goals: must have minItems 1"])

    records = ws.read_attempts()
    assert len(records) == 2
    assert records[0]["outcome"] == "success"
    assert records[1]["outcome"] == "schema_failure"
    assert records[1]["schema_errors"]


# ---------------------------------------------------------------------------
# FakeAgentLauncher integration
# ---------------------------------------------------------------------------

def test_fake_launcher_writes_workspace_files(tmp_path: Path) -> None:
    run_id = "RUN-20260606-001"
    contract = _minimal_contract(run_id)
    launcher = FakeAgentLauncher(response_fn=lambda t: contract)

    ws = AgentWorkspace(tmp_path / "agents", "claude-gen")
    task = _make_task(run_id=run_id, agent_id="claude-gen")
    ws.write_task(task)
    launcher.launch(ws, task)

    assert ws.status_path.exists()
    assert ws.output_path.exists()
    assert ws.stdout_path.exists()
    assert ws.stderr_path.exists()

    status = json.loads(ws.status_path.read_text())
    assert status["phase"] == "completed"

    output = json.loads(ws.output_path.read_text())
    assert output["attempt_id"] == "ATT-001"
    assert output["output"]["goals"] == contract["goals"]


def test_fake_launcher_writes_md_file(tmp_path: Path) -> None:
    run_id = "RUN-20260606-001"
    launcher = FakeAgentLauncher(response_fn=lambda t: _minimal_contract(run_id))
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id)
    ws.write_task(task)
    launcher.launch(ws, task)
    assert ws.output_md_path.exists()


def test_fake_launcher_enables_is_complete(tmp_path: Path) -> None:
    """FakeAgentLauncher writes all required files — is_complete returns True."""
    run_id = "RUN-20260606-001"
    launcher = FakeAgentLauncher(response_fn=lambda t: _minimal_contract(run_id))
    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id)
    ws.write_task(task)
    launcher.launch(ws, task)

    ok, reasons = ws.is_complete(task)
    assert ok, reasons


def test_stale_launcher_triggers_mismatch(tmp_path: Path) -> None:
    run_id = "RUN-20260606-001"
    launcher = StaleFakeAgentLauncher("ATT-000", _minimal_contract(run_id))

    ws = AgentWorkspace(tmp_path / "agents", "claude-test")
    task = _make_task(run_id=run_id, attempt_id="ATT-001")
    ws.write_task(task)
    launcher.launch(ws, task)

    ok, reasons = ws.is_complete(task)
    assert not ok
    assert any("stale" in r for r in reasons)
