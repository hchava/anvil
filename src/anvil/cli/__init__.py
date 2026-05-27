"""Anvil CLI — command-line interface for the Anvil harness."""

import click

from anvil import __version__


@click.group()
@click.version_option(version=__version__, prog_name="anvil")
def cli():
    """Anvil: Where AI-generated code gets hardened before it ships."""
    pass


# ── Init ──────────────────────────────────────────────────────────────────────


@cli.command()
def init():
    """Initialize a local Anvil installation."""
    click.echo("Initializing Anvil...")
    # TODO: Milestone 0.5A — create ~/.anvil/, registry.sqlite, installation.json
    click.echo("✓ Anvil initialized at ~/.anvil/")


@cli.command()
def doctor():
    """Check that the Anvil installation is healthy."""
    click.echo("Running health checks...")
    # TODO: Milestone 0.5A — check SQLite, schemas, git, tmux
    click.echo("✓ All checks passed")


# ── Project Management ────────────────────────────────────────────────────────


@cli.group()
def project():
    """Manage projects."""
    pass


@project.command("create")
@click.option("--name", required=True, help="Project name")
@click.option("--repo", required=True, help="Repo ID to bind to")
def project_create(name: str, repo: str):
    """Create a new project."""
    # TODO: Milestone 0.5A — insert into projects table, create project dir
    click.echo(f"✓ Created project '{name}' bound to repo '{repo}'")


@project.command("list")
def project_list():
    """List all projects."""
    # TODO: Milestone 0.5A — query projects table
    click.echo("No projects registered yet.")


# ── Repo Management ──────────────────────────────────────────────────────────


@cli.group()
def repo():
    """Manage repositories."""
    pass


@repo.command("register")
@click.option("--path", required=True, type=click.Path(exists=True), help="Path to git repo")
@click.option("--name", required=True, help="Repo ID")
def repo_register(path: str, name: str):
    """Register a git repository."""
    # TODO: Milestone 0.5A — normalize path, insert into repos table
    click.echo(f"✓ Registered repo '{name}' at {path}")


@repo.command("list")
def repo_list():
    """List all registered repos."""
    # TODO: Milestone 0.5A — query repos table
    click.echo("No repos registered yet.")


# ── Scope Management ─────────────────────────────────────────────────────────


@cli.group()
def scope():
    """Manage task scopes within projects."""
    pass


@scope.command("create")
@click.option("--project", "project_name", required=True, help="Project name")
@click.option("--scope", "scope_name", required=True, help="Scope name")
@click.option("--root-paths", required=True, help="Comma-separated root paths")
def scope_create(project_name: str, scope_name: str, root_paths: str):
    """Create a task scope within a project."""
    # TODO: Milestone 0.5A — update project config
    click.echo(f"✓ Created scope '{scope_name}' in project '{project_name}'")


# ── Run Management ────────────────────────────────────────────────────────────


@cli.command()
@click.option("--project", required=True, help="Project name")
@click.option("--scope", default=None, help="Task scope (optional)")
@click.option("--task", required=True, help="Task description")
@click.option("--mode", default=None, type=click.Choice(["fast", "standard", "critical"]),
              help="Override mode (default: auto from risk score)")
@click.option("--dry-run", is_flag=True, help="Run with fixture artifacts, no LLM calls")
def run(project: str, scope: str | None, task: str, mode: str | None, dry_run: bool):
    """Start a new harness run."""
    click.echo(f"Starting run for project '{project}'...")
    if dry_run:
        click.echo("  (dry-run mode — using fixture artifacts)")
    # TODO: Milestone 1 — create run in registry, allocate worktree, start state machine
    click.echo("✓ Run created: RUN-XXXXXXXX-001")


@cli.command()
@click.option("--project", default=None, help="Filter by project")
@click.option("--repo", "repo_name", default=None, help="Filter by repo")
def status(project: str | None, repo_name: str | None):
    """Show status of active runs."""
    # TODO: Milestone 0.5A — query runs table
    click.echo("No active runs.")


@cli.command()
@click.argument("run_id")
def pause(run_id: str):
    """Pause an active run."""
    # TODO: Milestone 0.5B — update lifecycle state, handle leases
    click.echo(f"✓ Paused {run_id}")


@cli.command()
@click.argument("run_id")
def resume(run_id: str):
    """Resume a paused run."""
    # TODO: Milestone 0.5B — reacquire leases, check drift, resume state machine
    click.echo(f"✓ Resumed {run_id}")


@cli.command()
@click.argument("run_id")
def abort(run_id: str):
    """Abort a run."""
    # TODO: Milestone 0.5B — release leases, clean worktree, write scorecard
    click.echo(f"✓ Aborted {run_id}")


# ── Leases ────────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--repo", "repo_name", required=True, help="Repo ID")
def leases(repo_name: str):
    """Show active leases for a repo."""
    # TODO: Milestone 0.5B — query leases table
    click.echo(f"No active leases for repo '{repo_name}'.")


# ── Review ────────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--project", required=True, help="Project name")
def review(project: str):
    """Trigger periodic harness review for a project."""
    # TODO: Milestone 8 — analyze scorecard history, recommend pruning
    click.echo(f"Harness review for '{project}' — not enough data yet.")


if __name__ == "__main__":
    cli()
