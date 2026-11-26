"""Command line interface for steersync, built with click."""

import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click

from steersync import (
    __version__,
    gitlab,
    gitops,
    hooks,
    kiro_version,
    plan_flow,
    registry,
    setup_plan,
    sync,
)
from steersync import schema as schema_models
from steersync import setup as setup_steps
from steersync.cli_common import (
    _bootstrap,
    _echo,
    _fail,
    _is_blank,
    _plan_json,
    _plan_lines,
    _plans_json,
    _print_lint,
    _workspace_root,
)
from steersync.cli_fleet import fleet
from steersync.cli_options import plan_options
from steersync.cli_plan import interaction_from, run_plan
from steersync.config import (
    LINT_PROFILES,
    CollectionConfig,
    Config,
    LintProfile,
    default_config_text,
    load_config,
    validate_remote_url,
)
from steersync.errors import ConfigError, SteersyncError
from steersync.hooks import DEFAULT_TOKEN_ENV
from steersync.interaction import Interaction
from steersync.lint import LintReport, lint_paths, lint_workspace, merge_reports
from steersync.logging_utils import configure_logging
from steersync.mcp_tools import MISSING_MCP_EXTRA
from steersync.paths import (
    cache_root,
    global_collection_dir,
    kiro_home,
    workspace_collection_dir,
    workspace_steering_dir,
)
from steersync.steering import FileSpec

log = logging.getLogger("steersync")

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
ENV_FROM_HOOK_URL = "PRE_COMMIT_REMOTE_URL"


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
@click.option(
    "-q", "--quiet", "pull_quiet", is_flag=True, help="Only print errors (same as steersync -q)."
)
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
    pull_quiet: bool,
) -> None:
    """Fetch every collection (steering, specs, skills, ...) from the central repository."""
    if pull_quiet:
        # The Kiro SessionStart hook runs `steersync pull --quiet --best-effort` (via uvx).
        ctx.find_root().params["quiet"] = True
    try:
        config, root = _bootstrap(ctx, propagate=True)
    except SteersyncError as exc:
        _soft_failure(exc, soft=best_effort, label="steering pull skipped")
        return
    quiet = bool(ctx.find_root().params.get("quiet"))
    registry.register(os.environ, root, "command")
    if best_effort:
        config = _hook_budget(config)
        _maybe_check_kiro(config, root)
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
    if not dry_run and (note := knowledge_reminder(result)):
        _echo(note, err=True)
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
    registry.register(env, root, "command")
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


def _agent_dirs(config: Config, root: Path | None) -> list[Path]:
    agents = config.collections.get("agents")
    if root is None or agents is None or agents.workspace_path is None:
        return []
    return [root / agents.workspace_path]


def knowledge_reminder(result: sync.PullResult) -> str | None:
    """Kiro indexes are local: after a pull that changed knowledge notes, say to reindex."""
    changed = [
        r for r in result.results if r.collection == "knowledge" and (r.written or r.plan.delete)
    ]
    if not changed:
        return None
    folder = changed[0].target_dir.name
    return f"{folder}/ changed. In Kiro run /knowledge update and wait for it to finish."


def _maybe_check_kiro(config: Config, root: Path | None) -> None:
    """From the SessionStart hook: report a Kiro below the V3 baseline at most once a day."""
    env = os.environ
    try:
        if not kiro_version.check_due(cache_root(config, env), env):
            return
        status = _kiro_status(config, root)
    except OSError:
        return
    if not status.supported:
        click.echo(f"steersync: {kiro_version.problem_text(status)}", err=True)


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
    registry.register(os.environ, root, "command")
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
    return lint_workspace(
        root,
        config.lint,
        hooks_dir=hooks_dir,
        settings_dir=settings_dir,
        kiro_home=kiro_home(os.environ),
    )


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
        chosen = config.collections[name] if name else _collection_for_profile(config, profile)
        reports.append(
            lint_paths(
                group,
                config.lint,
                root,
                profile=_profile(profile or (chosen.lint if chosen else "steering")),
                spec=FileSpec(chosen.include, chosen.exclude, chosen.max_depth) if chosen else None,
            )
        )
    return merge_reports(reports)


