"""Show a plan, ask once, back it up, apply it and say how to undo it.

The shared tail of every command that changes state (setup, init, fleet). The decision
itself is :func:`steersync.plan.choose`; this module adds the terminal, the backup and the
exit codes:

- 0: applied, nothing to do, or ``--plan`` / ``--dry-run``.
- 1: the user cancelled.
- 2: input was needed but the run is not interactive (the message names ``--yes``).
- 5: at least one step failed.
"""

import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

import click
from rich.console import Console

from steersync import backups, plan_render, plan_select
from steersync.interaction import Interaction
from steersync.paths import backup_root
from steersync.plan import (
    Plan,
    StepResult,
    choose,
    non_interactive_message,
    pending,
    plan_to_json,
    run_steps,
)

EXIT_CANCELLED = 1
EXIT_NEEDS_INPUT = 2
EXIT_FAILED = 5


class FlowResult(NamedTuple):
    exit_code: int
    applied: tuple[str, ...] = ()
    results: tuple[StepResult, ...] = ()
    backup_id: str | None = None


Ask = Callable[[Plan, frozenset[str] | None], list[str] | None]


def _typed(command: str) -> bool:
    answer = click.prompt(
        f"Destructive steps are selected and no backup covers them. Type {command!r} to "
        "run them, anything else skips them",
        default="",
        show_default=False,
    )
    return str(answer).strip() == command


def _console() -> Console:
    # soft_wrap: result and note lines stay whole; tables still fit the width.
    return Console(highlight=False, soft_wrap=True)


def execute(
    plan: Plan,
    interaction: Interaction,
    env: Mapping[str, str],
    *,
    remembered: frozenset[str] | None = None,
    confirm_after_select: bool = False,
    undo_hint: bool = True,
    notes: tuple[str, ...] = (),
    ask: Ask | None = None,
    typed: Callable[[str], bool] = _typed,
) -> FlowResult:
    """Render, decide once, back up the selected steps' files, apply, report."""
    console = _console()
    as_json = interaction.output == "json"
    if as_json and (interaction.dry_run or not pending(plan)):
        click.echo(json.dumps(plan_to_json(plan), indent=2))
    elif not as_json:
        plan_render.render_plan(plan, console)
        for note in notes:
            click.echo(note, err=True)

    def ask_once(current: Plan, remember: frozenset[str] | None) -> list[str] | None:
        if ask is not None:
            return ask(current, remember)
        return plan_select.select_steps(current, remember, force=interaction.force)

    choice = choose(plan, interaction, ask=ask_once, typed=typed, remembered=remembered)
    if choice.status == "nothing":
        if not as_json:
            click.echo("Nothing to do")
        return FlowResult(0)
    if choice.status == "plan-only":
        return FlowResult(0)
    if choice.status == "needs-input":
        click.echo(non_interactive_message(plan), err=True)
        return FlowResult(EXIT_NEEDS_INPUT)
    if choice.status == "cancelled":
        click.echo("Nothing was changed.", err=True)
        return FlowResult(EXIT_CANCELLED)
    if (
        confirm_after_select
        and interaction.interactive
        and not interaction.assume_yes
        and not click.confirm(f"Apply {len(choice.step_ids)} step(s)?", default=True)
    ):
        click.echo("Nothing was changed.", err=True)
        return FlowResult(EXIT_CANCELLED)
    wanted = set(choice.step_ids)
    paths: list[Path] = list(
        dict.fromkeys(p for s in plan.steps if s.id in wanted for p in s.paths)
    )
    backup_id: str | None = None
    if paths:
        try:
            backup_id = backups.take_files(backup_root(env), paths, label=plan.command).stamp
        except backups.BackupError as exc:
            click.echo(f"steersync: {exc}", err=True)
            return FlowResult(EXIT_FAILED)
    results = run_steps(plan, choice.step_ids)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "command": plan.command,
                    "applied": list(choice.step_ids),
                    "backup": backup_id,
                    "results": [r.__dict__ for r in results],
                },
                indent=2,
            )
        )
    else:
        plan_render.render_results(results, backup_id, console, undo_hint=undo_hint)
    failed = any(not r.ok for r in results)
    return FlowResult(EXIT_FAILED if failed else 0, choice.step_ids, tuple(results), backup_id)


def finish(result: FlowResult) -> None:
    """Exit with the flow's code when it is not success."""
    if result.exit_code:
        sys.exit(result.exit_code)
