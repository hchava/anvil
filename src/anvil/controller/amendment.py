"""Task contract amendment protocol (Milestone 2).

Implements the controller-enforced amendment rules from the implementation spec:

  - goals         → can be refined with Claude + Codex co-sign
  - non_goals     → can only be RELAXED (i.e. items removed) with rationale + co-sign
                    (adding new non_goals is always allowed with co-sign)
  - forbidden_changes → relaxation requires human approval in Standard/Critical mode
  - constraints   → can be amended with evidence + co-sign
  - Any other field → requires explicit co-sign from both agents

Every accepted amendment is appended to task_contract.amendments and the
contract status transitions to "amended". Risk must be re-scored after any
amendment (the caller is responsible for triggering the re-score).

Amendment IDs follow the pattern AMEND-001, AMEND-002, ...
"""

from __future__ import annotations

from typing import Any

from ..errors import AnvilError
from ..timeutil import now_iso


class AmendmentError(AnvilError):
    """Raised when an amendment violates the protocol rules."""


def _next_amendment_id(contract: dict[str, Any]) -> str:
    existing = contract.get("amendments") or []
    return f"AMEND-{len(existing) + 1:03d}"


def _has_cosign(approved_by: list[str]) -> bool:
    """True if both a Claude-style and Codex-style agent have co-signed."""
    has_claude = any("claude" in a.lower() for a in approved_by)
    has_codex = any("codex" in a.lower() for a in approved_by)
    return has_claude and has_codex


def _has_human(approved_by: list[str]) -> bool:
    return any("human" in a.lower() or "user" in a.lower() for a in approved_by)