def _collection_for_profile(config: Config, profile: str | None) -> CollectionConfig | None:
    """The first collection using ``profile``, so its file patterns apply to a directory."""
    if profile is None:
        return None
    return next((c for c in config.collections.values() if c.lint == profile), None)


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
            # A collection rooted at the workspace (AGENTS.md, .kiroignore) owns only the
            # files its patterns select, never every path under the repository.
            spec = FileSpec(collection.include, collection.exclude, collection.max_depth)
            if (
                resolved.is_file()
                and base in resolved.parents
                and spec.matcher()(resolved.relative_to(base).as_posix())
            ):
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
    default=None,
    help="Add the synced collection directories to .git/info/exclude (default: on).",
)
@click.option("--hook/--no-hook", default=None, help="Also install the pre-push hook.")
@click.option(
    "--kiro/--no-kiro",
    default=None,
    help="Also write the Kiro SessionStart hook and MCP server entry into this repository's "
    ".kiro (for teams that commit them). `steersync setup` installs both once in ~/.kiro.",
)
@click.option(
    "--mcp-command",
    default="uvx",
    show_default=True,
    help="Executable Kiro uses to start the MCP server (use an absolute path if uvx is "
    "not on Kiro's PATH).",
)
@plan_options
@click.pass_context
def init(
    ctx: click.Context,
    /,
    write_config: bool,
    exclude: bool | None,
    hook: bool | None,
    kiro: bool | None,
    mcp_command: str,
    **plan_flags: object,
) -> None:
    """Prepare this repository: excludes, git hook, and optionally the Kiro pieces.

    Shows the plan once and asks once. The step flags (--exclude, --hook, --kiro,
    --write-config) are answers: with any of them nothing is asked. Without a terminal and
    without flags or --yes it prints the plan and exits 2.
    """
    config, root = _bootstrap(ctx)
    if root is None:
        _fail(SteersyncError("not inside a git repository"))
        return
    env = os.environ
    registry.register(env, root, "init")
    state = setup_plan.detect_setup_state(env, current=config, root=root, remote=None)
    plan = setup_plan.build_setup_plan(
        state,
        "project",
        exclude=exclude is not False,
        hook=bool(hook),
        write_config=write_config,
        kiro=bool(kiro),
        mcp_command=mcp_command,
    )
    flags_answered = write_config or any(v is not None for v in (exclude, hook, kiro))
    result = run_plan(
        plan, config, plan_flags, answered=flags_answered, notes=setup_plan.notes(state)
    )
    if result.exit_code == 0 and not plan_flags.get("dry_run") and not plan_flags.get("as_json"):
        if "repo.exclude" in result.applied:
            _hint_tracked_files(config, root, setup_plan.exclude_patterns(config))
        steering_dir = workspace_steering_dir(config, root)
        steering_dir.mkdir(parents=True, exist_ok=True)
        click.echo(f"ready: {steering_dir}")
        click.echo(f"collections: {', '.join(config.enabled_collections()) or '(none)'}")
    plan_flow.finish(result)


def _hint_tracked_files(config: Config, root: Path, patterns: list[str]) -> None:
    """Explain how to stop tracking files that the central repository now manages."""
    folders = [p.strip("/") for p in patterns if p.endswith("/")]
    listed = gitops.run_git(config, ["ls-files", "--", *folders], cwd=root, check=False)
    tracked = [line for line in listed.stdout.splitlines() if line.strip()]
    if listed.returncode != 0 or not tracked:
        return
    with_files = [f for f in folders if any(path.startswith(f"{f}/") for path in tracked)]
    click.echo(
        f"note: {len(tracked)} file(s) in synced directories are still tracked by this "
        "repository. Once the central repository has them (steersync push, then merge), "
        "you can make it their only source:\n"
        f"  git rm -r --cached --ignore-unmatch {' '.join(with_files)}\n"
        '  git commit -m "Kiro configuration comes from the central repository"\n'
        "or keep committing them and rerun init with --no-exclude.",
        err=True,
    )


