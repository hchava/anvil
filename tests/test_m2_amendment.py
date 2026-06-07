"""Tests for the task contract amendment protocol (Milestone 2).

Covers: goals refinement, non_goals relaxation, forbidden_changes human
approval in Standard/Critical, constraints amendment, amendment appended to
list, risk recalculation, error cases.
"""

from __future__ import annotations

import pytest

from anvil.controller.amendment import (
    AmendmentError,
    apply_amendment,
    recalculate_risk_after_amendment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_contract() -> dict:
    return {
        "run_id": "RUN-20260606-001",
        "task_summary": "Add input validation to the config parser module.",
        "goals": ["Validate all config fields on load"],
        "non_goals": ["Rewrite the config module from scratch"],
        "constraints": ["Must not break existing tests"],
        "forbidden_changes": ["infra/", "pipelines/export/"],
        "acceptance_criteria": ["All tests pass"],
        "status": "proposed",
    }


def _cosigners() -> list[str]:
    return ["claude-orchestrator", "codex-validator"]


def _human_cosigners() -> list[str]:
    return ["claude-orchestrator", "codex-validator", "human-alice"]


# ---------------------------------------------------------------------------
# Goals amendment
# ---------------------------------------------------------------------------

def test_goals_amended_with_cosign() -> None:
    contract = _base_contract()
    updated = apply_amendment(
        contract,
        field_changed="goals",
        proposed_change="Add rollback goal",
        reason="Rollback was missing from original scope.",
        evidence=[],
        approved_by=_cosigners(),
        mode="fast",
    )
    assert updated["status"] == "amended"
    assert len(updated["amendments"]) == 1
    assert updated["amendments"][0]["amendment_id"] == "AMEND-001"
    assert updated["amendments"][0]["field_changed"] == "goals"


def test_goals_amendment_requires_cosign() -> None:
    with pytest.raises(AmendmentError, match="co-sign"):
        apply_amendment(
            _base_contract(),
            field_changed="goals",
            proposed_change="Add rollback goal",
            reason="reason",
            evidence=[],
            approved_by=["claude-orchestrator"],  # only one agent — no co-sign
            mode="fast",
        )


# ---------------------------------------------------------------------------
# Non-goals amendment
# ---------------------------------------------------------------------------

def test_non_goals_relaxed_with_cosign_and_rationale() -> None:
    updated = apply_amendment(
        _base_contract(),
        field_changed="non_goals",
        proposed_change="Remove 'rewrite from scratch' from non_goals",
        reason="Architecture review determined partial rewrite is acceptable.",
        evidence=[],
        approved_by=_cosigners(),
        mode="standard",
    )
    assert updated["status"] == "amended"
    assert updated["amendments"][0]["field_changed"] == "non_goals"


def test_non_goals_requires_cosign() -> None:
    with pytest.raises(AmendmentError, match="co-sign"):
        apply_amendment(
            _base_contract(),
            field_changed="non_goals",
            proposed_change="change",
            reason="some reason",
            evidence=[],
            approved_by=["codex-validator"],  # only one agent
            mode="fast",
        )


# ---------------------------------------------------------------------------
# Forbidden changes — requires human in Standard/Critical
# ---------------------------------------------------------------------------

def test_forbidden_changes_fast_mode_cosign_only() -> None:
    updated = apply_amendment(
        _base_contract(),
        field_changed="forbidden_changes",
        proposed_change="Relax infra/ restriction",
        reason="DevOps approved infra change.",
        evidence=["ticket-123"],
        approved_by=_cosigners(),
        mode="fast",  # fast mode: human not required
    )
    assert updated["status"] == "amended"


def test_forbidden_changes_standard_requires_human() -> None:
    with pytest.raises(AmendmentError, match="human"):
        apply_amendment(
            _base_contract(),
            field_changed="forbidden_changes",
            proposed_change="Relax infra/ restriction",
            reason="reason",
            evidence=["ticket-123"],
            approved_by=_cosigners(),  # no human
            mode="standard",
        )


def test_forbidden_changes_critical_requires_human() -> None:
    with pytest.raises(AmendmentError, match="human"):
        apply_amendment(
            _base_contract(),
            field_changed="forbidden_changes",
            proposed_change="relax",
            reason="reason",
            evidence=["e1"],
            approved_by=_cosigners(),
            mode="critical",
        )


def _fake_validated_decision(decision_id: str = "DEC-001") -> dict:
    """Minimal validated human decision dict for amendment tests."""
    return {
        "decision_id": decision_id,
        "run_id": "RUN-20260606-001",
        "decision": "modify_scope",
        "rationale": "Approved.",
        "approved_by": "alice",
    }


def test_forbidden_changes_standard_with_human_in_approved_by() -> None:
    updated = apply_amendment(
        _base_contract(),
        field_changed="forbidden_changes",
        proposed_change="Relax infra/ restriction",
        reason="Human approved via ticket.",
        evidence=["DEC-001"],
        approved_by=_human_cosigners(),
        mode="standard",
        human_decision_id="DEC-001",
        validated_human_decision=_fake_validated_decision("DEC-001"),
    )
    assert updated["status"] == "amended"
    amend = updated["amendments"][0]
    assert amend["human_approved"] is True
    assert amend["human_required"] is True


def test_forbidden_changes_standard_with_decision_id_satisfies_human() -> None:
    """human_decision_id + validated document together satisfy human approval."""
    updated = apply_amendment(
        _base_contract(),
        field_changed="forbidden_changes",
        proposed_change="relax",
        reason="reason",
        evidence=["e1"],
        approved_by=_cosigners(),
        mode="standard",
        human_decision_id="DEC-001",
        validated_human_decision=_fake_validated_decision("DEC-001"),
    )
    assert updated["status"] == "amended"


def test_human_decision_id_without_validated_document_raises() -> None:
    """Passing human_decision_id without validated_human_decision must raise."""
    with pytest.raises(AmendmentError, match="validated_human_decision"):
        apply_amendment(
            _base_contract(),
            field_changed="forbidden_changes",
            proposed_change="relax",
            reason="reason",
            evidence=["e1"],
            approved_by=_cosigners(),
            mode="standard",
            human_decision_id="DEC-001",
            validated_human_decision=None,  # missing!
        )


def test_human_decision_id_mismatches_validated_document_raises() -> None:
    """validated_human_decision.decision_id must match human_decision_id."""
    with pytest.raises(AmendmentError, match="does not match"):
        apply_amendment(
            _base_contract(),
            field_changed="forbidden_changes",
            proposed_change="relax",
            reason="reason",
            evidence=["e1"],
            approved_by=_cosigners(),
            mode="standard",
            human_decision_id="DEC-001",
            validated_human_decision=_fake_validated_decision("DEC-999"),  # wrong ID!
        )


def test_forbidden_changes_standard_bare_human_string_not_sufficient() -> None:
    """'human' in approved_by alone is not sufficient in Standard mode.

    This covers the B2 fix: a human-sounding string in approved_by was
    previously accepted without a validated decision document. It must now
    be rejected so only validated decisions satisfy human approval.
    """
    with pytest.raises(AmendmentError, match="human"):
        apply_amendment(
            _base_contract(),
            field_changed="forbidden_changes",
            proposed_change="Relax infra/ restriction",
            reason="reason",
            evidence=["e1"],
            approved_by=["claude-orchestrator", "codex-validator", "human-alice"],
            mode="standard",
            # No human_decision_id, no validated_human_decision.
        )


def test_forbidden_changes_critical_bare_human_string_not_sufficient() -> None:
    """'human' in approved_by alone is not sufficient in Critical mode either."""
    with pytest.raises(AmendmentError, match="human"):
        apply_amendment(
            _base_contract(),
            field_changed="forbidden_changes",
            proposed_change="relax",
            reason="reason",
            evidence=["e1"],
            approved_by=["claude-orchestrator", "codex-validator", "human-bob"],
            mode="critical",
        )


# ---------------------------------------------------------------------------
# Constraints amendment
# ---------------------------------------------------------------------------

def test_constraints_amended_with_evidence_and_cosign() -> None:
    updated = apply_amendment(
        _base_contract(),
        field_changed="constraints",
        proposed_change="Allow modifying one existing test",
        reason="Test needed updating.",
        evidence=["tests/test_config.py:42"],
        approved_by=_cosigners(),
        mode="standard",
    )
    assert updated["status"] == "amended"


def test_constraints_requires_evidence() -> None:
    with pytest.raises(AmendmentError, match="evidence"):
        apply_amendment(
            _base_contract(),
            field_changed="constraints",
            proposed_change="change",
            reason="reason",
            evidence=[],  # empty — not allowed
            approved_by=_cosigners(),
            mode="fast",
        )


def test_constraints_requires_cosign() -> None:
    with pytest.raises(AmendmentError, match="co-sign"):
        apply_amendment(
            _base_contract(),
            field_changed="constraints",
            proposed_change="change",
            reason="reason",
            evidence=["src/foo.py"],
            approved_by=["claude-orchestrator"],  # only one agent
            mode="fast",
        )


# ---------------------------------------------------------------------------
# Multiple amendments accumulate
# ---------------------------------------------------------------------------

def test_multiple_amendments_accumulate() -> None:
    c2 = apply_amendment(
        _base_contract(),
        field_changed="goals",
        proposed_change="add goal",
        reason="r",
        evidence=[],
        approved_by=_cosigners(),
    )
    c3 = apply_amendment(
        c2,
        field_changed="non_goals",
        proposed_change="relax non_goal",
        reason="r2",
        evidence=[],
        approved_by=_cosigners(),
    )
    assert len(c3["amendments"]) == 2
    assert c3["amendments"][0]["amendment_id"] == "AMEND-001"
    assert c3["amendments"][1]["amendment_id"] == "AMEND-002"


# ---------------------------------------------------------------------------
# Original contract is not mutated
# ---------------------------------------------------------------------------

def test_apply_amendment_does_not_mutate_original() -> None:
    contract = _base_contract()
    original_status = contract.get("status")
    apply_amendment(
        contract,
        field_changed="goals",
        proposed_change="add goal",
        reason="r",
        evidence=[],
        approved_by=_cosigners(),
    )
    assert contract.get("status") == original_status
    assert not contract.get("amendments")


# ---------------------------------------------------------------------------
# Risk recalculation after amendment
# ---------------------------------------------------------------------------

def test_risk_increases_after_forbidden_changes_relaxation() -> None:
    amend = {"amendment_id": "AMEND-001", "field_changed": "forbidden_changes"}
    new_score, reason = recalculate_risk_after_amendment(amend, current_risk_score=4)
    assert new_score > 4
    assert "forbidden_changes" in reason


def test_risk_increases_after_non_goals_relaxation() -> None:
    amend = {"amendment_id": "AMEND-001", "field_changed": "non_goals"}
    new_score, reason = recalculate_risk_after_amendment(amend, current_risk_score=3)
    assert new_score > 3


def test_risk_neutral_for_goals_refinement() -> None:
    amend = {"amendment_id": "AMEND-001", "field_changed": "goals"}
    new_score, _ = recalculate_risk_after_amendment(amend, current_risk_score=3)
    assert new_score == 3


def test_risk_score_never_negative() -> None:
    amend = {"amendment_id": "AMEND-001", "field_changed": "goals"}
    new_score, _ = recalculate_risk_after_amendment(amend, current_risk_score=0)
    assert new_score >= 0
