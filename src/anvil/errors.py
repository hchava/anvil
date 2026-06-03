"""Explicit error types for the Anvil runtime.

The runtime prefers raising specific errors over silent fallback, so callers
(and the CLI) can map each failure to a precise message and exit code.
"""

from __future__ import annotations


class AnvilError(Exception):
    """Base class for all Anvil runtime errors."""


class NotInitializedError(AnvilError):
    """Raised when the Anvil home has not been initialized yet."""


class AlreadyExistsError(AnvilError):
    """Raised when creating an entity whose id already exists."""


class NotFoundError(AnvilError):
    """Raised when a referenced entity does not exist."""


class ValidationError(AnvilError):
    """Raised when input or an on-disk config fails validation."""


class GitError(AnvilError):
    """Raised when an underlying git command fails."""


class LeaseConflictError(AnvilError):
    """Raised when an exclusive lease cannot be acquired due to a conflict."""

    def __init__(self, message: str, conflicting_lease_id: str | None = None) -> None:
        super().__init__(message)
        self.conflicting_lease_id = conflicting_lease_id


class StateTransitionError(AnvilError):
    """Raised when a lifecycle transition is not allowed from the current state."""