@main.command("setup")
@click.option("--remote", help="Central repository: a GitLab page address, SSH or HTTPS URL.")
@click.option("--branch", help="Default branch of the central repository (detected when omitted).")
@click.option(
    "--mcp/--no-mcp", default=None, help="Register the MCP server in ~/.kiro/settings/mcp.json."
)
@click.option(
    "--session-hook/--no-session-hook",
    default=None,
    help="Add a global Kiro hook that pulls when any session starts.",
)
@click.option(
    "--project/--no-project",
    default=None,
    help="Also prepare the git repository in the current directory.",
)
@plan_options
@click.pass_context
def setup(
    ctx: click.Context,
    /,
    remote: str | None,
    branch: str | None,
    mcp: bool | None,
    session_hook: bool | None,
    project: bool | None,
    **plan_flags: object,
) -> None:
    """Set steersync up on this machine: one plan, one approval. Safe to run again.

    Detects git, the central GitLab repository (asked only when nothing is configured),
    its branch, extra .kiro folders, the MCP registration in ~/.kiro/settings/mcp.json
    (added only when no server there runs steersync; other servers stay unchanged), the
    global Kiro session hook and this repository's excludes and git hook. Everything is
    shown as one plan and applied after one approval, with a backup:
    `steersync restore <id>` undoes the run. Rerunning on a configured machine prints
    "Nothing to do".
    """
    env = os.environ
    params = ctx.find_root().params
    configure_logging(
        params.get("log_level") or "WARNING", params.get("log_format") or "text", quiet=False
    )
    interaction = interaction_from(plan_flags)
    git = setup_steps.check_git()
    if not git.ok:
        _fail(SteersyncError(git.problem or "git is not usable"))
        return
    try:
        current = load_config(workspace_root=None, env=env)
    except SteersyncError as exc:
        _fail(exc)
        return
    chosen, local = _setup_remote(current, remote, interaction, env)
    picked = _setup_branch(current, chosen, branch)
    root = _workspace_root(current, env)
    registry.register(env, root, "setup")
    scope: setup_plan.Scope = "machine" if root is None or project is False else "all"
    try:
        state = setup_plan.detect_setup_state(
            env, current=current, root=root, remote=chosen, local=local, branch=picked
        )
        plan = setup_plan.build_setup_plan(state, scope)
    except (SteersyncError, ValueError) as exc:
        _fail(ConfigError(f"the configuration would be invalid: {exc}"))
        return
    skipped = tuple(
        step_id
        for flag, step_id in ((mcp, "mcp.register"), (session_hook, "hook.session"))
        if flag is False and any(s.id == step_id for s in plan.steps)
    )
    result = run_plan(
        plan,
        current,
        plan_flags,
        notes=setup_plan.notes(state),
        extra_skip=skipped,
        remember=True,
    )
    if result.applied:
        profile = plan_flags.get("profile")
        _remember_selection(
            current,
            state.config_path,
            result.applied,
            profile if isinstance(profile, str) else None,
        )
    if root is not None and scope == "all" and result.exit_code == 0 and not interaction.dry_run:
        _setup_summary(root, env)
    plan_flow.finish(result)


