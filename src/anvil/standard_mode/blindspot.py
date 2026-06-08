"""Codex blind-spot scan integration (Milestone 4).

The blindspot agent is a Codex-style reviewer that independently compares the
claim ledger against the source manifest and task contract.  It returns a list
of finding dicts describing gaps or contradictions that Claude's research
missed.

Each finding is converted to an issue_ledger entry here.  Critical/high
blindspot issues block progress at the cross-validation gate.
"""

from __future__ import annotations

from typing import Any

from ..timeutil import now_iso


def findings_to_issues(
    run_id: str,
    findings: list[dict[str, Any]],
    existing_issue_count: int = 0,
) -> list[dict[str, Any]]:
    """Convert blindspot agent findings to issue_ledger entries.

    Each finding dict should have:
        severity: "critical" | "high" | "medium" | "low"
        title: str
        description: str (optional)
        related_claims: list[str] (optional, CLAIM-xxx ids)
        related_sources: list[str] (optional, SRC-xxx ids)

    Returns a list of new issue dicts suitable for merging into the issue
    ledger.  Issues are numbered starting from existing_issue_count + 1.
    """
    new_issues: list[dict[str, Any]] = []
    counter = existing_issue_count

    for finding in findings:
        counter += 1
        issue_id = f"ISSUE-{counter:03d}"
        severity = finding.get("severity", "medium")
        safe_to_continue = severity not in ("critical", "high")

        new_issues.append(
            {
                "issue_id": issue_id,
                "title": finding.get("title", f"Blindspot finding {counter}"),
                "severity": severity,
                "raised_by": "codex-blindspot",
                "layer": "blindspot_scan",
                "related_claims": list(finding.get("related_claims", [])),
                "related_sources": list(finding.get("related_sources", [])),
                "resolution": "open",
                "safe_to_continue_without_resolution": safe_to_continue,
                "blocks_work_orders": [],
                "blocks_layers": [] if safe_to_continue else ["execution"],
            }
        )

    return new_issues
