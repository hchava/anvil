"""Controller state machine definition (Milestone 1).

Encodes the implementation-spec 1.1 states plus the roadmap's two added states
(``CROSS_VALIDATION_PENDING`` and ``READY_FOR_COMMIT_REVIEW``) and the per-mode
linear sequences the deterministic dry-run walks.

Design notes:
- Fast Mode skips deep research (claims/blindspot/cross-validation) and review.
- Standard Mode runs research and goes through CROSS_VALIDATION_PENDING with the
  full issue-closure gate before CROSS_VALIDATION_COMPLETE; it also gets a
  targeted (fixture-validated) plan review.
- Critical Mode additionally runs claim stress-testing.
- BLINDSPOT_SCAN_COMPLETE never jumps directly to CROSS_VALIDATION_COMPLETE; the
  pending state always sits between them.
- Single-WO runs go EXECUTION_COMPLETE -> READY_FOR_COMMIT_REVIEW; multi-WO runs
  insert INTEGRATION_VALIDATED first.
"""

from __future__ import annotations

# Canonical state identifiers.
INIT = "INIT"
BASELINE_CAPTURED = "BASELINE_CAPTURED"
TASK_CONTRACT_PROPOSED = "TASK_CONTRACT_PROPOSED"
TASK_CONTRACT_ACCEPTED = "TASK_CONTRACT_ACCEPTED"
SOURCES_DISCOVERED = "SOURCES_DISCOVERED"
SOURCE_GAPS_RESOLVED = "SOURCE_GAPS_RESOLVED"
CLAIMS_RESEARCHED = "CLAIMS_RESEARCHED"
BLINDSPOT_SCAN_COMPLETE = "BLINDSPOT_SCAN_COMPLETE"
CLAIMS_STRESS_TESTED = "CLAIMS_STRESS_TESTED"
CROSS_VALIDATION_PENDING = "CROSS_VALIDATION_PENDING"
CROSS_VALIDATION_COMPLETE = "CROSS_VALIDATION_COMPLETE"
PLAN_CREATED = "PLAN_CREATED"
PLAN_REVIEWED = "PLAN_REVIEWED"
WORK_ORDERS_AGREED = "WORK_ORDERS_AGREED"
EXECUTION_COMPLETE = "EXECUTION_COMPLETE"
INTEGRATION_VALIDATED = "INTEGRATION_VALIDATED"
READY_FOR_COMMIT_REVIEW = "READY_FOR_COMMIT_REVIEW"
COMMIT_REVIEWED = "COMMIT_REVIEWED"
FINALIZED = "FINALIZED"
ABORTED = "ABORTED"
HUMAN_ESCALATED = "HUMAN_ESCALATED"

MODES = ("fast", "standard", "critical")

# Shared head/tail segments. The body between discovery and planning differs by
# mode; the WO/execution/commit tail differs by single- vs multi-WO.
_HEAD = [
    INIT,
    BASELINE_CAPTURED,
    TASK_CONTRACT_PROPOSED,
    TASK_CONTRACT_ACCEPTED,
    SOURCES_DISCOVERED,
    SOURCE_GAPS_RESOLVED,
]

_RESEARCH_STANDARD = [
    CLAIMS_RESEARCHED,
    BLINDSPOT_SCAN_COMPLETE,
    CROSS_VALIDATION_PENDING,
    CROSS_VALIDATION_COMPLETE,
]

_RESEARCH_CRITICAL = [
    CLAIMS_RESEARCHED,
    BLINDSPOT_SCAN_COMPLETE,
    CLAIMS_STRESS_TESTED,
    CROSS_VALIDATION_PENDING,
    CROSS_VALIDATION_COMPLETE,
]

_PLAN_REVIEWED_MODES = {"standard", "critical"}


def sequence(mode: str, *, multi_wo: bool) -> list[str]:
    """Return the ordered state sequence for ``mode`` and WO cardinality."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")

    states = list(_HEAD)
    if mode == "fast":
        pass  # Fast skips deep research entirely.
    elif mode == "standard":
        states += _RESEARCH_STANDARD
    else:  # critical
        states += _RESEARCH_CRITICAL

    states.append(PLAN_CREATED)
    if mode in _PLAN_REVIEWED_MODES:
        states.append(PLAN_REVIEWED)

    states.append(WORK_ORDERS_AGREED)
    states.append(EXECUTION_COMPLETE)
    if multi_wo:
        states.append(INTEGRATION_VALIDATED)
    states.append(READY_FOR_COMMIT_REVIEW)
    states.append(COMMIT_REVIEWED)
    states.append(FINALIZED)
    return states
