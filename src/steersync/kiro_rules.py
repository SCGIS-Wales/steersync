"""Schema rules for Kiro configuration files, as pure functions over parsed data.

Every function takes already parsed JSON or YAML and returns :class:`Issue` records;
:mod:`steersync.lint` attaches the file path and turns them into findings. The
enumerations come from the Kiro documentation and, where it is silent, from the
validators shipped with Kiro CLI 3.5 (the workflow recipe and sandbox schemas).
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal, NamedTuple

Severity = Literal["error", "warning"]


class Issue(NamedTuple):
    rule: str
    severity: Severity
    message: str


# --- hooks ----------------------------------------------------------------------

HOOK_TRIGGERS: frozenset[str] = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "Stop",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "PreCompact",
        "PostCompact",
        "UserPromptSubmit",
        "PreTaskExec",
        "PostTaskExec",
        "PostFileCreate",
        "PostFileSave",
        "PostFileDelete",
        "Manual",
    }
)
HOOK_ACTION_TYPES: frozenset[str] = frozenset({"command", "agent"})
_NO_MATCHER_TRIGGERS: frozenset[str] = frozenset({"Stop", "SessionStart", "SessionEnd"})


def _one_of(value: object, options: frozenset[str] | tuple[str, ...]) -> bool:
    """Membership for untrusted JSON values, which may be unhashable lists or objects."""
    return isinstance(value, str) and value in options


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def hook_entry_issues(hook: Mapping[str, Any], label: str, *, standalone: bool) -> list[Issue]:
    """Rules for one hook, in a standalone hook file or inline in a V3 agent."""
    issues: list[Issue] = []
    required = ("name", "trigger", "action") if standalone else ("trigger", "action")
    issues.extend(
        Issue("hook-schema", "error", f"{label} lacks {key!r}")
        for key in required
        if key not in hook
    )
    if "when" in hook or "then" in hook:
        issues.append(
            Issue(
                "hook-legacy-file",
                "error",
                f"{label} uses the legacy when/then form; a V3 hook in .kiro/hooks/<id>.json "
                'has "name", "trigger" (PascalCase) and "action" {"type": "command" | "agent"}',
            )
        )
    trigger = hook.get("trigger")
    if isinstance(trigger, str):
        if trigger not in HOOK_TRIGGERS:
            issues.append(
                Issue(
                    "hook-trigger",
                    "error",
                    f"{label}: {trigger!r} is not a V3 trigger; V3 triggers are PascalCase "
                    f"({', '.join(sorted(HOOK_TRIGGERS))})",
                )
            )
    elif trigger is not None:
        issues.append(Issue("hook-schema", "error", f"{label}: trigger must be a string"))
    action = hook.get("action")
    action_type = action.get("type") if isinstance(action, Mapping) else None
    if action is not None and not isinstance(action, Mapping):
        issues.append(Issue("hook-action", "error", f"{label}: action must be an object"))
    elif isinstance(action, Mapping):
        if not _one_of(action_type, HOOK_ACTION_TYPES):
            issues.append(
                Issue("hook-action", "error", f'{label}: action.type must be "command" or "agent"')
            )
        elif action_type == "command" and not _non_empty_str(action.get("command")):
            issues.append(
                Issue("hook-action", "error", f"{label}: a command action needs action.command")
            )
        elif action_type == "agent" and not _non_empty_str(action.get("prompt")):
            issues.append(
                Issue("hook-action", "error", f"{label}: an agent action needs action.prompt")
            )
    timeout = hook.get("timeout")
    if timeout is not None and (not _is_int(timeout) or timeout < 0):
        issues.append(
            Issue(
                "hook-timeout-value",
                "error",
                f"{label}: timeout is a whole number of seconds, 0 or more (0 disables it)",
            )
        )
    enabled = hook.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        issues.append(Issue("hook-schema", "error", f"{label}: enabled must be true or false"))
    if "confirm" in hook:
        issues.extend(_confirm_issues(hook, label, action_type))
    matcher = hook.get("matcher")
    if isinstance(matcher, str) and "use_aws" in matcher:
        issues.append(
            Issue(
                "hook-use-aws",
                "warning",
                f"{label}: the use_aws tool is gone in V3; match the AWS MCP server instead",
            )
        )
    return issues


def _confirm_issues(hook: Mapping[str, Any], label: str, action_type: object) -> list[Issue]:
    if hook.get("trigger") != "Stop" or action_type != "command":
        return [
            Issue(
                "hook-confirm",
                "error",
                f"{label}: confirm is only allowed on Stop hooks with a command action",
            )
        ]
    confirm = hook["confirm"]
    if not isinstance(confirm, Mapping) or not _non_empty_str(confirm.get("question")):
        return [Issue("hook-confirm", "error", f"{label}: confirm needs a question")]
    options = confirm.get("options")
    if options is not None and (
        not isinstance(options, list)
        or not all(
            isinstance(o, Mapping)
            and _non_empty_str(o.get("id"))
            and _non_empty_str(o.get("label"))
            for o in options
        )
    ):
        return [
            Issue(
                "hook-confirm", "error", f"{label}: confirm.options are objects with id and label"
            )
        ]
    return []


def hook_file_issues(data: object) -> list[Issue]:
    """Rules for a standalone ``.kiro/hooks/*.json`` file."""
    if not isinstance(data, Mapping) or not isinstance(data.get("hooks"), list):
        return [
            Issue(
                "hook-schema",
                "error",
                'a standalone hook file is an object with "version": "v1" and a "hooks" list',
            )
        ]
    issues: list[Issue] = []
    if data.get("version") != "v1":
        issues.append(Issue("hook-schema", "error", 'a hook file needs "version": "v1"'))
    names: set[str] = set()
    for index, hook in enumerate(data["hooks"]):
        label = f"hooks[{index}]"
        if not isinstance(hook, Mapping):
            issues.append(Issue("hook-schema", "error", f"{label} must be an object"))
            continue
        issues.extend(hook_entry_issues(hook, label, standalone=True))
        name = hook.get("name")
        if isinstance(name, str):
            if name in names:
                issues.append(
                    Issue("hook-duplicate-name", "warning", f"{label}: duplicate {name!r}")
                )
            names.add(name)
    return issues


# --- agents ---------------------------------------------------------------------

PERMISSION_CAPABILITIES: frozenset[str] = frozenset(
    {
        "fs_read",
        "fs_write",
        "shell",
        "web_fetch",
        "web_search",
        "mcp",
        "subagent",
        "skill",
        "power",
        "context",
        "diagnostics",
        "workflow",
        "sessions",
        "sandbox_network",
        "all",
        "builtin",
        "filesystem",
    }
)
# ``evaluate`` is the weakest effect, written by the approve-for-me preset.
PERMISSION_EFFECTS: frozenset[str] = frozenset({"allow", "ask", "deny", "evaluate"})
RESOURCE_SCHEMES: tuple[str, ...] = ("file://", "skill://")
KNOWLEDGE_INDEX_TYPES: frozenset[str] = frozenset({"best", "fast"})
_HOME_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:/Users/|/home/)[^/\s\"']+|[A-Za-z]:\\\\Users\\\\")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def agent_issues(
    data: Mapping[str, Any],
    *,
    fields: Sequence[str],
    legacy_keys: Mapping[str, str],
    upgraded_keys: Sequence[str] = (),
    markdown: bool = False,
) -> list[Issue]:
    """V3 schema rules for a JSON agent or a Markdown agent's front matter.

    ``fields`` are the V3 top level keys and ``legacy_keys`` maps each key V3 no longer
    reads to its replacement (both from ``lint.agent_fields`` and
    ``lint.legacy_agent_keys``). Kiro validates JSON agents, so an unknown key there is an
    error; in Markdown front matter it is a warning. ``upgraded_keys`` are what
    `/upgrade-agent` leaves next to ``permissions``: accepted in an upgraded agent.
    """
    upgraded = "permissions" in data
    issues: list[Issue] = [
        Issue(
            "agent-legacy-key",
            "error",
            f"{key} is not read by Kiro V3; use {replacement} instead. Run /upgrade-agent "
            "in Kiro, then commit the result",
        )
        for key, replacement in legacy_keys.items()
        if key in data and not (upgraded and key in upgraded_keys)
    ]
    known = set(fields) | set(legacy_keys) | set(upgraded_keys)
    issues.extend(
        Issue(
            "agent-unknown-key",
            "warning" if markdown else "error",
            f"{key!r} is not a Kiro V3 agent field ({', '.join(fields)})",
        )
        for key in data
        if key not in known
    )
    if "use_aws" in _strings(data.get("tools")):
        issues.append(
            Issue(
                "agent-use-aws",
                "warning",
                "the use_aws tool is gone in V3; add the AWS API MCP server instead",
            )
        )
    issues.extend(_permission_issues(data.get("permissions")))
    issues.extend(_resource_issues(data.get("resources")))
    issues.extend(_knowledge_agent_issues(data, markdown=markdown))
    hooks = data.get("hooks")
    if isinstance(hooks, list):
        for index, hook in enumerate(hooks):
            label = f"hooks[{index}]"
            if isinstance(hook, Mapping):
                issues.extend(hook_entry_issues(hook, label, standalone=False))
            else:
                issues.append(Issue("hook-schema", "error", f"{label} must be an object"))
    elif isinstance(hooks, Mapping):
        issues.append(
            Issue(
                "hook-legacy-file",
                "error",
                "hooks keyed by trigger is the legacy agent form; V3 agent hooks are a list "
                'of {"trigger", "action"}, or move them to .kiro/hooks/<id>.json with '
                '"version": "v1" and "hooks": [{"name", "trigger", "action"}]',
            )
        )
    return issues


def _knowledge_agent_issues(data: Mapping[str, Any], *, markdown: bool) -> list[Issue]:
    """An agent with a knowledgeBase resource must be able to call the knowledge tool."""
    resources = data.get("resources")
    bases = (
        [r for r in resources or [] if isinstance(r, Mapping) and r.get("type") == "knowledgeBase"]
        if isinstance(resources, list)
        else []
    )
    if not bases:
        return []
    issues: list[Issue] = []
    names = [str(b["name"]) for b in bases if isinstance(b.get("name"), str)]
    issues.extend(
        Issue("agent-knowledge-base", "error", f"knowledge base name {name!r} is used twice")
        for name in sorted({n for n in names if names.count(n) > 1})
    )
    tools = _strings(data.get("tools"))
    if not {"knowledge", "*", "@builtin"} & set(tools):
        issues.append(
            Issue(
                "agent-knowledge-base",
                "warning",
                'a knowledgeBase resource needs the knowledge tool: add "knowledge" to tools',
            )
        )
    if "knowledge" in _strings(data.get("excludedTools")):
        issues.append(
            Issue(
                "agent-knowledge-base",
                "error",
                "knowledge is in excludedTools, so the knowledge base can never be searched",
            )
        )
    if markdown:
        issues.append(
            Issue(
                "agent-knowledge-base",
                "warning",
                "Markdown agents have silently dropped knowledgeBase resources in some Kiro "
                "builds (kirodotdev/Kiro#9790); declare it in a JSON agent",
            )
        )
    return issues


def _permission_issues(permissions: object) -> list[Issue]:
    if permissions is None:
        return []
    rules = permissions.get("rules") if isinstance(permissions, Mapping) else None
    if not isinstance(rules, list):
        return [Issue("agent-permissions", "error", 'permissions is an object with a "rules" list')]
    issues: list[Issue] = []
    for index, rule in enumerate(rules):
        label = f"permissions.rules[{index}]"
        if not isinstance(rule, Mapping):
            issues.append(Issue("agent-permissions", "error", f"{label} must be an object"))
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str):
            issues.append(Issue("agent-permissions", "error", f"{label} needs a capability"))
        elif capability not in PERMISSION_CAPABILITIES:
            issues.append(
                Issue(
                    "agent-permission-capability",
                    "warning",
                    f"{label}: unknown capability {capability!r}; known: "
                    f"{', '.join(sorted(PERMISSION_CAPABILITIES))}",
                )
            )
        effect = rule.get("effect")
        if not _one_of(effect, PERMISSION_EFFECTS):
            issues.append(
                Issue(
                    "agent-permissions",
                    "error",
                    f"{label}: effect must be allow, ask, deny or evaluate; got {effect!r}",
                )
            )
        for key in ("match", "exclude"):
            value = rule.get(key)
            if value is not None and not (
                isinstance(value, str)
                or (isinstance(value, list) and all(isinstance(v, str) for v in value))
            ):
                issues.append(
                    Issue(
                        "agent-permissions",
                        "error",
                        f"{label}: {key} is a string or a list of strings",
                    )
                )
    return issues


def _resource_issues(resources: object) -> list[Issue]:
    if not isinstance(resources, list):
        return []
    issues: list[Issue] = []
    for index, resource in enumerate(resources):
        label = f"resources[{index}]"
        if isinstance(resource, str):
            if not resource.startswith(RESOURCE_SCHEMES):
                issues.append(
                    Issue(
                        "agent-resource-scheme",
                        "warning",
                        f"{label}: {resource!r} should start with file:// or skill://",
                    )
                )
            if _HOME_PATH_RE.search(resource):
                issues.append(
                    Issue(
                        "agent-machine-path",
                        "warning",
                        f"{label}: {resource!r} points into one person's home directory and "
                        "will not resolve for the rest of the team",
                    )
                )
        elif isinstance(resource, Mapping):
            if resource.get("type") == "knowledgeBase":
                source = resource.get("source")
                if not _non_empty_str(source):
                    issues.append(
                        Issue(
                            "agent-knowledge-base", "error", f"{label}: knowledgeBase needs source"
                        )
                    )
                elif not str(source).startswith("file://"):
                    issues.append(
                        Issue(
                            "agent-knowledge-base",
                            "error",
                            f"{label}: source must be a file:// folder; got {source!r}",
                        )
                    )
                if not _non_empty_str(resource.get("name")):
                    issues.append(
                        Issue("agent-knowledge-base", "error", f"{label}: knowledgeBase needs name")
                    )
                if "autoUpdate" in resource and not isinstance(resource["autoUpdate"], bool):
                    issues.append(
                        Issue("agent-knowledge-base", "error", f"{label}: autoUpdate is a boolean")
                    )
                index_type = resource.get("indexType")
                if index_type is not None and not _one_of(index_type, KNOWLEDGE_INDEX_TYPES):
                    issues.append(
                        Issue(
                            "agent-knowledge-base",
                            "warning",
                            f"{label}: indexType is best or fast; got {index_type!r}",
                        )
                    )
        else:
            issues.append(
                Issue("agent-resource-scheme", "error", f"{label} must be a string or an object")
            )
    return issues


# --- settings: mcp.json, sandbox.json, cli.json -----------------------------------

OAUTH_MODES: frozenset[str] = frozenset({"browser", "enterprise", "auto"})
_SECRET_NAME_RE = re.compile(
    r"(?i)(token|secret|passw(or)?d|api[_-]?key|access[_-]?key|private[_-]?key|credential|"
    r"^authorization$|^x-api-key$|bearer)"
)
_PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _literal_secret(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _PLACEHOLDER_RE.search(value)


def mcp_issues(data: object) -> list[Issue]:
    """Errors in ``mcp.json`` that would leak secrets or that Kiro rejects."""
    servers = data.get("mcpServers") if isinstance(data, Mapping) else None
    if not isinstance(servers, Mapping):
        return []
    issues: list[Issue] = []
    for name, spec in servers.items():
        if not isinstance(spec, Mapping):
            continue
        label = f"mcpServers.{name}"
        for section in ("env", "headers"):
            values = spec.get(section)
            if not isinstance(values, Mapping):
                continue
            issues.extend(
                Issue(
                    "mcp-literal-secret",
                    "error",
                    f"{label}.{section}.{key} holds a literal value; use ${{{key.upper()}}} so the "
                    "secret stays on each machine",
                )
                for key, value in values.items()
                if _SECRET_NAME_RE.search(str(key)) and _literal_secret(value)
            )
        issues.extend(_oauth_issues(spec.get("oauth"), label))
        for key in ("timeout", "timeoutMs"):
            value = spec.get(key)
            if value is not None and (not _is_int(value) or value <= 0):
                issues.append(
                    Issue("mcp-timeout", "error", f"{label}.{key} is a positive whole number")
                )
        for key in ("autoApprove", "disabledTools"):
            value = spec.get(key)
            if value is not None and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                issues.append(Issue("mcp-shape", "error", f"{label}.{key} is a list of tool names"))
        disabled = spec.get("disabled")
        if disabled is not None and not isinstance(disabled, bool):
            issues.append(Issue("mcp-shape", "error", f"{label}.disabled is true or false"))
    return issues


def _oauth_issues(oauth: object, label: str) -> list[Issue]:
    if oauth is None:
        return []
    if not isinstance(oauth, Mapping):
        return [Issue("mcp-oauth", "error", f"{label}.oauth must be an object")]
    issues: list[Issue] = []
    mode = oauth.get("mode")
    if mode is not None and not _one_of(mode, OAUTH_MODES):
        issues.append(
            Issue("mcp-oauth", "error", f"{label}.oauth.mode is browser, enterprise or auto")
        )
    if _literal_secret(oauth.get("clientSecret")):
        issues.append(
            Issue(
                "mcp-literal-secret",
                "error",
                f"{label}.oauth.clientSecret holds a literal value; use ${{VAR}} instead",
            )
        )
    metadata = oauth.get("clientMetadataUrl")
    if metadata is not None:
        if not (isinstance(metadata, str) and metadata.startswith("https://")):
            issues.append(
                Issue("mcp-oauth", "error", f"{label}.oauth.clientMetadataUrl must be https://")
            )
        elif oauth.get("clientSecret"):
            issues.append(
                Issue(
                    "mcp-oauth",
                    "warning",
                    f"{label}.oauth: Kiro ignores clientMetadataUrl when clientSecret is set",
                )
            )
    return issues


SANDBOX_NETWORK_ACCESS: tuple[str, ...] = (
    "DEFAULT_ALLOWED",
    "COMMON_DEPENDENCIES",
    "DEFAULT_BLOCKED",
)
SANDBOX_KEYS: frozenset[str] = frozenset({"enabled", "networkAccess", "fsMode", "mcpSandboxing"})


def sandbox_issues(data: object) -> list[Issue]:
    """Rules for ``.kiro/settings/sandbox.json``."""
    if not isinstance(data, Mapping):
        return [Issue("sandbox-shape", "error", "sandbox.json is a JSON object")]
    issues = [
        Issue("sandbox-shape", "error", f"{key} must be true or false")
        for key in ("enabled", "mcpSandboxing")
        if key in data and not isinstance(data[key], bool)
    ]
    access = data.get("networkAccess")
    if access is not None and not _one_of(access, SANDBOX_NETWORK_ACCESS):
        issues.append(
            Issue(
                "sandbox-network-access",
                "error",
                f"networkAccess is one of {', '.join(SANDBOX_NETWORK_ACCESS)}; got {access!r}",
            )
        )
    if data.get("enabled") is False or access == "DEFAULT_ALLOWED":
        issues.append(
            Issue(
                "sandbox-cannot-loosen",
                "warning",
                "a project sandbox file can only tighten an active global policy; this setting "
                "has no effect on machines whose global sandbox is stricter",
            )
        )
    issues.extend(
        Issue("sandbox-unknown-key", "warning", f"unknown sandbox key {key!r}")
        for key in data
        if key not in SANDBOX_KEYS
    )
    return issues


# Keys kiro-cli reads only from ~/.kiro/settings/cli.json (Kiro settings reference).
GLOBAL_ONLY_CLI_KEYS: frozenset[str] = frozenset(
    {
        "chat.agentEngine",
        "acp.agentEngine",
        "chat.enableAutoAgentUpgrade",
        "app.warnOnElevatedPrivileges",
        "auth.portalUrl",
        "chat.uiMode",
        "chat.screenReader",
        "chat.terminalTitle",
    }
)


def flatten_settings(data: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield dotted keys from either the flat (``"chat.agentEngine"``) or nested layout."""
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            yield from flatten_settings(value, f"{dotted}.")
        else:
            yield dotted, value


def cli_settings_issues(data: object) -> list[Issue]:
    """Rules for a workspace ``.kiro/settings/cli.json``."""
    if not isinstance(data, Mapping):
        return [Issue("cli-settings-shape", "error", "cli.json is a JSON object")]
    return [
        Issue(
            "cli-settings-global-only",
            "error",
            f"{key} is only read from ~/.kiro/settings/cli.json; a workspace file cannot set it",
        )
        for key, _ in flatten_settings(data)
        if key in GLOBAL_ONLY_CLI_KEYS
    ]


# --- workflow recipes ------------------------------------------------------------

WORKFLOW_NODE_TYPES: frozenset[str] = frozenset({"step", "sequence", "repeat", "parallel", "watch"})
WORKFLOW_JOIN_POLICIES: frozenset[str] = frozenset({"all", "allSettled", "any"})
WORKFLOW_ON_MAX: frozenset[str] = frozenset({"abort", "continue", "pause"})
WORKFLOW_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "inputs",
        "steps",
        "modelId",
        "effortLevel",
        "injectOriginalUserRequest",
        "planRevision",
    }
)
# Documented limits: at most 50 ``step`` nodes and 8 nesting levels per recipe; the
# repeat ceiling comes from the recipe schema in Kiro CLI 3.5.
WORKFLOW_MAX_ITERATIONS = 1000
WORKFLOW_MAX_DEPTH = 8
WORKFLOW_MAX_STEPS = 50
_KEBAB_RE = re.compile(r"[^a-z0-9]+")


class _WalkState(NamedTuple):
    issues: list[Issue]
    ids: dict[str, str]
    counts: list[int]  # [static steps, deepest level]


def workflow_issues(data: object, file_name: str) -> list[Issue]:
    """Rules for a workflow recipe, following the recipe schema in Kiro CLI 3.5."""
    if not isinstance(data, Mapping):
        return [Issue("workflow-schema", "error", "a workflow recipe is an object")]
    issues: list[Issue] = []
    name = data.get("name")
    if not _non_empty_str(name):
        issues.append(Issue("workflow-schema", "error", "a workflow recipe needs a name"))
    elif isinstance(name, str):
        expected = _KEBAB_RE.sub("-", name.lower()).strip("-")
        stem = file_name.split(".workflow.", 1)[0]
        if expected and stem != expected:
            issues.append(
                Issue(
                    "workflow-file-name",
                    "warning",
                    f"Kiro saves recipe {name!r} as {expected}.workflow.json; this file is "
                    f"{file_name}",
                )
            )
    inputs = data.get("inputs")
    if inputs is not None and not (
        isinstance(inputs, Mapping) and all(isinstance(v, str) for v in inputs.values())
    ):
        issues.append(
            Issue("workflow-schema", "error", "inputs maps each input name to a description")
        )
    issues.extend(
        Issue("workflow-unknown-key", "warning", f"unknown recipe key {key!r} is ignored")
        for key in data
        if key not in WORKFLOW_KEYS
    )
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append(Issue("workflow-schema", "error", "a workflow recipe needs a steps list"))
        return issues
    state = _WalkState(issues, {}, [0, 0])
    _walk_nodes(steps, "steps", 1, state)
    if state.counts[1] > WORKFLOW_MAX_DEPTH:
        issues.append(
            Issue(
                "workflow-depth",
                "error",
                f"nodes nest {state.counts[1]} levels deep; Kiro allows at most "
                f"{WORKFLOW_MAX_DEPTH}",
            )
        )
    if state.counts[0] > WORKFLOW_MAX_STEPS:
        issues.append(
            Issue(
                "workflow-size",
                "error",
                f"{state.counts[0]} step nodes; Kiro allows at most {WORKFLOW_MAX_STEPS} per "
                "recipe (repeat iterations do not count), so split it",
            )
        )
    return issues


def _walk_nodes(nodes: Sequence[object], where: str, depth: int, state: _WalkState) -> None:
    state.counts[1] = max(state.counts[1], depth)
    for index, node in enumerate(nodes):
        label = f"{where}[{index}]"
        if not isinstance(node, Mapping):
            state.issues.append(Issue("workflow-schema", "error", f"{label} must be an object"))
            continue
        _check_node(node, label, depth, state)


def _check_node(node: Mapping[str, Any], label: str, depth: int, state: _WalkState) -> None:
    issues = state.issues
    node_type = node.get("type")
    if not _one_of(node_type, WORKFLOW_NODE_TYPES):
        issues.append(
            Issue(
                "workflow-schema",
                "error",
                f"{label}: type is one of {', '.join(sorted(WORKFLOW_NODE_TYPES))}",
            )
        )
        return
    node_id = node.get("id")
    if not _non_empty_str(node_id):
        issues.append(Issue("workflow-schema", "error", f"{label} needs an id"))
    elif isinstance(node_id, str):
        if node_id in state.ids:
            issues.append(
                Issue(
                    "workflow-duplicate-id",
                    "error",
                    f"{label}: id {node_id!r} is already used by {state.ids[node_id]}",
                )
            )
        state.ids.setdefault(node_id, label)

    def need(key: str, description: str) -> None:
        if not _non_empty_str(node.get(key)):
            issues.append(Issue("workflow-schema", "error", f"{label}: {node_type} {description}"))

    match node_type:
        case "step":
            state.counts[0] += 1
            need("agent", "needs an agent")
            need("prompt", "needs a prompt")
        case "watch":
            need("handler", "needs a handler")
        case "sequence" | "repeat":
            _children(node, "steps", label, depth, state)
            if node_type == "repeat":
                _repeat_issues(node, label, issues)
        case "parallel":
            _children(node, "branches", label, depth, state)
            if not _one_of(node.get("joinPolicy"), WORKFLOW_JOIN_POLICIES):
                issues.append(
                    Issue(
                        "workflow-schema",
                        "error",
                        f"{label}: joinPolicy is all, allSettled or any",
                    )
                )


def _children(node: Mapping[str, Any], key: str, label: str, depth: int, state: _WalkState) -> None:
    children = node.get(key)
    if not isinstance(children, list):
        state.issues.append(Issue("workflow-schema", "error", f"{label} needs a {key} list"))
        return
    _walk_nodes(children, f"{label}.{key}", depth + 1, state)


def _repeat_issues(node: Mapping[str, Any], label: str, issues: list[Issue]) -> None:
    limit = node.get("maxIterations")
    if not (isinstance(limit, int) and not isinstance(limit, bool)) or not (
        0 < limit <= WORKFLOW_MAX_ITERATIONS
    ):
        issues.append(
            Issue(
                "workflow-repeat",
                "error",
                f"{label}: maxIterations is a whole number from 1 to {WORKFLOW_MAX_ITERATIONS}",
            )
        )
    if not _one_of(node.get("onMaxIterations"), WORKFLOW_ON_MAX):
        issues.append(
            Issue(
                "workflow-repeat", "error", f"{label}: onMaxIterations is abort, continue or pause"
            )
        )
    if "stopCondition" in node and "stopWhen" in node:
        issues.append(
            Issue(
                "workflow-repeat",
                "error",
                f"{label}: a repeat node allows at most one of stopCondition or stopWhen",
            )
        )


# --- .kiroignore ------------------------------------------------------------------

KIROIGNORE_ADVISED: tuple[tuple[str, str], ...] = ((".env", ".env*"), (".pem", "*.pem"))


def kiroignore_issues(text: str, *, root_file: bool) -> list[Issue]:
    """Rules for a ``.kiroignore`` file (gitignore syntax)."""
    issues: list[Issue] = []
    patterns: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.endswith("\\") and not line.endswith("\\\\"):
            issues.append(
                Issue(
                    "kiroignore-syntax",
                    "error",
                    f"line {number}: a trailing backslash escapes nothing",
                )
            )
        if _misplaced_globstar(stripped):
            issues.append(
                Issue(
                    "kiroignore-syntax",
                    "warning",
                    f"line {number}: ** only spans directories when it stands alone between "
                    "slashes",
                )
            )
        patterns.append(stripped.lstrip("!/"))
    if root_file:
        for probe, suggestion in KIROIGNORE_ADVISED:
            if not any(_covers(pattern, probe) for pattern in patterns):
                issues.append(
                    Issue(
                        "kiroignore-secrets",
                        "warning",
                        f"nothing keeps {probe} files from the agent; consider adding "
                        f"{suggestion!r}",
                    )
                )
    return issues


def _misplaced_globstar(pattern: str) -> bool:
    """True when ``**`` is glued to other characters, where it acts like a single ``*``."""
    body = pattern.lstrip("!")
    for match in re.finditer(r"\*\*+", body):
        before = body[match.start() - 1] if match.start() else "/"
        after = body[match.end()] if match.end() < len(body) else "/"
        if before != "/" or after != "/":
            return True
    return False


def _covers(pattern: str, probe: str) -> bool:
    if probe == ".env":
        return pattern.startswith(".env") or pattern in {"*.env", "**/.env", "**/.env*"}
    return pattern.endswith(".pem") or pattern in {"*.pem", "**/*.pem"}
