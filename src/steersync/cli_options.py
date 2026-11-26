"""The options every state changing command shares."""

from collections.abc import Callable
from typing import Any

import click

_PLAN_OPTIONS: tuple[Callable[[Callable[..., Any]], Callable[..., Any]], ...] = (
    click.option("-y", "--yes", is_flag=True, help="Apply the default selection without asking."),
    click.option("--no-input", is_flag=True, help="Never prompt; fail when input is needed."),
    click.option("-f", "--force", is_flag=True, help="Allow destructive steps."),
    click.option("--plan", "--dry-run", "dry_run", is_flag=True, help="Show the plan and exit."),
    click.option("--only", multiple=True, help="Limit the plan to these step ids."),
    click.option("--skip", multiple=True, help="Leave these step ids out."),
    click.option("--profile", default=None, help="Load a saved selection."),
    click.option("--json", "as_json", is_flag=True, help="Machine readable output."),
)


def plan_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Add --yes, --no-input, --force, --plan, --only, --skip, --profile and --json."""
    for option in reversed(_PLAN_OPTIONS):
        func = option(func)
    return func
