# Tests

Milestone 0 ships a schema-validation suite (`test_schema_validation.py`) that
checks every JSON schema in `schemas/` against its valid and invalid fixtures.

## Setup

No system Python is guaranteed to have the test dependencies, so use a project
virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

This installs the runtime deps (`jsonschema`) plus the `dev` extra (`pytest`).

## Running

```bash
.venv/bin/python -m pytest
```

The suite is offline and deterministic — no network calls. It:

- meta-validates every schema as draft 2020-12 (with an `$id`),
- asserts each schema has at least one valid and one invalid fixture,
- validates fixtures with a `FormatChecker` (so `format: date-time` is enforced),
- and requires each invalid fixture to fail at the keyword/JSON path named in its
  `<name>.expect.json` sidecar (so a fixture can't "pass" by failing for an
  unrelated reason).
