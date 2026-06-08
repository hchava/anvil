"""Gap analysis helpers (Milestone 4).

Produces a gap_matrix.json from a gap analysis agent output and enforces the
gap gate: required coverage areas with blocking gaps stop the run.

If no gap_analysis_agent is provided, a simple default gap matrix is produced
that marks sources already in the manifest as covered.
"""

from __future__ import annotations

from typing import Any

from ..errors import AnvilError


class GapError(AnvilError):
    """Raised when the gap gate fails (blocking required gap)."""


# Required coverage categories for Standard Mode.
_DEFAULT_AREAS = [
    ("codebase", "required"),
    ("tests", "conditional"),
    ("documentation", "optional"),
]


def build_default_gap_matrix(
    run_id: str,
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Produce a minimal passing gap_matrix from the discovered source manifest."""
    source_types = {s["source_type"] for s in source_manifest.get("sources", [])}
    src_ids = [s["source_id"] for s in source_manifest.get("sources", [])]

    has_code = bool(source_types & {"code", "config", "lockfile", "migration"})
    has_tests = bool(source_types & {"test"})
    has_docs = bool(source_types & {"doc", "runbook", "post"})

    def _area(label: str, required_level: str, evidence: bool) -> dict[str, Any]:
        return {
            "area": label,
            "required_level": required_level,
            "evidence_found": evidence,
            "source_ids": src_ids if evidence else [],
            "gap_status": "covered" if evidence else "not_applicable",
        }

    areas = [
        _area("codebase", "required", has_code or bool(src_ids)),
        _area("tests", "conditional", has_tests),
        _area("documentation", "optional", has_docs),
    ]
    return {
        "run_id": run_id,
        "coverage_areas": areas,
        "overall_sufficient": True,
    }


def gap_gate(gap_matrix: dict[str, Any]) -> None:
    """Raise GapError if the gap matrix has a blocking required gap.

    This mirrors the Controller's _gate_gap_matrix() logic for use by the
    Standard Mode runner.
    """
    if not gap_matrix.get("overall_sufficient", False):
        raise GapError("gap matrix reports sources not sufficient")
    for area in gap_matrix.get("coverage_areas", []):
        if (
            area.get("required_level") == "required"
            and area.get("gap_status") == "gap"
            and area.get("blocking", False)
        ):
            raise GapError(
                f"required coverage area '{area['area']}' has a blocking gap"
            )
