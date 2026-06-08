"""Review consolidation verification (Milestone 4).

After a review panel produces raw findings (review_findings_raw.json), a
consolidation pass must account for EVERY raw issue exactly once.  The
controller fails the gate if any raw_issue_id is silently dropped.

Dispositions:
  preserved         — the raw issue becomes an independent consolidated entry
  merged_into       — the raw issue is absorbed into another consolidated entry
  rejected_with_reason — the raw issue is rejected with an explicit reason
  converted_to_low_priority — the raw issue is downgraded to low severity

The controller verifies disposition coverage; it does NOT verify reviewer
judgment (that is a human responsibility).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..errors import AnvilError


class ConsolidationError(AnvilError):
    """Raised when the consolidation gate fails (dropped raw issue)."""


def verify_consolidation(
    raw_findings: dict[str, Any],
    consolidated: dict[str, Any],
) -> None:
    """Raise ConsolidationError if any raw_issue_id is not accounted for.

    Every raw_issue_id in review_findings_raw must appear in exactly one
    consolidated_issues entry's raw_issue_ids list.  Missing IDs mean a raw
    issue was silently dropped.
    """
    raw_ids = {r["raw_issue_id"] for r in raw_findings.get("raw_issues", [])}
    if not raw_ids:
        return  # Nothing to account for.

    # Count how many times each raw_issue_id is referenced across all entries.
    counts: Counter[str] = Counter()
    for entry in consolidated.get("consolidated_issues", []):
        for rid in entry.get("raw_issue_ids", []):
            counts[rid] += 1

    # Exactly-once: duplicates violate the contract as much as drops do.
    duplicated = sorted(rid for rid, n in counts.items() if n > 1)
    if duplicated:
        raise ConsolidationError(
            f"consolidation accounted for {len(duplicated)} raw issue(s) more than once: "
            + ", ".join(duplicated)
        )

    dropped = sorted(raw_ids - set(counts.keys()))
    if dropped:
        raise ConsolidationError(
            f"consolidation dropped {len(dropped)} raw issue(s) without disposition: "
            + ", ".join(dropped)
        )


def build_default_consolidated(
    run_id: str,
    raw_findings: dict[str, Any],
) -> dict[str, Any]:
    """Build a simple consolidated document that preserves every raw issue.

    This is the pass-through consolidation used when no explicit consolidation
    agent is provided.  Each raw issue becomes an independent preserved entry.
    """
    consolidated_issues: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_findings.get("raw_issues", []), start=1):
        consolidated_issues.append(
            {
                "consolidated_id": f"CONSOL-{i:03d}",
                "raw_issue_ids": [raw["raw_issue_id"]],
                "disposition": "preserved",
                "title": raw["title"],
                "severity": raw["severity"],
            }
        )
    return {"run_id": run_id, "consolidated_issues": consolidated_issues}


def preserved_issues_from_consolidation(
    consolidated: dict[str, Any],
    raw_findings: dict[str, Any],
    existing_issue_count: int = 0,
) -> list[dict[str, Any]]:
    """Convert preserved consolidated findings to issue_ledger entries.

    Only entries with disposition="preserved" become issues.  Merged,
    rejected, or converted entries are handled at the consolidated level.
    """
    # Build a lookup from raw_issue_id to raw finding for severity access.
    raw_by_id = {r["raw_issue_id"]: r for r in raw_findings.get("raw_issues", [])}

    new_issues: list[dict[str, Any]] = []
    counter = existing_issue_count

    for entry in consolidated.get("consolidated_issues", []):
        if entry.get("disposition") != "preserved":
            continue
        counter += 1
        severity = entry.get("severity", "medium")
        safe_to_continue = severity not in ("critical", "high")
        new_issues.append(
            {
                "issue_id": f"ISSUE-{counter:03d}",
                "title": entry.get("title", f"Review finding {counter}"),
                "severity": severity,
                "raised_by": "plan-review",
                "layer": "plan_review",
                "related_claims": [],
                "related_sources": [],
                "resolution": "open",
                "safe_to_continue_without_resolution": safe_to_continue,
                "blocks_work_orders": [],
                "blocks_layers": [] if safe_to_continue else ["execution"],
            }
        )

    return new_issues
