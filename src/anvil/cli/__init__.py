"""Anvil CLI — command-line interface for the Anvil harness.

Milestone 0.5 wires the registry commands (init / repo / project / scope /
status / pause / resume / abort / leases) to :class:`anvil.registry.Registry`.
``run`` remains a Milestone 1 stub.
"""

from __future__ import annotations

import click

from anvil import __version__
from anvil.errors import AnvilError
from anvil.registry import Registry


def _registry() -> Registry:
    return Registry()


def _fail(message: str) -> None:
    raise click.ClickException(message)


@click.group()
@click.version_option(version=__version__, prog_name="anvil")
def cli():
    """Anvil: Where AI-generated code gets hardened before it ships."""
    pass


# ── Init ──────────────────────────────────────────────────────────────────────


@cli.command()
def init():
    """Initialize a local Anvil installation."""
    reg = _registry()
    reg.init()
    click.echo(f"✓ Anvil initialized at {reg.paths.home}")


@cli.command()
def doctor():
    """Check that the Anvil installation is healthy."""
    reg = _registry()
    if not reg.paths.exists():
        _fail(f"No Anvil installation at {reg.paths.home}. Run `anvil init` first.")
    projects = len(reg.list_projects())
    repos = len(reg.list_repos())
    runs = len(reg.list_runs())
    click.echo(f"✓ Installation OK at {reg.paths.home}")
    click.echo(f"  projects={projects} repos={repos} runs={runs}")


# ── Project Management ────────────────────────────────────────────────────────


@cli.group()
def project():
    """Manage projects."""
    pass


@project.command("create")
@click.option("--name", "project_id", required=True, help="Project id (proj-...)")
@click.option("--repo", "repo_ids", required=True, multiple=True, help="Repo id to reference (repeatable)")
def project_create(project_id: str, repo_ids: tuple[str, ...]):
    """Create a new project referencing one or more registered repos."""
    try:
        cfg = _registry().create_project(project_id, list(repo_ids))
    except AnvilError as exc:
        _fail(str(exc))
    click.echo(f"✓ Created project '{cfg.project_id}' referencing {', '.join(cfg.repos)}")


@project.command("list")
def project_list():
    """List all projects."""
    reg = _registry()
    try:
        rows = reg.list_projects()
    except AnvilError as exc:
        _fail(str(exc))
    if not rows:
        click.echo("No projects registered yet.")
        return
    for row in rows:
        click.echo(f"{row['project_id']}\t{row['name']}")


# ── Repo Management ──────────────────────────────────────────────────────────


@cli.group()
def repo():
    """Manage repositories."""
    pass


@repo.command("register")
@click.option("--path", required=True, type=click.Path(exists=True), help="Path to git repo")
@click.option("--name", "repo_id", required=True, help="Repo id (repo-...)")
def repo_register(path: str, repo_id: str):
    """Register a git repository as a shared resource."""
    try:
        cfg = _registry().register_repo(repo_id, path)
    except AnvilError as exc:
        _fail(str(exc))
    click.echo(f"✓ Registered repo '{cfg.repo_id}' at {cfg.path} (default branch {cfg.default_branch})")


@repo.command("list")
def repo_list():
    """List all registered repos."""
    reg = _registry()
    try:
        rows = reg.list_repos()
    except AnvilError as exc:
        _fail(str(exc))
    if not rows:
        click.echo("No repos registered yet.")
        return
    for row in rows:
        click.echo(f"{row['repo_id']}\t{row['path']}\t{row['default_branch']}")


# ── Scope Management ─────────────────────────────────────────────────────────


@cli.group()
def scope():
    """Manage task scopes within projects."""
    pass


@scope.command("create")
@click.option("--project", "project_id", required=True, help="Project id")
@click.option("--scope", "scope_id", required=True, help="Scope id")
@click.option("--root-paths", required=True, help="Comma-separated root paths")
def scope_create(project_id: str, scope_id: str, root_paths: str):
    """Create a task scope within a project."""
    roots = [p.strip() for p in root_paths.split(",") if p.strip()]
    try:
        _registry().create_scope(project_id, scope_id, roots)
    except AnvilError as exc:
        _fail(str(exc))
    click.echo(f"✓ Created scope '{scope_id}' in project '{project_id}' over {', '.join(roots)}")


# ── Run Management ────────────────────────────────────────────────────────────


