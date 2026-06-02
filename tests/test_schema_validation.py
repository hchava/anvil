"""Schema validation tests for Anvil's gate-critical and supplementary schemas.

Milestone 0 foundation. This suite proves that:

1. Every JSON schema in ``schemas/`` is itself a well-formed draft 2020-12 schema
   with an ``$id``.
2. Every Milestone 0 schema is present and exercised by >= 1 valid and >= 1
   invalid fixture (coverage is asserted for *every discovered* schema, so a new
   schema added without fixtures fails the suite).
3. Every valid fixture passes its schema.
4. Every invalid fixture fails its schema *for the intended reason* — each
   invalid fixture has a sidecar ``<name>.expect.json`` naming the JSON Schema
   keyword and/or instance path the failure must occur at. This stops an invalid
   fixture from "passing the test" by failing for an unrelated reason.

Validation uses ``Draft202012Validator`` with a ``FormatChecker`` so that
``format: date-time`` and friends are actually enforced. Schemas are
self-contained (no cross-file ``$ref``), so validation is fully offline and
deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"
VALID_DIR = REPO_ROOT / "tests" / "fixtures" / "valid"
INVALID_DIR = REPO_ROOT / "tests" / "fixtures" / "invalid"

FORMAT_CHECKER = FormatChecker()

# Every schema named in the Milestone 0 scope. Used to assert none silently
# disappeared. Coverage (valid + invalid fixtures) is asserted for the union of
# this set and whatever else is discovered on disk.
EXPECTED_SCHEMAS = {
    # Gate-critical
    "run_manifest",
    "task_contract",
    "risk_assessment",
    "source_manifest",
    "gap_matrix",
    "claim_ledger",
    "issue_ledger",
    "guardrail_matrix",
    "execution_work_orders",
    "run_scorecard",
    "event_log_line",
    "worktree_manifest",
    "validation_results",
    # Supplementary
    "controller_state",
    "baseline_validation",
    "command_result",
    "agent_status",
    "agent_quality_report",
    "review_findings_raw",
    "review_findings_consolidated",
    "human_decision",
    "project",
    "repo",
}


def _schema_stem(path: Path) -> str:
    """``run_manifest.schema.json`` -> ``run_manifest``."""
    return path.name[: -len(".schema.json")]


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _discover_schemas() -> dict[str, Path]:
    return {_schema_stem(p): p for p in sorted(SCHEMAS_DIR.glob("*.schema.json"))}


def _fixture_cases(root: Path) -> list[tuple[str, Path]]:
    """Collect (schema_stem, fixture_path) pairs, skipping .expect.json sidecars."""
    cases: list[tuple[str, Path]] = []
    if not root.exists():
        return cases
    for schema_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for fixture in sorted(schema_dir.glob("*.json")):
            if fixture.name.endswith(".expect.json"):
                continue
            cases.append((schema_dir.name, fixture))
    return cases


SCHEMAS = _discover_schemas()
ALL_SCHEMA_STEMS = sorted(set(SCHEMAS) | EXPECTED_SCHEMAS)
VALID_CASES = _fixture_cases(VALID_DIR)
INVALID_CASES = _fixture_cases(INVALID_DIR)


def _validator_for(stem: str) -> Draft202012Validator:
    assert stem in SCHEMAS, f"No schema file found for fixture group '{stem}'"
    return Draft202012Validator(_load_json(SCHEMAS[stem]), format_checker=FORMAT_CHECKER)


def _case_id(case: tuple[str, Path]) -> str:
    stem, path = case
    return f"{stem}/{path.name}"


# ---------------------------------------------------------------------------
# Schema integrity
# ---------------------------------------------------------------------------

def test_all_expected_schemas_present() -> None:
    """Every Milestone 0 schema exists on disk."""
    missing = sorted(EXPECTED_SCHEMAS - set(SCHEMAS))
    assert not missing, f"Missing schema files: {missing}"


@pytest.mark.parametrize("stem", sorted(SCHEMAS), ids=sorted(SCHEMAS))
def test_schema_is_valid_draft_2020_12(stem: str) -> None:
    """Each schema is itself a valid draft 2020-12 schema with an $id."""
    schema = _load_json(SCHEMAS[stem])
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", (
        f"{stem} must declare the draft 2020-12 $schema"
    )
    assert "$id" in schema, f"{stem} must declare an $id"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:  # pragma: no cover - failure path
        pytest.fail(f"Schema '{stem}' is not a valid draft 2020-12 schema: {exc.message}")


# ---------------------------------------------------------------------------
# Fixture coverage (asserted for every discovered schema, not just an allow-list)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stem", ALL_SCHEMA_STEMS, ids=ALL_SCHEMA_STEMS)
def test_schema_has_valid_and_invalid_fixtures(stem: str) -> None:
    """Every schema is exercised by at least one valid and one invalid fixture."""
    assert stem in SCHEMAS, f"Fixtures reference unknown schema '{stem}'"
    valid = [c for c in VALID_CASES if c[0] == stem]
    invalid = [c for c in INVALID_CASES if c[0] == stem]
    assert valid, f"Schema '{stem}' has no valid fixtures"
    assert invalid, f"Schema '{stem}' has no invalid fixtures"


def test_no_orphan_fixture_groups() -> None:
    """Every fixture group maps to a real schema."""
    groups = {c[0] for c in VALID_CASES} | {c[0] for c in INVALID_CASES}
    orphans = sorted(groups - set(SCHEMAS))
    assert not orphans, f"Fixture groups with no matching schema: {orphans}"


# ---------------------------------------------------------------------------
# Fixture validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", VALID_CASES, ids=[_case_id(c) for c in VALID_CASES])
def test_valid_fixtures_pass(case: tuple[str, Path]) -> None:
    stem, fixture = case
    validator = _validator_for(stem)
    errors = sorted(validator.iter_errors(_load_json(fixture)), key=str)
    assert not errors, (
        f"Expected '{fixture.name}' to satisfy schema '{stem}' but found "
        f"{len(errors)} error(s): "
        + "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
    )


@pytest.mark.parametrize("case", INVALID_CASES, ids=[_case_id(c) for c in INVALID_CASES])
def test_invalid_fixtures_fail(case: tuple[str, Path]) -> None:
    """Invalid fixtures must fail, and fail for the *intended* reason.

    Each invalid fixture has a sidecar ``<name>.expect.json`` of the form::

        {"keyword": "required", "path": ["claims", 0]}

    ``keyword`` (the failing validator, e.g. ``required``/``enum``/``pattern``/
    ``minItems``/``type``/``format``) is required. ``path`` (the instance path
    prefix the error must occur under) is optional. The test asserts at least
    one validation error matches both.
    """
    stem, fixture = case
    validator = _validator_for(stem)
    errors = list(validator.iter_errors(_load_json(fixture)))
    assert errors, (
        f"Expected '{fixture.name}' to violate schema '{stem}' but it validated cleanly"
    )

    expect_path = fixture.with_name(fixture.stem + ".expect.json")
    assert expect_path.exists(), (
        f"Invalid fixture '{fixture.name}' is missing its sidecar "
        f"'{expect_path.name}' describing the intended failure"
    )
    expect = _load_json(expect_path)
    want_keyword = expect["keyword"]
    want_path = expect.get("path")

    def matches(err) -> bool:
        if err.validator != want_keyword:
            return False
        if want_path is None:
            return True
        actual = list(err.absolute_path)
        return actual[: len(want_path)] == list(want_path)

    matching = [e for e in errors if matches(e)]
    assert matching, (
        f"'{fixture.name}' failed, but not for the intended reason "
        f"(keyword={want_keyword!r}, path={want_path!r}). Actual errors: "
        + "; ".join(f"{e.validator}@{list(e.absolute_path)}: {e.message}" for e in errors)
    )
