"""Claim ledger research helpers (Milestone 4).

After a research agent produces the claim_ledger.json, the Standard Mode
runner performs a weak-evidence check before writing the artifact.

Weak evidence: a high/critical impact claim where NO direct evidence entry
references a code-bearing source type (code, test, config, migration, lockfile).
Such claims must cite code-level evidence — file path, line range, commit SHA,
and content hash — to be considered strongly evidenced.  Doc-only direct
evidence is flagged as weak.

When weak evidence is detected the runner creates a blocking issue in the
issue ledger (severity=high, resolution=open) that prevents work-order
execution.  The claim ledger itself is written as-is (the schema still
requires at least one direct evidence entry for high/critical claims, so
the agent should produce a structurally valid ledger — the issue captures the
quality gap, not a structural error).
"""

from __future__ import annotations

from typing import Any

from ..timeutil import now_iso

_HIGH_CRITICAL = frozenset(["critical", "high"])
_CODE_BEARING = frozenset(["code", "test", "config", "migration", "lockfile"])


def check_weak_evidence(
    claim_ledger: dict[str, Any],
    existing_issue_count: int = 0,
) -> list[dict[str, Any]]:
    """Return new issue dicts for high/critical claims with weak evidence.

    A claim is weakly evidenced when:
      - impact is critical or high
      - No evidence entry has (evidence_type=direct AND source_type in CODE_BEARING)

    Each returned dict is a fully-formed issue_ledger entry (sans closure
    fields, which are not needed for open issues).
    """
    new_issues: list[dict[str, Any]] = []
    issue_counter = existing_issue_count

    for claim in claim_ledger.get("claims", []):
        if claim.get("impact") not in _HIGH_CRITICAL:
            continue
        evidence = claim.get("evidence", [])
        has_strong = any(
            e.get("evidence_type") == "direct" and e.get("source_type") in _CODE_BEARING
            for e in evidence
        )
        if not has_strong:
            issue_counter += 1
            issue_id = f"ISSUE-{issue_counter:03d}"
            new_issues.append(
                {
                    "issue_id": issue_id,
                    "title": (
                        f"Claim '{claim['claim_id']}' ({claim.get('impact')} impact) "
                        "lacks code-level direct evidence"
                    ),
                    "severity": "high",
                    "raised_by": "standard-mode-research-gate",
                    "layer": "research",
                    "related_claims": [claim["claim_id"]],
                    "related_sources": [],
                    "resolution": "open",
                    "safe_to_continue_without_resolution": False,
                    "blocks_work_orders": [],
                    "blocks_layers": ["execution"],
                }
            )

    return new_issues