@cli.command()
@click.option("--project", required=True, help="Project id")
@click.option("--scope", default=None, help="Task scope (optional)")
@click.option("--task", required=True, help="Task description")
@click.option("--mode", default=None, type=click.Choice(["fast", "standard", "critical"]),
              help="Override mode (default: auto from risk score)")
@click.option("--dry-run", is_flag=True, help="Run with fixture artifacts, no LLM calls")
def run(project: str, scope: str | None, task: str, mode: str | None, dry_run: bool):
    """Start a new harness run. (Milestone 1 — not yet wired.)"""
    click.echo(f"Starting run for project '{project}'...")
    if dry_run:
        click.echo("  (dry-run mode — using fixture artifacts)")
    # Milestone 1 — create run in registry, allocate worktree, start state machine.
    click.echo("`anvil run` is implemented in Milestone 1.")


@cli.command()
@click.option("--project", "project_id", default=None, help="Filter by project")
@click.option("--repo", "repo_id", default=None, help="Filter by repo")
def status(project_id: str | None, repo_id: str | None):
    """Show status of runs (optionally filtered by project or repo)."""
    reg = _registry()
    try:
        rows = reg.list_runs(project_id=project_id, repo_id=repo_id)
    except AnvilError as exc:
        _fail(str(exc))
    if not rows:
        click.echo("No runs.")
        return
    click.echo("RUN_ID\tPROJECT\tREPO\tSCOPE\tLIFECYCLE\tPIPELINE\tMODE")
    for r in rows:
        click.echo(
            f"{r['run_id']}\t{r['project_id']}\t{r['repo_id']}\t{r['task_scope_id'] or '-'}\t"
            f"{r['lifecycle_state']}\t{r['pipeline_state']}\t{r['mode']}"
        )


@cli.command()
@click.argument("run_id")
def pause(run_id: str):
    """Pause an active run (lifecycle only; pipeline state preserved)."""
    try:
        r = _registry().pause_run(run_id)
    except AnvilError as exc:
        _fail(str(exc))
    click.echo(f"✓ Paused {run_id} (lifecycle={r['lifecycle_state']}, pipeline={r['pipeline_state']})")


@cli.command()
@click.argument("run_id")
def resume(run_id: str):
    """Resume a paused run (lifecycle only; pipeline state preserved)."""
    try:
        r = _registry().resume_run(run_id)
    except AnvilError as exc:
        _fail(str(exc))
    click.echo(f"✓ Resumed {run_id} (lifecycle={r['lifecycle_state']}, pipeline={r['pipeline_state']})")


@cli.command()
@click.argument("run_id")
def abort(run_id: str):
    """Abort a run: release leases and remove its worktree."""
    try:
        r = _registry().abort_run(run_id)
    except AnvilError as exc:
        _fail(str(exc))
    click.echo(f"✓ Aborted {run_id} (lifecycle={r['lifecycle_state']})")


# ── Leases ────────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--repo", "repo_id", required=True, help="Repo id")
@click.option("--release-stale", is_flag=True, help="Force-release expired leases of non-active runs")
def leases(repo_id: str, release_stale: bool):
    """Show active leases for a repo (or force-release stale ones)."""
    reg = _registry()
    try:
        reg.get_repo(repo_id)
        if release_stale:
            released = reg.force_release_stale_leases(repo_id=repo_id)
            if not released:
                click.echo("No stale leases to release.")
            else:
                click.echo(f"✓ Force-released {len(released)} stale lease(s): {', '.join(released)}")
            return
        rows = reg.list_leases(repo_id=repo_id, active_only=True)
    except AnvilError as exc:
        _fail(str(exc))
    if not rows:
        click.echo(f"No active leases for repo '{repo_id}'.")
        return
    click.echo("LEASE_ID\tTYPE\tRUN\tSCOPE\tACCESS\tEXPIRES")
    for lrow in rows:
        click.echo(
            f"{lrow['lease_id']}\t{lrow['lease_type']}\t{lrow['run_id']}\t"
            f"{lrow['scope']}\t{lrow['access']}\t{lrow['expires_at'] or '-'}"
        )


# ── Review ────────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--project", required=True, help="Project id")
def review(project: str):
    """Trigger periodic harness review for a project. (Milestone 8.)"""
    click.echo(f"Harness review for '{project}' — not enough data yet.")


if __name__ == "__main__":
    cli()
