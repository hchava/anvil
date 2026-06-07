"""File ownership tracker (Milestone 3).

Tracks which work orders own which files with access type and sequence number.

For Milestone 3 only a single work order is expected, but the data structure
is intentionally designed for future sequential work orders (Milestone 5+):
each file entry carries a list of owners with their sequence position, so
multiple work orders can declare overlapping reads without conflicts.

Output format (per the roadmap spec):
  {
    "file_path": "src/service/config.py",
    "owners": [
      {"work_order_id": "EXEC-001", "access": "write", "sequence": 1}
    ]
  }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _OwnerEntry:
    work_order_id: str
    access: str   # "read" | "write"
    sequence: int


@dataclass
class _FileOwnership:
    file_path: str
    owners: list[_OwnerEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "owners": [
                {
                    "work_order_id": o.work_order_id,
                    "access": o.access,
                    "sequence": o.sequence,
                }
                for o in self.owners
            ],
        }


class FileOwnershipTracker:
    def __init__(self) -> None:
        self._files: dict[str, _FileOwnership] = {}

    def track(
        self,
        file_path: str,
        work_order_id: str,
        access: str = "write",
        sequence: int = 1,
    ) -> None:
        if file_path not in self._files:
            self._files[file_path] = _FileOwnership(file_path=file_path)
        self._files[file_path].owners.append(
            _OwnerEntry(work_order_id=work_order_id, access=access, sequence=sequence)
        )

    def track_work_order(
        self,
        work_order_id: str,
        file_paths: list[str],
        access: str = "write",
        sequence: int = 1,
    ) -> None:
        for path in file_paths:
            self.track(path, work_order_id, access, sequence)

    def to_list(self) -> list[dict[str, Any]]:
        return [v.to_dict() for v in self._files.values()]
