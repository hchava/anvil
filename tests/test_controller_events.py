"""Event log: monotonic sequence numbers, schema validity, atomic append."""

from __future__ import annotations

import json
from pathlib import Path

from anvil.controller import Controller, RunInputs
from anvil.controller.events import EventLog
from anvil.controller.risk import FloorRules
from anvil.schemas_util import validate_artifact

from tests import m1_fixtures


def test_event_seq_is_monotonic_and_zero_based(tmp_path: Path):
    log = EventLog(tmp_path / "event_log.jsonl")
    seqs = [
        log.append("run_finalized"),
        log.append("gate_passed"),
        log.append("artifact_written", artifact_ref="x.json"),
    ]
    assert seqs == [0, 1, 2]
    records = log.read_all()
    assert [r["seq"] for r in records] == [0, 1, 2]
    for r in records:
        assert validate_artifact("event_log_line", r) == []


def test_event_seq_resumes_from_existing_log(tmp_path: Path):
    path = tmp_path / "event_log.jsonl"
    first = EventLog(path)
    first.append("run_finalized")
    first.append("gate_passed")
    # A fresh EventLog over the same file continues the counter.
    second = EventLog(path)
    assert second.next_seq == 2
    assert second.append("artifact_written", artifact_ref="y.json") == 2


def test_run_event_log_seqs_are_contiguous(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    inputs = RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )
    Controller(env.registry, env.run_id).run(inputs)
    records = [json.loads(l) for l in (env.run_dir / "event_log.jsonl").read_text().splitlines() if l.strip()]
    seqs = [r["seq"] for r in records]
    assert seqs == list(range(len(seqs)))  # 0,1,2,... no gaps, no dupes