def _setup_remote(
    current: Config, remote: str | None, interaction: Interaction, env: Mapping[str, str]
) -> tuple[setup_steps.RemoteCheck, bool]:
    """The reachable central repository: --remote, then the configuration, then one question."""
    answer = remote or current.remote.url or None
    for _ in range(3):
        if answer is None:
            if not interaction.interactive:
                _fail(ConfigError("pass --remote: no central repository is configured yet"))
            answer = click.prompt(
                "GitLab repository that holds the shared Kiro configuration "
                "(page address or clone URL)"
            )
        candidates = setup_steps.candidate_urls(answer)
        if not candidates:
            click.echo("that is not a repository address", err=True)
        failures: list[setup_steps.RemoteCheck] = []
        for candidate in candidates:
            probe = current.model_copy(
                update={
                    "remote": current.remote.model_copy(update={"allow_local": candidate.local})
                }
            )
            click.echo(f"checking {gitops.redact_url(candidate.url)} ...", err=True)
            check = setup_steps.probe_remote(probe, candidate.url, env)
            if check.reachable:
                return check, candidate.local
            failures.append(check)
        for failure in failures:
            click.echo(f"  not reachable: {failure.error}", err=True)
        if failures:
            hint = setup_steps.explain_unreachable(failures[0].url, failures[0].error or "")
            click.echo(f"  next step: {hint}", err=True)
        if remote or not interaction.interactive:
            _fail(SteersyncError("the central repository is not reachable with git"))
        answer = None
    _fail(SteersyncError("no reachable central repository after three attempts"))
    raise AssertionError  # pragma: no cover - _fail always exits


def _setup_branch(current: Config, chosen: setup_steps.RemoteCheck, branch: str | None) -> str:
    """--branch, then the configured branch, then the remote's default; checked with git."""
    if not chosen.branches:
        _fail(
            SteersyncError(
                "the repository has no branches yet. In GitLab create it with 'Initialize "
                "repository with a README' (or push one commit), then run setup again"
            )
        )
    picked = branch or (
        current.remote.default_branch
        if current.remote.url == chosen.url and current.remote.default_branch in chosen.branches
        else setup_steps.suggested_branch(chosen)
    )
    if picked not in chosen.branches:
        _fail(
            ConfigError(
                f"branch {picked!r} does not exist in the central repository; branches: "
                f"{', '.join(chosen.branches[:10])}"
            )
        )
    return picked


def _remember_selection(
    current: Config, config_path: Path, applied: tuple[str, ...], profile: str | None
) -> None:
    """setup.last (and a new --profile) in the user configuration; never secrets."""
    if not config_path.is_file() or not (current.ux.remember_selection or profile):
        return
    data = setup_steps.load_yaml_file(config_path)
    section = dict(data.get("setup") or {})
    if current.ux.remember_selection:
        section["last"] = list(applied)
    if profile and profile not in current.setup.profiles:
        profiles = dict(section.get("profiles") or {})
        profiles[profile] = list(applied)
        section["profiles"] = profiles
    setup_steps.write_user_config(config_path, {**data, "setup": section})


def _setup_summary(root: Path, env: Mapping[str, str]) -> None:
    """What the first push would propose, so the next step is obvious."""
    try:
        config = load_config(workspace_root=root, env=env)
        result = sync.status(config, workspace_root=root, env=env, run_lint=False)
    except SteersyncError as exc:
        click.echo(f"note: could not compare with the central repository: {exc}", err=True)
        return
    plans = result.workspace or {}
    pending = sum(len(p.add) for p in plans.values())
    click.echo(f"project {result.project}: {pending} file(s) only here", err=False)
    if pending:
        click.echo("next: `steersync push` proposes them as one merge request")


@main.command("kiro-hook")
@click.option("--write", is_flag=True, help="Write the hook to .kiro/hooks/steersync.json.")
@click.pass_context
def kiro_hook(ctx: click.Context, write: bool) -> None:
    """Print (or write) a Kiro hook that runs `steersync pull` when a session starts."""
    rendered = hooks.render_kiro_hook()
    if write:
        _, root = _bootstrap(ctx)
        if root is None:
            _fail(SteersyncError("not inside a git repository"))
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
    try:
        from steersync.mcp_server import serve  # noqa: PLC0415  (optional dependency)
    except ImportError:
        _fail(SteersyncError(MISSING_MCP_EXTRA))
        return
    try:
        serve()
    except SteersyncError as exc:
        _fail(exc)


