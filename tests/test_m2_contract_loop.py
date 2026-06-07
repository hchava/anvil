"""Tests for the Claude/Codex contract loop (Milestone 2).

Covers: task_contract generation success, retry on invalid output, max-retry
failure, contract review co-sign (success), request_changes/reject/disagree_lower
raise ContractBlockedError, secret redaction wired into task construction,
Codex risk disagreement handling, attempt records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from anvil.agents.launcher import FakeAgentLauncher, StaleFakeAgentLauncher
from anvil.controller.contract_loop import (
    ContractBlockedError,
    ContractLoopError,
    ContractLoopRunner,
)
from anvil.controller.events import EventLog
from anvil.timeutil import now_iso


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

RUN_ID = "RUN-20260606-001"


def _valid_contract() -> dict:
    return {
        "run_id": RUN_ID,
        "task_summary": "Add input validation to the config parser module.",
        "goals": ["Validate all config fields on load"],
        "acceptance_criteria": ["All validation tests pass"],
        "status": "proposed",
    }


def _invalid_contract() -> dict:
    # Missing required 'goals' and 'acceptance_criteria'.
    return {"run_id": RUN_ID, "task_summary": "short"}


def _valid_review(decision: str = "co-sign", agreement: str = "agree") -> dict:
    r: dict = {
        "reviewer_id": "codex-contract-rev",
        "attempt_id": "ATT-001",
        "run_id": RUN_ID,
        "decision": decision,
        "rationale": "Contract looks complete and well-scoped.",
        "reviewed_at": now_iso(),
    }
    if agreement:
        r["risk_score_agreement"] = agreement
    return r


def _invalid_review() -> dict:
    # Missing 'decision' — will fail contract_review schema.
    return {"reviewer_id": "codex-contract-rev", "run_id": RUN_ID}


def _runner(
    tmp_path: Path,
    claude_launcher: FakeAgentLauncher,
    codex_launcher: FakeAgentLauncher,
) -> ContractLoopRunner:
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    event_log = EventLog(run_dir / "event_log.jsonl")
    return ContractLoopRunner(
        run_id=RUN_ID,
        run_dir=run_dir,
        event_log=event_log,
        claude_launcher=claude_launcher,
        codex_launcher=codex_launcher,
    )


# ---------------------------------------------------------------------------
# Contract generation — success on first attempt
# ---------------------------------------------------------------------------

def test_generate_succeeds_first_attempt(tmp_path: Path) -> None:
    contract = _valid_contract()
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: contract),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    result = runner.generate_task_contract("Add validation to config parser")
    assert result["goals"] == contract["goals"]
    # artifact written to disk
    artifact = json.loads((tmp_path / "runs" / RUN_ID / "task_contract.json").read_text())
    assert artifact["goals"] == contract["goals"]


def test_generate_writes_event_log(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    runner.generate_task_contract("task")
    events = EventLog(tmp_path / "runs" / RUN_ID / "event_log.jsonl").read_all()
    types = [e["event_type"] for e in events]
    assert "agent_launched" in types
    assert "agent_completed" in types
    assert "artifact_written" in types


# ---------------------------------------------------------------------------
# Secret redaction wired into task construction
# ---------------------------------------------------------------------------

def test_generate_redacts_secrets_in_task_description(tmp_path: Path) -> None:
    """Raw secrets must not appear in agent_task.json prompt."""
    secret_description = "Add validation. Token: AKIAIOSFODNN7EXAMPLE."
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    runner.generate_task_contract(secret_description)

    task_file = (
        tmp_path / "runs" / RUN_ID / "agents" / "claude-contract-gen" / "agent_task.json"
    )
    task_doc = json.loads(task_file.read_text())
    assert "AKIAIOSFODNN7EXAMPLE" not in task_doc["prompt"]
    assert "[REDACTED]" in task_doc["prompt"]
    assert task_doc.get("redaction_count", 0) >= 1


def test_generate_increments_redaction_count(tmp_path: Path) -> None:
    """redaction_count in agent_task.json must be non-zero when secrets present."""
    # GitHub token format: ghp_ + exactly 36 alphanumeric chars
    secret_desc = "gh_token: ghp_1234567890abcdef1234567890abcdef1234"
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    runner.generate_task_contract(secret_desc)

    task_file = (
        tmp_path / "runs" / RUN_ID / "agents" / "claude-contract-gen" / "agent_task.json"
    )
    task_doc = json.loads(task_file.read_text())
    assert task_doc.get("redaction_count", 0) >= 1


def test_generate_no_redaction_when_clean(tmp_path: Path) -> None:
    """redaction_count should be 0 for benign descriptions."""
    clean_desc = "Add input validation to the config parser module."
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    runner.generate_task_contract(clean_desc)

    task_file = (
        tmp_path / "runs" / RUN_ID / "agents" / "claude-contract-gen" / "agent_task.json"
    )
    task_doc = json.loads(task_file.read_text())
    assert task_doc.get("redaction_count", 0) == 0


# ---------------------------------------------------------------------------
# Contract generation — retry on invalid output
# ---------------------------------------------------------------------------

def test_generate_retries_after_invalid_output(tmp_path: Path) -> None:
    """First attempt returns bad output; second returns valid contract."""
    call_count = 0

    def response(task: Any) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _invalid_contract()
        return _valid_contract()

    runner = _runner(tmp_path, FakeAgentLauncher(response), FakeAgentLauncher(lambda t: _valid_review()))
    result = runner.generate_task_contract("task")
    assert result["goals"]
    assert call_count == 2


def test_generate_records_attempts(tmp_path: Path) -> None:
    call_count = 0

    def response(task: Any) -> dict:
        nonlocal call_count
        call_count += 1
        return _invalid_contract() if call_count == 1 else _valid_contract()

    runner = _runner(tmp_path, FakeAgentLauncher(response), FakeAgentLauncher(lambda t: _valid_review()))
    runner.generate_task_contract("task")

    ws_dir = tmp_path / "runs" / RUN_ID / "agents" / "claude-contract-gen"
    attempts_file = ws_dir / "agent_attempts.jsonl"
    records = [json.loads(l) for l in attempts_file.read_text().splitlines() if l.strip()]
    assert len(records) == 2
    assert records[0]["outcome"] == "schema_failure"
    assert records[1]["outcome"] == "success"


# ---------------------------------------------------------------------------
# Contract generation — max retries exceeded
# ---------------------------------------------------------------------------

def test_generate_fails_after_max_retries(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _invalid_contract()),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    with pytest.raises(ContractLoopError, match="attempt"):
        runner.generate_task_contract("task")


def test_generate_logs_agent_failed_event(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _invalid_contract()),
        FakeAgentLauncher(lambda t: _valid_review()),
    )
    with pytest.raises(ContractLoopError):
        runner.generate_task_contract("task")

    events = EventLog(tmp_path / "runs" / RUN_ID / "event_log.jsonl").read_all()
    assert any(e["event_type"] == "agent_failed" for e in events)


# ---------------------------------------------------------------------------
# Contract review — co-sign (the only success path)
# ---------------------------------------------------------------------------

def test_review_cosign_succeeds(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review("co-sign")),
    )
    review = runner.review_task_contract(_valid_contract())
    assert review["decision"] == "co-sign"
    artifact = json.loads((tmp_path / "runs" / RUN_ID / "contract_review.json").read_text())
    assert artifact["decision"] == "co-sign"


# ---------------------------------------------------------------------------
# Contract review — request_changes and reject raise ContractBlockedError
# ---------------------------------------------------------------------------

def test_review_request_changes_raises_blocked(tmp_path: Path) -> None:
    """request_changes must raise ContractBlockedError, not return the review."""
    review_payload = _valid_review("request_changes")
    review_payload["requested_changes"] = ["Add rollback plan to goals"]
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: review_payload),
    )
    with pytest.raises(ContractBlockedError) as exc_info:
        runner.review_task_contract(_valid_contract())

    err = exc_info.value
    assert err.decision == "request_changes"
    assert err.review.get("decision") == "request_changes"
    assert "requested_changes" in err.review


def test_review_reject_raises_blocked(tmp_path: Path) -> None:
    """reject must raise ContractBlockedError."""
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review("reject")),
    )
    with pytest.raises(ContractBlockedError) as exc_info:
        runner.review_task_contract(_valid_contract())

    assert exc_info.value.decision == "reject"


def test_review_blocked_logs_gate_failed_event(tmp_path: Path) -> None:
    """A blocked review must log a gate_failed event."""
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review("reject")),
    )
    with pytest.raises(ContractBlockedError):
        runner.review_task_contract(_valid_contract())

    events = EventLog(tmp_path / "runs" / RUN_ID / "event_log.jsonl").read_all()
    assert any(e["event_type"] == "gate_failed" for e in events)


# ---------------------------------------------------------------------------
# Contract review — retry on invalid output
# ---------------------------------------------------------------------------

def test_review_retries_on_invalid_output(tmp_path: Path) -> None:
    call_count = 0

    def response(task: Any) -> dict:
        nonlocal call_count
        call_count += 1
        return _invalid_review() if call_count == 1 else _valid_review()

    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(response),
    )
    review = runner.review_task_contract(_valid_contract())
    assert review["decision"] == "co-sign"
    assert call_count == 2


def test_review_fails_after_max_retries(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _invalid_review()),
    )
    with pytest.raises(ContractLoopError, match="attempt"):
        runner.review_task_contract(_valid_contract())


# ---------------------------------------------------------------------------
# Codex risk disagreement: disagree_lower raises ContractBlockedError
# ---------------------------------------------------------------------------

def test_codex_disagree_lower_raises_blocked_and_logs_gate_failed(tmp_path: Path) -> None:
    """disagree_lower must raise ContractBlockedError and log gate_failed."""
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review("co-sign", "disagree_lower")),
    )
    with pytest.raises(ContractBlockedError) as exc_info:
        runner.review_task_contract(_valid_contract())

    assert exc_info.value.decision == "risk_blocked_disagree_lower"

    events = EventLog(tmp_path / "runs" / RUN_ID / "event_log.jsonl").read_all()
    gate_failed = [e for e in events if e["event_type"] == "gate_failed"]
    assert gate_failed, "Expected gate_failed event for disagree_lower"
    assert any("downgrade" in e.get("error", "") for e in gate_failed)


def test_codex_disagree_higher_logs_escalation_but_does_not_raise(tmp_path: Path) -> None:
    """disagree_higher should log risk_rescored and return the review (warning, not a block)."""
    runner = _runner(
        tmp_path,
        FakeAgentLauncher(lambda t: _valid_contract()),
        FakeAgentLauncher(lambda t: _valid_review("co-sign", "disagree_higher")),
    )
    review = runner.review_task_contract(_valid_contract())
    assert review["decision"] == "co-sign"  # still returned

    events = EventLog(tmp_path / "runs" / RUN_ID / "event_log.jsonl").read_all()
    rescored = [e for e in events if e["event_type"] == "risk_rescored"]
    assert rescored


# ---------------------------------------------------------------------------
# Stale output is rejected
# ---------------------------------------------------------------------------

def test_stale_output_causes_retry(tmp_path: Path) -> None:
    """Agent writes output with ATT-000 (stale); ContractLoopRunner retries."""
    stale = StaleFakeAgentLauncher("ATT-000", _valid_contract())
    runner = _runner(tmp_path, stale, FakeAgentLauncher(lambda t: _valid_review()))
    with pytest.raises(ContractLoopError):
        runner.generate_task_contract("task")
    # All 3 attempts should have been recorded.
    ws_dir = tmp_path / "runs" / RUN_ID / "agents" / "claude-contract-gen"
    records = [json.loads(l) for l in (ws_dir / "agent_attempts.jsonl").read_text().splitlines() if l.strip()]
    assert len(records) == 3
    assert all(r["outcome"] == "schema_failure" for r in records)
