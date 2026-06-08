"""Targeted plan review (Milestone 4).

Standard Mode requires at least one reviewer selected based on the run's risk
profile.  Security review is mandatory when any of the security risk factors
are active.

Reviewers produce raw findings (review_findings_raw.json items).  A separate
consolidation pass (see consolidation.py) verifies that no raw issue is silently
dropped before findings are added to the issue ledger.
"""

from __future__ import annotations

from typing import Any, Callable

from ..errors import AnvilError
from ..timeutil import now_iso


class ReviewError(AnvilError):
    """Raised when the review gate cannot proceed (missing required reviewer)."""

# Risk factor IDs from the risk engine that mandate security review.
SECURITY_RISK_FACTORS = frozenset(
    [
        "security_auth_data",
        "dependency_lockfile",
        "external_api_behavior",
        "db_schema_migration",
    ]
)

# All reviewer types available in Standard Mode.
ALL_REVIEWERS = ("correctness", "performance", "security", "quality", "integration")


def select_reviewers(
    active_factor_ids: list[str],
    available_reviewer_agents: dict[str, Any],
) -> list[str]:
    """Return the list of reviewer types to invoke for this run.

    Rules:
      - Security reviewer is mandatory when any SECURITY_RISK_FACTORS are active;
        absence of a registered security agent raises ReviewError.
      - At least one reviewer must be registered; raises ReviewError otherwise.
      - Additional reviewers are selected if their agents are registered (key present).
    """
    selected: list[str] = []

    security_required = bool(set(active_factor_ids) & SECURITY_RISK_FACTORS)

    if security_required and "security" not in available_reviewer_agents:
        raise ReviewError(
            "security reviewer is required when security risk factors are active "
            f"({sorted(set(active_factor_ids) & SECURITY_RISK_FACTORS)}) "
            "but no security reviewer agent is registered"
        )

    # Security first when required.
    if security_required:
        selected.append("security")

    # Add other available reviewers (in canonical order, no duplicates).
    for reviewer in ALL_REVIEWERS:
        if reviewer == "security":
            continue  # already handled
        if reviewer in available_reviewer_agents and reviewer not in selected:
            selected.append(reviewer)

    if not selected:
        raise ReviewError(
            "no reviewer agents are registered; Standard Mode requires at least one reviewer"
        )

    return selected


def run_review(
    run_id: str,
    reviewer_types: list[str],
    reviewer_agents: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Call each reviewer agent and collect raw findings into one dict.

    Each agent receives the full context dict (run_id, task_contract,
    claim_ledger, source_manifest, implementation_plan) and returns a list
    of raw finding dicts:
        {
            "severity": "critical" | "high" | "medium" | "low" | "info",
            "title": str,
            "detail": str (optional),
        }

    Returns a review_findings_raw-schema-compatible dict.
    """
    raw_issues: list[dict[str, Any]] = []

    for reviewer in reviewer_types:
        agent = reviewer_agents.get(reviewer)
        if agent is None:
            raise ReviewError(
                f"reviewer '{reviewer}' was selected but has no registered agent callable"
            )
        findings = agent(context) or []
        prefix = reviewer.upper()[:5]  # RAW-CORRE-001, RAW-SECUR-001, etc.
        for i, finding in enumerate(findings, start=1):
            raw_id = f"RAW-{prefix}-{i:03d}"
            raw_issues.append(
                {
                    "raw_issue_id": raw_id,
                    "reviewer": reviewer,
                    "severity": finding.get("severity", "medium"),
                    "title": finding.get("title", f"{reviewer} finding {i}"),
                    "detail": finding.get("detail", ""),
                }
            )

    return {"run_id": run_id, "raw_issues": raw_issues}
