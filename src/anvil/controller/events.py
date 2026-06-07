"""Append-only event log writer (Milestone 1).

Writes ``event_log.jsonl`` in the run directory. Each line is validated against
event_log_line.schema and carries a monotonically increasing ``seq``. Appends
are atomic at the line level (open in append mode, single ``write`` of one
newline-terminated JSON record, flush + fsync).

Mode-sensitive failure behavior: for gate-critical artifacts the controller
fails closed in Standard/Critical mode; the event log itself tolerates a write
error in any mode (it is an audit trail, not a gate) and surfaces it without
crashing the run. That tolerance is expressed by ``append`` returning the seq it
attempted and never raising on a best-effort basis when ``tolerate_errors`` is
set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX only (macOS/Linux); the project targets these.
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from ..schemas_util import assert_valid
from ..timeutil import now_iso


class EventLog:
    """Atomic, monotonic, schema-validated JSONL event log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = path.with_name(path.name + ".lock")
        self._seq = self._highest_seq() + 1

    def _highest_seq(self) -> int:
        """Resume the counter from an existing log (supports controller restart)."""
        if not self.path.exists():
            return -1
        highest = -1
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seq = record.get("seq")
                if isinstance(seq, int) and seq > highest:
                    highest = seq
        return highest

    @property
    def next_seq(self) -> int:
        return self._seq

    def append(
        self,
        event_type: str,
        actor: str = "controller",
        *,
        state_before: str | None = None,
        state_after: str | None = None,
        details: dict[str, Any] | None = None,
        artifact_ref: str | None = None,
        error: str | None = None,
        tolerate_errors: bool = True,
    ) -> int:
        """Append one event; return its seq.

        Sequence assignment is atomic across processes/instances: under an
        exclusive file lock we recompute the next seq from the on-disk log, so
        two controllers writing to the same log can never both emit the same seq.
        """
        record: dict[str, Any] = {
            "timestamp": now_iso(),
            "event_type": event_type,
            "actor": actor,
        }
        if state_before is not None:
            record["state_before"] = state_before
        if state_after is not None:
            record["state_after"] = state_after
        if details is not None:
            record["details"] = details
        if artifact_ref is not None:
            record["artifact_ref"] = artifact_ref
        if error is not None:
            record["error"] = error

        lock_handle = None
        try:
            lock_handle = open(self._lock_path, "w", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            seq = max(self._seq, self._highest_seq() + 1)
            record["seq"] = seq
            assert_valid("event_log_line", record)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                # The audit log is not a gate; never crash the run on a log failure.
                if not tolerate_errors:
                    raise
            self._seq = seq + 1
            return seq
        finally:
            if lock_handle is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    except OSError:  # pragma: no cover
                        pass
                lock_handle.close()

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
