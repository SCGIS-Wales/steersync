"""Optional MCP stdio server exposing steersync operations as tools.

Requires the ``mcp`` extra: ``pip install "steersync[mcp]"``. All tools call the
same functions the CLI uses, so behaviour and safety checks are identical. Every
tool takes an optional ``collections`` list (steering, specs, skills, agents,
hooks, settings or any configured name); the default is every enabled collection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from steersync import gitops, sync
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.lint import lint_paths, merge_reports
from steersync.paths import workspace_collection_dir
from steersync.steering import FileSpec


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


def pull_steering(
    cwd: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    collections: list[str] | None = None,
) -> dict[str, Any]:
    """Pull global and workspace collections (steering, specs, skills, ...) from central."""
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
    return {"ok": True, "commit": result.commit, "dry_run": result.dry_run, "scopes": scopes}


def push_steering(
    cwd: str | None = None,
    dry_run: bool = False,
    message: str | None = None,
    collections: list[str] | None = None,
) -> dict[str, Any]:
    """Propose the workspace collections as one branch on the central repository."""
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
        "plan": _plan_dict(result.plan),
    }


def diff_steering(cwd: str | None = None, collections: list[str] | None = None) -> dict[str, Any]:
    """Compare local collections with the central repository without changing anything."""
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
    }


def lint_steering(cwd: str | None = None, collections: list[str] | None = None) -> dict[str, Any]:
    """Lint the workspace collections with each collection's profile."""
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
        report = merge_reports(reports)
    except (SteersyncError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": report.ok,
        "checked": report.checked,
        "findings": [f._asdict() for f in report.findings],
    }


def build_server() -> Any:  # noqa: ANN401
    """Create the MCP server. Imported lazily so the core has no MCP dependency."""
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        msg = "the MCP server needs the optional dependency: pip install 'steersync[mcp]'"
        raise SteersyncError(msg) from exc
    server = MCPServer(
        "steersync",
        instructions=(
            "Sync Kiro steering, specs, skills, agents, hooks and settings with the central "
            "repository. Pass collections=[...] to limit a tool to some collections."
        ),
    )
    server.tool(name="pull_steering")(pull_steering)
    server.tool(name="push_steering")(push_steering)
    server.tool(name="diff_steering")(diff_steering)
    server.tool(name="lint_steering")(lint_steering)
    return server


def serve() -> None:
    """Run the stdio MCP server until the client disconnects."""
    build_server().run(transport="stdio")
