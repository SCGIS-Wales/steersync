"""Pull, push and status orchestration built on the git and steering modules.

Every operation works on *collections* (steering, specs, skills, agents, hooks,
settings or anything configured): a pull applies each collection of each scope,
a push proposes all selected collections of one scope in a single branch.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, NamedTuple, ParamSpec, TypeVar, cast
from urllib.parse import urlsplit

from steersync import gitlab, gitops, paths
from steersync.config import CollectionConfig, Config, remote_host, validate_remote_url
from steersync.errors import ConfigError, GitError, GitLabError, LintError, SyncConflictError
from steersync.lint import LintReport, lint_snapshot, merge_reports, prefix_report
from steersync.locking import exclusive_lock
from steersync.steering import (
    ChangePlan,
    FileSpec,
    Snapshot,
    apply_plan,
    discover_files,
    enforce_limits,
    executable_paths,
    lock_collections,
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

# Explicit ParamSpec/TypeVar instead of PEP 695 syntax: CodeQL's Python extractor
# does not model inline type parameters and reports them as uninitialised.
P = ParamSpec("P")
R = TypeVar("R")


class CollectionResult(NamedTuple):
    """Outcome of applying one collection in one scope."""

    scope: Scope
    collection: str
    target_dir: Path
    plan: ChangePlan
    written: tuple[str, ...]
    lint: LintReport


class PullResult(NamedTuple):
    remote: str
    commit: str
    results: tuple[CollectionResult, ...]
    dry_run: bool
    # True when the central repository was unreachable and the cached copy was used.
    offline: bool = False

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(f"{r.scope}:{r.collection}:{c}" for r in self.results for c in r.plan.conflict)

    @property
    def change_count(self) -> int:
        return sum(r.plan.change_count for r in self.results)


class PushResult(NamedTuple):
    status: Literal[
        "pushed", "up-to-date", "already-proposed", "nothing-to-push", "dry-run", "skipped"
    ]
    project: str
    collections: tuple[str, ...]
    branch: str | None
    commit: str | None
    digest: str | None
    # Paths in the plan are prefixed with the collection's central directory.
    plan: ChangePlan | None
    lint: LintReport | None
    message: str
    # Other proposal branches for the same project still on the remote.
    existing_branches: tuple[str, ...] = ()
    # URL of the merge request for the pushed branch when the GitLab API confirmed or
    # created it, and a short note on how it was obtained.
    merge_request: str | None = None
    merge_request_note: str = ""


class StatusResult(NamedTuple):
    project: str | None
    remote: str
    remote_commit: str | None
    # Plans keyed by collection name; ``None`` when the scope is unavailable.
    workspace: dict[str, ChangePlan] | None
    global_: dict[str, ChangePlan]
    lint: LintReport | None
    # Proposal branches for this project still on the central remote.
    proposals: tuple[str, ...] = ()


class MergeRequestResult(NamedTuple):
    project: str
    branch: str
    url: str
    created: bool
    # Other open merge requests from proposal branches of the same project.
    others: tuple[str, ...]


class _ScopeTarget(NamedTuple):
    """Where a scope reads from in the cache clone and writes to locally."""

    scope: Scope
    source_base: Path
    lock_path: Path
    target_for: Callable[[CollectionConfig], Path | None]


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _validated_url(config: Config) -> str:
    return validate_remote_url(
        config.remote.url,
        allowed_hosts=config.remote.allowed_hosts,
        allow_local=config.remote.allow_local,
    )


def _central_url(config: Config) -> str:
    url = _validated_url(config)
    if urlsplit(url).password:
        log.warning(
            "remote.url embeds a password or token; prefer an ssh key or a git credential helper"
        )
    return url


def _cache_git_env(config: Config, env: Mapping[str, str]) -> dict[str, str]:
    """git environment for the cache clone: prompts off and every git hook disabled."""
    no_hooks = paths.cache_root(config, env) / "no-hooks"
    no_hooks.mkdir(parents=True, exist_ok=True, mode=0o700)
    return gitops.git_environment(env, hooks_dir=no_hooks)


def _refresh_cache(
    config: Config, env: Mapping[str, str], url: str, *, allow_offline: bool = False
) -> tuple[Path, str, bool]:
    """Refresh the cache clone and return its path, head commit and an offline flag.

    With ``allow_offline`` a transient failure to reach the central repository falls
    back to the cached default branch when ``sync.offline_fallback`` is on, so a
    session start or a pull on a train still applies the last known configuration.
    """
    cache_dir = paths.cache_repo_dir(config, env, url)
    git_env = _cache_git_env(config, env)
    try:
        gitops.ensure_cache_clone(config, url=url, cache_dir=cache_dir, env=git_env)
    except GitError as exc:
        usable = (cache_dir / ".git").is_dir()
        if not (allow_offline and config.sync.offline_fallback and usable):
            raise
        if not gitops.is_transient(str(exc)):
            raise
        log.warning(
            "central repository unreachable (%s); applying the cached copy of %s instead",
            exc,
            config.remote.default_branch,
        )
        gitops.reset_cache_clone(config, cache_dir=cache_dir, env=git_env)
        return cache_dir, gitops.head_commit(config, cache_dir, env=git_env), True
    return cache_dir, gitops.head_commit(config, cache_dir, env=git_env), False


def _spec(collection: CollectionConfig) -> FileSpec:
    return FileSpec(collection.include, collection.exclude, collection.max_depth)


def _central_snapshot(config: Config, directory: Path, spec: FileSpec) -> Snapshot:
    """Read central repository content without following symlinks, within the size limits."""
    snapshot = snapshot_directory(directory, spec, follow_symlinks=False)
    enforce_limits(
        snapshot, max_files=config.sync.max_files, max_total_bytes=config.sync.max_total_bytes
    )
    return snapshot


def _lint_collection(
    collection: CollectionConfig,
    config: Config,
    snapshot: Snapshot,
    workspace_root: Path | None,
) -> LintReport:
    return lint_snapshot(
        snapshot,
        config.lint,
        workspace_root,
        profile=collection.lint,
        max_file_bytes=collection.max_file_bytes,
    )


def _warn_legacy_layout(base: Path, collection: CollectionConfig, source_dir: Path) -> None:
    """Point at the pre 0.2 flat layout (``global/*.md``) when the collection dir is absent."""
    if source_dir.is_dir() or not base.is_dir():
        return
    flat = FileSpec(collection.include, collection.exclude, max_depth=1)
    if discover_files(base, flat, follow_symlinks=False):
        log.warning(
            "%s has files directly under it but no %s/ directory; since steersync 0.2 each "
            "collection lives in its own directory, move them to %s/",
            base,
            collection.central_dir,
            collection.central_dir,
        )


def _with_cache_lock(func: Callable[P, R]) -> Callable[P, R]:  # noqa: UP047
    """Serialise operations that share a cache clone across processes."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        config = cast(Config, args[0] if args else kwargs["config"])
        environment = _env(cast(Mapping[str, str] | None, kwargs.get("env")))
        url = _validated_url(config)
        cache_dir = paths.cache_repo_dir(config, environment, url)
        paths.cache_root(config, environment).mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = cache_dir.with_name(cache_dir.name + ".lock")
        with exclusive_lock(lock_path, timeout=config.sync.lock_timeout_seconds):
            return func(*args, **kwargs)

    return wrapper


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


@_with_cache_lock
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
    collections: Sequence[str] | None = None,
) -> PullResult:
    """Fetch the central repository and apply every selected collection locally."""
    environment = _env(env)
    url = _central_url(config)
    selected = config.select_collections(collections)
    cache_dir, commit, offline = _refresh_cache(config, environment, url, allow_offline=True)
    strict_pull = config.sync.strict_pull if strict is None else strict
    targets: list[_ScopeTarget] = []

    if include_global:
        targets.append(
            _ScopeTarget(
                "global",
                paths.ensure_within(cache_dir, Path(config.remote.global_dir)),
                paths.global_lock_path(config, environment),
                lambda c: paths.global_collection_dir(c, environment),
            )
        )
    if include_workspace and workspace_root is not None:
        project = resolve_project(
            config, workspace_root=workspace_root, env=environment, remote_url=remote_url
        )
        if project is None:
            log.warning("workspace has no origin remote; skipping workspace collections")
        else:
            targets.append(
                _ScopeTarget(
                    "workspace",
                    _project_dir(config, cache_dir, project),
                    paths.workspace_lock_path(config, workspace_root),
                    lambda c: paths.workspace_collection_dir(c, workspace_root),
                )
            )

    results: list[CollectionResult] = []
    for target in targets:
        results.extend(
            _pull_scope(
                target,
                config,
                selected,
                url=url,
                commit=commit,
                force=force,
                dry_run=dry_run,
                strict_pull=strict_pull,
                workspace_root=workspace_root if target.scope == "workspace" else None,
            )
        )
    return PullResult(url, commit, tuple(results), dry_run, offline)


def _pull_scope(
    target: _ScopeTarget,
    config: Config,
    selected: Mapping[str, CollectionConfig],
    *,
    url: str,
    commit: str,
    force: bool,
    dry_run: bool,
    strict_pull: bool,
    workspace_root: Path | None,
) -> list[CollectionResult]:
    """Apply every selected collection of one scope and rewrite the scope's lock once."""
    lock = read_lock(target.lock_path)
    next_lock = lock_collections(lock)
    results: list[CollectionResult] = []
    for name, collection in selected.items():
        target_dir = target.target_for(collection)
        if target_dir is None:
            continue
        source_dir = paths.central_collection_dir(target.source_base, collection)
        _warn_legacy_layout(target.source_base, collection, source_dir)
        result, hashes = _apply_collection(
            target.scope,
            name,
            collection,
            config,
            source_dir,
            target_dir,
            next_lock.get(name, {}),
            force=force,
            dry_run=dry_run,
            strict_pull=strict_pull,
            workspace_root=workspace_root,
        )
        next_lock[name] = hashes
        results.append(result)
    if results and not dry_run:
        write_lock(target.lock_path, remote=url, commit=commit, collections=next_lock)
    return results


def _apply_collection(
    scope: Scope,
    name: str,
    collection: CollectionConfig,
    config: Config,
    source_dir: Path,
    target_dir: Path,
    previous: Mapping[str, str],
    *,
    force: bool,
    dry_run: bool,
    strict_pull: bool,
    workspace_root: Path | None,
) -> tuple[CollectionResult, dict[str, str]]:
    label = f"{scope}/{name}"
    spec = _spec(collection)
    source = _central_snapshot(config, source_dir, spec)
    report = _lint_collection(collection, config, source, None)
    if strict_pull and not report.ok:
        raise LintError(
            f"{label}: remote content has {len(report.errors)} lint error(s); refusing to pull"
        )
    for finding in report.findings:
        log.log(
            logging.ERROR if finding.severity == "error" else logging.DEBUG,
            "%s: %s",
            label,
            finding.render(),
        )
    if report.warnings:
        log.warning(
            "%s: %d warning(s) in the central content; run 'steersync lint' for details",
            label,
            len(report.warnings),
        )
    del workspace_root
    local = snapshot_directory(target_dir, spec)
    plan = plan_changes(source, local, previous, prune=config.sync.prune)
    if plan.conflict and not force:
        log.warning(
            "%s: %d locally modified file(s) kept; use --force to overwrite",
            label,
            len(plan.conflict),
        )
    written: tuple[str, ...] = ()
    if not dry_run:
        executable = executable_paths(source_dir, source)
        written = tuple(apply_plan(target_dir, source, plan, force=force, executable=executable))
    return (
        CollectionResult(scope, name, target_dir, plan, written, report),
        _next_lock(source, plan, previous, force),
    )


class _LocalCollection(NamedTuple):
    name: str
    collection: CollectionConfig
    local_dir: Path
    snapshot: Snapshot


def _local_collections(
    config: Config,
    selected: Mapping[str, CollectionConfig],
    target_for: Callable[[CollectionConfig], Path | None],
) -> list[_LocalCollection]:
    found: list[_LocalCollection] = []
    for name, collection in selected.items():
        local_dir = target_for(collection)
        if local_dir is None:
            continue
        snapshot = snapshot_directory(local_dir, _spec(collection))
        enforce_limits(
            snapshot, max_files=config.sync.max_files, max_total_bytes=config.sync.max_total_bytes
        )
        found.append(_LocalCollection(name, collection, local_dir, snapshot))
    return found


def _prefixed(collection: CollectionConfig, snapshot: Mapping[str, bytes]) -> Snapshot:
    """Key a collection snapshot by its path inside the central project directory."""
    prefix = collection.central_dir or ""
    return {f"{prefix}/{rel}" if prefix else rel: data for rel, data in snapshot.items()}


def _push_unlocked(
    config: Config,
    *,
    workspace_root: Path,
    env: Mapping[str, str] | None = None,
    remote_url: str | None = None,
    scope: Scope = "workspace",
    dry_run: bool = False,
    run_lint: bool = True,
    message: str | None = None,
    collections: Sequence[str] | None = None,
) -> PushResult:
    """Propose the selected local collections as one branch (and merge request) centrally."""
    environment = _env(env)
    url = _central_url(config)
    git_env = gitops.git_environment(environment)
    selected = config.select_collections(collections)
    names = tuple(selected)

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
                names,
                None,
                None,
                None,
                None,
                None,
                "workspace is the central repository",
            )
        locals_ = _local_collections(
            config, selected, lambda c: paths.workspace_collection_dir(c, workspace_root)
        )
    else:
        project = "global"
        locals_ = _local_collections(
            config, selected, lambda c: paths.global_collection_dir(c, environment)
        )

    combined: Snapshot = {}
    for item in locals_:
        combined.update(_prefixed(item.collection, item.snapshot))
    enforce_limits(
        combined, max_files=config.sync.max_files, max_total_bytes=config.sync.max_total_bytes
    )
    if not combined:
        where = ", ".join(str(item.local_dir) for item in locals_) or "the selected collections"
        return PushResult(
            "nothing-to-push",
            project,
            names,
            None,
            None,
            None,
            None,
            None,
            f"no files in {where}",
        )

    report: LintReport | None = None
    if run_lint:
        lint_root = workspace_root if scope == "workspace" else None
        report = merge_reports(
            prefix_report(
                _lint_collection(item.collection, config, item.snapshot, lint_root),
                item.collection.central_dir or "",
            )
            for item in locals_
        )
        if not report.ok:
            raise LintError(
                f"{len(report.errors)} lint error(s) in {scope} collections; "
                "fix them or pass --no-lint"
            )

    cache_dir, base_commit, _ = _refresh_cache(config, environment, url)
    central_base = (
        _project_dir(config, cache_dir, project)
        if scope == "workspace"
        else paths.ensure_within(cache_dir, Path(config.remote.global_dir))
    )
    central: Snapshot = {}
    remote_sizes: dict[str, int] = {}
    for item in locals_:
        central_dir = paths.central_collection_dir(central_base, item.collection)
        remote_snapshot = _central_snapshot(config, central_dir, _spec(item.collection))
        remote_sizes[item.name] = len(remote_snapshot)
        central.update(_prefixed(item.collection, remote_snapshot))
    plan = plan_changes(combined, central, snapshot_hashes(central), prune=True)
    if plan.is_empty:
        return PushResult(
            "up-to-date",
            project,
            names,
            None,
            base_commit,
            snapshot_digest(combined),
            plan,
            report,
            "central repository already matches",
        )

    digest = snapshot_digest(combined)
    user = gitops.current_user_id(config, workspace_root, environment)
    branch = gitops.build_branch_name(config, project=project, user=user, content_hash=digest)
    if not gitops.check_ref_format(config, branch, env=git_env):
        raise ConfigError(f"generated branch name is not a valid git ref: {branch}")
    existing = _other_proposals(config, url, project, branch, git_env)
    if dry_run:
        return PushResult(
            "dry-run",
            project,
            names,
            branch,
            None,
            digest,
            plan,
            report,
            f"would push {plan.change_count} change(s)",
            existing,
        )
    if gitops.remote_branch_exists(config, url, branch, env=git_env):
        mr_url, mr_note = _merge_request_for(config, environment, url, branch, project, plan)
        return PushResult(
            "already-proposed",
            project,
            names,
            branch,
            None,
            digest,
            plan,
            report,
            "an identical proposal branch already exists",
            existing,
            mr_url,
            mr_note,
        )

    cache_env = _cache_git_env(config, environment)
    author_name = (
        gitops.config_value(config, workspace_root, "user.name", env=git_env) or "steersync"
    )
    author_email = (
        gitops.config_value(config, workspace_root, "user.email", env=git_env)
        or "steersync@localhost"
    )
    options = gitops.render_push_options(config, project=project, user=user, content_hash=digest)
    try:
        gitops.create_branch(
            config, cache_dir, branch, f"origin/{config.remote.default_branch}", env=cache_env
        )
        commit_paths: list[str] = []
        for item in locals_:
            if not item.snapshot and not remote_sizes.get(item.name):
                continue  # nothing to write or delete; git rejects a pathspec with no files
            central_dir = paths.central_collection_dir(central_base, item.collection)
            replace_directory_contents(
                central_dir,
                item.snapshot,
                _spec(item.collection),
                executable=executable_paths(item.local_dir, item.snapshot),
            )
            commit_paths.append(central_dir.relative_to(cache_dir).as_posix())
        commit = gitops.commit_paths(
            config,
            cache_dir,
            paths=commit_paths,
            message=message or _commit_message(project, user, plan),
            author_name=author_name,
            author_email=author_email,
            env=cache_env,
        )
        if commit is None:
            return PushResult(
                "up-to-date",
                project,
                names,
                branch,
                base_commit,
                digest,
                plan,
                report,
                "no changes after staging",
            )
        _, options_delivered = gitops.push_branch(
            config, cache_dir, url=url, branch=branch, push_options=options, env=cache_env
        )
    finally:
        # Always leave the cache on a clean default branch, even after a failed push.
        gitops.create_branch(
            config,
            cache_dir,
            config.remote.default_branch,
            f"origin/{config.remote.default_branch}",
            env=cache_env,
        )
    mr_url, mr_note = _merge_request_for(config, environment, url, branch, project, plan)
    return PushResult(
        "pushed",
        project,
        names,
        branch,
        commit,
        digest,
        plan,
        report,
        f"pushed {plan.change_count} change(s) to {branch}"
        + ("" if options_delivered or not options else " (push options not accepted by remote)"),
        existing,
        mr_url,
        mr_note,
    )


push = _with_cache_lock(_push_unlocked)
"""Propose the selected local collections as one branch (and merge request) centrally."""


def _other_proposals(
    config: Config, url: str, project: str, branch: str, git_env: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the other proposal branches for ``project`` still on the remote."""
    if not config.branch.warn_existing:
        return ()
    pattern = config.branch.project_glob(project)
    try:
        found = gitops.list_remote_branches(config, url, pattern, env=git_env)
    except GitError as exc:
        log.warning("could not list existing proposals for %s: %s", project, exc)
        return ()
    others = tuple(name for name in found if name != branch)
    if others:
        log.warning(
            "%d other proposal branch(es) for %s not merged yet: %s",
            len(others),
            project,
            ", ".join(others),
        )
    return others


def _merge_request_for(
    config: Config,
    env: Mapping[str, str],
    url: str,
    branch: str,
    project: str,
    plan: ChangePlan,
) -> tuple[str | None, str]:
    """Confirm or open the merge request for ``branch`` through the API when a token exists."""
    if not config.gitlab.auto_merge_request or not gitlab.token_available(config, env):
        return None, "no GitLab token; the merge request is opened by push options if supported"
    try:
        client = gitlab.resolve_client(config, env, url)
        mr, created = gitlab.ensure_merge_request(
            client,
            config,
            source_branch=branch,
            title=_merge_request_title(project),
            description=_commit_message(project, gitops.current_user_id(config, None, env), plan),
        )
    except (GitLabError, ConfigError) as exc:
        log.warning("merge request not confirmed: %s", exc)
        return None, f"merge request not confirmed: {exc}"
    return mr.url, "merge request created" if created else "merge request already open"


def _merge_request_title(project: str) -> str:
    return f"kiro-config: update {project}"


@_with_cache_lock
def merge_request(
    config: Config,
    *,
    workspace_root: Path,
    env: Mapping[str, str] | None = None,
    remote_url: str | None = None,
    scope: Scope = "workspace",
    branch: str | None = None,
    title: str | None = None,
    collections: Sequence[str] | None = None,
) -> MergeRequestResult:
    """Open (or find) the merge request for the current proposal branch through the GitLab API.

    Without ``branch`` the proposal branch is derived from the local content exactly as
    ``push`` derives it; it must already exist on the central remote.
    """
    environment = _env(env)
    url = _central_url(config)
    git_env = gitops.git_environment(environment)
    client = gitlab.resolve_client(config, environment, url)
    if scope == "workspace":
        project = resolve_project(
            config, workspace_root=workspace_root, env=environment, remote_url=remote_url
        )
        if project is None:
            raise ConfigError(
                "cannot determine the project: no origin remote and no --remote-url given"
            )
    else:
        project = "global"
    if branch is None:
        preview = _push_unlocked(
            config,
            workspace_root=workspace_root,
            env=environment,
            remote_url=remote_url,
            scope=scope,
            dry_run=True,
            run_lint=False,
            collections=collections,
        )
        if preview.branch is None:
            raise GitLabError(
                f"nothing to propose for {project}: {preview.message}; "
                "edit the collections or pass --branch"
            )
        branch = preview.branch
    if not gitops.remote_branch_exists(config, url, branch, env=git_env):
        raise GitLabError(f"branch {branch} is not on the central remote yet; run steersync push")
    others = _other_proposals(config, url, project, branch, git_env)
    mr, created = gitlab.ensure_merge_request(
        client,
        config,
        source_branch=branch,
        title=title or _merge_request_title(project),
        description=f"Proposal branch `{branch}` pushed by steersync.",
    )
    open_others = tuple(
        f"{item.source_branch}: {item.url}"
        for item in gitlab.open_merge_requests(client, source_branches=others)
    )
    return MergeRequestResult(project, branch, mr.url, created, open_others)


def _commit_message(project: str, user: str, plan: ChangePlan) -> str:
    lines = [f"kiro-config: update {project} from {user}", ""]
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


@_with_cache_lock
def status(
    config: Config,
    *,
    workspace_root: Path | None,
    env: Mapping[str, str] | None = None,
    remote_url: str | None = None,
    run_lint: bool = True,
    collections: Sequence[str] | None = None,
) -> StatusResult:
    """Compare local collections with the central repository without changing anything."""
    environment = _env(env)
    url = _central_url(config)
    selected = config.select_collections(collections)
    cache_dir, commit, _ = _refresh_cache(config, environment, url, allow_offline=True)
    global_base = paths.ensure_within(cache_dir, Path(config.remote.global_dir))
    global_lock = lock_collections(read_lock(paths.global_lock_path(config, environment)))
    global_plans: dict[str, ChangePlan] = {}
    for name, collection in selected.items():
        target = paths.global_collection_dir(collection, environment)
        if target is None:
            continue
        source_dir = paths.central_collection_dir(global_base, collection)
        _warn_legacy_layout(global_base, collection, source_dir)
        spec = _spec(collection)
        global_plans[name] = plan_changes(
            _central_snapshot(config, source_dir, spec),
            snapshot_directory(target, spec),
            global_lock.get(name, {}),
            prune=config.sync.prune,
        )
    project: str | None = None
    workspace_plans: dict[str, ChangePlan] | None = None
    report: LintReport | None = None
    if workspace_root is not None:
        project = resolve_project(
            config, workspace_root=workspace_root, env=environment, remote_url=remote_url
        )
        if project is not None:
            workspace_plans = {}
            reports: list[LintReport] = []
            project_base = _project_dir(config, cache_dir, project)
            for name, collection in selected.items():
                target = paths.workspace_collection_dir(collection, workspace_root)
                if target is None:
                    continue
                spec = _spec(collection)
                local = snapshot_directory(target, spec)
                source_dir = paths.central_collection_dir(project_base, collection)
                _warn_legacy_layout(project_base, collection, source_dir)
                central = _central_snapshot(config, source_dir, spec)
                workspace_plans[name] = plan_changes(
                    local, central, snapshot_hashes(central), prune=True
                )
                if run_lint:
                    reports.append(
                        prefix_report(
                            _lint_collection(collection, config, local, workspace_root),
                            collection.workspace_path or "",
                        )
                    )
            if run_lint:
                report = merge_reports(reports)
    proposals: tuple[str, ...] = ()
    if project is not None and config.branch.warn_existing:
        try:
            proposals = tuple(
                gitops.list_remote_branches(
                    config,
                    url,
                    config.branch.project_glob(project),
                    env=gitops.git_environment(environment),
                )
            )
        except GitError as exc:
            log.warning("could not list proposals for %s: %s", project, exc)
    return StatusResult(project, url, commit, workspace_plans, global_plans, report, proposals)


def raise_on_conflicts(result: PullResult) -> None:
    if result.conflicts:
        raise SyncConflictError("locally modified files kept: " + ", ".join(result.conflicts))
