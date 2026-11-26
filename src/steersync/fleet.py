"""Status, push and pull across every registered repository (no printing here).

``collect`` loads each repository's configuration, groups the repositories by central
remote, refreshes each remote once (in parallel across remotes, under that remote's
cache lock) and compares every repository against the shared snapshot in a thread pool.
Each repository is computed inside its own error boundary, so one failure never stops
the others. Progress is reported through ``on_event`` for the interface to render.
"""

import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from steersync import gitops, sync
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.registry import is_work_tree
from steersync.retry import RetryEvent
from steersync.steering import file_digest

log = logging.getLogger(__name__)

State = Literal[
    "error",
    "diverged",
    "local-ahead",
    "central-ahead",
    "proposed",
    "in-sync",
    "unconfigured",
    "missing",
]
STATE_ORDER: tuple[State, ...] = (
    "error",
    "diverged",
    "local-ahead",
    "central-ahead",
    "proposed",
    "in-sync",
    "unconfigured",
    "missing",
)
GLOBAL_PATH = "~/.kiro"


class Counts(NamedTuple):
    add: int = 0
    update: int = 0
    delete: int = 0

    def any(self) -> bool:
        return bool(self.add or self.update or self.delete)

    def render(self) -> str:
        return f"+{self.add} ~{self.update} -{self.delete}" if self.any() else "none"


class FileChange(NamedTuple):
    collection: str
    path: str
    direction: Literal["push", "pull", "conflict"]
    kind: Literal["add", "update", "delete", "both"]


class Diff(NamedTuple):
    push: Counts
    pull: Counts
    conflicts: tuple[str, ...]
    files: tuple[FileChange, ...]


def three_way(views: Sequence[sync.CollectionView]) -> Diff:
    """What changed locally, what changed centrally, and what changed on both sides.

    The pull side is exactly what ``steersync pull`` would do (its plan). The push side
    and conflicts compare local and central with the hashes of the last sync: a file
    edited locally while the central copy stayed the same is local work to push, not a
    conflict; one changed on both sides (or present on both with no record) conflicts.
    """
    push = [0, 0, 0]
    files: list[FileChange] = []
    conflicts: list[str] = []
    pull = Counts()
    for view in views:
        plan = view.pull
        pull = Counts(
            pull.add + len(plan.add), pull.update + len(plan.update), pull.delete + len(plan.delete)
        )
        files.extend(FileChange(view.name, p, "pull", "add") for p in plan.add)
        files.extend(FileChange(view.name, p, "pull", "update") for p in plan.update)
        files.extend(FileChange(view.name, p, "pull", "delete") for p in plan.delete)
        for path in sorted(set(view.local) | set(view.central)):
            local = file_digest(view.local[path]) if path in view.local else None
            central = file_digest(view.central[path]) if path in view.central else None
            base = view.last_sync.get(path)
            if local == central:
                continue
            local_changed = local != base
            central_changed = central != base
            if base is None and local is not None and central is not None:
                local_changed = central_changed = True
            if local_changed and central_changed:
                conflicts.append(f"{view.name}/{path}")
                files.append(FileChange(view.name, path, "conflict", "both"))
            elif local_changed:
                kind: Literal["add", "update", "delete"] = (
                    "delete" if local is None else "add" if central is None else "update"
                )
                push[("add", "update", "delete").index(kind)] += 1
                files.append(FileChange(view.name, path, "push", kind))
    return Diff(Counts(*push), pull, tuple(conflicts), tuple(files))


def classify(push: Counts, pull: Counts, conflicts: int, *, proposed: bool) -> State:
    """The state of one repository from its two directions (see the fleet guide)."""
    if conflicts or (push.any() and pull.any()):
        return "diverged"
    if push.any():
        return "proposed" if proposed else "local-ahead"
    if pull.any():
        return "central-ahead"
    return "in-sync"


class RemoteStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    commit: str | None
    offline: bool
    cache_age_seconds: float | None
    error: str | None = None


class RepoStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    project: str | None
    remote: str | None
    state: State
    push: Counts = Counts()
    pull: Counts = Counts()
    conflicts: tuple[str, ...] = ()
    proposal: str | None = None
    merge_request: str | None = None
    error: str | None = None
    offline: bool = False
    files: tuple[FileChange, ...] = ()

    def is_global(self) -> bool:
        return self.path == GLOBAL_PATH


class FleetReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    remotes: tuple[RemoteStatus, ...]
    repositories: tuple[RepoStatus, ...]

    def needing_attention(self) -> int:
        """Rows with something to do: fix, resolve, push, pull or prune."""
        todo = {"error", "diverged", "local-ahead", "central-ahead", "missing"}
        return sum(r.state in todo for r in self.repositories if not r.is_global())

    def document(self) -> dict[str, object]:
        """The stable ``fleet status --json`` document."""
        return {
            "remotes": [r.model_dump() for r in self.remotes],
            "repositories": [
                {
                    "path": r.path,
                    "project": r.project,
                    "remote": r.remote,
                    "state": r.state,
                    "push": r.push._asdict(),
                    "pull": r.pull._asdict(),
                    "conflicts": list(r.conflicts),
                    "proposal": r.proposal,
                    "merge_request": r.merge_request,
                    "offline": r.offline,
                    "error": r.error,
                }
                for r in self.repositories
            ],
        }


Event = tuple[str, str, str]  # (subject, phase, message)
OnEvent = Callable[[Event], None]


def _quiet(event: Event) -> None:
    del event


class _Prepared(NamedTuple):
    path: Path
    config: Config
    url: str
    project: str


def _prepare(path: Path, env: Mapping[str, str]) -> _Prepared | RepoStatus:
    key = str(path)
    if not is_work_tree(path):
        return RepoStatus(
            path=key,
            project=None,
            remote=None,
            state="missing",
            error="path no longer exists or is not a git work tree",
        )
    try:
        config = load_config(workspace_root=path, env=env)
        if not config.remote.url:
            return RepoStatus(
                path=key,
                project=None,
                remote=None,
                state="unconfigured",
                error="no central repository configured (steersync setup)",
            )
        url = sync.central_url(config)
        project = sync.resolve_project(config, workspace_root=path, env=env, remote_url=None)
    except SteersyncError as exc:
        return RepoStatus(path=key, project=None, remote=None, state="error", error=str(exc))
    if project is None:
        return RepoStatus(
            path=key,
            project=None,
            remote=gitops.redact_url(url),
            state="unconfigured",
            error="no origin remote to derive a project",
        )
    return _Prepared(path, config, url, project)


def _proposal(
    snapshot: sync.CentralSnapshot, config: Config, project: str, digest: str
) -> str | None:
    prefix = config.branch.project_glob(project).rstrip("*")
    short = digest[: config.branch.hash_length]
    return next((b for b in snapshot.branches if b.startswith(prefix) and short in b), None)


def _repo_status(
    prepared: _Prepared, snapshot: sync.CentralSnapshot, env: Mapping[str, str]
) -> RepoStatus:
    remote = gitops.redact_url(prepared.url)
    try:
        view = sync.compare_workspace(
            prepared.config, snapshot, prepared.path, env, project=prepared.project
        )
        diff = three_way(view.collections)
        proposal = _proposal(snapshot, prepared.config, prepared.project, view.digest)
        state = classify(diff.push, diff.pull, len(diff.conflicts), proposed=proposal is not None)
    except SteersyncError as exc:
        return RepoStatus(
            path=str(prepared.path),
            project=prepared.project,
            remote=remote,
            state="error",
            error=str(exc),
            offline=snapshot.offline,
        )
    except Exception as exc:
        log.debug("unexpected error for %s", prepared.path, exc_info=True)
        return RepoStatus(
            path=str(prepared.path),
            project=prepared.project,
            remote=remote,
            state="error",
            error=f"{type(exc).__name__}: {exc}",
            offline=snapshot.offline,
        )
    return RepoStatus(
        path=str(prepared.path),
        project=prepared.project,
        remote=remote,
        state=state,
        push=diff.push,
        pull=diff.pull,
        conflicts=diff.conflicts,
        proposal=proposal if state == "proposed" else None,
        offline=snapshot.offline,
        files=diff.files,
    )


def _global_status(
    config: Config, snapshot: sync.CentralSnapshot, env: Mapping[str, str]
) -> RepoStatus:
    remote = gitops.redact_url(snapshot.url)
    try:
        view = sync.compare_global(config, snapshot, env)
    except SteersyncError as exc:
        return RepoStatus(
            path=GLOBAL_PATH,
            project="global",
            remote=remote,
            state="error",
            error=str(exc),
            offline=snapshot.offline,
        )
    diff = three_way(view.collections)
    state = classify(diff.push, diff.pull, len(diff.conflicts), proposed=False)
    return RepoStatus(
        path=GLOBAL_PATH,
        project="global",
        remote=remote,
        state=state,
        push=diff.push,
        pull=diff.pull,
        conflicts=diff.conflicts,
        offline=snapshot.offline,
        files=diff.files,
    )


