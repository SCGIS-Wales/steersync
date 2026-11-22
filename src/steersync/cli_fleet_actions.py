"""Push, pull, restore and the interactive overview of ``steersync fleet``.

Scripted subcommands use click prompts (so ``--yes`` and piped answers work); the
interactive overview uses questionary. Every prompt goes through a :class:`Prompts`
value, so the flows are tested with scripted answers.
"""

import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

import click

from steersync import backups, fleet, registry
from steersync.cli_fleet import EXIT_PARTIAL
from steersync.cli_fleet import fleet as fleet_group
from steersync.fleet import FleetReport, Outcome, RepoStatus
from steersync.paths import backup_root, registry_path
from steersync.retry import RetryEvent

EXIT_INTERRUPTED = 130
OVERWRITE_WORD = "overwrite"


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


def expected_word(count: int) -> str:
    """What the user types to overwrite local edits in ``count`` repositories."""
    return OVERWRITE_WORD if count == 1 else f"{OVERWRITE_WORD} {count}"


def typed_overwrite(rows: Sequence[RepoStatus], ask: Callable[[str], str | None]) -> bool:
    """Show every repository and file that would be overwritten; require the typed word."""
    click.echo("These local edits will be replaced by the central versions (a verified")
    click.echo("backup is taken first; `steersync fleet restore` puts it back):")
    for row in rows:
        click.echo(f"  {row.path}")
        for name in row.conflicts:
            click.echo(f"    {name}")
    word = expected_word(len(rows))
    answer = ask(f"Type '{word}' to overwrite, anything else keeps your edits")
    return (answer or "").strip() == word


def _report(env: dict[str, str] | os._Environ[str], *, refresh: bool | None = None) -> FleetReport:
    def retried(event: RetryEvent) -> None:
        click.echo(f"  {event.render()}", err=True)

    entries = registry.load(registry_path(env)).paths()
    return fleet.collect(entries, env, refresh=refresh, on_retry=retried)


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
            row.is_global() and wanted_paths & {str(Path("~/.kiro").expanduser().resolve())}
        )
        if key_matches or row.state in wanted_states or (all_rows and reason(row) is None):
            rows.append(row)
    unknown = wanted_paths - {r.path for r in report.repositories}
    unknown.discard(str(Path("~/.kiro").expanduser().resolve()))
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
            click.echo(f"{outcome.status:<8} {outcome.path}: {outcome.detail} {extra}".rstrip())
            for backup in outcome.backups:
                click.echo(f"         backup: {backup}")
    for outcome in outcomes:
        if outcome.status == "skipped":
            click.echo(f"skipped  {outcome.path}: {outcome.detail}")
    for outcome in outcomes:
        if outcome.status == "failed":
            click.echo(f"FAILED   {outcome.path}: {outcome.detail}", err=True)
            if outcome.rerun:
                click.echo(f"         rerun: {outcome.rerun}", err=True)


def _finish(outcomes: Sequence[Outcome], interrupted: bool, *, as_json: bool) -> None:
    _summary(outcomes, as_json=as_json)
    if interrupted:
        click.echo("interrupted; the summary above shows what completed", err=True)
        sys.exit(EXIT_INTERRUPTED)
    if any(o.status == "failed" for o in outcomes):
        sys.exit(EXIT_PARTIAL)


def _progress(event: tuple[str, str, str]) -> None:
    subject, phase, message = event
    if phase == "running":
        click.echo(f"{subject}: {message}", err=True)


def do_push(rows: Sequence[RepoStatus], *, dry_run: bool) -> tuple[list[Outcome], bool]:
    env = os.environ
    return fleet.run_batch(
        rows, lambda r: fleet.push_one(r, env, dry_run=dry_run), kind="push", on_event=_progress
    )


def do_pull(
    rows: Sequence[RepoStatus], overwrite: set[str], *, dry_run: bool
) -> tuple[list[Outcome], bool]:
    env = os.environ
    return fleet.run_batch(
        rows,
        lambda r: fleet.pull_one(r, env, overwrite=r.path in overwrite, dry_run=dry_run),
        kind="pull",
        on_event=_progress,
    )


repo_option = click.option(
    "--repo", "repos", multiple=True, type=click.Path(path_type=Path), help="A repository."
)
json_option = click.option("--json", "as_json", is_flag=True, help="Machine readable output.")


