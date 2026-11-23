"""Generation and installation of git hooks, Kiro hooks and MCP configuration."""

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from steersync import __version__, gitops
from steersync.config import Config

HOOK_MARKER = "# managed by steersync"
# steersync needs Python 3.14. uvx resolves a tool with its default interpreter, which may
# be older (the server then exits before Kiro connects), so the constraint is explicit.
MIN_PYTHON = ">=3.14"
# The Kiro SessionStart hook runs a pinned steersync through uvx rather than whatever
# `steersync` comes first on PATH, which may be a stale install with other options.
SESSION_PULL_COMMAND = (
    f'uvx --quiet --python "{MIN_PYTHON}" --from "steersync=={__version__}" '
    "steersync pull --quiet --best-effort"
)
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
    command: str = SESSION_PULL_COMMAND,
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
# uvx needs the mcp extra; pinning the version keeps every machine on the same server.
DEFAULT_MCP_ARGS: tuple[str, ...] = (
    "--python",
    MIN_PYTHON,
    "--from",
    f"steersync[mcp]=={__version__}",
    "steersync",
    "mcp",
)
# Read only tools Kiro may run without asking (mutating tools always ask).
AUTO_APPROVE_TOOLS: tuple[str, ...] = (
    "diff_kiro_config",
    "validate_kiro_config",
    "list_kiro_collections",
    "doctor_kiro_config",
    "fleet_status",
    "kiro_v3_status",
)


def mcp_args_for(command: str) -> tuple[str, ...]:
    """Arguments that start the server with ``command``: uvx, steersync or steersync-mcp."""
    match Path(command).name:
        case "uvx":
            return DEFAULT_MCP_ARGS
        case "steersync":
            return ("mcp",)
        case "steersync-mcp":
            return ()
        case _:
            return ("steersync", "mcp")


def mcp_server_entry(
    *,
    command: str = "uvx",
    args: tuple[str, ...] | None = None,
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
        "args": list(mcp_args_for(command) if args is None else args),
        "env": env,
        "timeout": 60000,
        "requestTimeout": 180000,
        "disabled": False,
        "autoApprove": list(AUTO_APPROVE_TOOLS),
        "disabledTools": [],
    }


