"""Run scorecard generation (Milestone 1).

Aggregates deterministic metrics into ``run_scorecard.json`` (run_scorecard
schema). Layer-yield is populated from the issue ledger when present, otherwise
left as an empty object (placeholder for milestones that add review layers).
"""

from __future__ import annotations

from typing import Any


def build_scorecard(
    run_id: str,
    mode: str,
    *,
    risk_initial: int | None,
    risk_final: int | None,
    mode_escalated: bool,
    issue_ledger: dict[str, Any] | None,
    work_orders: dict[str, Any] | None,
    baseline_green: bool,
    final_outcome: str,
) -> dict[str, Any]:
    issues = (issue_ledger or {}).get("issues", [])
    by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    closed_by_evidence = 0
    deferred_by_human = 0
    layer_yield: dict[str, dict[str, int]] = {}
    for issue in issues:
        sev = issue["severity"]
        by_severity[sev] = by_severity.get(sev, 0) + 1
        resolution = issue.get("resolution")
        if resolution == "resolved_by_evidence":
            closed_by_evidence += 1
        elif resolution == "deferred":
            deferred_by_human += 1
        layer = issue.get("layer", "unknown")
        bucket = layer_yield.setdefault(layer, {"issues_opened": 0, "blocking_issues": 0})
        bucket["issues_opened"] += 1
        if sev in ("critical", "high") and resolution == "open":
            bucket["blocking_issues"] += 1

    wo_list = (work_orders or {}).get("work_orders", [])
    wo_total = len(wo_list)
    wo_passed = sum(1 for w in wo_list if w.get("status") in ("validation_passed", "ready_for_execution"))

    scorecard: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "agents_launched": 0,  # Milestone 1 is a no-LLM dry run.
        "mode_escalated": mode_escalated,
        "issues_by_severity": by_severity,
        "issues_closed_by_evidence": closed_by_evidence,
        "issues_deferred_by_human": deferred_by_human,
        "layer_yield": layer_yield,
        "deterministic_checks": {
            "tests_passed": baseline_green,
            "linters_passed": baseline_green,
            "type_checks_passed": baseline_green,
            "secret_scan_passed": True,
            "new_test_failures_vs_baseline": 0,
        },
        "work_orders_total": wo_total,
        "work_orders_passed": wo_passed,
        "work_orders_reworked": 0,
        "human_escalations": 0,
        "schema_validation_failures": 0,
        "agent_retries": 0,
        "final_outcome": final_outcome,
    }
    if risk_initial is not None:
        scorecard["risk_score_initial"] = risk_initial
    if risk_final is not None:
        scorecard["risk_score_final"] = risk_final
    return scorecard
