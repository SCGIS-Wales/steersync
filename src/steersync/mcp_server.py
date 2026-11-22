"""Optional MCP stdio server exposing steersync operations as tools.

Requires the ``mcp`` extra: ``uvx --from "steersync[mcp]" steersync mcp`` (or the
``steersync-mcp`` alias). Built on the official SDK v2 ``MCPServer`` (MCP specification
2026-07-28): tools return structured JSON, carry titles and read only, destructive and
idempotent hints, and log to stderr because protocol logging is deprecated. Every tool calls the
same functions the CLI uses, so behaviour and safety checks are identical. The
server description and each tool description are written for the agent that
reads them: they say which tool answers which request ("pull the Kiro config",
"push my steering", "raise an MR for the kiro config", "validate the agents")
so a session in any repository can act on a plain sentence.

Every tool takes an optional ``collections`` list (steering, specs, skills,
agents, hooks, settings, sandbox, workflows, prompts, agents-md, kiroignore or any
configured name); the default is every enabled collection.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from steersync import __version__, gitops, kiro_version, sync
from steersync.config import Config, load_config
from steersync.errors import SteersyncError
from steersync.lint import lint_paths, lint_workspace, merge_reports
from steersync.logging_utils import configure_logging
from steersync.paths import workspace_collection_dir
from steersync.steering import FileSpec

SERVER_INSTRUCTIONS = """\
steersync keeps the Kiro configuration of this workspace (.kiro/: steering, specs, \
skills, agents, hooks, settings, sandbox policy, workflow recipes, prompts, AGENTS.md \
files and .kiroignore) and of this machine (~/.kiro/) in sync with a central git \
repository where every change is reviewed as a merge request. Per user files such as \
workspace permissions, memories, sessions and powers are never synced.

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
- kiro_v3_status: "am I on Kiro CLI V3 / do my agents need upgrading". Read only; never \
starts a chat, so it costs no Kiro credits.
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
    confirm: bool = False,
) -> dict[str, Any]:
    """Pull the Kiro configuration from the central repository into this workspace and ~/.kiro.

    Use for "pull / sync / refresh / update the kiro config (steering, specs, skills,
    agents, hooks, settings, prompts)". Files edited locally are kept and listed under
    "conflict" unless force is true; force overwrites local edits, so it also needs
    confirm true after the user agreed. dry_run shows the plan without writing. If the
    central repository is unreachable the last cached copy is applied and "offline"
    is true.
    """
    if force and not dry_run and not confirm:
        return {
            "ok": False,
            "error": "force overwrites locally modified files; ask the user, then call again "
            "with confirm=true (or use dry_run=true to preview)",
        }
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


def kiro_v3_status(cwd: str | None = None) -> dict[str, Any]:
    """Report whether kiro-cli runs the V3 agent harness and which agents are not V3 ready.

    Use for "am I on Kiro V3", "do my agents need upgrading", "why does my agent not
    load". Runs only `kiro-cli --version` and `kiro-cli settings`; never starts a chat.
    "message" holds the reminder to show the user when "needs_reminder" is true.
    """
    try:
        config, root = _load(cwd)
    except SteersyncError, OSError:
        config, root = None, None
    agent_dirs: list[Path] = []
    if config is not None and root is not None:
        agents = config.collections.get("agents")
        if agents is not None and agents.workspace_path is not None:
            agent_dirs.append(root / agents.workspace_path)
    status = kiro_version.detect_harness(os.environ, agent_dirs=agent_dirs)
    return {"ok": True, **status.to_dict()}


def _workspace_path(config: Config, name: str, default: str) -> str:
    collection = config.collections.get(name)
    if collection is None or collection.workspace_path is None:
        return default
    return collection.workspace_path


class ToolSpec(NamedTuple):
    func: Callable[..., dict[str, Any]]
    title: str
    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True


TOOL_SPECS: dict[str, ToolSpec] = {
    "pull_kiro_config": ToolSpec(
        pull_kiro_config, "Pull Kiro configuration", read_only=False, idempotent=True
    ),
    "push_kiro_config": ToolSpec(push_kiro_config, "Propose Kiro configuration", read_only=False),
    "raise_merge_request": ToolSpec(
        raise_merge_request, "Raise merge request", read_only=False, idempotent=True
    ),
    "diff_kiro_config": ToolSpec(diff_kiro_config, "Compare Kiro configuration", read_only=True),
    "validate_kiro_config": ToolSpec(
        validate_kiro_config, "Validate Kiro configuration", read_only=True, open_world=False
    ),
    "list_kiro_collections": ToolSpec(
        list_kiro_collections, "List synced collections", read_only=True, open_world=False
    ),
    "kiro_v3_status": ToolSpec(
        kiro_v3_status, "Kiro CLI V3 status", read_only=True, open_world=False
    ),
}
TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    name: spec.func for name, spec in TOOL_SPECS.items()
}
READ_ONLY_TOOLS: tuple[str, ...] = tuple(n for n, s in TOOL_SPECS.items() if s.read_only)
MISSING_EXTRA = (
    "the MCP server needs the optional mcp dependency; run it with "
    "'uvx --from \"steersync[mcp]\" steersync mcp' or install 'steersync[mcp]'"
)


def build_server() -> Any:  # noqa: ANN401
    """Create the MCP server. Imported lazily so the core has no MCP dependency."""
    try:
        from mcp.server import MCPServer  # noqa: PLC0415
        from mcp.types import ToolAnnotations  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SteersyncError(MISSING_EXTRA) from exc
    server = MCPServer(
        "steersync", title="steersync", version=__version__, instructions=SERVER_INSTRUCTIONS
    )
    for name, spec in TOOL_SPECS.items():
        annotations = ToolAnnotations(
            title=spec.title,
            read_only_hint=spec.read_only,
            destructive_hint=spec.destructive if not spec.read_only else False,
            idempotent_hint=spec.idempotent or spec.read_only,
            open_world_hint=spec.open_world,
        )
        server.tool(name=name, title=spec.title, annotations=annotations)(spec.func)
    return server


def serve() -> None:
    """Run the stdio MCP server until the client disconnects. Logs go to stderr."""
    configure_logging(os.environ.get("STEERSYNC_LOG_LEVEL", "WARNING").upper(), "text")
    build_server().run(transport="stdio")


def main() -> None:
    """Console entry point ``steersync-mcp``."""
    try:
        serve()
    except SteersyncError as exc:
        print(f"steersync: error: {exc}", file=sys.stderr)
        sys.exit(exc.exit_code)
