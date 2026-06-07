"""Tests for the file ownership tracker (Milestone 3)."""

from __future__ import annotations

from anvil.executor.ownership import FileOwnershipTracker


def test_track_single_file() -> None:
    tracker = FileOwnershipTracker()
    tracker.track("src/app.py", "EXEC-001", access="write", sequence=1)
    listing = tracker.to_list()
    assert len(listing) == 1
    assert listing[0]["file_path"] == "src/app.py"
    assert listing[0]["owners"][0]["work_order_id"] == "EXEC-001"
    assert listing[0]["owners"][0]["access"] == "write"
    assert listing[0]["owners"][0]["sequence"] == 1


def test_track_multiple_files() -> None:
    tracker = FileOwnershipTracker()
    tracker.track("src/a.py", "EXEC-001")
    tracker.track("src/b.py", "EXEC-001")
    listing = tracker.to_list()
    assert len(listing) == 2
    paths = {e["file_path"] for e in listing}
    assert paths == {"src/a.py", "src/b.py"}


def test_track_work_order_bulk() -> None:
    tracker = FileOwnershipTracker()
    files = ["src/a.py", "src/b.py", "src/c.py"]
    tracker.track_work_order("EXEC-002", files, access="write", sequence=1)
    listing = tracker.to_list()
    assert len(listing) == 3
    for entry in listing:
        assert entry["owners"][0]["work_order_id"] == "EXEC-002"


def test_multiple_owners_per_file() -> None:
    """Two work orders can share a file (e.g. second is read-only)."""
    tracker = FileOwnershipTracker()
    tracker.track("shared/config.py", "EXEC-001", access="write", sequence=1)
    tracker.track("shared/config.py", "EXEC-002", access="read", sequence=2)
    listing = tracker.to_list()
    assert len(listing) == 1
    owners = listing[0]["owners"]
    assert len(owners) == 2
    assert owners[0]["access"] == "write"
    assert owners[1]["access"] == "read"
    assert owners[1]["sequence"] == 2


def test_empty_tracker_returns_empty_list() -> None:
    tracker = FileOwnershipTracker()
    assert tracker.to_list() == []


def test_to_list_structure_matches_roadmap_spec() -> None:
    """Output matches the roadmap's file_ownership structure."""
    tracker = FileOwnershipTracker()
    tracker.track("services/ingestion/config.py", "EXEC-001", access="write", sequence=1)
    tracker.track("services/ingestion/config.py", "EXEC-002", access="read", sequence=2)

    listing = tracker.to_list()
    entry = listing[0]

    assert "file_path" in entry
    assert "owners" in entry
    for owner in entry["owners"]:
        assert "work_order_id" in owner
        assert "access" in owner
        assert "sequence" in owner