@fleet_group.command("push")
@repo_option
@click.option("--state", "states", multiple=True, type=click.Choice(fleet.STATE_ORDER))
@click.option("--all", "all_rows", is_flag=True, help="Every repository with something to push.")
@click.option("--dry-run", is_flag=True, help="Compute each proposal without pushing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@json_option
def fleet_push(
    repos: tuple[Path, ...],
    states: tuple[str, ...],
    all_rows: bool,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Propose the Kiro changes of several repositories, one merge request each."""
    # Act on the current central state: always refresh before changing anything.
    report = _report(os.environ, refresh=True)
    rows = _select(report, repos, states, all_rows, fleet.push_reason)
    if not as_json:
        for row in rows:
            reason = fleet.push_reason(row)
            note = f"skip: {reason}" if reason else f"push {row.push.render()}"
            click.echo(f"  {row.path}  [{row.state}]  {note}")
    if not rows:
        click.echo("nothing selected")
        return
    if not (yes or dry_run or click.confirm("Proceed?", default=False)):
        click.echo("cancelled; nothing was pushed")
        return
    outcomes, interrupted = do_push(rows, dry_run=dry_run)
    _finish(outcomes, interrupted, as_json=as_json)


@fleet_group.command("pull")
@repo_option
@click.option("--state", "states", multiple=True, type=click.Choice(fleet.STATE_ORDER))
@click.option("--all", "all_rows", is_flag=True, help="Every repository with something to pull.")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option(
    "--overwrite-local",
    is_flag=True,
    help="Replace locally edited files with the central versions after a verified backup "
    "(asks for a typed confirmation unless --yes).",
)
@json_option
def fleet_pull(
    repos: tuple[Path, ...],
    states: tuple[str, ...],
    all_rows: bool,
    dry_run: bool,
    yes: bool,
    overwrite_local: bool,
    as_json: bool,
) -> None:
    """Apply the central configuration to several repositories; local edits are kept."""
    report = _report(os.environ, refresh=True)
    rows = _select(report, repos, states, all_rows, fleet.pull_reason)
    if not rows:
        click.echo("nothing selected")
        return
    if not as_json:
        for row in rows:
            reason = fleet.pull_reason(row)
            note = f"skip: {reason}" if reason else f"pull {row.pull.render()}"
            if row.conflicts:
                note += f", {len(row.conflicts)} file(s) edited locally"
            click.echo(f"  {row.path}  [{row.state}]  {note}")
    conflicted = [r for r in rows if r.conflicts and fleet.pull_reason(r) is None]
    overwrite: set[str] = set()
    if overwrite_local and conflicted and not dry_run:
        if yes or typed_overwrite(conflicted, lambda m: click.prompt(m, default="")):
            overwrite = {r.path for r in conflicted}
        else:
            click.echo("overwrite cancelled; local edits are kept and the rest is pulled")
    if not (yes or dry_run or click.confirm("Proceed?", default=False)):
        click.echo("cancelled; nothing was pulled")
        return
    outcomes, interrupted = do_pull(rows, overwrite, dry_run=dry_run)
    _finish(outcomes, interrupted, as_json=as_json)


@fleet_group.command("restore")
@click.argument("stamp", required=False)
@click.option("--list", "list_only", is_flag=True, help="List the backups.")
@click.option("--repo", type=click.Path(path_type=Path), help="Only this repository.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
def fleet_restore(stamp: str | None, list_only: bool, repo: Path | None, yes: bool) -> None:
    """Put back the files a forced pull replaced, from a verified backup."""
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


# --- interactive overview -------------------------------------------------------------------

PUSH, PULL, DETAILS, REFRESH, QUIT = (
    "Push selected repositories",
    "Pull selected repositories",
    "Show details for one repository",
    "Refresh",
    "Quit",
)
KEEP, OVERWRITE, SKIP = (
    "Keep my local edits (safe default)",
    "Overwrite local .kiro with central",
    "Skip",
)


def overview(
    prompts: Prompts,
    collect: Callable[[], FleetReport],
    render: Callable[[FleetReport], None],
    show_details: Callable[[RepoStatus], None],
    push: Callable[[Sequence[RepoStatus]], tuple[list[Outcome], bool]],
    pull: Callable[[Sequence[RepoStatus], set[str]], tuple[list[Outcome], bool]],
) -> list[Outcome]:
    """The interactive loop; returns every outcome produced."""
    from steersync.fleet_ui import choose_repositories  # noqa: PLC0415

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
        is_push = action == PUSH
        chosen = choose_repositories(
            rows,
            fleet.push_reason if is_push else fleet.pull_reason,
            (lambda r: r.state in {"local-ahead", "diverged"})
            if is_push
            else (lambda r: r.state == "central-ahead"),
            message="Select repositories (space to toggle, enter to confirm)",
            checkbox=prompts.checkbox,
        )
        if not chosen:
            continue
        overwrite: set[str] = set()
        if not is_push:
            wanting: list[RepoStatus] = []
            for row in [r for r in chosen if r.conflicts]:
                answer = prompts.select(
                    f"{row.path} has local edits that also changed centrally: "
                    f"{', '.join(row.conflicts)}",
                    [KEEP, OVERWRITE, SKIP],
                )
                if answer == OVERWRITE:
                    wanting.append(row)
                elif answer in {SKIP, None}:
                    chosen = [r for r in chosen if r.path != row.path]
            if wanting:
                if typed_overwrite(wanting, prompts.text):
                    overwrite = {r.path for r in wanting}
                else:
                    click.echo("overwrite cancelled; those repositories keep their local edits")
        verb = "push" if is_push else "pull"
        click.echo(f"{len(chosen)} repositor{'y' if len(chosen) == 1 else 'ies'} to {verb}:")
        for row in chosen:
            click.echo(f"  {row.path}" + ("  (overwrite)" if row.path in overwrite else ""))
        if not prompts.confirm("Proceed?", False):
            continue
        outcomes, interrupted = push(chosen) if is_push else pull(chosen, overwrite)
        produced.extend(outcomes)
        _summary(outcomes, as_json=False)
        if interrupted:
            return produced
        report = collect()


@click.command("interactive")
def interactive() -> None:
    """The interactive overview (used by bare `steersync fleet` on a terminal)."""
    from rich.console import Console  # noqa: PLC0415 - interface library, on demand

    from steersync import fleet_ui  # noqa: PLC0415

    console = Console(highlight=False)
    home = str(Path.home())
    outcomes = overview(
        questionary_prompts(),
        lambda: _report(os.environ),
        lambda report: fleet_ui.render(report, console, home=home),
        lambda row: fleet_ui.details(row, console),
        lambda rows: do_push(rows, dry_run=False),
        lambda rows, overwrite: do_pull(rows, overwrite, dry_run=False),
    )
    if any(o.status == "failed" for o in outcomes):
        sys.exit(EXIT_PARTIAL)