def _kiro_status(config: Config, root: Path | None) -> kiro_version.KiroStatus:
    return kiro_version.detect(
        os.environ,
        min_cli_version=config.kiro.min_cli_version,
        min_ide_version=config.kiro.min_ide_version,
        legacy_keys=config.lint.legacy_agent_keys,
        upgraded_keys=config.lint.upgraded_agent_keys,
        agent_dirs=_agent_dirs(config, root),
    )


def _kiro_lines(status: kiro_version.KiroStatus) -> list[str]:
    if status.cli_path is None:
        cli = "kiro-cli: not found (Kiro IDE only)"
    else:
        cli = f"kiro-cli: {status.cli_version or 'unknown version'} ({status.cli_path})"
    engine = status.engine or ("v3 (default)" if status.engine_source == "default" else "unknown")
    lines = [
        cli,
        f"agent engine: {engine} [{status.engine_source}]",
        f"supported: Kiro CLI {status.min_cli_version}+ (V3), Kiro IDE {status.min_ide_version}+",
    ]
    if status.cli_path is not None and status.workflows_enabled is not True:
        lines.append(
            "workflows: off (chat.enableWorkflows); synced recipes load after "
            "`kiro-cli settings chat.enableWorkflows true`"
        )
    lines.extend(f"legacy agent: {path}" for path in status.legacy_agents)
    lines.append("kiro: supported" if status.supported else "kiro: below the supported baseline")
    return lines


@main.group("schema")
def schema_group() -> None:
    """JSON Schemas of the Kiro files steersync understands."""


@schema_group.command("export")
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    type=click.Choice(sorted(schema_models.KINDS)),
    help="Which schema (repeatable; default: all).",
)
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory to write <kind>.schema.json into (default: print to stdout).",
)
def schema_export(kinds: tuple[str, ...], out: Path | None) -> None:
    """Write the JSON Schema for steering, skill, agent and hook files (by Kiro field name)."""
    chosen = kinds or tuple(sorted(schema_models.KINDS))
    for kind in chosen:
        text = json.dumps(schema_models.json_schema(kind), indent=2, sort_keys=True) + "\n"
        if out is None:
            click.echo(text, nl=False)
            continue
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{kind}.schema.json"
        target.write_text(text, encoding="utf-8")
        click.echo(f"written: {target}")


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
@click.option("--kiro", "kiro_only", is_flag=True, help="Only check the Kiro installation.")
@click.option("--strict", is_flag=True, help="With --kiro: exit 10 below the V3 baseline.")
@click.option("--json", "as_json", is_flag=True, help="With --kiro: machine readable output.")
@click.pass_context
def doctor(ctx: click.Context, kiro_only: bool, strict: bool, as_json: bool) -> None:
    """Check git, configuration and directories, and the Kiro installation.

    `--kiro` runs only the Kiro check: kiro-cli version, chat.agentEngine and agent files
    against the supported V3 baseline (kiro.min_cli_version). It runs `kiro-cli --version`
    and `kiro-cli settings` only, so it never starts a chat or uses Kiro credits.
    """
    config, root = _bootstrap(ctx)
    env = os.environ
    if kiro_only:
        status = _kiro_status(config, root)
        if as_json:
            click.echo(json.dumps(status.to_dict(), indent=2))
        else:
            for line in _kiro_lines(status):
                click.echo(line)
            if not status.supported:
                click.echo(kiro_version.problem_text(status), err=True)
        if strict and not status.supported:
            sys.exit(10)
        return
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
    status = _kiro_status(config, root)
    for line in _kiro_lines(status):
        click.echo(line)
    if not status.supported:
        click.echo(kiro_version.problem_text(status), err=True)


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


main.add_command(fleet)
# Registers `fleet push`, `fleet pull` and `fleet restore` on the group (no UI imports).
import steersync.cli_fleet_actions  # noqa: E402

main.add_command(steersync.cli_fleet_actions.restore_command)

if __name__ == "__main__":  # pragma: no cover
    run()
