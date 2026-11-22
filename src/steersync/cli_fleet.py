"""`steersync fleet`: every repository steersync has worked in on this machine.

The group is registered on the main command in :mod:`steersync.cli`. The rich and
questionary based interface lives in :mod:`steersync.fleet_ui` and is imported only when
needed, so other commands keep their start up time.
"""

import json
import os
import sys
from pathlib import Path

import click

from steersync import discovery, registry
from steersync import fleet as fleet_core
from steersync.cli_common import _bootstrap
from steersync.config import load_config
from steersync.errors import SteersyncError
from steersync.paths import registry_path
from steersync.retry import RetryEvent


@click.group("fleet", invoke_without_command=True)
@click.pass_context
def fleet(ctx: click.Context) -> None:
    """Every repository set up for steersync on this machine: status, push and pull.

    Without a subcommand on a terminal this opens the interactive overview; otherwise it
    prints the status table.
    """
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from steersync.cli_fleet_actions import interactive  # noqa: PLC0415 - lazy UI

            ctx.invoke(interactive)
        else:
            ctx.invoke(fleet_status)


@fleet.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
def fleet_list(as_json: bool) -> None:
    """List the registered repositories."""
    entries = registry.load(registry_path(os.environ)).entries
    if as_json:
        click.echo(json.dumps([e.model_dump() for e in entries.values()], indent=2))
        return
    if not entries:
        click.echo("no repositories registered yet; run `steersync fleet scan ~/code`")
        return
    for key in sorted(entries):
        entry = entries[key]
        state = "" if registry.is_work_tree(Path(key)) else "  (missing)"
        click.echo(f"{key}  [{entry.source}, last seen {entry.last_seen_at}]{state}")


@fleet.command("add")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path))
@click.pass_context
def fleet_add(ctx: click.Context, paths: tuple[Path, ...]) -> None:
    """Register repositories (default: the current one)."""
    if not paths:
        _, root = _bootstrap(ctx)
        if root is None:
            raise click.UsageError("not inside a git repository; pass a path")
        paths = (root,)
    bad = [str(p) for p in paths if not registry.is_work_tree(p)]
    if bad:
        raise click.UsageError(f"not a git work tree: {', '.join(bad)}")
    registry.upsert(registry_path(os.environ), paths, "manual")
    for path in paths:
        click.echo(f"registered: {path.resolve()}")


@fleet.command("remove")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
def fleet_remove(paths: tuple[Path, ...]) -> None:
    """Forget repositories (their files are not touched)."""
    _, removed = registry.remove(registry_path(os.environ), paths)
    for key in removed:
        click.echo(f"removed: {key}")
    missing = len(paths) - len(removed)
    if missing:
        click.echo(f"{missing} path(s) were not registered", err=True)


@fleet.command("prune")
def fleet_prune() -> None:
    """Forget repositories whose folder no longer exists or is no longer a git work tree."""
    _, pruned = registry.prune(registry_path(os.environ))
    for key in pruned:
        click.echo(f"pruned: {key}")
    if not pruned:
        click.echo("nothing to prune")


@fleet.command("scan")
@click.argument("roots", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--max-depth", type=click.IntRange(1, 12), help="Folder levels to search.")
@click.option("--add/--no-add", default=True, show_default=True, help="Register what is found.")
def fleet_scan(roots: tuple[Path, ...], max_depth: int | None, add: bool) -> None:
    """Find repositories set up for steersync below ROOTS (read only unless --add)."""
    env = os.environ
    try:
        config = load_config(workspace_root=None, env=env)
    except SteersyncError as exc:
        raise click.ClickException(str(exc)) from exc
    depth = max_depth or config.fleet.scan_max_depth
    found = discovery.scan(roots, depth)
    for repo in found:
        click.echo(str(repo))
    if add and found:
        registry.upsert(registry_path(env), found, "scan")
    click.echo(
        f"{len(found)} repositor{'y' if len(found) == 1 else 'ies'} found"
        + (" and registered" if add and found else ""),
        err=True,
    )


STATES = (
    "error",
    "diverged",
    "local-ahead",
    "central-ahead",
    "proposed",
    "in-sync",
    "unconfigured",
    "missing",
)
EXIT_PARTIAL = 5


def _report(refresh: bool | None, *, show_progress: bool) -> fleet_core.FleetReport:
    env = os.environ
    entries = registry.load(registry_path(env)).paths()

    def progress(event: tuple[str, str, str]) -> None:
        if show_progress:
            subject, phase, message = event
            click.echo(f"{subject}: {phase} {message}", err=True)

    def retried(event: RetryEvent) -> None:
        click.echo(f"  {event.render()}", err=True)

    return fleet_core.collect(entries, env, refresh=refresh, on_event=progress, on_retry=retried)


@fleet.command("status")
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.option(
    "--refresh/--no-refresh",
    default=None,
    help="Refresh the central repositories first (default: when older than "
    "fleet.refresh_max_age_seconds).",
)
@click.option("--state", "states", multiple=True, type=click.Choice(STATES), help="Only these.")
def fleet_status(as_json: bool, refresh: bool | None, states: tuple[str, ...]) -> None:
    """Show what is pushed and what is not for every registered repository."""
    report = _report(refresh, show_progress=not as_json)
    if states:
        report = report.model_copy(
            update={"repositories": tuple(r for r in report.repositories if r.state in states)}
        )
    if as_json:
        click.echo(json.dumps(report.document(), indent=2))
    else:
        from rich.console import Console  # noqa: PLC0415 - interface library, on demand

        from steersync.fleet_ui import render  # noqa: PLC0415

        render(report, Console(highlight=False), home=str(Path.home()))
    if any(r.state == "error" for r in report.repositories):
        sys.exit(EXIT_PARTIAL)


@fleet.command("doctor")
@click.option("--repair", is_flag=True, help="Apply the safe repairs (never touches work trees).")
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
def fleet_doctor(repair: bool, as_json: bool) -> None:
    """Check the registry, cache clones, sync records, locks and backups."""
    from steersync import fleet_doctor as doctor  # noqa: PLC0415

    checks = doctor.run(os.environ, repair=repair)
    if as_json:
        click.echo(json.dumps([c._asdict() for c in checks], indent=2))
    else:
        for item in checks:
            mark = "repaired" if item.repaired else "ok" if item.ok else "PROBLEM"
            click.echo(f"{mark:<9} {item.check:<12} {item.subject}: {item.detail}")
    if not all(item.ok for item in checks):
        if not as_json:
            click.echo(
                "problems remain; `steersync fleet doctor --repair` fixes the safe ones", err=True
            )
        sys.exit(EXIT_PARTIAL)
