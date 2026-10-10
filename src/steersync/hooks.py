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
# Chains to {chained} when present, then proposes .kiro/steering to the central repository.
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
    engine: Literal["v2", "v3"], *, command: str = "steersync pull --quiet", timeout: int = 30
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


def render_mcp_config(
    *, command: str = "uvx", args: tuple[str, ...] = ("steersync", "mcp"), remote: str | None = None
) -> str:
    """Render the ``mcp.json`` fragment registering the steersync MCP server."""
    env: dict[str, str] = {
        "STEERSYNC_REMOTE": remote or "${STEERSYNC_REMOTE}",
        "SSH_AUTH_SOCK": "${SSH_AUTH_SOCK}",
        "HTTPS_PROXY": "${HTTPS_PROXY}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    payload = {
        "mcpServers": {
            "steersync": {
                "command": command,
                "args": list(args),
                "env": env,
                "timeout": 60000,
                "requestTimeout": 180000,
                "disabled": False,
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def render_pre_commit_config(rev: str = "v0.1.0") -> str:
    """Render the ``.pre-commit-config.yaml`` snippet consumers add to their projects."""
    return (
        "repos:\n"
        "  - repo: https://github.com/SCGIS-Wales/steersync\n"
        f"    rev: {rev}\n"
        "    hooks:\n"
        "      - id: steersync-push\n"
    )
