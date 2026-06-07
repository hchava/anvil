"""Resume from controller_state.json after a simulated interruption."""

from __future__ import annotations

import json

from anvil.controller import Controller, RunInputs
from anvil.controller import states
from anvil.controller.risk import FloorRules

from tests import m1_fixtures


def _inputs(env) -> RunInputs:
    return RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )


def test_state_persisted_after_each_transition(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    state = json.loads((env.run_dir / "controller_state.json").read_text())
    assert state["current_state"] == states.FINALIZED
    # state_history is ordered and the last entry is FINALIZED.
    assert state["state_history"][-1]["state"] == states.FINALIZED
    assert state["mode"] == "standard"


def test_resume_loads_persisted_state(controller_env):
    """Simulate an interruption: run once, then a fresh Controller reads the
    persisted controller_state.json and recovers the last state + mode."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))

    resumed = Controller(env.registry, env.run_id)
    doc = resumed.load_state()
    assert doc is not None
    assert doc["current_state"] == states.FINALIZED
    assert doc["mode"] == "standard"
    # The in-memory controller picked up the persisted state.
    assert resumed._state == states.FINALIZED  # noqa: SLF001 - test introspection


def test_event_log_seq_continues_after_resume(controller_env):
    """A second controller instance over the same run keeps appending without
    resetting the sequence counter."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    records = [json.loads(l) for l in (env.run_dir / "event_log.jsonl").read_text().splitlines() if l.strip()]
    last_seq = records[-1]["seq"]

    resumed = Controller(env.registry, env.run_id)
    new_seq = resumed.events.append("run_finalized")
    assert new_seq == last_seq + 1


def test_load_state_returns_none_when_file_absent(controller_env):
    env = controller_env
    # The M0.5 registry seeds controller_state.json at run creation; remove it to
    # exercise the no-state path explicitly.
    (env.run_dir / "controller_state.json").unlink()
    ctrl = Controller(env.registry, env.run_id)
    assert ctrl.load_state() is None


def test_load_state_reads_registry_seeded_init(controller_env):
    """Before the controller runs, create_run (Milestone 0.5) has already seeded
    controller_state.json at INIT; load_state recovers it."""
    env = controller_env
    ctrl = Controller(env.registry, env.run_id)
    doc = ctrl.load_state()
    assert doc is not None
    assert doc["current_state"] == states.INIT
