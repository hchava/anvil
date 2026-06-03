"""Time helpers — single source of UTC, ISO-8601 timestamps.

Centralizing this keeps every stored timestamp in the same ``...Z`` format and
makes it trivial to inject a fixed clock in tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def iso_after_seconds(seconds: int, *, base: datetime | None = None) -> str:
    """ISO-8601 UTC string ``seconds`` in the future from ``base`` (default now)."""
    start = base or utcnow()
    return to_iso(start + timedelta(seconds=seconds))


def to_iso(moment: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC string with a trailing ``Z``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return to_iso(utcnow())
