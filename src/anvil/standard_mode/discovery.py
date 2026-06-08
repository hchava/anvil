"""LLM source discovery augmentation (Milestone 4).

Wraps deterministic discovery with three semantic expansion agents:
  Agent 0A — codebase semantic expansion
  Agent 0B — documentation / runbook expansion
  Agent 0C — discussion / context expansion

Each agent receives context (run_id, existing source_manifest, task contract)
and returns a list of new source dicts to add to the manifest.  If an agent
is called but returns no new sources, a negative_discovery_attempt is recorded
in the manifest so reviewers know the agent ran and found nothing.

Negative attempts are stored under the optional ``negative_discovery_attempts``
key which is added to the source_manifest for M4 (backward-compatible: the
field is optional in the schema).
"""

from __future__ import annotations

from typing import Any, Callable

from ..timeutil import now_iso

# Source types that are considered code-bearing for evidence-quality checks.
CODE_BEARING_TYPES = frozenset(["code", "test", "config", "migration", "lockfile"])


def augment_source_manifest(
    manifest: dict[str, Any],
    agents: list[tuple[str, Callable[[dict[str, Any]], list[dict[str, Any]]]]],
    context: dict[str, Any],
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """Call each discovery agent and merge new sources into the manifest.

    Returns:
        (augmented_manifest, agents_launched_count, negative_attempts)

    Each agent in ``agents`` is a (label, callable) pair.  The callable
    receives the full context dict and returns a list of raw source dicts
    (without source_id — these are renumbered here).

    A source that already exists in the manifest (same path) is skipped to
    avoid duplicates.  If an agent returns an empty list, a
    negative_discovery_attempt record is added to ``manifest``.
    """
    existing_paths = {s["path"] for s in manifest.get("sources", [])}
    sources = list(manifest.get("sources", []))
    agents_launched = 0
    negative_attempts: list[dict[str, Any]] = list(
        manifest.get("negative_discovery_attempts", [])
    )

    for label, agent_fn in agents:
        agents_launched += 1
        try:
            new_sources = agent_fn(context) or []
        except Exception:
            new_sources = []

        added = 0
        for src in new_sources:
            path = src.get("path", "")
            if not path or path in existing_paths:
                continue
            # Ensure required fields have minimal defaults so schema validation
            # later can give a precise error rather than a confusing one.
            src.setdefault("source_type", "other")
            src.setdefault("discovered_by", [label])
            src.setdefault("reason_for_inclusion", f"Semantic expansion by {label}")
            src.setdefault("freshness", {"checked_at": now_iso()})
            sources.append(src)
            existing_paths.add(path)
            added += 1

        if added == 0:
            negative_attempts.append(
                {
                    "agent": label,
                    "searched_at": now_iso(),
                    "description": f"{label} found no new sources",
                }
            )

    # Renumber source_ids deterministically after merging.
    for idx, src in enumerate(sources, start=1):
        src["source_id"] = f"SRC-{idx:03d}"

    augmented = dict(manifest)
    augmented["sources"] = sources
    if negative_attempts:
        augmented["negative_discovery_attempts"] = negative_attempts

    return augmented, agents_launched, negative_attempts