def render_mcp_config(
    *,
    command: str = "uvx",
    args: tuple[str, ...] | None = None,
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
    hooks_dir: Path, *, command: str = SESSION_PULL_COMMAND, timeout: int = 30
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


def render_pre_commit_config(rev: str = "v0.9.0") -> str:
    """Render the ``.pre-commit-config.yaml`` snippet consumers add to their projects."""
    return (
        "repos:\n"
        "  - repo: https://github.com/SCGIS-Wales/steersync\n"
        f"    rev: {rev}\n"
        "    hooks:\n"
        "      - id: steersync-push\n"
    )


# --- careful registration in a user's existing mcp.json ---------------------------------


class McpRegistration(NamedTuple):
    action: Literal["added", "present", "would-add"]
    path: Path
    # Name of the server entry that runs steersync (an existing one when "present").
    name: str
    backup: Path | None = None


def _runs_steersync(spec: object) -> bool:
    """True when an mcp.json server entry starts the steersync MCP server."""
    if not isinstance(spec, Mapping):
        return False
    command = spec.get("command")
    args = spec.get("args")
    words = [command] if isinstance(command, str) else []
    if isinstance(args, list):
        words.extend(a for a in args if isinstance(a, str))
    names = {Path(w).name for w in words}
    if names & {"steersync-mcp"}:
        return True
    mentions = any(
        w == "steersync"
        or w.startswith(("steersync[", "steersync=", "steersync@"))
        or Path(w).name == "steersync"
        for w in words
    )
    return mentions and "mcp" in words


def find_steersync_server(path: Path) -> str | None:
    """Name of the server in ``path`` that runs steersync, if any (by name or by command)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError, ValueError:
        return None
    servers = data.get("mcpServers") if isinstance(data, Mapping) else None
    if not isinstance(servers, Mapping):
        return None
    if MCP_SERVER_NAME in servers:
        return MCP_SERVER_NAME
    return next((name for name, spec in servers.items() if _runs_steersync(spec)), None)


def register_mcp_server(
    path: Path, entry: Mapping[str, object], *, dry_run: bool = False
) -> McpRegistration:
    """Add the steersync server to ``path`` only when no server there runs steersync yet.

    Nothing else in the file changes: every other server keeps its exact content and its
    order. The original is copied to ``<name>.bak-steersync`` first, the new file is written
    to a temporary file and moved into place, and the result is read back and compared with
    the original before the backup is kept. An unreadable or unexpected file is never
    touched; the error says what to fix.
    """
    original_text: str | None = None
    data: dict[str, object] = {}
    if path.exists():
        try:
            original_text = path.read_text("utf-8")
            loaded = json.loads(original_text) if original_text.strip() else {}
        except (OSError, ValueError) as exc:
            msg = f"{path} is not valid JSON ({exc}); fix it, then run setup again"
            raise ValueError(msg) from exc
        if not isinstance(loaded, dict):
            msg = f"{path} is not a JSON object; left unchanged"
            raise ValueError(msg)
        data = loaded
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        msg = f'{path}: "mcpServers" is not an object; left unchanged'
        raise ValueError(msg)  # noqa: TRY004 - callers report ValueError as a file problem
    existing = find_steersync_server(path) if original_text is not None else None
    if existing is not None:
        return McpRegistration("present", path, existing)
    name = MCP_SERVER_NAME
    if dry_run:
        return McpRegistration("would-add", path, name)
    updated = dict(data)
    updated["mcpServers"] = {**servers, name: dict(entry)}
    text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if original_text is not None:
        backup = path.with_name(path.name + ".bak-steersync")
        backup.write_text(original_text, encoding="utf-8")
    tmp = path.with_name(f".{path.name}.steersync-tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        tmp.chmod(path.stat().st_mode & 0o777)
    tmp.replace(path)
    check = json.loads(path.read_text("utf-8"))
    others_intact = {k: v for k, v in check["mcpServers"].items() if k != name} == servers
    rest_intact = {k: v for k, v in check.items() if k != "mcpServers"} == {
        k: v for k, v in data.items() if k != "mcpServers"
    }
    if not (others_intact and rest_intact and check["mcpServers"][name] == dict(entry)):
        if original_text is not None:
            path.write_text(original_text, encoding="utf-8")
        msg = f"{path}: verification after writing failed; the original was restored"
        raise ValueError(msg)
    return McpRegistration("added", path, name, backup)


def setup_mcp_entry(*, token_env: str = DEFAULT_TOKEN_ENV) -> dict[str, object]:
    """The entry ``steersync setup`` adds to ``~/.kiro/settings/mcp.json``.

    The central repository comes from the steersync configuration file, so unlike the
    per project entry it does not forward STEERSYNC_REMOTE.
    """
    entry = mcp_server_entry(token_env=token_env)
    forwarded = entry["env"]
    env = {
        key: value
        for key, value in (forwarded.items() if isinstance(forwarded, Mapping) else ())
        if key != "STEERSYNC_REMOTE"
    }
    return {**entry, "env": env}


def session_hook_present(hooks_dir: Path) -> bool:
    """True when some hook file in ``hooks_dir`` already runs ``steersync pull`` at start."""
    if not hooks_dir.is_dir():
        return False
    for file in sorted(hooks_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text("utf-8"))
        except OSError, ValueError:
            continue
        hooks = data.get("hooks") if isinstance(data, Mapping) else None
        for hook in hooks if isinstance(hooks, list) else []:
            action = hook.get("action") if isinstance(hook, Mapping) else None
            command = action.get("command") if isinstance(action, Mapping) else None
            if (
                hook.get("trigger") == "SessionStart"
                and isinstance(command, str)
                and ("steersync pull" in command)
            ):
                return True
    return False
