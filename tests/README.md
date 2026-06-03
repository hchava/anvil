# Tests

Milestone 0 ships a schema-validation suite (`test_schema_validation.py`) that
checks every JSON schema in `schemas/` against its valid and invalid fixtures.

Milestone 0.5 adds the runtime-registry suites:

- `test_registry_projects_repos.py` — init, repo registration, projects, the
  shared-repo invariant, task scopes.
- `test_runs_worktree.py` — run creation and per-run git worktree allocation.
- `test_leases.py` — `file_write` / `merge_queue` lease conflicts and release.
- `test_lease_safety.py` — adversarial cases: cross-connection lease races,
  duplicate repo identity, cross-repo leases, path traversal, paused-lease
  expiry + resume blocking, lifecycle resurrection, run-creation compensation,
  and controller-state JSON.
- `test_pause_resume.py` — lifecycle vs pipeline state; pause/resume preserves
  pipeline progress.
- `test_drift.py` — base-commit drift detection.
- `test_cli.py` — the `anvil` CLI end to end via Click's `CliRunner`.

These run against a temporary `ANVIL_HOME` and temporary local git repos
(`tests/conftest.py`), so they never touch your real `~/.anvil` and never use
the network.

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
