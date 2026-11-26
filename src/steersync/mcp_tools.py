"""The operations behind the MCP tools, as plain functions returning typed reports.

Nothing here imports the ``mcp`` package, so the logic is testable without the
optional extra. Every function returns a frozen pydantic model, which the MCP
server publishes as the tool's output schema and structured result, and raises
:class:`~steersync.errors.SteersyncError` (or ``OSError``) on failure, which the
server turns into an ``isError`` tool result the agent can read.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from steersync import gitlab, gitops, kiro_version, registry, sync
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.lint import lint_paths, lint_workspace, merge_reports
from steersync.paths import global_collection_dir, kiro_home, workspace_collection_dir
from steersync.steering import ChangePlan, FileSpec

Progress = sync.Progress
MISSING_MCP_EXTRA = (
    "the MCP server needs the optional mcp dependency; run it with "
    "'uvx --from \"steersync[mcp]\" steersync mcp' or install 'steersync[mcp]'"
)


class _Report(BaseModel):
    model_config = ConfigDict(frozen=True)


class PlanSummary(_Report):
    """How a collection differs between the central repository and this machine."""

    add: list[str] = Field(default_factory=list, description="Files that will be or were created")
    update: list[str] = Field(default_factory=list, description="Files updated from upstream")
    delete: list[str] = Field(default_factory=list, description="Files removed upstream")
    conflict: list[str] = Field(
        default_factory=list, description="Files edited locally that are kept unless forced"
    )
    unchanged: int = Field(default=0, description="Number of files already identical")
    local_only: list[str] = Field(
        default_factory=list, description="Files that exist only on this machine"
    )


def _plan(plan: ChangePlan | None) -> PlanSummary:
    if plan is None:
        return PlanSummary()
    return PlanSummary(
        add=list(plan.add),
        update=list(plan.update),
        delete=list(plan.delete),
        conflict=list(plan.conflict),
        unchanged=len(plan.unchanged),
        local_only=list(plan.local_only),
    )


class CollectionPull(_Report):
    target: str = Field(description="Directory the collection was written to")
    plan: PlanSummary
    written: list[str] = Field(description="Files actually written (empty on a dry run)")


class PullReport(_Report):
    commit: str = Field(description="Central repository commit that was applied")
    dry_run: bool
    offline: bool = Field(description="True when the cached copy was used (remote unreachable)")
    scopes: dict[str, dict[str, CollectionPull]] = Field(
        description="Per scope (workspace, global), per collection results"
    )


class PushReport(_Report):
    status: Literal[
        "pushed", "up-to-date", "already-proposed", "nothing-to-push", "dry-run", "skipped"
    ]
    project: str
    collections: list[str]
    branch: str | None
    commit: str | None
    message: str
    merge_request: str | None = Field(description="Merge request URL when GitLab confirmed it")
    merge_request_note: str
    existing_branches: list[str] = Field(
        description="Other unmerged proposals for this project the user should review first"
    )
    plan: PlanSummary


class MergeRequestReport(_Report):
    project: str
    branch: str
    url: str
    created: bool = Field(description="False when the merge request already existed")
    others: list[str] = Field(description="Other open merge requests for this project")


class DiffReport(_Report):
    project: str | None
    remote_commit: str | None
    workspace: dict[str, PlanSummary] = Field(description="Workspace plans per collection")
    global_: dict[str, PlanSummary] = Field(
        alias="global", serialization_alias="global", description="Global plans per collection"
    )
    proposals: list[str] = Field(description="Open proposal branches for this project")

    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)


class Finding(_Report):
    path: str
    rule: str
    severity: Literal["error", "warning"]
    message: str
    line: int | None = None


class ValidateReport(_Report):
    ok: bool = Field(description="False when an error would block a push")
    checked: int
    errors: int
    warnings: int
    findings: list[Finding]


class CollectionInfo(_Report):
    name: str
    enabled: bool
    description: str
    workspace_path: str | None
    global_path: str | None
    central_dir: str | None
    include: list[str]
    lint: str


class CollectionsReport(_Report):
    workspace: str | None
    collections: list[CollectionInfo]


class KiroReport(_Report):
    cli_path: str | None = Field(description="kiro-cli on PATH, or null for Kiro IDE only")
    cli_version: str | None
    engine: str | None = Field(description="chat.agentEngine, or null when unset (V3 default)")
    engine_source: Literal["kiro-cli", "settings-file", "default", "unknown"]
    min_cli_version: str = Field(description="Oldest supported kiro-cli (kiro.min_cli_version)")
    min_ide_version: str = Field(description="Oldest supported Kiro IDE (kiro.min_ide_version)")
    workflows_enabled: bool | None = Field(
        description="chat.enableWorkflows; synced workflow recipes load only when true"
    )
    legacy_agents: list[str] = Field(description="Agent files with keys V3 does not read")
    problems: list[str]
    supported: bool = Field(description="True when the machine meets the V3 baseline")
    message: str | None = Field(description="What to fix, to show the user when not supported")


class DoctorReport(_Report):
    git: str
    config: str = Field(description="Configuration file in use, or 'packaged defaults'")
    remote: str = Field(description="Central repository URL with credentials redacted")
    gitlab_token: bool = Field(description="True when the GitLab token variable is set")
    workspace: str | None
    origin: str | None
    project: str | None
    directories: dict[str, dict[str, str]] = Field(
        description="Per collection workspace and global directories"
    )
    kiro: KiroReport


def load(cwd: str | None) -> tuple[Config, Path | None]:
    """Configuration and workspace root for ``cwd`` (default: the server's directory)."""
    start = Path(cwd).expanduser() if cwd else Path.cwd()
    bootstrap = load_config(workspace_root=None)
    root = gitops.repo_toplevel(bootstrap, start, env=gitops.git_environment(os.environ))
    config = load_config(workspace_root=root)
    registry.register(os.environ, root, "command")
    return config, root


def _require_repo(root: Path | None) -> Path:
    if root is None:
        raise SteersyncError("not inside a git repository; pass cwd for the project")
    return root


def pull(
    cwd: str | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    collections: list[str] | None = None,
    progress: Progress | None = None,
) -> PullReport:
    config, root = load(cwd)
    result = sync.pull(
        config,
        workspace_root=root,
        force=force,
        dry_run=dry_run,
        collections=collections,
        progress=progress,
    )
    scopes: dict[str, dict[str, CollectionPull]] = {}
    for item in result.results:
        scopes.setdefault(item.scope, {})[item.collection] = CollectionPull(
            target=str(item.target_dir), plan=_plan(item.plan), written=list(item.written)
        )
    return PullReport(
        commit=result.commit, dry_run=result.dry_run, offline=result.offline, scopes=scopes
    )


def push(
    cwd: str | None = None,
    *,
    dry_run: bool = False,
    message: str | None = None,
    collections: list[str] | None = None,
    progress: Progress | None = None,
) -> PushReport:
    config, root = load(cwd)
    result = sync.push(
        config,
        workspace_root=_require_repo(root),
        dry_run=dry_run,
        message=message,
        collections=collections,
        progress=progress,
    )
    return PushReport(
        status=result.status,
        project=result.project,
        collections=list(result.collections),
        branch=result.branch,
        commit=result.commit,
        message=result.message,
        merge_request=result.merge_request,
        merge_request_note=result.merge_request_note,
        existing_branches=list(result.existing_branches),
        plan=_plan(result.plan),
    )


def merge_request(
    cwd: str | None = None,
    *,
    branch: str | None = None,
    title: str | None = None,
    collections: list[str] | None = None,
) -> MergeRequestReport:
    config, root = load(cwd)
    result = sync.merge_request(
        config,
        workspace_root=_require_repo(root),
        branch=branch,
        title=title,
        collections=collections,
    )
    return MergeRequestReport(
        project=result.project,
        branch=result.branch,
        url=result.url,
        created=result.created,
        others=list(result.others),
    )


def _plans(plans: Mapping[str, ChangePlan] | None) -> dict[str, PlanSummary]:
    return {name: _plan(plan) for name, plan in (plans or {}).items()}


def diff(cwd: str | None = None, *, collections: list[str] | None = None) -> DiffReport:
    config, root = load(cwd)
    result = sync.status(config, workspace_root=root, collections=collections)
    return DiffReport(
        project=result.project,
        remote_commit=result.remote_commit,
        workspace=_plans(result.workspace),
        global_=_plans(result.global_),
        proposals=list(result.proposals),
    )


def validate(cwd: str | None = None, *, collections: list[str] | None = None) -> ValidateReport:
    config, root = load(cwd)
    workspace = _require_repo(root)
    reports = []
    for collection in config.select_collections(collections).values():
        target = workspace_collection_dir(collection, workspace)
        if target is None:
            continue
        reports.append(
            lint_paths(
                [target],
                config.lint,
                workspace,
                profile=collection.lint,
                spec=FileSpec(collection.include, collection.exclude, collection.max_depth),
            )
        )
    if not collections:
        reports.append(
            lint_workspace(
                workspace,
                config.lint,
                hooks_dir=_workspace_path(config, "hooks", ".kiro/hooks"),
                settings_dir=_workspace_path(config, "settings", ".kiro/settings"),
                kiro_home=kiro_home(os.environ),
            )
        )
    report = merge_reports(reports)
    return ValidateReport(
        ok=report.ok,
        checked=report.checked,
        errors=len(report.errors),
        warnings=len(report.warnings),
        findings=[Finding(**f._asdict()) for f in report.findings],
    )


def list_collections(cwd: str | None = None) -> CollectionsReport:
    config, root = load(cwd)
    return CollectionsReport(
        workspace=str(root) if root else None,
        collections=[
            CollectionInfo(
                name=name,
                enabled=c.enabled,
                description=c.description,
                workspace_path=c.workspace_path,
                global_path=c.global_path,
                central_dir=c.central_dir,
                include=list(c.include),
                lint=c.lint,
            )
            for name, c in config.collections.items()
        ],
    )


def _agent_dirs(config: Config | None, root: Path | None) -> list[Path]:
    agents = config.collections.get("agents") if config is not None else None
    if root is None or agents is None or agents.workspace_path is None:
        return []
    return [root / agents.workspace_path]


def _kiro(config: Config | None, root: Path | None) -> KiroReport:
    settings = config or Config()
    status = kiro_version.detect(
        os.environ,
        min_cli_version=settings.kiro.min_cli_version,
        min_ide_version=settings.kiro.min_ide_version,
        legacy_keys=settings.lint.legacy_agent_keys,
        upgraded_keys=settings.lint.upgraded_agent_keys,
        agent_dirs=_agent_dirs(config, root),
    )
    return KiroReport.model_validate(status.to_dict())


def kiro_check(cwd: str | None = None) -> KiroReport:
    try:
        config, root = load(cwd)
    except SteersyncError, OSError:
        return _kiro(None, None)
    return _kiro(config, root)


def doctor(cwd: str | None = None) -> DoctorReport:
    config, root = load(cwd)
    env = os.environ
    origin = gitops.remote_url(config, root) if root is not None else None
    directories: dict[str, dict[str, str]] = {}
    for name, collection in config.enabled_collections().items():
        entry: dict[str, str] = {}
        if root is not None and (ws := workspace_collection_dir(collection, root)) is not None:
            entry["workspace"] = str(ws)
        if (glob := global_collection_dir(collection, env)) is not None:
            entry["global"] = str(glob)
        directories[name] = entry
    return DoctorReport(
        git=gitops.git_version(config),
        config=str(config.source_file or "packaged defaults"),
        remote=gitops.redact_url(config.remote.url) or "(unset)",
        gitlab_token=gitlab.token_available(config, env),
        workspace=str(root) if root else None,
        origin=gitops.redact_url(origin) if origin else None,
        project=gitops.project_slug(origin) if origin else None,
        directories=directories,
        kiro=_kiro(config, root),
    )


def _workspace_path(config: Config, name: str, default: str) -> str:
    collection = config.collections.get(name)
    if collection is None or collection.workspace_path is None:
        return default
    return collection.workspace_path


class FleetStatusReport(_Report):
    """``steersync fleet status --json``: every registered repository on this machine."""

    remotes: list[dict[str, Any]]
    repositories: list[dict[str, Any]]


def fleet_status(refresh: bool | None = None) -> FleetStatusReport:
    from steersync import fleet, registry  # noqa: PLC0415 - only for this tool
    from steersync.paths import registry_path  # noqa: PLC0415

    entries = registry.load(registry_path(os.environ)).paths()
    document = fleet.collect(entries, os.environ, refresh=refresh).document()
    return FleetStatusReport.model_validate(document)
