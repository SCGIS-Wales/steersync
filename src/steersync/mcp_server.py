"""Optional MCP stdio server exposing steersync operations as tools.

Requires the ``mcp`` extra: ``pip install "steersync[mcp]"``. All tools shell out
to the same functions the CLI uses, so behaviour and safety checks are identical.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from steersync import gitops, sync
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.lint import lint_paths
from steersync.paths import workspace_steering_dir


def _workspace(config: Config, cwd: str | None) -> Path | None:
    start = Path(cwd).expanduser() if cwd else Path.cwd()
    return gitops.repo_toplevel(config, start, env=gitops.git_environment(os.environ))


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


def pull_steering(
    cwd: str | None = None, force: bool = False, dry_run: bool = False
) -> dict[str, Any]:
    """Pull global and workspace steering from the central repository."""
    config, root = _load(cwd)
    try:
        result = sync.pull(config, workspace_root=root, force=force, dry_run=dry_run)
    except SteersyncError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "commit": result.commit,
        "dry_run": result.dry_run,
        "scopes": {
            s.scope: {
                "target": str(s.target_dir),
                "plan": _plan_dict(s.plan),
                "written": list(s.written),
            }
            for s in result.scopes
        },
    }


def push_steering(
    cwd: str | None = None, dry_run: bool = False, message: str | None = None
) -> dict[str, Any]:
    """Propose the workspace steering as a branch on the central repository."""
    config, root = _load(cwd)
    if root is None:
        return {"ok": False, "error": "not inside a git repository"}
    try:
        result = sync.push(config, workspace_root=root, dry_run=dry_run, message=message)
    except SteersyncError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "status": result.status,
        "project": result.project,
        "branch": result.branch,
        "commit": result.commit,
        "message": result.message,
        "plan": _plan_dict(result.plan),
    }


def diff_steering(cwd: str | None = None) -> dict[str, Any]:
    """Compare local steering with the central repository without changing anything."""
    config, root = _load(cwd)
    try:
        result = sync.status(config, workspace_root=root)
    except SteersyncError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "project": result.project,
        "remote_commit": result.remote_commit,
        "workspace": _plan_dict(result.workspace),
        "global": _plan_dict(result.global_),
    }


def lint_steering(cwd: str | None = None) -> dict[str, Any]:
    """Lint the workspace steering documents."""
    config, root = _load(cwd)
    if root is None:
        return {"ok": False, "error": "not inside a git repository"}
    report = lint_paths([workspace_steering_dir(config, root)], config.lint, root)
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
        "steersync", instructions="Sync Kiro steering documents with the central repository."
    )
    server.tool(name="pull_steering")(pull_steering)
    server.tool(name="push_steering")(push_steering)
    server.tool(name="diff_steering")(diff_steering)
    server.tool(name="lint_steering")(lint_steering)
    return server


def serve() -> None:
    """Run the stdio MCP server until the client disconnects."""
    build_server().run(transport="stdio")
