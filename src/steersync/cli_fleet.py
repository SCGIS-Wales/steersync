"""`steersync fleet`: every repository steersync has worked in on this machine.

The group is registered on the main command in :mod:`steersync.cli`. The rich and
questionary based interface lives in :mod:`steersync.fleet_ui` and is imported only when
needed, so other commands keep their start up time.
"""

import json
import os
from pathlib import Path

import click

from steersync import discovery, registry
from steersync.cli_common import _bootstrap
from steersync.config import load_config
from steersync.errors import SteersyncError
from steersync.paths import registry_path


@click.group("fleet", invoke_without_command=True)
@click.pass_context
def fleet(ctx: click.Context) -> None:
    """Every repository set up for steersync on this machine: status, push and pull.

    Without a subcommand on a terminal this opens the interactive overview; otherwise it
    prints the status table.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(fleet_list)


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
