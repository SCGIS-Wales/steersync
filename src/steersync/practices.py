"""Best practice checks for Kiro configuration, reported as warnings.

Every rule here cites the Kiro documentation it comes from and is objective: it
checks a measurable property (a key, a size, a name, a path) rather than taste.
None of them is an error, because a document that breaks a practice still works;
they exist so a reviewer sees the trade off before it reaches the central
repository. Thresholds live in :class:`PracticeLimits` and can be tuned from
``lint.practices`` in configuration.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from steersync.steering import SteeringDocument

FOUNDATION_FILES: tuple[str, ...] = ("product.md", "tech.md", "structure.md")
_NUMERIC_PREFIX_RE = re.compile(r"^\d+[a-z]?[-_]")
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*\.md$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MACHINE_PATH_RE = re.compile(
    r"(?:^|[\s\"'=:])(/home/[^/\s\"']+|/Users/[^/\s\"']+|[A-Z]:\\\\Users\\\\)"
)
_TRIGGER_WORDS = ("use when", "when ", "for ", "applies to")
_STEERING_HOOK_COMMAND = "steersync pull"


class PracticeLimits(NamedTuple):
    """Thresholds behind the size based practices."""

    always_file_bytes: int = 16_384
    always_total_bytes: int = 49_152
    auto_description_min_chars: int = 20
    skill_name_max_chars: int = 64
    skill_description_max_chars: int = 1024
    skill_body_max_lines: int = 500
    hook_timeout_max_seconds: int = 300
    prompt_name_max_chars: int = 50


DEFAULT_LIMITS = PracticeLimits()


class Finding(NamedTuple):
    path: str
    rule: str
    message: str
    line: int | None = None


def _name(path: str) -> str:
    return PurePosixPath(path).name


# --- steering -----------------------------------------------------------------


def steering_document_findings(
    doc: SteeringDocument, meta: Mapping[str, Any], limits: PracticeLimits = DEFAULT_LIMITS
) -> list[Finding]:
    """Per document steering practices from the Kiro steering documentation."""
    findings: list[Finding] = []
    name = _name(doc.relative_path)
    inclusion = meta.get("inclusion", "always")
    if _NUMERIC_PREFIX_RE.match(name):
        findings.append(
            Finding(
                doc.relative_path,
                "steering-numeric-prefix",
                "numeric file prefixes collide and become the #reference and slash command "
                "name; Kiro's examples use domain first kebab case (api-rest-conventions.md)",
            )
        )
    elif not _KEBAB_RE.match(name):
        findings.append(
            Finding(
                doc.relative_path,
                "steering-file-name",
                "use lowercase kebab case; the file name is the #reference and slash command "
                "for manual and auto steering",
            )
        )
    if "globs" in meta:
        findings.append(
            Finding(
                doc.relative_path,
                "steering-globs-key",
                "'globs' is Cursor syntax; Kiro reads fileMatchPattern, so this file never matches",
                2,
            )
        )
    if inclusion == "auto":
        description = meta.get("description")
        text = description.strip() if isinstance(description, str) else ""
        if text and (
            len(text) < limits.auto_description_min_chars
            or not any(word in text.lower() for word in _TRIGGER_WORDS)
        ):
            findings.append(
                Finding(
                    doc.relative_path,
                    "steering-auto-description",
                    "Kiro matches the description against the request; write it as a trigger "
                    "('Use when ...') with enough words to match",
                    2,
                )
            )
    if inclusion == "always" and len(doc.raw) > limits.always_file_bytes:
        findings.append(
            Finding(
                doc.relative_path,
                "steering-always-size",
                f"an always included file of {len(doc.raw)} bytes costs context in every "
                f"session; keep always on files lean (limit {limits.always_file_bytes}) and "
                "move detail to manual or auto steering or a skill",
            )
        )
    return findings


def steering_collection_findings(snapshot: Mapping[str, bytes]) -> list[Finding]:
    """Whole collection steering practices: the foundation files."""
    if not snapshot:
        return []
    findings: list[Finding] = []
    names = {_name(path) for path in snapshot}
    missing = [f for f in FOUNDATION_FILES if f not in names]
    if missing:
        findings.append(
            Finding(
                ".",
                "steering-foundation-missing",
                "foundation files included in every interaction by default are missing: "
                + ", ".join(missing),
            )
        )
    return findings


# --- agents -------------------------------------------------------------------


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def agent_findings(
    doc: SteeringDocument, data: Mapping[str, Any], directory: Path | None
) -> list[Finding]:
    """Custom agent practices from the Kiro agent configuration reference."""
    findings: list[Finding] = []
    path = doc.relative_path
    tools = _as_list(data.get("tools"))
    if "*" in tools:
        findings.append(
            Finding(
                path,
                "agent-tools-wildcard",
                'tools ["*"] grants every built in and MCP tool; list the tags the agent needs '
                "(read, write, shell, web, subagent, @mcp/...) and gate the rest with permissions",
            )
        )
    allowed = _as_list(data.get("allowedTools"))
    if "*" in allowed:
        findings.append(
            Finding(
                path,
                "agent-allowed-wildcard",
                'allowedTools ["*"] disables every permission prompt, including shell and '
                "writes; allow read only tools and use permissions.rules for the rest",
            )
        )
    for resource in _as_list(data.get("resources")):
        if not isinstance(resource, str):
            continue
        if resource.startswith("file://") and "**" in resource:
            if ".kiro/steering" in resource:
                findings.append(
                    Finding(
                        path,
                        "agent-steering-glob",
                        f"{resource} loads every steering file at startup and bypasses the "
                        "fileMatch, manual and auto inclusion modes; list the always on files "
                        "or rely on default resource inheritance (CLI 3.0)",
                    )
                )
            else:
                findings.append(
                    Finding(
                        path,
                        "agent-source-glob",
                        f"{resource} injects a whole tree into context at every session start; "
                        "let the agent read files on demand or through the code tool",
                    )
                )
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.startswith("file://") and directory is not None:
        target = (directory / PurePosixPath(doc.relative_path).parent / prompt[7:]).resolve()
        if not target.exists():
            findings.append(
                Finding(
                    path,
                    "agent-prompt-file-missing",
                    f"prompt {prompt} does not exist relative to the agent file",
                )
            )
    return findings


# --- hooks --------------------------------------------------------------------


def hook_findings(
    doc: SteeringDocument, data: object, limits: PracticeLimits = DEFAULT_LIMITS
) -> list[Finding]:
    findings: list[Finding] = []
    path = doc.relative_path
    if not isinstance(data, Mapping):
        return findings
    for index, hook in enumerate(_as_list(data.get("hooks"))):
        if not isinstance(hook, Mapping):
            continue
        label = f"hooks[{index}]"
        timeout = hook.get("timeout")
        if isinstance(timeout, int | float) and timeout > limits.hook_timeout_max_seconds:
            findings.append(
                Finding(
                    path,
                    "hook-timeout",
                    f"{label} waits up to {timeout}s; a hook that long blocks the session, "
                    f"keep it under {limits.hook_timeout_max_seconds}s or run the work "
                    "asynchronously",
                )
            )
        trigger = hook.get("trigger")
        if isinstance(trigger, str) and trigger in {"Stop", "SessionStart"} and hook.get("matcher"):
            findings.append(
                Finding(
                    path,
                    "hook-matcher-ignored",
                    f"{label}: matchers are ignored for Stop and SessionStart triggers",
                )
            )
    return findings


# --- skills -------------------------------------------------------------------


def skill_findings(
    doc: SteeringDocument,
    meta: Mapping[str, Any],
    limits: PracticeLimits = DEFAULT_LIMITS,
) -> list[Finding]:
    findings: list[Finding] = []
    path = doc.relative_path
    description = meta.get("description")
    if isinstance(description, str) and len(description) > limits.skill_description_max_chars:
        findings.append(
            Finding(
                path,
                "skill-description-length",
                f"description is {len(description)} characters; the Agent Skills standard "
                f"caps it at {limits.skill_description_max_chars}, and only name and "
                "description load at startup",
                2,
            )
        )
    return findings


def skill_collection_findings(snapshot: Mapping[str, bytes]) -> list[Finding]:
    """Skills must live in ``<name>/SKILL.md``; a top level SKILL.md is not discovered."""
    return [
        Finding(
            path,
            "skill-misplaced",
            "SKILL.md must sit in its own directory (skills/<name>/SKILL.md); Kiro does not "
            "discover a SKILL.md placed directly in the skills folder",
        )
        for path in snapshot
        if path == "SKILL.md"
    ]


# --- settings and prompts -----------------------------------------------------


def settings_findings(doc: SteeringDocument, data: object) -> list[Finding]:
    findings: list[Finding] = []
    path = doc.relative_path
    name = _name(path)
    if name == "mcp.json":
        servers = data.get("mcpServers") if isinstance(data, Mapping) else None
        if not isinstance(servers, Mapping):
            findings.append(
                Finding(path, "mcp-shape", 'mcp.json is an object with an "mcpServers" mapping')
            )
            return findings
        for server, spec in servers.items():
            if not isinstance(spec, Mapping):
                findings.append(
                    Finding(path, "mcp-shape", f"mcpServers.{server} must be an object")
                )
                continue
            if not spec.get("command") and not spec.get("url"):
                findings.append(
                    Finding(
                        path,
                        "mcp-server-command",
                        f"mcpServers.{server} needs a command (stdio) or a url (remote)",
                    )
                )
            blob = json.dumps(spec)
            if _MACHINE_PATH_RE.search(blob):
                findings.append(
                    Finding(
                        path,
                        "mcp-machine-path",
                        f"mcpServers.{server} contains a home directory path; it will not work "
                        "on other machines, use a command on PATH or ${HOME}",
                    )
                )
    elif (
        name == "lsp.json"
        and isinstance(data, Mapping)
        and not isinstance(data.get("languages"), Mapping)
    ):
        findings.append(
            Finding(path, "lsp-shape", 'lsp.json is an object with a "languages" mapping')
        )
    return findings


def prompt_findings(
    doc: SteeringDocument, limits: PracticeLimits = DEFAULT_LIMITS
) -> list[Finding]:
    stem = PurePosixPath(doc.relative_path).stem
    if len(stem) > limits.prompt_name_max_chars:
        return [
            Finding(
                doc.relative_path,
                "prompt-name-length",
                f"prompt names are limited to {limits.prompt_name_max_chars} characters",
            )
        ]
    return []


# --- workspace: does Kiro load everything automatically? ------------------------


def workspace_findings(
    root: Path, *, hooks_dir: str, settings_dir: str, kiro_home: Path | None = None
) -> list[Finding]:
    """Check that a Kiro session in this workspace refreshes and loads the configuration.

    The steersync SessionStart hook keeps the collections current without anyone
    asking, and the MCP server entry lets the agent pull, push and validate on
    request. ``steersync setup`` writes both once in ``~/.kiro`` (``kiro_home``), which
    covers every workspace; ``steersync init --kiro`` writes them into the workspace.
    """
    findings: list[Finding] = []
    hooks = root / hooks_dir
    has_hook = _has_session_start_pull(hooks) or (
        kiro_home is not None and _has_session_start_pull(kiro_home / "hooks")
    )
    has_mcp = _has_steersync_mcp(root / settings_dir / "mcp.json") or (
        kiro_home is not None and _has_steersync_mcp(kiro_home / "settings" / "mcp.json")
    )
    if not has_hook:
        findings.append(
            Finding(
                hooks_dir,
                "autoload-hook-missing",
                "no SessionStart hook runs 'steersync pull'; Kiro will start with whatever "
                "is on disk. Run 'steersync setup' to add it to ~/.kiro for every repository",
            )
        )
    if not has_mcp:
        findings.append(
            Finding(
                f"{settings_dir}/mcp.json",
                "autoload-mcp-missing",
                "the steersync MCP server is not registered, so 'pull kiro config' or "
                "'raise an MR for kiro config' are not available in chat. Run 'steersync setup' "
                "to add it to ~/.kiro/settings/mcp.json",
            )
        )
    return findings


def _has_session_start_pull(hooks: Path) -> bool:
    if not hooks.is_dir():
        return False
    for file in sorted(hooks.glob("*.json")):
        try:
            data = json.loads(file.read_text("utf-8"))
        except OSError, json.JSONDecodeError, UnicodeDecodeError:
            continue
        if not isinstance(data, Mapping):
            continue
        for hook in _as_list(data.get("hooks")):
            if not isinstance(hook, Mapping) or hook.get("trigger") != "SessionStart":
                continue
            action = hook.get("action")
            command = action.get("command") if isinstance(action, Mapping) else None
            if isinstance(command, str) and _STEERING_HOOK_COMMAND in command:
                return True
    return False


def _has_steersync_mcp(mcp: Path) -> bool:
    if not mcp.is_file():
        return False
    try:
        data = json.loads(mcp.read_text("utf-8"))
    except OSError, json.JSONDecodeError, UnicodeDecodeError:
        return False
    servers = data.get("mcpServers") if isinstance(data, Mapping) else None
    if not isinstance(servers, Mapping):
        return False
    # Registered under any name, as long as it runs steersync (setup never adds a second).
    return "steersync" in servers or any(_runs_steersync(spec) for spec in servers.values())


def _runs_steersync(spec: object) -> bool:
    if not isinstance(spec, Mapping):
        return False
    words = [str(spec.get("command", "")), *map(str, _as_list(spec.get("args")))]
    return any("steersync" in word for word in words)