def workers(config: Config) -> int:
    return config.fleet.max_workers or min(8, os.cpu_count() or 1)


def collect(
    paths: Iterable[Path],
    env: Mapping[str, str],
    *,
    refresh: bool | None = None,
    include_global: bool | None = None,
    on_event: OnEvent = _quiet,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> FleetReport:
    """Status for every repository in ``paths``, one refresh per remote.

    ``include_global`` adds the ``~/.kiro`` row; None follows ``fleet.include_global``
    in the machine configuration (off by default).

    ``refresh`` None refreshes a remote whose cache is older than
    ``fleet.refresh_max_age_seconds``; True always refreshes; False uses the cache.
    """
    rows: list[RepoStatus] = []
    by_remote: dict[str, list[_Prepared]] = {}
    for path in dict.fromkeys(Path(p) for p in paths):
        prepared = _prepare(path, env)
        if isinstance(prepared, RepoStatus):
            rows.append(prepared)
        else:
            by_remote.setdefault(prepared.url, []).append(prepared)
    machine: Config | None = None
    try:
        machine = load_config(workspace_root=None, env=env)
        if include_global is None:
            include_global = machine.fleet.include_global
        if include_global and machine.remote.url:
            by_remote.setdefault(sync.central_url(machine), [])
    except SteersyncError as exc:
        log.debug("no machine configuration for the global row: %s", exc)
    remotes: list[RemoteStatus] = []

    def one_remote(url: str) -> tuple[RemoteStatus, list[RepoStatus]]:
        group = by_remote[url]
        config = group[0].config if group else machine
        assert config is not None  # noqa: S101 - a remote comes from a repo or the machine
        redacted = gitops.redact_url(url)
        on_event((redacted, "reading", "the central repository"))
        try:
            with sync.central_lock(config, env, url):
                stale = refresh is True or (
                    refresh is None
                    and not sync.cache_is_fresh(
                        config, env, url, config.fleet.refresh_max_age_seconds
                    )
                )
                snapshot = sync.refresh_central(config, env, url, refresh=stale, on_retry=on_retry)
                on_event((redacted, "comparing", f"{len(group)} repositories"))
                with ThreadPoolExecutor(max_workers=workers(config)) as pool:
                    found = list(pool.map(lambda p: _repo_status(p, snapshot, env), group))
                if (
                    include_global
                    and machine is not None
                    and machine.remote.url
                    and (sync.central_url(machine) == url)
                ):
                    found.append(_global_status(machine, snapshot, env))
        except SteersyncError as exc:
            message = str(exc)
            on_event((redacted, "failed", message))
            failed = [
                RepoStatus(
                    path=str(p.path),
                    project=p.project,
                    remote=redacted,
                    state="error",
                    error=message,
                )
                for p in group
            ]
            return RemoteStatus(
                url=redacted, commit=None, offline=False, cache_age_seconds=None, error=message
            ), failed
        on_event((redacted, "done", "offline: cached copy" if snapshot.offline else "ok"))
        age = None if snapshot.age_seconds == float("inf") else round(snapshot.age_seconds, 1)
        return RemoteStatus(
            url=redacted, commit=snapshot.commit, offline=snapshot.offline, cache_age_seconds=age
        ), found

    if by_remote:
        with ThreadPoolExecutor(max_workers=min(len(by_remote), 4)) as pool:
            for remote_status, found in pool.map(one_remote, list(by_remote)):
                remotes.append(remote_status)
                rows.extend(found)
    rows.sort(key=lambda r: (r.is_global(), STATE_ORDER.index(r.state), r.path))
    return FleetReport(remotes=tuple(remotes), repositories=tuple(rows))


# --- push and pull many --------------------------------------------------------------------

PUSHABLE: frozenset[State] = frozenset({"local-ahead", "diverged", "proposed"})


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    project: str | None
    action: Literal["push", "pull"]
    status: Literal["done", "failed", "skipped", "dry-run"]
    detail: str
    branch: str | None = None
    merge_request: str | None = None
    backups: tuple[str, ...] = ()
    rerun: str | None = None


def push_reason(row: RepoStatus) -> str | None:
    """Why ``row`` cannot be pushed (None: it can)."""
    if row.state in {"missing", "unconfigured", "error"}:
        return row.error or row.state
    if row.offline:
        return "central repository unreachable (offline, cached copy); push is disabled"
    if row.state not in PUSHABLE:
        return f"nothing to push ({row.state})"
    return None


def pull_reason(row: RepoStatus) -> str | None:
    """Why ``row`` cannot be pulled (None: it can)."""
    if row.state in {"missing", "unconfigured", "error"}:
        return row.error or row.state
    if row.offline:
        return "central repository unreachable (offline, cached copy); pull is disabled"
    if not row.pull.any() and not row.conflicts:
        return f"nothing to pull ({row.state})"
    return None


def _rerun(row: RepoStatus, command: str) -> str:
    if row.is_global():
        return f"steersync {command}" + (" --scope global" if command == "push" else "")
    return f"cd {row.path} && steersync {command}"


Action = Literal["push", "pull"]
Status = Literal["done", "failed", "skipped", "dry-run"]


def _outcome(
    row: RepoStatus, action: Action, status: Status, detail: str, **extra: object
) -> Outcome:
    fields = {"path": row.path, "project": row.project, "action": action}
    return Outcome.model_validate({**fields, "status": status, "detail": detail, **extra})


def push_one(row: RepoStatus, env: Mapping[str, str], *, dry_run: bool = False) -> Outcome:
    """Lint and push one repository; failures are returned, never raised."""
    reason = push_reason(row)
    if reason is not None:
        return _outcome(row, "push", "skipped", reason)
    try:
        if row.is_global():
            config = load_config(workspace_root=None, env=env)
            result = sync.push(
                config, workspace_root=Path.home(), env=env, scope="global", dry_run=dry_run
            )
        else:
            root = Path(row.path)
            config = load_config(workspace_root=root, env=env)
            result = sync.push(config, workspace_root=root, env=env, dry_run=dry_run)
    except SteersyncError as exc:
        return _outcome(row, "push", "failed", str(exc), rerun=_rerun(row, "push"))
    except Exception as exc:
        log.debug("unexpected push failure for %s", row.path, exc_info=True)
        detail = f"{type(exc).__name__}: {exc}"
        return _outcome(row, "push", "failed", detail, rerun=_rerun(row, "push"))
    return _outcome(
        row,
        "push",
        "dry-run" if dry_run else "done",
        result.message if result.status in result.message else f"{result.status}: {result.message}",
        branch=result.branch,
        merge_request=result.merge_request,
    )


def pull_one(
    row: RepoStatus, env: Mapping[str, str], *, overwrite: bool = False, dry_run: bool = False
) -> Outcome:
    """Pull one repository; ``overwrite`` replaces local edits after a verified backup."""
    reason = pull_reason(row)
    if reason is not None:
        return _outcome(row, "pull", "skipped", reason)
    try:
        if row.is_global():
            config = load_config(workspace_root=None, env=env)
            result = sync.pull(
                config,
                workspace_root=None,
                env=env,
                include_workspace=False,
                force=overwrite,
                dry_run=dry_run,
            )
        else:
            root = Path(row.path)
            config = load_config(workspace_root=root, env=env)
            result = sync.pull(
                config,
                workspace_root=root,
                env=env,
                include_global=False,
                force=overwrite,
                dry_run=dry_run,
            )
    except SteersyncError as exc:
        return _outcome(row, "pull", "failed", str(exc), rerun=_rerun(row, "pull"))
    except Exception as exc:
        log.debug("unexpected pull failure for %s", row.path, exc_info=True)
        detail = f"{type(exc).__name__}: {exc}"
        return _outcome(row, "pull", "failed", detail, rerun=_rerun(row, "pull"))
    kept = len(result.conflicts)
    detail = f"{result.change_count} change(s)" + (
        f", {kept} locally edited file(s) kept" if kept else ""
    )
    status: Status = "dry-run" if dry_run else "done"
    return _outcome(row, "pull", status, detail, backups=result.backups)


def run_batch(
    rows: Sequence[RepoStatus],
    action: Callable[[RepoStatus], Outcome],
    *,
    kind: Action,
    on_event: OnEvent = _quiet,
) -> tuple[list[Outcome], bool]:
    """Run ``action`` for each row in order; returns outcomes and whether Ctrl C stopped it.

    Repositories run one after another, so pushes to one central remote stay serial. An
    interrupt marks the rest as skipped so the summary shows what completed.
    """
    outcomes: list[Outcome] = []
    for index, row in enumerate(rows):
        on_event((row.path, "running", f"{index + 1} of {len(rows)}"))
        try:
            outcome = action(row)
        except KeyboardInterrupt:
            outcomes.extend(_outcome(r, kind, "skipped", "interrupted") for r in rows[index:])
            return outcomes, True
        on_event((row.path, outcome.status, outcome.detail))
        outcomes.append(outcome)
    return outcomes, False
