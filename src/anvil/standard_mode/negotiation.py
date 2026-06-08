"""Work-order negotiation (Milestone 4).

Claude (planner) proposes execution work orders; Codex (negotiator) challenges
or approves them.  For M4 (Standard Mode MVP, single work order), all required
work orders must reach negotiation_status="agreed" before execution.

The negotiation flow:
  1. planner_agent() → proposed execution_work_orders dict
  2. negotiator_agent() → negotiated execution_work_orders dict
     (negotiator receives the proposed dict in context and may modify statuses)
  3. Controller verifies: every required WO has status="agreed"
"""

from __future__ import annotations

from typing import Any, Callable

from ..errors import AnvilError


class NegotiationError(AnvilError):
    """Raised when work-order negotiation fails (required WO not agreed)."""


def negotiate(
    run_id: str,
    planner_output: dict[str, Any],
    negotiator_output: dict[str, Any],
) -> dict[str, Any]:
    """Merge planner proposal with negotiator response into agreed work orders.

    The negotiator_output takes precedence for negotiation_status and
    agreed_by fields.  The final work orders are the negotiator's version.

    Raises NegotiationError if any required work order is not agreed.
    """
    if negotiator_output.get("run_id") != run_id:
        raise NegotiationError(
            f"negotiator output run_id '{negotiator_output.get('run_id')}' "
            f"!= expected '{run_id}'"
        )

    work_orders = negotiator_output.get("work_orders", [])
    not_agreed = [
        wo["work_order_id"]
        for wo in work_orders
        if wo.get("criticality") == "required"
        and wo.get("negotiation_status") != "agreed"
    ]
    if not_agreed:
        raise NegotiationError(
            f"required work order(s) not agreed after negotiation: {not_agreed}"
        )

    # Every agreed required work order must be signed off by both Claude and Codex.
    for wo in work_orders:
        if wo.get("criticality") != "required" or wo.get("negotiation_status") != "agreed":
            continue
        agreed_by = wo.get("agreed_by") or []
        has_claude = any(a.lower().startswith("claude") for a in agreed_by)
        has_codex = any(a.lower().startswith("codex") for a in agreed_by)
        if not has_claude or not has_codex:
            raise NegotiationError(
                f"work order '{wo['work_order_id']}' requires both Claude and Codex "
                f"in agreed_by, got: {agreed_by}"
            )

    return negotiator_output


def build_planner_context(
    run_id: str,
    task_contract: dict[str, Any],
    claim_ledger: dict[str, Any],
    implementation_plan: str,
    issue_ledger: dict[str, Any],
) -> dict[str, Any]:
    """Build the context dict passed to the planner agent."""
    return {
        "run_id": run_id,
        "task_contract": task_contract,
        "claim_ledger": claim_ledger,
        "implementation_plan": implementation_plan,
        "issue_ledger": issue_ledger,
    }


def build_negotiator_context(
    run_id: str,
    proposed_work_orders: dict[str, Any],
    task_contract: dict[str, Any],
    issue_ledger: dict[str, Any],
) -> dict[str, Any]:
    """Build the context dict passed to the negotiator agent."""
    return {
        "run_id": run_id,
        "proposed_work_orders": proposed_work_orders,
        "task_contract": task_contract,
        "issue_ledger": issue_ledger,
    }
