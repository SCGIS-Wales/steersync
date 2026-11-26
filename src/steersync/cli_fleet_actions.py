"""Push, pull, restore and the interactive overview of ``steersync fleet``.

Push and pull build one plan (a step per repository, :mod:`steersync.fleet_plan`), show it
and ask once. The interactive overview uses questionary; every prompt goes through a
:class:`Prompts` value, so the flows are tested with scripted answers.
"""

import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

import click

from steersync import backups, fleet, fleet_plan, plan_flow, plan_select, registry
from steersync.cli_fleet import EXIT_PARTIAL, global_option, wants_global
from steersync.cli_fleet import fleet as fleet_group
from steersync.cli_options import plan_options
from steersync.cli_plan import run_plan
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.fleet import FleetReport, Outcome, RepoStatus
from steersync.paths import backup_root, registry_path
from steersync.plan import Plan, pending, run_steps
from steersync.retry import RetryEvent


class Prompts(NamedTuple):
    """Each returns the answer, or None when the user cancelled (Ctrl C, Escape)."""

    select: Callable[[str, list[str]], str | None]
    checkbox: Callable[..., list[str] | None]
    text: Callable[[str], str | None]
    confirm: Callable[[str, bool], bool | None]


def questionary_prompts() -> Prompts:
    import questionary  # noqa: PLC0415 - interface library, loaded on demand

    return Prompts(
        select=lambda message, choices: questionary.select(message, choices=choices).ask(),
        checkbox=lambda message, choices: questionary.checkbox(message, choices=choices).ask(),
        text=lambda message: questionary.text(message).ask(),
        confirm=lambda message, default: questionary.confirm(message, default=default).ask(),
    )


def global_path() -> str:
    """Where the global row lives (the home directory can change in tests)."""
    return str(Path("~/.kiro").expanduser().resolve())


def short(path: str) -> str:
    """``~/code/app`` instead of the full home directory, as in the status table."""
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + "/") else path


def _report(
    env: dict[str, str] | os._Environ[str],
    *,
    refresh: bool | None = None,
    include_global: bool | None = None,
) -> FleetReport:
    def retried(event: RetryEvent) -> None:
        click.echo(f"  {event.render()}", err=True)

    entries = registry.load(registry_path(env)).paths()
    if include_global is None:
        include_global = wants_global()
    return fleet.collect(
        entries, env, refresh=refresh, include_global=include_global, on_retry=retried
    )


def _select(
    report: FleetReport,
    repos: Iterable[Path],
    states: Iterable[str],
    all_rows: bool,
    reason: Callable[[RepoStatus], str | None],
) -> list[RepoStatus]:
    wanted_paths = {str(Path(p).expanduser().resolve()) for p in repos}
    wanted_states = set(states)
    if not (wanted_paths or wanted_states or all_rows):
        raise click.UsageError("choose repositories with --repo PATH, --state STATE or --all")
    rows = []
    for row in report.repositories:
        key_matches = row.path in wanted_paths or (
            row.is_global() and wanted_paths & {global_path()}
        )
        if key_matches or row.state in wanted_states or (all_rows and reason(row) is None):
            rows.append(row)
    unknown = wanted_paths - {r.path for r in report.repositories}
    unknown.discard(global_path())
    if unknown:
        raise click.UsageError(f"not registered: {', '.join(sorted(unknown))}")
    return rows


def _summary(outcomes: Sequence[Outcome], *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps([o.model_dump() for o in outcomes], indent=2))
        return
    click.echo()
    for outcome in outcomes:
        if outcome.status in {"done", "dry-run"}:
            extra = outcome.merge_request or outcome.branch or ""
            if extra and extra in outcome.detail:
                extra = ""
            click.echo(
                f"{outcome.status:<8} {short(outcome.path)}: {outcome.detail} {extra}".rstrip()
            )
            for backup in outcome.backups:
                click.echo(f"         backup: {backup}")
    for outcome in outcomes:
        if outcome.status == "skipped":
            click.echo(f"skipped  {short(outcome.path)}: {outcome.detail}")
    for outcome in outcomes:
        if outcome.status == "failed":
            click.echo(f"FAILED   {short(outcome.path)}: {outcome.detail}", err=True)
            if outcome.rerun:
                click.echo(f"         rerun: {outcome.rerun}", err=True)


repo_option = click.option(
    "--repo", "repos", multiple=True, type=click.Path(path_type=Path), help="A repository."
)


def _machine_config() -> Config:
    try:
        return load_config(workspace_root=None, env=os.environ)
    except SteersyncError:
        return Config()


