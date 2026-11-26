"""Run a plan from a command's flags: --only/--skip/--profile, then the one approval."""

import os
from collections.abc import Callable, Mapping, Sequence

import click

from steersync import plan_flow
from steersync.cli_common import _fail
from steersync.config import Config
from steersync.errors import ConfigError
from steersync.interaction import Interaction, resolve_interaction
from steersync.plan import Plan, StepResult, restrict, with_defaults


def interaction_from(plan_flags: Mapping[str, object], *, answered: bool = False) -> Interaction:
    """``answered``: the command's own flags already chose the steps (like --yes)."""
    return resolve_interaction(
        yes=bool(plan_flags.get("yes")) or answered,
        no_input=bool(plan_flags.get("no_input")),
        force=bool(plan_flags.get("force")),
        dry_run=bool(plan_flags.get("dry_run")),
        output="json" if plan_flags.get("as_json") else "text",
    )


def _names(value: object) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, tuple | list) else ()


def run_plan(
    plan: Plan,
    config: Config,
    plan_flags: Mapping[str, object],
    *,
    answered: bool = False,
    notes: Sequence[str] = (),
    extra_skip: tuple[str, ...] = (),
    remember: bool = False,
    report: Callable[[list[StepResult], str | None], None] | None = None,
) -> plan_flow.FlowResult:
    """Shape the plan with the flags and the saved selection, then show it and ask once."""
    try:
        plan = restrict(
            plan,
            only=_names(plan_flags.get("only")),
            skip=(*_names(plan_flags.get("skip")), *extra_skip),
        )
    except click.UsageError as exc:
        _fail(ConfigError(exc.format_message()))
        raise AssertionError from exc  # pragma: no cover - _fail always exits
    profile = plan_flags.get("profile")
    saved = config.setup.profiles.get(profile) if isinstance(profile, str) else None
    remembered: frozenset[str] | None = None
    if saved is not None:
        plan = with_defaults(plan, saved)
    elif remember and config.ux.remember_selection and config.setup.last:
        remembered = frozenset(config.setup.last)
    return plan_flow.execute(
        plan,
        interaction_from(plan_flags, answered=answered),
        os.environ,
        remembered=remembered,
        confirm_after_select=config.ux.confirm_after_select,
        undo_hint=config.ux.show_undo_hint,
        notes=tuple(notes),
        report=report,
    )