def apply_amendment(
    contract: dict[str, Any],
    *,
    field_changed: str,
    proposed_change: str,
    reason: str,
    evidence: list[str],
    approved_by: list[str],
    mode: str = "fast",
    human_decision_id: str | None = None,
    validated_human_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply an amendment to a task contract copy and return the updated copy.

    Raises AmendmentError if the amendment violates protocol rules.

    Args:
        contract: The current task_contract dict (will not be mutated).
        field_changed: Which contract field is being amended.
        proposed_change: Description of the change (human-readable).
        reason: Rationale for the amendment.
        evidence: List of evidence references supporting the amendment.
        approved_by: List of agent/human IDs that approved this amendment.
        mode: Current pipeline mode ("fast" / "standard" / "critical").
        human_decision_id: If a human decision authorized this amendment,
            its decision_id string.
        validated_human_decision: Required when human_decision_id is set.
            Must be the already-validated human decision dict (output of
            load_and_observe / validate_and_observe). The decision_id must
            match human_decision_id. A bare string ID is not sufficient —
            the controller must verify the decision document first.
    """
    _validate_amendment(
        contract,
        field_changed=field_changed,
        reason=reason,
        evidence=evidence,
        approved_by=approved_by,
        mode=mode,
        human_decision_id=human_decision_id,
        validated_human_decision=validated_human_decision,
    )

    updated = dict(contract)
    amendments = list(contract.get("amendments") or [])

    amendment_id = _next_amendment_id(contract)
    amendment: dict[str, Any] = {
        "amendment_id": amendment_id,
        "reason": reason,
        "proposed_change": proposed_change,
        "field_changed": field_changed,
        "evidence": evidence,
        "approved_by": approved_by,
        "timestamp": now_iso(),
        "human_required": field_changed == "forbidden_changes" and mode in ("standard", "critical"),
        "human_approved": human_decision_id is not None and validated_human_decision is not None,
    }
    if human_decision_id:
        amendment["human_decision_ref"] = human_decision_id
    amendments.append(amendment)

    updated["amendments"] = amendments
    updated["status"] = "amended"
    return updated


def _validate_amendment(
    contract: dict[str, Any],
    *,
    field_changed: str,
    reason: str,
    evidence: list[str],
    approved_by: list[str],
    mode: str,
    human_decision_id: str | None,
    validated_human_decision: dict[str, Any] | None,
) -> None:
    """Raise AmendmentError if the amendment request violates protocol."""
    if not reason:
        raise AmendmentError("Amendment reason must not be empty.")
    if not approved_by:
        raise AmendmentError("Amendment must have at least one approver.")

    # When human_decision_id is supplied, the caller must also supply the
    # validated decision document — a bare string ID is not sufficient.
    if human_decision_id is not None:
        if validated_human_decision is None:
            raise AmendmentError(
                "human_decision_id requires a validated_human_decision document. "
                "Call load_and_observe() or validate_and_observe() first, then pass the result."
            )
        doc_id = validated_human_decision.get("decision_id")
        if doc_id != human_decision_id:
            raise AmendmentError(
                f"validated_human_decision.decision_id '{doc_id}' does not match "
                f"human_decision_id '{human_decision_id}'."
            )

    if field_changed == "goals":
        # Goals can be refined with Claude + Codex co-sign.
        if not _has_cosign(approved_by):
            raise AmendmentError(
                "Amending 'goals' requires co-sign from both Claude and Codex. "
                f"approved_by={approved_by}"
            )

    elif field_changed == "non_goals":
        # Can be relaxed (items removed) with rationale + co-sign.
        # Adding new non_goals is fine with co-sign.
        if not _has_cosign(approved_by):
            raise AmendmentError(
                "Amending 'non_goals' requires co-sign from both Claude and Codex. "
                f"approved_by={approved_by}"
            )
        if not reason:
            raise AmendmentError("Amending 'non_goals' requires an explicit rationale.")

    elif field_changed == "forbidden_changes":
        # Relaxation requires human approval in Standard/Critical.
        if mode in ("standard", "critical"):
            if not _has_cosign(approved_by):
                raise AmendmentError(
                    "Relaxing 'forbidden_changes' in Standard/Critical mode requires "
                    "co-sign from both Claude and Codex."
                )
            # A bare "human" string in approved_by is not sufficient — the controller
            # must verify a human decision document through load_and_observe/
            # validate_and_observe and pass validated_human_decision.
            if human_decision_id is None:
                raise AmendmentError(
                    f"Relaxing 'forbidden_changes' in {mode} mode requires a validated human "
                    "decision document. Provide human_decision_id + validated_human_decision "
                    "from load_and_observe(); a 'human' string in approved_by is not sufficient."
                )

    elif field_changed == "constraints":
        # Can be amended with evidence + co-sign.
        if not _has_cosign(approved_by):
            raise AmendmentError(
                "Amending 'constraints' requires co-sign from both Claude and Codex. "
                f"approved_by={approved_by}"
            )
        if not evidence:
            raise AmendmentError("Amending 'constraints' requires at least one evidence reference.")

    else:
        # Any other field requires explicit co-sign from both agents.
        if not _has_cosign(approved_by):
            raise AmendmentError(
                f"Amending '{field_changed}' requires co-sign from both Claude and Codex. "
                f"approved_by={approved_by}"
            )


def recalculate_risk_after_amendment(
    amendment: dict[str, Any],
    current_risk_score: int,
) -> tuple[int, str]:
    """Return (new_score, reason) after an amendment.

    Conservative heuristic: relaxing forbidden_changes or non_goals always
    adds risk. Tightening goals (adding acceptance criteria) reduces risk by 1.
    Other amendments are neutral.

    The caller must still run the full RiskEngine.assess() — this function
    only provides the delta recommendation.
    """
    field = amendment.get("field_changed", "")
    if field == "forbidden_changes":
        delta = +2
        reason = "forbidden_changes relaxed — risk increased"
    elif field == "non_goals":
        delta = +1
        reason = "non_goals relaxed — scope risk increased"
    elif field == "goals":
        delta = 0
        reason = "goals refined — risk neutral"
    elif field == "constraints":
        delta = +1
        reason = "constraints amended — risk increased conservatively"
    else:
        delta = +1
        reason = f"field '{field}' amended — risk increased conservatively"

    new_score = max(0, current_risk_score + delta)
    return new_score, reason
