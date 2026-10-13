"""Optional MCP stdio server exposing steersync operations as tools.

Requires the ``mcp`` extra: ``pip install "steersync[mcp]"``. Every tool calls the
same functions the CLI uses, so behaviour and safety checks are identical. The
server description and each tool description are written for the agent that
reads them: they say which tool answers which request ("pull the Kiro config",
"push my steering", "raise an MR for the kiro config", "validate the agents")
so a session in any repository can act on a plain sentence.

Every tool takes an optional ``collections`` list (steering, specs, skills,
agents, hooks, settings, prompts, agents-md or any configured name); the default
is every enabled collection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from steersync import gitops, sync
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.lint import lint_paths, lint_workspace, merge_reports
from steersync.paths import workspace_collection_dir
from steersync.steering import FileSpec

SERVER_INSTRUCTIONS = """\
steersync keeps the Kiro configuration of this workspace (.kiro/: steering, specs, \
skills, agents, hooks, settings, prompts, AGENTS.md) and of this machine (~/.kiro/) \
in sync with a central git repository where every change is reviewed as a merge request.

Use these tools when the user talks about Kiro configuration, steering, specs, skills, \
agents, hooks, prompts or settings being pulled, pushed, synced, proposed, validated or \
reviewed:
- pull_kiro_config: "pull/sync/refresh/update the kiro config (steering, skills, ...)" \
from the central repository into this workspace and ~/.kiro. Safe to run at any time; \
local edits are kept and reported.
- push_kiro_config: "push/propose/sync up/share my kiro config / steering / skills" to the \
central repository. Lints first, then pushes one proposal branch for this project and \
opens or confirms its merge request.
- raise_merge_request: "raise/open/create an MR (merge request) for the kiro config". Use \
after push_kiro_config, or when the user asks for the merge request link.
- diff_kiro_config: "what changed / status / is my kiro config in sync / any open \
proposals". Read only.
- validate_kiro_config: "validate/lint/check the kiro config, steering, agents, hooks, \
skills" against Kiro's rules and best practices. Errors block a push; warnings are \
advice from the Kiro documentation.
- list_kiro_collections: which kinds of content are synced and where they live.
Reports are JSON; "ok": false carries an "error" the user should see verbatim.
"""


def _load(cwd: str | None) -> tuple[Config, Path | None]:
    start = Path(cwd).expanduser() if cwd else Path.cwd()
    bootstrap = load_config(workspace_root=None)
    root = gitops.repo_toplevel(bootstrap, start, env=gitops.git_environment(os.environ))
    return load_config(workspace_root=root), root


def _plan_dict(plan: Any) -> dict[str, Any]:  # noqa: ANN401
    if plan is None:
        return {}
    return {
        "add": list(plan.add),
        "update": list(plan.update),
        "delete": list(plan.delete),
        "conflict": list(plan.conflict),
        "unchanged": len(plan.unchanged),
        "local_only": list(plan.local_only),
    }


def _plans_dict(plans: dict[str, Any] | None) -> dict[str, Any]:
    if plans is None:
        return {}
    return {name: _plan_dict(plan) for name, plan in plans.items()}


def pull_kiro_config(
    cwd: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    collections: list[str] | None = None,
) -> dict[str, Any]:
    """Pull the Kiro configuration from the central repository into this workspace and ~/.kiro.

    Use for "pull / sync / refresh / update the kiro config (steering, specs, skills,
    agents, hooks, settings, prompts)". Files edited locally are kept and listed under
    "conflict" unless force is true. dry_run shows the plan without writing. If the
    central repository is unreachable the last cached copy is applied and "offline"
    is true.
    """
    try:
        config, root = _load(cwd)
        result = sync.pull(
            config, workspace_root=root, force=force, dry_run=dry_run, collections=collections
        )
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    scopes: dict[str, dict[str, Any]] = {}
    for item in result.results:
        scopes.setdefault(item.scope, {})[item.collection] = {
            "target": str(item.target_dir),
            "plan": _plan_dict(item.plan),
            "written": list(item.written),
        }
    return {
        "ok": True,
        "commit": result.commit,
        "dry_run": result.dry_run,
        "offline": result.offline,
        "scopes": scopes,
    }


def push_kiro_config(
    cwd: str | None = None,
    dry_run: bool = False,
    message: str | None = None,
    collections: list[str] | None = None,
) -> dict[str, Any]:
    """Propose this workspace's Kiro configuration to the central repository.

    Use for "push / propose / share / sync up my kiro config, steering, skills, agents".
    Lints every collection first (errors stop the push), then pushes one branch named
    after the project and the user and opens or confirms its merge request. The result
    carries "branch", "merge_request" (URL when confirmed through the GitLab API) and
    "existing_branches": other unmerged proposals for the same project that the user
    should review before adding more.
    """
    try:
        config, root = _load(cwd)
        if root is None:
            return {"ok": False, "error": "not inside a git repository"}
        result = sync.push(
            config, workspace_root=root, dry_run=dry_run, message=message, collections=collections
        )
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "status": result.status,
        "project": result.project,
        "collections": list(result.collections),
        "branch": result.branch,
        "commit": result.commit,
        "message": result.message,
        "merge_request": result.merge_request,
        "merge_request_note": result.merge_request_note,
        "existing_branches": list(result.existing_branches),
        "plan": _plan_dict(result.plan),
    }


def raise_merge_request(
    cwd: str | None = None,
    branch: str | None = None,
    title: str | None = None,
    collections: list[str] | None = None,
) -> dict[str, Any]:
    """Open, or return, the GitLab merge request for the pushed Kiro configuration branch.

    Use for "raise / open / create an MR for the kiro config" or "give me the merge
    request link". Needs the GitLab token in the configured environment variable
    (GITLAB_TOKEN by default). Without branch the proposal branch is derived from the
    local content; it must already be on the central remote (push_kiro_config first).
    "others" lists other open merge requests for this project.
    """
    try:
        config, root = _load(cwd)
        if root is None:
            return {"ok": False, "error": "not inside a git repository"}
        result = sync.merge_request(
            config, workspace_root=root, branch=branch, title=title, collections=collections
        )
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result._asdict(), "others": list(result.others)}


def diff_kiro_config(
    cwd: str | None = None, collections: list[str] | None = None
) -> dict[str, Any]:
    """Compare the local Kiro configuration with the central repository, read only.

    Use for "status / what changed / is my kiro config in sync / any open proposals".
    "workspace" and "global" hold per collection plans; "proposals" lists this
    project's proposal branches still on the central remote.
    """
    try:
        config, root = _load(cwd)
        result = sync.status(config, workspace_root=root, collections=collections)
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "project": result.project,
        "remote_commit": result.remote_commit,
        "workspace": _plans_dict(result.workspace),
        "global": _plans_dict(result.global_),
        "proposals": list(result.proposals),
    }


def validate_kiro_config(
    cwd: str | None = None, collections: list[str] | None = None
) -> dict[str, Any]:
    """Validate the workspace Kiro configuration against Kiro's rules and best practices.

    Use for "validate / lint / check the kiro config, steering, agents, hooks, skills,
    prompts, settings". Each collection is checked with its own profile (front matter,
    JSON shapes, SKILL.md metadata, secrets) plus best practice warnings taken from the
    Kiro documentation. Errors would block a push; warnings are advice. With no
    collections the auto load checks (SessionStart hook, MCP entry) are included.
    """
    try:
        config, root = _load(cwd)
        if root is None:
            return {"ok": False, "error": "not inside a git repository"}
        reports = []
        for collection in config.select_collections(collections).values():
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
            hooks_dir = _workspace_path(config, "hooks", ".kiro/hooks")
            settings_dir = _workspace_path(config, "settings", ".kiro/settings")
            reports.append(
                lint_workspace(root, config.lint, hooks_dir=hooks_dir, settings_dir=settings_dir)
            )
        report = merge_reports(reports)
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": report.ok,
        "checked": report.checked,
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "findings": [f._asdict() for f in report.findings],
    }


def list_kiro_collections(cwd: str | None = None) -> dict[str, Any]:
    """List the kinds of Kiro content steersync syncs and the directories involved."""
    try:
        config, root = _load(cwd)
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "workspace": str(root) if root else None,
        "collections": [
            {
                "name": name,
                "enabled": c.enabled,
                "description": c.description,
                "workspace_path": c.workspace_path,
                "global_path": c.global_path,
                "central_dir": c.central_dir,
                "include": list(c.include),
                "lint": c.lint,
            }
            for name, c in config.collections.items()
        ],
    }


def _workspace_path(config: Config, name: str, default: str) -> str:
    collection = config.collections.get(name)
    if collection is None or collection.workspace_path is None:
        return default
    return collection.workspace_path


TOOLS: dict[str, Any] = {
    "pull_kiro_config": pull_kiro_config,
    "push_kiro_config": push_kiro_config,
    "raise_merge_request": raise_merge_request,
    "diff_kiro_config": diff_kiro_config,
    "validate_kiro_config": validate_kiro_config,
    "list_kiro_collections": list_kiro_collections,
}


def build_server() -> Any:  # noqa: ANN401
    """Create the MCP server. Imported lazily so the core has no MCP dependency."""
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        msg = "the MCP server needs the optional dependency: pip install 'steersync[mcp]'"
        raise SteersyncError(msg) from exc
    server = MCPServer("steersync", instructions=SERVER_INSTRUCTIONS)
    for name, func in TOOLS.items():
        server.tool(name=name)(func)
    return server


def serve() -> None:
    """Run the stdio MCP server until the client disconnects."""
    build_server().run(transport="stdio")
