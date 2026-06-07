"""Read-only command policy for baseline capture (Milestone 1).

Milestone 1 is a deterministic dry run: it must never run an arbitrary,
worktree-mutating command. Baseline commands are validators (tests, linters,
type checks), so this module enforces a read-only allowlist and rejects shells,
inline code (``-c``), and known mutating binaries before anything executes.

This is intentionally conservative for Milestone 1; the richer command-policy
engine (allowlists per mode, env scrubbing) is a later milestone.
"""

from __future__ import annotations

import os

# Validator binaries that do not modify the working tree.
_ALLOWED_BINARIES = {
    "pytest",
    "py.test",
    "ruff",
    "mypy",
    "flake8",
    "pyflakes",
    "pylint",
    "pyright",
    "tsc",
    "eslint",
}

# Formatters can rewrite files; only their check/diff modes are read-only.
_CHECK_ONLY_BINARIES = {"black", "isort"}

# git is allowed only for read-only subcommands.
_ALLOWED_GIT_SUBCOMMANDS = {
    "status",
    "rev-parse",
    "log",
    "diff",
    "show",
    "ls-files",
    "cat-file",
    "describe",
}

_PYTHON = {"python", "python3"}
_ALLOWED_PY_MODULES = {"pytest", "mypy", "ruff", "flake8", "pylint", "pyright", "unittest"}

# Shell / indirection binaries are never allowed (they can run arbitrary code).
_BLOCKED_BINARIES = {
    "sh", "bash", "zsh", "fish", "dash", "csh", "tcsh", "ksh",
    "env", "eval", "xargs", "rm", "mv", "cp", "tee", "dd", "chmod", "chown",
}

# Flags that make an otherwise read-only tool WRITE to the filesystem. These are
# rejected on any baseline command, regardless of binary, because binary-level
# allowlisting alone is bypassable (e.g. `git diff --output=FILE`, `ruff --fix`,
# `eslint --fix`, `pytest --cache-clear`). The worktree cleanliness check in the
# controller is the backstop for anything this list misses.
_WRITE_FLAGS_EXACT = {
    "--fix", "--unsafe-fixes", "--apply", "--write", "--in-place",
    "--cache-clear", "--create", "--output", "--out", "--output-file",
    "-w", "-i", "-o",
}
_WRITE_FLAG_PREFIXES = ("--output", "--out", "--output-file", "-o")


def _write_flag(args: list[str]) -> str | None:
    """Return the first filesystem-writing flag found, else None."""
    for arg in args:
        if arg in _WRITE_FLAGS_EXACT:
            return arg
        for prefix in _WRITE_FLAG_PREFIXES:
            if arg.startswith(prefix + "="):
                return arg
    return None


def check_read_only(command_array: list[str]) -> tuple[bool, str | None]:
    """Return (allowed, reason). ``reason`` is set only when not allowed."""
    if not command_array:
        return False, "empty command"
    binary = os.path.basename(command_array[0])

    if binary in _BLOCKED_BINARIES:
        return False, f"binary '{binary}' is not allowed in a dry-run baseline command"
    if "-c" in command_array:
        return False, "inline code ('-c') is not allowed in a dry-run baseline command"

    write_flag = _write_flag(command_array[1:])
    if write_flag is not None:
        return False, f"flag '{write_flag}' can modify the filesystem; not allowed in a dry run"

    if binary == "git":
        sub = command_array[1] if len(command_array) > 1 else ""
        if sub not in _ALLOWED_GIT_SUBCOMMANDS:
            return False, f"git subcommand '{sub}' is not a read-only operation"
        return True, None

    if binary in _PYTHON:
        if (
            len(command_array) >= 3
            and command_array[1] == "-m"
            and command_array[2] in _ALLOWED_PY_MODULES
        ):
            return True, None
        return False, "python baseline commands must be 'python -m <validator>'"

    if binary in _CHECK_ONLY_BINARIES:
        if "--check" in command_array or "--diff" in command_array:
            return True, None
        return False, f"formatter '{binary}' must run in --check/--diff mode for a dry run"

    if binary in _ALLOWED_BINARIES:
        return True, None

    return False, f"binary '{binary}' is not an allowed read-only validator"
