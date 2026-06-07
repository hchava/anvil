"""Deterministic gate checks and cross-reference validators (Milestone 1).

Every function here is pure (operates on already-parsed artifact dicts) and
raises :class:`GateError` with a precise reason on failure, so the controller
can fail closed and log a ``gate_failed`` event. None of these call an LLM.
"""

from __future__ import annotations

from typing import Any

from ..errors import AnvilError


class GateError(AnvilError):
    """Raised when a controller gate check fails."""


# ---------------------------------------------------------------------------
# Cross-reference validators (referential integrity across artifacts)
# ---------------------------------------------------------------------------

def _source_ids(source_manifest: dict[str, Any]) -> set[str]:
    return {s["source_id"] for s in source_manifest.get("sources", [])}


def _claim_ids(claim_ledger: dict[str, Any]) -> set[str]:
    return {c["claim_id"] for c in claim_ledger.get("claims", [])}


def _issue_ids(issue_ledger: dict[str, Any]) -> set[str]:
    return {i["issue_id"] for i in issue_ledger.get("issues", [])}


def _work_order_ids(work_orders: dict[str, Any]) -> set[str]:
    return {w["work_order_id"] for w in work_orders.get("work_orders", [])}


def validate_source_refs(claim_ledger: dict[str, Any], source_manifest: dict[str, Any]) -> None:
    """Every claim evidence source_id must exist in the source manifest."""
    known = _source_ids(source_manifest)
    for claim in claim_ledger.get("claims", []):
        for ev in claim.get("evidence", []):
            sid = ev.get("source_id")
            if sid is not None and sid not in known:
                raise GateError(
                    f"claim '{claim['claim_id']}' references unknown source '{sid}'"
                )


def validate_claim_refs(issue_ledger: dict[str, Any], claim_ledger: dict[str, Any]) -> None:
    """Every issue.related_claims entry must exist in the claim ledger."""
    known = _claim_ids(claim_ledger)
    for issue in issue_ledger.get("issues", []):
        for cid in issue.get("related_claims", []):
            if cid not in known:
                raise GateError(
                    f"issue '{issue['issue_id']}' references unknown claim '{cid}'"
                )


def validate_issue_refs(issue_ledger: dict[str, Any], work_orders: dict[str, Any]) -> None:
    """Every issue.blocks_work_orders entry must exist in the work orders."""
    known = _work_order_ids(work_orders)
    for issue in issue_ledger.get("issues", []):
        for wid in issue.get("blocks_work_orders", []):
            if wid not in known:
                raise GateError(
                    f"issue '{issue['issue_id']}' blocks unknown work order '{wid}'"
                )


def validate_task_contract_refs(
    claim_ledger: dict[str, Any], task_contract: dict[str, Any]
) -> None:
    """Every claim must carry a non-empty task_contract_ref."""
    for claim in claim_ledger.get("claims", []):
        refs = claim.get("task_contract_ref")
        if not refs:
            raise GateError(
                f"claim '{claim['claim_id']}' has no task_contract_ref"
            )


def validate_work_order_dependencies(work_orders: dict[str, Any]) -> None:
    """Dependency matrix must reference real work orders and have no cycles."""
    ids = _work_order_ids(work_orders)
    matrix = work_orders.get("dependency_matrix", [])
    deps: dict[str, list[str]] = {}
    for row in matrix:
        wid = row["work_order_id"]
        if wid not in ids:
            raise GateError(f"dependency_matrix references unknown work order '{wid}'")
        for dep in row.get("depends_on", []):
            if dep not in ids:
                raise GateError(
                    f"work order '{wid}' depends on unknown work order '{dep}'"
                )
        deps[wid] = list(row.get("depends_on", []))

    # Cycle detection via DFS.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {wid: WHITE for wid in deps}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        for nxt in deps.get(node, []):
            if color.get(nxt) == GRAY:
                cycle = " -> ".join(stack + [node, nxt])
                raise GateError(f"work order dependency cycle: {cycle}")
            if color.get(nxt, BLACK) == WHITE:
                visit(nxt, stack + [node])
        color[node] = BLACK

    for wid in deps:
        if color[wid] == WHITE:
            visit(wid, [])


def validate_guardrail_refs(guardrail_matrix: dict[str, Any]) -> None:
    """Guardrail ids must be unique within the matrix."""
    seen: set[str] = set()
    for gr in guardrail_matrix.get("guardrails", []):
        gid = gr["guardrail_id"]
        if gid in seen:
            raise GateError(f"duplicate guardrail id '{gid}'")
        seen.add(gid)


