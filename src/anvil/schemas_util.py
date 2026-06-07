"""Reusable JSON-schema validation against the Milestone 0 schema set.

Centralizes loading + caching of the schemas in ``schemas/`` so the controller
validates every artifact the same way the test suite does (draft 2020-12 with a
FormatChecker, so ``format: date-time`` is enforced).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ValidationError

_FORMAT_CHECKER = FormatChecker()
# src/anvil/schemas_util.py -> parents[2]/schemas
_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    schema_path = _SCHEMAS_DIR / f"{schema_name}.schema.json"
    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    return Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def validate_artifact(schema_name: str, payload: Any) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    errors = sorted(_validator(schema_name).iter_errors(payload), key=str)
    return [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def assert_valid(schema_name: str, payload: Any) -> None:
    """Raise :class:`ValidationError` if ``payload`` violates ``schema_name``."""
    errors = validate_artifact(schema_name, payload)
    if errors:
        raise ValidationError(f"{schema_name} is invalid: {'; '.join(errors)}")