def _run_fleet(
    action: fleet_plan.Action,
    repos: tuple[Path, ...],
    states: tuple[str, ...],
    all_rows: bool,
    plan_flags: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> None:
    # Act on the current central state: always refresh before changing anything.
    # --repo ~/.kiro asks for the global row by name, whatever the default.
    named_global = any(str(Path(p).expanduser().resolve()) == global_path() for p in repos)
    report = _report(os.environ, refresh=True, include_global=wants_global(forced=named_global))
    reason = fleet.push_reason if action == "push" else fleet.pull_reason
    rows = _select(report, repos, states, all_rows, reason)
    if not rows:
        click.echo("nothing selected")
        return
    home = str(Path.home())
    sink: list[Outcome] = []
    plan = fleet_plan.build_fleet_plan(
        action, rows, os.environ, sink=sink, overwrite=overwrite, home=home
    )
    as_json = bool(plan_flags.get("as_json"))
    result = run_plan(
        plan,
        _machine_config(),
        plan_flags,
        notes=fleet_plan.skipped(action, rows, home),
        report=lambda _results, _backup: _summary(sink, as_json=as_json),
    )
    plan_flow.finish(result)


@fleet_group.command("push")
@repo_option
@click.option("--state", "states", multiple=True, type=click.Choice(fleet.STATE_ORDER))
@click.option("--all", "all_rows", is_flag=True, help="Every repository with something to push.")
@global_option
@plan_options
def fleet_push(
    repos: tuple[Path, ...], states: tuple[str, ...], all_rows: bool, **plan_flags: object
) -> None:
    """Propose the Kiro changes of several repositories, one merge request each.

    Shows one plan (a step per repository) and asks once; --yes applies it.
    """
    _run_fleet("push", repos, states, all_rows, plan_flags)


@fleet_group.command("pull")
@repo_option
@click.option("--state", "states", multiple=True, type=click.Choice(fleet.STATE_ORDER))
@click.option("--all", "all_rows", is_flag=True, help="Every repository with something to pull.")
@click.option(
    "--overwrite-local",
    is_flag=True,
    help="Preselect replacing locally edited files with the central versions (a verified "
    "backup is taken first; `steersync restore` undoes it).",
)
@global_option
@plan_options
def fleet_pull(
    repos: tuple[Path, ...],
    states: tuple[str, ...],
    all_rows: bool,
    overwrite_local: bool,
    **plan_flags: object,
) -> None:
    """Apply the central configuration to several repositories; local edits are kept.

    Shows one plan and asks once. Repositories with local edits that also changed centrally
    get an "overwrite" step, off unless --overwrite-local.
    """
    _run_fleet("pull", repos, states, all_rows, plan_flags, overwrite=overwrite_local)


@fleet_group.command("restore")
@click.argument("stamp", required=False)
@click.option("--list", "list_only", is_flag=True, help="List the backups.")
@click.option("--repo", type=click.Path(path_type=Path), help="Only this repository.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
def fleet_restore(stamp: str | None, list_only: bool, repo: Path | None, yes: bool) -> None:
    """Put back the files a steersync run replaced, from a verified backup.

    Every run that writes (setup, init, fleet pull --overwrite-local, pull --force) takes
    one backup first and prints its id; this undoes it. Files the run created are removed.
    """
    found = backups.listing(backup_root(os.environ))
    if repo is not None:
        wanted = str(repo.expanduser().resolve())
        found = [b for b in found if b.workspace == wanted]
    if list_only or stamp is None:
        if not found:
            click.echo("no backups")
        for backup in found:
            click.echo(
                f"{backup.stamp}  {backup.project}  {backup.workspace}  {len(backup.items)} file(s)"
            )
        return
    chosen = [b for b in found if b.stamp == stamp]
    if not chosen:
        raise click.ClickException(f"no backup {stamp}" + (f" for {repo}" if repo else ""))
    for backup in chosen:
        if not (
            yes
            or click.confirm(
                f"Restore {len(backup.items)} file(s) into {backup.workspace}?", default=False
            )
        ):
            continue
        try:
            restored = backups.restore(backup)
        except backups.BackupError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"restored {len(restored)} file(s) into {backup.workspace}")


# `steersync restore` (top level) is the same command; `fleet restore` stays as an alias.
restore_command = click.Command(
    "restore", callback=fleet_restore.callback, params=fleet_restore.params, help=fleet_restore.help
)


# --- interactive overview -------------------------------------------------------------------

PUSH, PULL, DETAILS, REFRESH, QUIT = (
    "Push selected repositories",
    "Pull selected repositories",
    "Show details for one repository",
    "Refresh",
    "Quit",
)

Build = Callable[[fleet_plan.Action, Sequence[RepoStatus]], tuple[Plan, list[Outcome]]]


def overview(
    prompts: Prompts,
    collect: Callable[[], FleetReport],
    render: Callable[[FleetReport], None],
    show_details: Callable[[RepoStatus], None],
    build: Build,
) -> list[Outcome]:
    """The interactive loop; push and pull are one plan and one checkbox each."""
    report = collect()
    produced: list[Outcome] = []
    while True:
        render(report)
        action = prompts.select("What next?", [PUSH, PULL, DETAILS, REFRESH, QUIT])
        if action in {None, QUIT}:
            return produced
        if action == REFRESH:
            report = collect()
            continue
        rows = list(report.repositories)
        if action == DETAILS:
            label = prompts.select("Which repository?", [r.path for r in rows])
            for row in rows:
                if row.path == label:
                    show_details(row)
            continue
        verb: fleet_plan.Action = "push" if action == PUSH else "pull"
        plan, sink = build(verb, rows)
        if not pending(plan):
            click.echo(f"nothing to {verb}")
            continue
        picked = plan_select.select_steps(plan, None, checkbox=prompts.checkbox)
        if not picked:
            continue
        results = run_steps(plan, picked)
        produced.extend(sink)
        _summary(sink, as_json=False)
        if any(r.interrupted for r in results):
            return produced
        report = collect()


@click.command("interactive")
def interactive() -> None:
    """The interactive overview (used by bare `steersync fleet` on a terminal)."""
    from rich.console import Console  # noqa: PLC0415 - interface library, on demand

    from steersync import fleet_ui  # noqa: PLC0415

    console = Console(highlight=False)
    home = str(Path.home())

    def build(action: fleet_plan.Action, rows: Sequence[RepoStatus]) -> tuple[Plan, list[Outcome]]:
        sink: list[Outcome] = []
        return fleet_plan.build_fleet_plan(action, rows, os.environ, sink=sink, home=home), sink

    outcomes = overview(
        questionary_prompts(),
        lambda: _report(os.environ),
        lambda report: fleet_ui.render(report, console, home=home),
        lambda row: fleet_ui.details(row, console),
        build,
    )
    if any(o.status == "failed" for o in outcomes):
        sys.exit(EXIT_PARTIAL)
