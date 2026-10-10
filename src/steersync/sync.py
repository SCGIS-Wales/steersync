"""Pull, push and status orchestration built on the git and steering modules."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from steersync import gitops, paths
from steersync.config import Config, remote_host, validate_remote_url
from steersync.errors import ConfigError, LintError, SyncConflictError
from steersync.lint import LintReport, lint_snapshot
from steersync.steering import (
    ChangePlan,
    Snapshot,
    apply_plan,
    lock_hashes,
    plan_changes,
    read_lock,
    replace_directory_contents,
    snapshot_digest,
    snapshot_directory,
    snapshot_hashes,
    write_lock,
)

log = logging.getLogger(__name__)

Scope = Literal["workspace", "global"]


class ScopeResult(NamedTuple):
    scope: Scope
    target_dir: Path
    plan: ChangePlan
    written: tuple[str, ...]
    lint: LintReport


class PullResult(NamedTuple):
    remote: str
    commit: str
    scopes: tuple[ScopeResult, ...]
    dry_run: bool

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(f"{s.scope}:{c}" for s in self.scopes for c in s.plan.conflict)

    @property
    def change_count(self) -> int:
        return sum(s.plan.change_count for s in self.scopes)


class PushResult(NamedTuple):
    status: Literal[
        "pushed", "up-to-date", "already-proposed", "nothing-to-push", "dry-run", "skipped"
    ]
    project: str
    branch: str | None
    commit: str | None
    digest: str | None
    plan: ChangePlan | None
    lint: LintReport | None
    message: str


class StatusResult(NamedTuple):
    project: str | None
    remote: str
    remote_commit: str | None
    workspace: ChangePlan | None
    global_: ChangePlan | None
    lint: LintReport | None


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _central_url(config: Config) -> str:
    return validate_remote_url(
        config.remote.url,
        allowed_hosts=config.remote.allowed_hosts,
        allow_local=config.remote.allow_local,
    )


def _refresh_cache(config: Config, env: Mapping[str, str], url: str) -> tuple[Path, str]:
    cache_dir = paths.cache_repo_dir(config, env, url)
    git_env = gitops.git_environment(env)
    gitops.ensure_cache_clone(config, url=url, cache_dir=cache_dir, env=git_env)
    return cache_dir, gitops.head_commit(config, cache_dir, env=git_env)


def _project_dir(config: Config, cache_dir: Path, project: str) -> Path:
    return paths.ensure_within(cache_dir, Path(config.remote.projects_dir) / project)


def resolve_project(
    config: Config, *, workspace_root: Path, env: Mapping[str, str], remote_url: str | None
) -> str | None:
    """Return the project slug for the workspace, or ``None`` when it cannot be determined."""
    url = remote_url or gitops.remote_url(config, workspace_root, env=gitops.git_environment(env))
    if not url:
        return None
    return gitops.project_slug(url)


def pull(
    config: Config,
    *,
    workspace_root: Path | None,
    env: Mapping[str, str] | None = None,
    include_workspace: bool = True,
    include_global: bool = True,
    remote_url: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    strict: bool | None = None,
) -> PullResult:
    """Fetch the central repository and apply global and workspace steering locally."""
    environment = _env(env)
    url = _central_url(config)
    cache_dir, commit = _refresh_cache(config, environment, url)
    strict_pull = config.sync.strict_pull if strict is None else strict
    results: list[ScopeResult] = []

    if include_global:
        source_dir = paths.ensure_within(cache_dir, Path(config.remote.global_dir))
        target = paths.global_steering_dir(config, environment)
        lock_path = paths.global_lock_path(config, environment)
        results.append(
            _apply_scope(
                "global",
                config,
                source_dir,
                target,
                lock_path,
                url,
                commit,
                force,
                dry_run,
                strict_pull,
            )
        )

    if include_workspace and workspace_root is not None:
        project = resolve_project(
            config, workspace_root=workspace_root, env=environment, remote_url=remote_url
        )
        if project is None:
            log.warning("workspace has no origin remote; skipping workspace steering")
        else:
            source_dir = _project_dir(config, cache_dir, project)
            target = paths.workspace_steering_dir(config, workspace_root)
            lock_path = paths.workspace_lock_path(config, workspace_root)
            results.append(
                _apply_scope(
                    "workspace",
                    config,
                    source_dir,
                    target,
                    lock_path,
                    url,
                    commit,
                    force,
                    dry_run,
                    strict_pull,
                )
            )
    return PullResult(url, commit, tuple(results), dry_run)


def _apply_scope(
    scope: Scope,
    config: Config,
    source_dir: Path,
    target_dir: Path,
    lock_path: Path,
    url: str,
    commit: str,
    force: bool,
    dry_run: bool,
    strict_pull: bool,
) -> ScopeResult:
    source: Snapshot = snapshot_directory(source_dir)
    report = lint_snapshot(source, config.lint, None)
    if strict_pull and not report.ok:
        raise LintError(
            f"{scope}: remote steering has {len(report.errors)} lint error(s); refusing to pull"
        )
    for finding in report.findings:
        log.log(
            logging.ERROR if finding.severity == "error" else logging.WARNING,
            "%s: %s",
            scope,
            finding.render(),
        )
    target = snapshot_directory(target_dir)
    lock = read_lock(lock_path)
    plan = plan_changes(source, target, lock_hashes(lock), prune=config.sync.prune)
    if plan.conflict and not force:
        log.warning(
            "%s: %d locally modified file(s) kept; use --force to overwrite",
            scope,
            len(plan.conflict),
        )
    written: tuple[str, ...] = ()
    if not dry_run:
        written = tuple(apply_plan(target_dir, source, plan, force=force))
        write_lock(
            lock_path,
            remote=url,
            commit=commit,
            files=_next_lock(source, plan, lock_hashes(lock), force),
        )
    return ScopeResult(scope, target_dir, plan, written, report)


def push(
    config: Config,
    *,
    workspace_root: Path,
    env: Mapping[str, str] | None = None,
    remote_url: str | None = None,
    scope: Scope = "workspace",
    dry_run: bool = False,
    run_lint: bool = True,
    message: str | None = None,
) -> PushResult:
    """Propose the local steering as a branch (and merge request) on the central repository."""
    environment = _env(env)
    url = _central_url(config)
    git_env = gitops.git_environment(environment)

    if scope == "workspace":
        project = resolve_project(
            config, workspace_root=workspace_root, env=environment, remote_url=remote_url
        )
        if project is None:
            raise ConfigError(
                "cannot determine the project: no origin remote and no --remote-url given"
            )
        project_remote = remote_url or gitops.remote_url(config, workspace_root, env=git_env) or ""
        if _same_remote(project_remote, url):
            return PushResult(
                "skipped",
                project,
                None,
                None,
                None,
                None,
                None,
                "workspace is the central repository",
            )
        local_dir = paths.workspace_steering_dir(config, workspace_root)
    else:
        project = "global"
        local_dir = paths.global_steering_dir(config, environment)

    local = snapshot_directory(local_dir)
    if not local:
        return PushResult(
            "nothing-to-push",
            project,
            None,
            None,
            None,
            None,
            None,
            f"no steering files in {local_dir}",
        )

    report = (
        lint_snapshot(local, config.lint, workspace_root if scope == "workspace" else None)
        if run_lint
        else None
    )
    if report is not None and not report.ok:
        raise LintError(
            f"{len(report.errors)} lint error(s) in {local_dir}; fix them or pass --no-lint"
        )

    cache_dir, base_commit = _refresh_cache(config, environment, url)
    central_dir = (
        _project_dir(config, cache_dir, project)
        if scope == "workspace"
        else paths.ensure_within(cache_dir, Path(config.remote.global_dir))
    )
    central = snapshot_directory(central_dir)
    plan = plan_changes(local, central, snapshot_hashes(central), prune=True)
    if plan.is_empty:
        return PushResult(
            "up-to-date",
            project,
            None,
            base_commit,
            snapshot_digest(local),
            plan,
            report,
            "central repository already matches",
        )

    digest = snapshot_digest(local)
    user = gitops.current_user_id(config, workspace_root, environment)
    branch = gitops.build_branch_name(config, project=project, user=user, content_hash=digest)
    if not gitops.check_ref_format(config, branch, env=git_env):
        raise ConfigError(f"generated branch name is not a valid git ref: {branch}")
    if dry_run:
        return PushResult(
            "dry-run",
            project,
            branch,
            None,
            digest,
            plan,
            report,
            f"would push {plan.change_count} change(s)",
        )
    if gitops.remote_branch_exists(config, url, branch, env=git_env):
        return PushResult(
            "already-proposed",
            project,
            branch,
            None,
            digest,
            plan,
            report,
            "an identical proposal branch already exists",
        )

    gitops.create_branch(
        config, cache_dir, branch, f"origin/{config.remote.default_branch}", env=git_env
    )
    replace_directory_contents(central_dir, local)
    relative_dir = central_dir.relative_to(cache_dir).as_posix()
    author_name = (
        gitops.config_value(config, workspace_root, "user.name", env=git_env) or "steersync"
    )
    author_email = (
        gitops.config_value(config, workspace_root, "user.email", env=git_env)
        or "steersync@localhost"
    )
    commit_message = message or _commit_message(project, user, plan)
    commit = gitops.commit_paths(
        config,
        cache_dir,
        paths=[relative_dir],
        message=commit_message,
        author_name=author_name,
        author_email=author_email,
        env=git_env,
    )
    if commit is None:
        return PushResult(
            "up-to-date",
            project,
            branch,
            base_commit,
            digest,
            plan,
            report,
            "no changes after staging",
        )
    no_hooks = paths.cache_root(config, environment) / "no-hooks"
    no_hooks.mkdir(parents=True, exist_ok=True)
    push_env = gitops.git_environment(environment, hooks_dir=no_hooks)
    options = gitops.render_push_options(config, project=project, user=user, content_hash=digest)
    _, options_delivered = gitops.push_branch(
        config, cache_dir, url=url, branch=branch, push_options=options, env=push_env
    )
    gitops.create_branch(
        config,
        cache_dir,
        config.remote.default_branch,
        f"origin/{config.remote.default_branch}",
        env=git_env,
    )
    return PushResult(
        "pushed",
        project,
        branch,
        commit,
        digest,
        plan,
        report,
        f"pushed {plan.change_count} change(s) to {branch}"
        + ("" if options_delivered or not options else " (push options not accepted by remote)"),
    )


def _commit_message(project: str, user: str, plan: ChangePlan) -> str:
    lines = [f"steering: update {project} from {user}", ""]
    lines.extend(f"add: {p}" for p in plan.add)
    lines.extend(f"update: {p}" for p in plan.update)
    lines.extend(f"delete: {p}" for p in plan.delete)
    return "\n".join(lines).rstrip() + "\n"


def _next_lock(
    source: Snapshot, plan: ChangePlan, previous: Mapping[str, str], force: bool
) -> dict[str, str]:
    """Compute the lock content after applying ``plan``.

    Conflicting files keep their previous lock entry so later pulls still see
    them as locally modified. With ``force`` everything tracks the source.
    """
    source_hashes = snapshot_hashes(source)
    if force:
        return source_hashes
    next_lock = {rel: digest for rel, digest in source_hashes.items() if rel not in plan.conflict}
    next_lock.update({rel: previous[rel] for rel in plan.conflict if rel in previous})
    return next_lock


def _same_remote(left: str, right: str) -> bool:
    """True when two URLs name the same repository (same host and project path)."""
    try:
        same_path = (
            gitops.project_path_from_url(left).lower()
            == gitops.project_path_from_url(right).lower()
        )
        same_host = (remote_host(left) or "").lower() == (remote_host(right) or "").lower()
    except ConfigError, Exception:
        return False
    return same_path and same_host


def status(
    config: Config,
    *,
    workspace_root: Path | None,
    env: Mapping[str, str] | None = None,
    remote_url: str | None = None,
    run_lint: bool = True,
) -> StatusResult:
    """Compare local steering with the central repository without changing anything."""
    environment = _env(env)
    url = _central_url(config)
    cache_dir, commit = _refresh_cache(config, environment, url)
    global_source = snapshot_directory(
        paths.ensure_within(cache_dir, Path(config.remote.global_dir))
    )
    global_target = snapshot_directory(paths.global_steering_dir(config, environment))
    global_plan = plan_changes(
        global_source,
        global_target,
        lock_hashes(read_lock(paths.global_lock_path(config, environment))),
        prune=config.sync.prune,
    )
    project: str | None = None
    workspace_plan: ChangePlan | None = None
    report: LintReport | None = None
    if workspace_root is not None:
        project = resolve_project(
            config, workspace_root=workspace_root, env=environment, remote_url=remote_url
        )
        if project is not None:
            local = snapshot_directory(paths.workspace_steering_dir(config, workspace_root))
            central = snapshot_directory(_project_dir(config, cache_dir, project))
            workspace_plan = plan_changes(local, central, snapshot_hashes(central), prune=True)
            if run_lint:
                report = lint_snapshot(local, config.lint, workspace_root)
    return StatusResult(project, url, commit, workspace_plan, global_plan, report)


def raise_on_conflicts(result: PullResult) -> None:
    if result.conflicts:
        raise SyncConflictError("locally modified steering kept: " + ", ".join(result.conflicts))