def validate_file_scope_against_contract(
    work_orders: dict[str, Any], task_contract: dict[str, Any]
) -> None:
    """No work order may touch a file the task contract forbids."""
    forbidden = list(task_contract.get("forbidden_changes", []))
    if not forbidden:
        return
    for wo in work_orders.get("work_orders", []):
        allowed = wo.get("assigned_scope", {}).get("allowed_files", [])
        for path in allowed:
            for bad in forbidden:
                if _path_matches(path, bad):
                    raise GateError(
                        f"work order '{wo['work_order_id']}' allows forbidden path "
                        f"'{path}' (contract forbids '{bad}')"
                    )


def _path_matches(path: str, pattern: str) -> bool:
    """Match a path against a contract forbidden entry (exact or directory prefix)."""
    path = path.strip()
    pattern = pattern.strip()
    if pattern.endswith("/"):
        return path == pattern or path.startswith(pattern)
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


# ---------------------------------------------------------------------------
# Gate checks (mode-aware policy gates)
# ---------------------------------------------------------------------------

def issue_closure_gate(issue_ledger: dict[str, Any]) -> None:
    """Critical/high issues must be properly closed (roadmap closure gate).

    Pass only if, for every critical/high issue:
      - resolution == resolved_by_evidence with non-empty closure_evidence, OR
      - resolution == resolved_by_human with a human_decision_ref + rationale, OR
      - resolution == deferred with a human_decision_ref AND
        safe_to_continue_without_resolution == true.
    Any other state (notably open, or unsafe deferral) blocks.
    """
    for issue in issue_ledger.get("issues", []):
        if issue["severity"] not in ("critical", "high"):
            continue
        resolution = issue.get("resolution")
        iid = issue["issue_id"]
        if resolution == "resolved_by_evidence":
            if not issue.get("closure_evidence"):
                raise GateError(f"issue '{iid}' resolved_by_evidence but closure_evidence is empty")
        elif resolution == "resolved_by_human":
            if not issue.get("human_decision_ref"):
                raise GateError(f"issue '{iid}' resolved_by_human but no human_decision_ref")
        elif resolution == "deferred":
            if not issue.get("human_decision_ref"):
                raise GateError(f"critical/high issue '{iid}' deferred without human_decision_ref")
            if not issue.get("safe_to_continue_without_resolution", False):
                raise GateError(
                    f"critical/high issue '{iid}' deferred but not safe_to_continue_without_resolution"
                )
        else:
            raise GateError(
                f"critical/high issue '{iid}' is not closed (resolution='{resolution}')"
            )


def guardrail_gate(guardrail_matrix: dict[str, Any], mode: str) -> None:
    """Guardrail compliance gate (roadmap guardrail gate).

    - Critical applicable guardrails must be 'pass' or human-approved 'waived'.
    - 'not_checked' blocks in Standard/Critical mode.
    - High applicable guardrails must be 'pass', 'waived', or deferred-with-issue
      (modeled here as: not 'fail'/'not_checked').
    """
    strict = mode in ("standard", "critical")
    for gr in guardrail_matrix.get("guardrails", []):
        if not gr.get("applies", False):
            continue
        gid = gr["guardrail_id"]
        severity = gr["severity"]
        status = gr["status"]
        if severity == "critical":
            if status == "pass":
                continue
            if status == "waived":
                waiver = gr.get("waiver") or {}
                if not waiver.get("human_approved", False):
                    raise GateError(
                        f"critical guardrail '{gid}' waived without human approval"
                    )
                continue
            raise GateError(
                f"critical guardrail '{gid}' status '{status}' does not pass the gate"
            )
        if severity == "high":
            if status == "fail":
                raise GateError(f"high guardrail '{gid}' failed")
            if status == "not_checked" and strict:
                raise GateError(
                    f"high guardrail '{gid}' is not_checked (blocks in {mode} mode)"
                )
        # medium/low: advisory in Milestone 1.
        if status == "not_checked" and strict and severity == "critical":
            raise GateError(f"critical guardrail '{gid}' not_checked blocks in {mode} mode")


def cross_validation_gate(issue_ledger: dict[str, Any]) -> None:
    """No critical/high issue may remain 'open' at cross-validation."""
    for issue in issue_ledger.get("issues", []):
        if issue["severity"] in ("critical", "high") and issue.get("resolution") == "open":
            raise GateError(
                f"critical/high issue '{issue['issue_id']}' still open at cross-validation"
            )
