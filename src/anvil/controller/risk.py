"""Deterministic risk scoring engine (Milestone 1).

Risk factors are a controller-owned registry keyed by stable factor IDs — never
free-form strings — so scoring is reproducible and auditable. The engine
produces staged assessments (initial / post_discovery / post_plan /
post_execution), supports upward-only mode escalation, and enforces the Fast
Mode floor rules: if any floor rule fails, the minimum mode is Standard
regardless of the numeric score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..timeutil import now_iso

# Controller-owned factor registry: factor_id -> (weight, human-readable label).
# Weights mirror the architecture's risk rubric (Section 3).
FACTORS: dict[str, tuple[int, str]] = {
    "production_runtime_path": (3, "Production runtime path touched"),
    "security_auth_data": (4, "Security / auth / data access touched"),
    "many_subsystems": (3, "More than 3 subsystems touched"),
    "db_schema_migration": (3, "Database schema or migration touched"),
    "external_api_behavior": (2, "External API behavior touched"),
    "no_existing_tests": (2, "No existing tests for affected area"),
    "ambiguous_requirements": (2, "Ambiguous or incomplete requirements"),
    "dependency_lockfile": (2, "Dependency manifest or lockfile changed"),
    "generated_or_docs_only": (-2, "Generated code only / docs only"),
    "single_isolated_change": (-2, "Single file, isolated change"),
}

STAGES = ("initial", "post_discovery", "post_plan", "post_execution")

# Numeric mode brackets (architecture Section 3).
#   0-2 -> fast, 3-6 -> standard, 7+ -> critical
_MODE_ORDER = {"fast": 0, "standard": 1, "critical": 2}
_ORDER_MODE = {0: "fast", 1: "standard", 2: "critical"}


def mode_for_score(score: int) -> str:
    if score >= 7:
        return "critical"
    if score >= 3:
        return "standard"
    return "fast"


def higher_mode(a: str, b: str) -> str:
    """Return the stricter of two modes."""
    return a if _MODE_ORDER[a] >= _MODE_ORDER[b] else b


@dataclass
class FloorRules:
    """Fast Mode floor rules. All must hold for Fast Mode to be permitted."""

    production_runtime_path_touched: bool = False
    auth_security_data_touched: bool = False
    dependency_or_lockfile_changed: bool = False
    db_migration_or_schema_touched: bool = False
    external_api_changed: bool = False
    affected_tests_exist_or_docs_only: bool = True
    changed_files_count: int = 1
    all_changed_files_one_module: bool = True

    def violations(self) -> list[str]:
        """Return the list of floor-rule violations (empty means Fast is allowed)."""
        v: list[str] = []
        if self.production_runtime_path_touched:
            v.append("production runtime path touched")
        if self.auth_security_data_touched:
            v.append("auth/security/data-access touched")
        if self.dependency_or_lockfile_changed:
            v.append("dependency/lockfile changed")
        if self.db_migration_or_schema_touched:
            v.append("database migration/schema touched")
        if self.external_api_changed:
            v.append("external API behavior changed")
        if not self.affected_tests_exist_or_docs_only:
            v.append("no affected tests and not docs/config-only")
        if self.changed_files_count > 2:
            v.append(f"changed files > 2 ({self.changed_files_count})")
        if not self.all_changed_files_one_module:
            v.append("changed files span multiple modules")
        return v


@dataclass
class Assessment:
    stage: str
    score: int
    factors: list[dict[str, Any]]
    mode: str
    mode_changed: bool
    timestamp: str
    escalation_reason: str | None = None
    assessed_by: list[str] = field(default_factory=lambda: ["controller"])

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "stage": self.stage,
            "score": self.score,
            "factors": self.factors,
            "mode": self.mode,
            "mode_changed": self.mode_changed,
            "timestamp": self.timestamp,
            "assessed_by": list(self.assessed_by),
        }
        if self.escalation_reason is not None:
            d["escalation_reason"] = self.escalation_reason
        return d


class RiskEngine:
    """Accumulates staged risk assessments for one run."""

    def __init__(self) -> None:
        self._assessments: list[Assessment] = []
        self._current_mode: str = "fast"

    @property
    def current_mode(self) -> str:
        return self._current_mode

    @staticmethod
    def _score(active_factor_ids: list[str]) -> tuple[int, list[dict[str, Any]]]:
        factors: list[dict[str, Any]] = []
        total = 0
        for factor_id in active_factor_ids:
            if factor_id not in FACTORS:
                raise ValueError(f"unknown risk factor id: {factor_id}")
            weight, label = FACTORS[factor_id]
            total += weight
            factors.append({"factor": factor_id, "value": weight, "reason": label})
        if not factors:
            # The schema requires >= 1 factor; record an explicit zero-risk marker.
            factors.append(
                {"factor": "single_isolated_change", "value": 0, "reason": "Baseline (no risk factors active)"}
            )
        return total, factors

    def assess(
        self,
        stage: str,
        active_factor_ids: list[str],
        *,
        floor: FloorRules | None = None,
    ) -> Assessment:
        """Record an assessment for ``stage`` and update the current mode.

        Mode can only move UP (upward-only escalation). Floor-rule violations
        force a minimum of Standard regardless of the numeric bracket.
        """
        if stage not in STAGES:
            raise ValueError(f"unknown risk stage: {stage}")

        # Idempotent per stage: re-assessing an already-scored stage (e.g. after a
        # resume) returns the existing assessment instead of duplicating it.
        for existing in self._assessments:
            if existing.stage == stage:
                return existing

        score, factors = self._score(active_factor_ids)
        numeric_mode = mode_for_score(score)

        escalation_reason: str | None = None
        floor_mode = numeric_mode
        if floor is not None:
            violations = floor.violations()
            if violations and _MODE_ORDER[numeric_mode] < _MODE_ORDER["standard"]:
                floor_mode = "standard"
                escalation_reason = "Fast Mode floor rule(s) failed: " + "; ".join(violations)

        # Upward-only: never drop below the strictest mode seen so far.
        previous_mode = self._current_mode
        new_mode = higher_mode(floor_mode, previous_mode)
        mode_changed = new_mode != previous_mode
        if mode_changed and escalation_reason is None:
            escalation_reason = f"Risk re-scored {previous_mode} -> {new_mode} (score {score})"

        assessment = Assessment(
            stage=stage,
            score=score,
            factors=factors,
            mode=new_mode,
            mode_changed=mode_changed,
            timestamp=now_iso(),
            escalation_reason=escalation_reason,
        )
        self._assessments.append(assessment)
        self._current_mode = new_mode
        return assessment

    @property
    def initial_score(self) -> int | None:
        for a in self._assessments:
            if a.stage == "initial":
                return a.score
        return None

    @property
    def final_score(self) -> int | None:
        return self._assessments[-1].score if self._assessments else None

    @property
    def mode_escalated(self) -> bool:
        """True if any recorded assessment changed the mode."""
        return any(a.mode_changed for a in self._assessments)

    def to_dict(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "current_mode": self._current_mode,
            "current_score": self.final_score if self.final_score is not None else 0,
            "assessments": [a.to_dict() for a in self._assessments],
        }

    def load_from_dict(self, doc: dict[str, Any]) -> None:
        """Repopulate the engine from a persisted risk_assessment.json (resume)."""
        self._assessments = [
            Assessment(
                stage=a["stage"],
                score=a["score"],
                factors=a["factors"],
                mode=a["mode"],
                mode_changed=a.get("mode_changed", False),
                timestamp=a["timestamp"],
                escalation_reason=a.get("escalation_reason"),
                assessed_by=list(a.get("assessed_by", ["controller"])),
            )
            for a in doc.get("assessments", [])
        ]
        self._current_mode = doc.get("current_mode", self._current_mode)
