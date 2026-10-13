"""Generation and installation of git hooks, Kiro hooks and MCP configuration."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from steersync import gitops
from steersync.config import Config

HOOK_MARKER = "# managed by steersync"
CHAINED_SUFFIX = ".local"

PRE_PUSH_TEMPLATE = """#!/bin/sh
{marker} (do not edit; reinstall with `steersync install-hook --force`)
# Chains to {chained} when present, then proposes the .kiro collections to the central repository.
set -u
hook_dir=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp "${{TMPDIR:-/tmp}}/steersync-prepush.XXXXXX") || exit 1
trap 'rm -f "$tmp"' EXIT
cat >"$tmp"
if [ -x "$hook_dir/{chained}" ]; then
  "$hook_dir/{chained}" "$@" <"$tmp" || exit $?
fi
if [ "${{STEERSYNC_ACTIVE:-}}" = "1" ]; then
  exit 0
fi
if command -v steersync >/dev/null 2>&1; then
  steersync push --from-hook --remote-url "${{2:-}}" <"$tmp" || exit $?
else
  echo "steersync: not on PATH; skipping steering sync" >&2
fi
exit 0
"""


class HookInstallResult(NamedTuple):
    path: Path
    chained: Path | None
    action: Literal["installed", "updated", "unchanged"]


def render_pre_push_hook() -> str:
    return PRE_PUSH_TEMPLATE.format(marker=HOOK_MARKER, chained=f"pre-push{CHAINED_SUFFIX}")


def install_pre_push_hook(
    config: Config, repo: Path, *, force: bool = False, env: Mapping[str, str] | None = None
) -> HookInstallResult:
    """Install the chaining pre-push hook into the repository's hooks directory.

    An existing foreign hook is preserved as ``pre-push.local`` and invoked first.
    The function never changes ``core.hooksPath``.
    """
    hooks = gitops.hooks_dir(config, repo, env=env)
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-push"
    content = render_pre_push_hook()
    chained: Path | None = None
    if target.exists():
        existing = target.read_text("utf-8", errors="replace")
        if HOOK_MARKER in existing:
            if existing == content:
                return HookInstallResult(target, None, "unchanged")
            if not force:
                msg = f"{target} is an older steersync hook; rerun with --force to update it"
                raise FileExistsError(msg)
            action: Literal["installed", "updated", "unchanged"] = "updated"
        else:
            chained = hooks / f"pre-push{CHAINED_SUFFIX}"
            if chained.exists() and not force:
                msg = f"{chained} already exists; rerun with --force to overwrite it"
                raise FileExistsError(msg)
            target.replace(chained)
            action = "installed"
    else:
        action = "installed"
    target.write_text(content, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return HookInstallResult(target, chained, action)


def render_kiro_hook(
    engine: Literal["v2", "v3"],
    *,
    command: str = "steersync pull --quiet --best-effort",
    timeout: int = 30,
) -> str:
    """Render the Kiro hook definition that pulls steering when a session starts."""
    if engine == "v2":
        payload: dict[str, object] = {
            "hooks": {"agentSpawn": [{"command": command, "timeout_ms": timeout * 1000}]}
        }
    else:
        payload = {
            "version": "v1",
            "hooks": [
                {
                    "name": "steersync-pull",
                    "trigger": "SessionStart",
                    "action": {"type": "command", "command": command},
                    "timeout": timeout,
                    "enabled": True,
                }
            ],
        }
    return json.dumps(payload, indent=2) + "\n"


MCP_SERVER_NAME = "steersync"
KIRO_HOOK_FILE = "steersync.json"
# Name of the environment variable (not a secret) that carries the GitLab token.
DEFAULT_TOKEN_ENV = "GITLAB_TOKEN"  # noqa: S105


def mcp_server_entry(
    *,
    command: str = "uvx",
    args: tuple[str, ...] = ("steersync", "mcp"),
    remote: str | None = None,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> dict[str, object]:
    """Return the ``mcpServers.steersync`` entry for Kiro's ``mcp.json``.

    Kiro does not pass the shell environment to MCP servers, so the variables git
    and the GitLab API need are forwarded explicitly with ``${VAR}`` placeholders.
    """
    env: dict[str, str] = {
        "STEERSYNC_REMOTE": remote or "${STEERSYNC_REMOTE}",
        "SSH_AUTH_SOCK": "${SSH_AUTH_SOCK}",
        "HTTPS_PROXY": "${HTTPS_PROXY}",
        "GIT_TERMINAL_PROMPT": "0",
        token_env: f"${{{token_env}}}",
    }
    return {
        "command": command,
        "args": list(args),
        "env": env,
        "timeout": 60000,
        "requestTimeout": 180000,
        "disabled": False,
    }


def render_mcp_config(
    *,
    command: str = "uvx",
    args: tuple[str, ...] = ("steersync", "mcp"),
    remote: str | None = None,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> str:
    """Render the ``mcp.json`` fragment registering the steersync MCP server."""
    entry = mcp_server_entry(command=command, args=args, remote=remote, token_env=token_env)
    return json.dumps({"mcpServers": {MCP_SERVER_NAME: entry}}, indent=2) + "\n"


def merge_mcp_config(
    path: Path, entry: Mapping[str, object], *, force: bool = False
) -> Literal["written", "updated", "unchanged", "kept"]:
    """Add the steersync server to an ``mcp.json``, keeping every other server.

    An existing steersync entry is left alone unless ``force`` is set, so a team
    that pinned an absolute command path keeps it across ``steersync init`` runs.
    """
    data: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"{path} is not valid JSON; fix it before adding the steersync server: {exc}"
            raise ValueError(msg) from exc
        if isinstance(loaded, dict):
            data = loaded
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    existing = servers.get(MCP_SERVER_NAME)
    if existing == dict(entry):
        return "unchanged"
    if existing is not None and not force:
        return "kept"
    action: Literal["written", "updated"] = "updated" if existing is not None else "written"
    servers[MCP_SERVER_NAME] = dict(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return action


def write_kiro_hook(
    hooks_dir: Path, *, command: str = "steersync pull --quiet --best-effort", timeout: int = 30
) -> Literal["written", "updated", "unchanged"]:
    """Write the V3 SessionStart hook file that refreshes the configuration."""
    target = hooks_dir / KIRO_HOOK_FILE
    rendered = render_kiro_hook("v3", command=command, timeout=timeout)
    if target.is_file() and target.read_text("utf-8") == rendered:
        return "unchanged"
    action: Literal["written", "updated"] = "updated" if target.exists() else "written"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return action


def render_pre_commit_config(rev: str = "v0.2.0") -> str:
    """Render the ``.pre-commit-config.yaml`` snippet consumers add to their projects."""
    return (
        "repos:\n"
        "  - repo: https://github.com/SCGIS-Wales/steersync\n"
        f"    rev: {rev}\n"
        "    hooks:\n"
        "      - id: steersync-push\n"
    )
