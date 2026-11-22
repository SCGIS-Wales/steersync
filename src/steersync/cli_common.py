"""Helpers shared by the click commands in :mod:`steersync.cli` and :mod:`steersync.cli_fleet`."""

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from steersync import gitops
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.lint import LintReport
from steersync.logging_utils import configure_logging
from steersync.steering import ChangePlan

log = logging.getLogger("steersync")


def _workspace_root(config: Config, env: Mapping[str, str]) -> Path | None:
    return gitops.repo_toplevel(config, Path.cwd(), env=gitops.git_environment(env))


def _bootstrap(ctx: click.Context, *, propagate: bool = False) -> tuple[Config, Path | None]:
    """Load configuration for the current directory and return it with the workspace root.

    Configuration errors end the command with the error's exit code unless
    ``propagate`` is set, in which case the caller handles them.
    """
    params = ctx.find_root().params
    env = os.environ
    try:
        bootstrap = load_config(workspace_root=None, env=env, explicit=params.get("config_path"))
        root = _workspace_root(bootstrap, env)
        config = load_config(workspace_root=root, env=env, explicit=params.get("config_path"))
    except SteersyncError as exc:
        if propagate:
            raise
        _fail(exc)
        raise AssertionError from exc  # pragma: no cover - _fail always exits
    configure_logging(
        params.get("log_level") or config.logging.level,
        params.get("log_format") or config.logging.format,
        quiet=bool(params.get("quiet")),
    )
    return config, root


def _echo(message: str, *, quiet: bool = False, err: bool = False) -> None:
    if not quiet:
        click.echo(message, err=err)


def _plan_lines(label: str, plan: ChangePlan) -> list[str]:
    lines = [f"{label}: {plan.change_count} change(s), {len(plan.unchanged)} unchanged"]
    lines.extend(f"  + {p}" for p in plan.add)
    lines.extend(f"  ~ {p}" for p in plan.update)
    lines.extend(f"  - {p}" for p in plan.delete)
    lines.extend(f"  ! {p} (locally modified, kept)" for p in plan.conflict)
    lines.extend(f"  ? {p} (local only)" for p in plan.local_only)
    return lines


def _is_blank(plan: ChangePlan) -> bool:
    """True when a collection has nothing on either side worth a line of output."""
    return plan.is_empty and not (plan.unchanged or plan.conflict or plan.local_only)


def _plan_json(plan: ChangePlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {k: list(v) for k, v in plan._asdict().items()}


def _print_lint(report: LintReport, *, as_json: bool, quiet: bool) -> None:
    if as_json:
        click.echo(report.to_json())
        return
    for finding in report.findings:
        _echo(finding.render(), quiet=quiet and finding.severity != "error", err=True)
    _echo(
        f"linted {report.checked} file(s): "
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)",
        quiet=quiet,
        err=True,
    )


def _fail(exc: SteersyncError) -> None:
    log.error("%s", exc)
    sys.exit(exc.exit_code)


def _plans_json(plans: Mapping[str, ChangePlan] | None) -> dict[str, Any] | None:
    if plans is None:
        return None
    return {name: _plan_json(plan) for name, plan in plans.items()}
