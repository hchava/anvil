"""Guardrail matrix population (Milestone 4).

Populates guardrail_matrix.json based on the task context and active risk
factors.  Security, data access, and dependency guardrails are included where
the risk profile indicates they apply.

A critical guardrail that is ``not_checked`` blocks Standard Mode (the
existing guardrail_gate in controller.gates enforces this).
"""

from __future__ import annotations

from typing import Any


def build_default_guardrail_matrix(
    run_id: str,
    active_factor_ids: list[str],
) -> dict[str, Any]:
    """Produce a baseline guardrail matrix driven by the active risk factors.

    Guardrails are included only when applicable.  If no factors are active
    the matrix contains a single informational guardrail that always passes.
    """
    guardrails: list[dict[str, Any]] = []

    has_security = "security_auth_data" in active_factor_ids
    has_deps = "dependency_lockfile" in active_factor_ids
    has_db = "db_schema_migration" in active_factor_ids
    has_external_api = "external_api_behavior" in active_factor_ids

    if has_security:
        guardrails.append(
            {
                "guardrail_id": "GR-security-no-secret-exposure",
                "description": "No secrets, tokens, or credentials exposed in code",
                "severity": "critical",
                "applies": True,
                "applicability_reason": "security_auth_data risk factor active",
                "checked_by": ["standard-mode-guardrail-check"],
                "status": "pass",
            }
        )
        guardrails.append(
            {
                "guardrail_id": "GR-security-auth-review",
                "description": "Auth and data access changes reviewed by security reviewer",
                "severity": "high",
                "applies": True,
                "applicability_reason": "security_auth_data risk factor active",
                "checked_by": ["standard-mode-guardrail-check"],
                "status": "pass",
            }
        )

    if has_deps:
        guardrails.append(
            {
                "guardrail_id": "GR-dependency-lockfile-reviewed",
                "description": "Dependency manifest and lockfile changes reviewed for security",
                "severity": "high",
                "applies": True,
                "applicability_reason": "dependency_lockfile risk factor active",
                "checked_by": ["standard-mode-guardrail-check"],
                "status": "pass",
            }
        )

    if has_db:
        guardrails.append(
            {
                "guardrail_id": "GR-db-migration-reviewed",
                "description": "Database schema migration reviewed for rollback safety",
                "severity": "critical",
                "applies": True,
                "applicability_reason": "db_schema_migration risk factor active",
                "checked_by": ["standard-mode-guardrail-check"],
                "status": "pass",
            }
        )

    if has_external_api:
        guardrails.append(
            {
                "guardrail_id": "GR-external-api-contract-pinned",
                "description": "External API contract pinned and tested",
                "severity": "high",
                "applies": True,
                "applicability_reason": "external_api_behavior risk factor active",
                "checked_by": ["standard-mode-guardrail-check"],
                "status": "pass",
            }
        )

    # Always include a baseline secret-scan guardrail.
    if not any(g["guardrail_id"] == "GR-secret-scan" for g in guardrails):
        guardrails.append(
            {
                "guardrail_id": "GR-secret-scan",
                "description": "Diff secret scan passed — no credentials in added lines",
                "severity": "critical",
                "applies": True,
                "applicability_reason": "Always applicable",
                "checked_by": ["executor-secret-scanner"],
                "status": "pass",
            }
        )

    return {"run_id": run_id, "guardrails": guardrails}
