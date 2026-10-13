"""Command line interface for steersync, built with click."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click

from steersync import __version__, gitlab, gitops, hooks, sync
from steersync.config import (
    LINT_PROFILES,
    Config,
    LintProfile,
    default_config_text,
    load_config,
    validate_remote_url,
)
from steersync.errors import SteersyncError
from steersync.hooks import DEFAULT_TOKEN_ENV
from steersync.lint import LintReport, lint_paths, lint_workspace, merge_reports
from steersync.logging_utils import configure_logging
from steersync.paths import (
    global_collection_dir,
    workspace_collection_dir,
    workspace_steering_dir,
)
from steersync.steering import ChangePlan, FileSpec

log = logging.getLogger("steersync")

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
ENV_FROM_HOOK_URL = "PRE_COMMIT_REMOTE_URL"


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


collection_option = click.option(
    "-c",
    "--collection",
    "collections",
    multiple=True,
    metavar="NAME",
    help="Limit to a collection (steering, specs, skills, ...). Repeatable; default is all.",
)


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Configuration file.",
)
@click.option(
    "--log-level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False)
)
@click.option("--log-format", type=click.Choice(["text", "json"]))
@click.option("-q", "--quiet", is_flag=True, help="Only print errors.")
@click.version_option(__version__, prog_name="steersync")
def main(
    config_path: Path | None, log_level: str | None, log_format: str | None, quiet: bool
) -> None:
    """Sync Kiro steering, specs, skills, agents, hooks and settings with a central repository."""


@main.command()
@click.option("--force", is_flag=True, help="Overwrite locally modified steering files.")
@click.option("--dry-run", is_flag=True, help="Show the plan without writing anything.")
@click.option("--workspace/--no-workspace", default=True, help="Include workspace steering.")
@click.option(
    "--global/--no-global", "include_global", default=True, help="Include global steering."
)
@click.option("--strict", is_flag=True, help="Fail when remote steering has lint errors.")
@click.option("--remote-url", help="Project remote URL used to derive the project slug.")
@click.option(
    "--best-effort",
    is_flag=True,
    help="Warn and exit 0 on failure (for Kiro session hooks and schedulers).",
)
@collection_option
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.pass_context
def pull(
    ctx: click.Context,
    force: bool,
    dry_run: bool,
    workspace: bool,
    include_global: bool,
    strict: bool,
    remote_url: str | None,
    best_effort: bool,
    collections: tuple[str, ...],
    as_json: bool,
) -> None:
    """Fetch every collection (steering, specs, skills, ...) from the central repository."""
    try:
        config, root = _bootstrap(ctx, propagate=True)
    except SteersyncError as exc:
        _soft_failure(exc, soft=best_effort, label="steering pull skipped")
        return
    quiet = bool(ctx.find_root().params.get("quiet"))
    if best_effort:
        config = _hook_budget(config)
    try:
        result = sync.pull(
            config,
            workspace_root=root,
            include_workspace=workspace,
            include_global=include_global,
            remote_url=remote_url,
            force=force,
            dry_run=dry_run,
            strict=strict or None,
            collections=collections or None,
        )
    except (SteersyncError, OSError) as exc:
        _soft_failure(exc, soft=best_effort, label="steering pull skipped")
        return
    if as_json:
        scopes: dict[str, dict[str, Any]] = {}
        for item in result.results:
            scopes.setdefault(item.scope, {})[item.collection] = {
                "target": str(item.target_dir),
                "plan": _plan_json(item.plan),
                "written": list(item.written),
            }
        click.echo(
            json.dumps(
                {
                    "commit": result.commit,
                    "dry_run": result.dry_run,
                    "offline": result.offline,
                    "scopes": scopes,
                },
                indent=2,
            )
        )
        return
    for item in result.results:
        if _is_blank(item.plan):
            continue
        label = f"{item.scope}/{item.collection} ({item.target_dir})"
        for line in _plan_lines(label, item.plan):
            _echo(line, quiet=quiet)
    verb = "would apply" if dry_run else "applied"
    source = (
        f"{result.commit[:12]} (cached copy, central repository unreachable)"
        if result.offline
        else result.commit[:12]
    )
    _echo(f"{verb} {result.change_count} change(s) from {source}", quiet=quiet)
    if result.conflicts:
        _echo(
            f"{len(result.conflicts)} locally modified file(s) kept; "
            "rerun with --force to overwrite",
            err=True,
        )


@main.command()
@click.option(
    "--from-hook",
    is_flag=True,
    help="Run from a git pre-push hook: never block the push unless strict.",
)
@click.option("--strict", is_flag=True, help="Exit non zero on failure even from a hook.")
@click.option("--remote-url", help="Project remote URL (from the hook arguments).")
@click.option(
    "--scope", type=click.Choice(["workspace", "global"]), default="workspace", show_default=True
)
@click.option("--dry-run", is_flag=True, help="Compute the proposal without pushing.")
@click.option("--no-lint", is_flag=True, help="Skip lint checks before pushing.")
@click.option("-m", "--message", help="Commit message for the proposal.")
@collection_option
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.pass_context
def push(
    ctx: click.Context,
    from_hook: bool,
    strict: bool,
    remote_url: str | None,
    scope: str,
    dry_run: bool,
    no_lint: bool,
    message: str | None,
    collections: tuple[str, ...],
    as_json: bool,
) -> None:
    """Propose local collections as one branch (and merge request) on the central repository."""
    env = os.environ
    if from_hook and env.get("STEERSYNC_ACTIVE") == "1":
        return
    quiet = bool(ctx.find_root().params.get("quiet")) or from_hook
    try:
        config, root = _bootstrap(ctx, propagate=True)
    except SteersyncError as exc:
        _soft_failure(exc, soft=from_hook and not strict, label="steering sync skipped")
        return
    strict = strict or config.hook.strict
    if from_hook:
        config = _hook_budget(config)
        remote_url = remote_url or env.get(ENV_FROM_HOOK_URL) or None
        refs = gitops.parse_pre_push_stdin(_read_stdin())
        if refs and all(ref.is_delete for ref in refs):
            return
    if root is None:
        _soft_failure(
            SteersyncError("not inside a git repository"),
            soft=from_hook and not strict,
            label="steering sync skipped",
        )
        return
    try:
        result = sync.push(
            config,
            workspace_root=root,
            env=env,
            remote_url=remote_url,
            scope="global" if scope == "global" else "workspace",
            dry_run=dry_run,
            run_lint=not no_lint,
            message=message,
            collections=collections or None,
        )
    except (SteersyncError, OSError) as exc:
        _soft_failure(exc, soft=from_hook and not strict, label="steering sync skipped")
        return
    except Exception as exc:
        # The pre-push contract: an unexpected bug must never block the user's push.
        if not from_hook or strict:
            raise
        log.debug("unexpected error in hook mode", exc_info=True)
        _soft_failure(exc, soft=True, label="steering sync skipped (unexpected error)")
        return
    if as_json:
        click.echo(
            json.dumps(
                {**result._asdict(), "plan": _plan_json(result.plan), "lint": None}, indent=2
            )
        )
        return
    if result.lint is not None and result.lint.findings:
        _print_lint(result.lint, as_json=False, quiet=quiet)
    if result.plan is not None and not result.plan.is_empty:
        for line in _plan_lines(result.project, result.plan):
            _echo(line, quiet=quiet)
    _echo(
        f"{result.status}: {result.message}",
        quiet=quiet and result.status in {"up-to-date", "nothing-to-push"},
    )
    if result.merge_request:
        _echo(f"merge request: {result.merge_request} ({result.merge_request_note})", quiet=quiet)
    _echo_existing(result.existing_branches, result.project)


def _echo_existing(branches: tuple[str, ...], project: str | None) -> None:
    if not branches:
        return
    click.echo(
        f"warning: {len(branches)} other proposal branch(es) for {project} not merged yet; "
        "review or merge them before adding more:",
        err=True,
    )
    for name in branches:
        click.echo(f"  {name}", err=True)


def _soft_failure(exc: Exception, *, soft: bool, label: str) -> None:
    """Report ``exc`` and exit non zero, or only warn when ``soft`` is set."""
    if soft:
        click.echo(f"steersync: {label}: {exc}", err=True)
        return
    if isinstance(exc, SteersyncError):
        _fail(exc)
    log.error("%s", exc)
    sys.exit(1)


def _hook_budget(config: Config) -> Config:
    """Bound git time while running from a hook so a slow server cannot stall a push."""
    git = config.git.model_copy(
        update={
            "timeout_seconds": min(config.git.timeout_seconds, config.hook.timeout_seconds),
            "retries": min(config.git.retries, 1),
        }
    )
    return config.model_copy(update={"git": git})


def _read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except OSError, ValueError:
        return ""


@main.command()
@click.option("--remote-url", help="Project remote URL used to derive the project slug.")
@click.option("--no-lint", is_flag=True, help="Skip lint checks.")
@collection_option
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.pass_context
def status(
    ctx: click.Context,
    remote_url: str | None,
    no_lint: bool,
    collections: tuple[str, ...],
    as_json: bool,
) -> None:
    """Show how local collections differ from the central repository."""
    config, root = _bootstrap(ctx)
    try:
        result = sync.status(
            config,
            workspace_root=root,
            remote_url=remote_url,
            run_lint=not no_lint,
            collections=collections or None,
        )
    except SteersyncError as exc:
        _fail(exc)
        return
    if as_json:
        click.echo(
            json.dumps(
                {
                    "project": result.project,
                    "remote": gitops.redact_url(result.remote),
                    "remote_commit": result.remote_commit,
                    "workspace": _plans_json(result.workspace),
                    "global": _plans_json(result.global_),
                    "lint": json.loads(result.lint.to_json()) if result.lint else None,
                    "proposals": list(result.proposals),
                },
                indent=2,
            )
        )
        return
    click.echo(f"central: {gitops.redact_url(result.remote)} @ {(result.remote_commit or '')[:12]}")
    if result.workspace is not None:
        for name, plan in result.workspace.items():
            if not _is_blank(plan):
                for line in _plan_lines(f"workspace/{name} -> {result.project}", plan):
                    click.echo(line)
    else:
        click.echo("workspace: not in a git repository with an origin remote")
    for name, plan in result.global_.items():
        if not _is_blank(plan):
            for line in _plan_lines(f"global/{name} <- central", plan):
                click.echo(line)
    if result.lint is not None:
        _print_lint(result.lint, as_json=False, quiet=False)
    if result.proposals:
        click.echo(f"open proposal branch(es) for {result.project}: {', '.join(result.proposals)}")


@main.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@collection_option
@click.option(
    "--profile",
    type=click.Choice(list(LINT_PROFILES)),
    help="Lint profile for explicit PATHS (default: steering, or the collection's profile).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.pass_context
def lint(
    ctx: click.Context,
    paths: tuple[Path, ...],
    collections: tuple[str, ...],
    profile: str | None,
    as_json: bool,
) -> None:
    """Validate synced content. Defaults to every collection in this workspace."""
    config, root = _bootstrap(ctx)
    quiet = bool(ctx.find_root().params.get("quiet"))
    try:
        selected = config.select_collections(collections or None)
    except SteersyncError as exc:
        _fail(exc)
        return
    if paths:
        report = _lint_explicit_paths(config, root, paths, collections, profile)
    else:
        if root is None:
            _fail(SteersyncError("no paths given and not inside a git repository"))
            return
        reports: list[LintReport] = []
        for collection in selected.values():
            target = workspace_collection_dir(collection, root)
            if target is None:
                continue
            reports.append(
                lint_paths(
                    [target],
                    config.lint,
                    root,
                    profile=collection.lint,
                    spec=FileSpec(collection.include, collection.exclude, collection.max_depth),
                    max_file_bytes=collection.max_file_bytes,
                )
            )
        if not collections:
            reports.append(_workspace_report(config, root))
        report = merge_reports(reports)
    _print_lint(report, as_json=as_json, quiet=quiet)
    if not report.ok:
        sys.exit(4)


def _workspace_report(config: Config, root: Path) -> LintReport:
    """Warn when a Kiro session here would not refresh and expose the configuration."""
    hooks_dir = _collection_path(config, "hooks") or ".kiro/hooks"
    settings_dir = _collection_path(config, "settings") or ".kiro/settings"
    return lint_workspace(root, config.lint, hooks_dir=hooks_dir, settings_dir=settings_dir)


def _collection_path(config: Config, name: str) -> str | None:
    collection = config.collections.get(name)
    return collection.workspace_path if collection is not None else None


def _lint_explicit_paths(
    config: Config,
    root: Path | None,
    paths: tuple[Path, ...],
    collections: tuple[str, ...],
    profile: str | None,
) -> LintReport:
    """Lint explicit paths, grouping each by the collection whose directory contains it.

    ``--collection`` forces one collection for every path, ``--profile`` forces one
    profile; a path outside every collection falls back to the steering profile.
    """
    forced = config.collections[collections[0]] if collections else None
    groups: dict[str | None, list[Path]] = {}
    for path in paths:
        name = collections[0] if forced else _collection_for_path(config, root, path)
        groups.setdefault(name, []).append(path)
    reports: list[LintReport] = []
    for name, group in groups.items():
        chosen = config.collections[name] if name else None
        reports.append(
            lint_paths(
                group,
                config.lint,
                root,
                profile=_profile(profile or (chosen.lint if chosen else "steering")),
                spec=FileSpec(chosen.include, chosen.exclude, chosen.max_depth) if chosen else None,
                max_file_bytes=chosen.max_file_bytes if chosen else None,
            )
        )
    return merge_reports(reports)


def _collection_for_path(config: Config, root: Path | None, path: Path) -> str | None:
    """Return the enabled collection whose workspace directory contains ``path``."""
    if root is None:
        return None
    resolved = path.resolve()
    root_resolved = root.resolve()
    best: tuple[int, str] | None = None
    for name, collection in config.enabled_collections().items():
        target = workspace_collection_dir(collection, root)
        if target is None:
            continue
        base = target.resolve()
        if base == root_resolved:
            # A collection rooted at the workspace (AGENTS.md) owns only the files its
            # patterns select, never every path under the repository.
            spec = FileSpec(collection.include, collection.exclude, collection.max_depth)
            if resolved.is_file() and spec.matcher()(resolved.relative_to(base).as_posix()):
                best = best or (0, name)
            continue
        if resolved == base or base in resolved.parents:
            depth = len(base.parts)
            if best is None or depth > best[0]:
                best = (depth, name)
    return best[1] if best else None


def _profile(name: str) -> LintProfile:
    if name not in LINT_PROFILES:  # pragma: no cover - click validates the choice
        raise SteersyncError(f"unknown lint profile {name!r}")
    return cast(LintProfile, name)


@main.command()
@click.option("--force", is_flag=True, help="Replace an existing steersync hook or chained hook.")
@click.pass_context
def install_hook(ctx: click.Context, force: bool) -> None:
    """Install a chaining pre-push hook in this repository (core.hooksPath is never changed)."""
    config, root = _bootstrap(ctx)
    if root is None:
        _fail(SteersyncError("not inside a git repository"))
        return
    try:
        result = hooks.install_pre_push_hook(config, root, force=force)
    except FileExistsError as exc:
        _fail(SteersyncError(str(exc)))
        return
    click.echo(f"{result.action}: {result.path}")
    if result.chained is not None:
        click.echo(f"existing hook preserved and chained as {result.chained}")


@main.command()
@click.option(
    "--write-config", is_flag=True, help="Write .kiro/steersync.yaml from the packaged defaults."
)
@click.option(
    "--exclude/--no-exclude",
    default=True,
    help="Add the synced collection directories to .git/info/exclude.",
)
@click.option("--hook/--no-hook", default=False, help="Also install the pre-push hook.")
@click.option(
    "--kiro/--no-kiro",
    default=True,
    help="Write the Kiro SessionStart hook and register the MCP server so a session "
    "loads the configuration without being asked.",
)
@click.option(
    "--mcp-command",
    default="uvx",
    show_default=True,
    help="Executable Kiro uses to start the MCP server (use an absolute path if uvx is "
    "not on Kiro's PATH).",
)
@click.pass_context
def init(
    ctx: click.Context,
    write_config: bool,
    exclude: bool,
    hook: bool,
    kiro: bool,
    mcp_command: str,
) -> None:
    """Prepare this repository: excludes, git hook, Kiro session hook and MCP server."""
    config, root = _bootstrap(ctx)
    if root is None:
        _fail(SteersyncError("not inside a git repository"))
        return
    if write_config:
        target = root / ".kiro" / "steersync.yaml"
        if target.exists():
            click.echo(f"exists: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(default_config_text(), encoding="utf-8")
            click.echo(f"wrote: {target}")
    if exclude:
        patterns = [
            f"/{c.workspace_path.strip('/')}/"
            for c in config.enabled_collections().values()
            if c.workspace_path is not None and c.git_exclude
        ]
        patterns.append(f"/{config.paths.workspace_lock.strip('/')}")
        changed = False
        for pattern in patterns:
            changed |= gitops.write_info_exclude(config, root, pattern)
        click.echo(
            f"{'updated' if changed else 'unchanged'}: .git/info/exclude ({', '.join(patterns)})"
        )
    if hook:
        result = hooks.install_pre_push_hook(config, root, force=False)
        click.echo(f"{result.action}: {result.path}")
    if kiro:
        hooks_dir = root / (_collection_path(config, "hooks") or ".kiro/hooks")
        hook_action = hooks.write_kiro_hook(hooks_dir)
        click.echo(f"{hook_action}: {hooks_dir / hooks.KIRO_HOOK_FILE} (Kiro SessionStart hook)")
        mcp_path = root / (_collection_path(config, "settings") or ".kiro/settings") / "mcp.json"
        entry = hooks.mcp_server_entry(command=mcp_command, token_env=config.gitlab.token_env)
        try:
            mcp_action = hooks.merge_mcp_config(mcp_path, entry)
        except ValueError as exc:
            _fail(SteersyncError(str(exc)))
            return
        click.echo(f"{mcp_action}: {mcp_path} (steersync MCP server)")
    steering_dir = workspace_steering_dir(config, root)
    steering_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"ready: {steering_dir}")
    click.echo(f"collections: {', '.join(config.enabled_collections()) or '(none)'}")


@main.command("kiro-hook")
@click.option("--engine", type=click.Choice(["v2", "v3"]), default="v3", show_default=True)
@click.option("--write", is_flag=True, help="Write the v3 hook to .kiro/hooks/steersync.json.")
@click.pass_context
def kiro_hook(ctx: click.Context, engine: str, write: bool) -> None:
    """Print (or write) a Kiro hook that runs `steersync pull` when a session starts."""
    rendered = hooks.render_kiro_hook("v2" if engine == "v2" else "v3")
    if write:
        _, root = _bootstrap(ctx)
        if root is None:
            _fail(SteersyncError("not inside a git repository"))
            return
        if engine != "v3":
            _fail(
                SteersyncError(
                    "only v3 standalone hooks can be written; "
                    "paste the v2 JSON into your agent file"
                )
            )
            return
        target = root / ".kiro" / "hooks" / "steersync.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        click.echo(f"wrote: {target}")
        return
    click.echo(rendered, nl=False)


@main.command("mcp-config")
@click.option(
    "--command", default="uvx", show_default=True, help="Executable used to launch the server."
)
@click.option("--remote", help="Central repository URL to embed instead of ${STEERSYNC_REMOTE}.")
@click.pass_context
def mcp_config(ctx: click.Context, command: str, remote: str | None) -> None:
    """Print an mcp.json fragment registering the steersync MCP server."""
    try:
        config, _ = _bootstrap(ctx, propagate=True)
        token_env = config.gitlab.token_env
    except SteersyncError:
        token_env = DEFAULT_TOKEN_ENV
    click.echo(
        hooks.render_mcp_config(command=command, remote=remote, token_env=token_env), nl=False
    )


@main.command("mr")
@click.option("--branch", help="Proposal branch to open the merge request for (default: derived).")
@click.option("--title", help="Merge request title.")
@click.option("--remote-url", help="Project remote URL used to derive the project slug.")
@click.option(
    "--scope", type=click.Choice(["workspace", "global"]), default="workspace", show_default=True
)
@collection_option
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.pass_context
def merge_request(
    ctx: click.Context,
    branch: str | None,
    title: str | None,
    remote_url: str | None,
    scope: str,
    collections: tuple[str, ...],
    as_json: bool,
) -> None:
    """Open (or find) the GitLab merge request for the pushed proposal branch."""
    config, root = _bootstrap(ctx)
    if root is None:
        _fail(SteersyncError("not inside a git repository"))
        return
    try:
        result = sync.merge_request(
            config,
            workspace_root=root,
            remote_url=remote_url,
            scope="global" if scope == "global" else "workspace",
            branch=branch,
            title=title,
            collections=collections or None,
        )
    except SteersyncError as exc:
        _fail(exc)
        return
    if as_json:
        click.echo(json.dumps(result._asdict(), indent=2))
        return
    verb = "created" if result.created else "already open"
    click.echo(f"merge request {verb}: {result.url} ({result.branch})")
    if result.others:
        click.echo("other open merge requests for this project:", err=True)
        for item in result.others:
            click.echo(f"  {item}", err=True)


@main.command()
def mcp() -> None:
    """Run the MCP stdio server (requires the `mcp` extra)."""
    from steersync.mcp_server import serve  # noqa: PLC0415  (optional dependency)

    try:
        serve()
    except SteersyncError as exc:
        _fail(exc)


@main.group()
def config() -> None:
    """Inspect configuration."""


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print the effective configuration as JSON (remote credentials redacted)."""
    cfg, _ = _bootstrap(ctx)
    data = cfg.model_dump(mode="json")
    data["remote"]["url"] = gitops.redact_url(data["remote"]["url"])
    click.echo(json.dumps(data, indent=2))


@config.command("validate")
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """Validate the configuration and the central remote URL."""
    cfg, _ = _bootstrap(ctx)
    try:
        validate_remote_url(
            cfg.remote.url,
            allowed_hosts=cfg.remote.allowed_hosts,
            allow_local=cfg.remote.allow_local,
        )
    except SteersyncError as exc:
        _fail(exc)
        return
    click.echo(f"ok: {cfg.source_file or 'packaged defaults'}")


@config.command("default")
def config_default() -> None:
    """Print the packaged default configuration."""
    click.echo(default_config_text(), nl=False)


@config.command("collections")
@click.option("--json", "as_json", is_flag=True, help="Machine readable output.")
@click.pass_context
def config_collections(ctx: click.Context, as_json: bool) -> None:
    """List the configured collections and where each one lives."""
    cfg, root = _bootstrap(ctx)
    env = os.environ
    rows: list[dict[str, Any]] = []
    for name, collection in cfg.collections.items():
        workspace = workspace_collection_dir(collection, root) if root is not None else None
        rows.append(
            {
                "name": name,
                "enabled": collection.enabled,
                "description": collection.description,
                "central_dir": collection.central_dir,
                "workspace_path": collection.workspace_path,
                "workspace_dir": str(workspace) if workspace else None,
                "global_path": collection.global_path,
                "global_dir": (
                    str(d) if (d := global_collection_dir(collection, env)) is not None else None
                ),
                "include": list(collection.include),
                "exclude": list(collection.exclude),
                "lint": collection.lint,
            }
        )
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for name, collection in cfg.collections.items():
        state = "" if collection.enabled else " (disabled)"
        click.echo(f"{name}{state}: {collection.description}".rstrip(": "))
        click.echo(f"  central:   {collection.central_dir}/")
        click.echo(f"  workspace: {collection.workspace_path or '(none)'}")
        click.echo(f"  global:    {collection.global_path or '(none)'}")
        click.echo(f"  include:   {', '.join(collection.include)}")
        if collection.exclude:
            click.echo(f"  exclude:   {', '.join(collection.exclude)}")
        click.echo(f"  lint:      {collection.lint}")


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check git, configuration and directories."""
    config, root = _bootstrap(ctx)
    env = os.environ
    try:
        click.echo(f"git: {gitops.git_version(config)}")
    except SteersyncError as exc:
        _fail(exc)
        return
    click.echo(f"config: {config.source_file or 'packaged defaults'}")
    click.echo(f"remote: {gitops.redact_url(config.remote.url) or '(unset)'}")
    token_state = "set" if gitlab.token_available(config, env) else "not set"
    click.echo(f"gitlab token: {config.gitlab.token_env} {token_state}")
    click.echo(f"collections: {', '.join(config.enabled_collections()) or '(none)'}")
    for name, collection in config.enabled_collections().items():
        global_dir = global_collection_dir(collection, env)
        if global_dir is not None:
            click.echo(f"global {name}: {global_dir}")
    if root is not None:
        click.echo(f"workspace: {root}")
        for name, collection in config.enabled_collections().items():
            workspace_dir = workspace_collection_dir(collection, root)
            if workspace_dir is not None:
                click.echo(f"workspace {name}: {workspace_dir}")
        origin = gitops.remote_url(config, root)
        click.echo(f"origin: {gitops.redact_url(origin) if origin else '(none)'}")
        if origin:
            click.echo(f"project slug: {gitops.project_slug(origin)}")
    else:
        click.echo("workspace: not inside a git repository")


def run() -> None:
    """Console entry point: turn unexpected failures into a clean message and exit code."""
    try:
        main(standalone_mode=True)
    except KeyboardInterrupt:
        click.echo("steersync: interrupted", err=True)
        sys.exit(130)
    except OSError as exc:
        click.echo(f"steersync: error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(
            f"steersync: unexpected error: {exc.__class__.__name__}: {exc}\n"
            "rerun with --log-level DEBUG and report it if the problem persists",
            err=True,
        )
        log.debug("unexpected error", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    run()
