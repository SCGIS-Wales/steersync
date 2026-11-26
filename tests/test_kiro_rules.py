"""Schema rules for hooks, agents, settings, workflows and .kiroignore."""

import json
from typing import Any

import pytest
import yaml

from steersync import kiro_rules
from steersync.config import Config
from steersync.lint import lint_document
from steersync.steering import document_from_bytes


def rules_of(issues: list[kiro_rules.Issue]) -> set[str]:
    return {issue.rule for issue in issues}


DEFAULTS = Config().lint


def agent(data: dict[str, Any], *, markdown: bool = False) -> list[kiro_rules.Issue]:
    return kiro_rules.agent_issues(
        data,
        fields=DEFAULTS.agent_fields,
        legacy_keys=DEFAULTS.legacy_agent_keys,
        upgraded_keys=DEFAULTS.upgraded_agent_keys,
        markdown=markdown,
    )


def lint_rules(config: Config, path: str, profile: str, content: str | dict[str, Any]) -> set[str]:
    text = content if isinstance(content, str) else json.dumps(content)
    doc = document_from_bytes(path, text.encode())
    return {f.rule for f in lint_document(doc, config.lint, profile=profile)}  # type: ignore[arg-type]


# --- hooks ---------------------------------------------------------------------


def hook(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "h",
        "trigger": "Stop",
        "action": {"type": "command", "command": "make test"},
    }
    base.update(fields)
    return base


def test_hook_file_requires_v1_and_valid_entries() -> None:
    assert kiro_rules.hook_file_issues({"version": "v1", "hooks": [hook()]}) == []
    assert "hook-schema" in rules_of(kiro_rules.hook_file_issues({"hooks": [hook()]}))
    assert "hook-schema" in rules_of(kiro_rules.hook_file_issues([]))
    dup = kiro_rules.hook_file_issues({"version": "v1", "hooks": [hook(), hook()]})
    assert rules_of(dup) == {"hook-duplicate-name"}


@pytest.mark.parametrize(
    ("fields", "rule"),
    [
        ({"action": {"type": "command"}}, "hook-action"),
        ({"action": {"type": "agent", "prompt": " "}}, "hook-action"),
        ({"action": {"type": "shell", "command": "x"}}, "hook-action"),
        ({"action": "make"}, "hook-action"),
        ({"timeout": -1}, "hook-timeout-value"),
        ({"timeout": 1.5}, "hook-timeout-value"),
        ({"timeout": True}, "hook-timeout-value"),
        ({"enabled": "yes"}, "hook-schema"),
        ({"trigger": 3}, "hook-schema"),
        ({"trigger": "agentSpawn"}, "hook-trigger"),
        ({"when": {"type": "fileEdited"}}, "hook-legacy-file"),
        ({"trigger": "OnSave"}, "hook-trigger"),
        ({"matcher": "use_aws", "trigger": "PreToolUse"}, "hook-use-aws"),
        ({"trigger": "PreToolUse", "confirm": {"question": "ok?"}}, "hook-confirm"),
        ({"confirm": {"question": ""}}, "hook-confirm"),
        ({"confirm": {"question": "Run?", "options": [{"id": "a"}]}}, "hook-confirm"),
    ],
)
def test_hook_entry_rules(fields: dict[str, Any], rule: str) -> None:
    assert rule in rules_of(kiro_rules.hook_entry_issues(hook(**fields), "h", standalone=True))


def test_hook_entry_accepts_current_triggers_and_confirm() -> None:
    for trigger in ("PermissionRequest", "PreCompact", "PostCompact", "SessionEnd"):
        assert kiro_rules.hook_entry_issues(hook(trigger=trigger), "h", standalone=True) == []
    confirmed = hook(
        timeout=0,
        confirm={"question": "Deploy?", "options": [{"id": "y", "label": "Yes", "run": "make"}]},
    )
    assert kiro_rules.hook_entry_issues(confirmed, "h", standalone=True) == []
    agent = hook(action={"type": "agent", "prompt": "Review the diff"})
    assert kiro_rules.hook_entry_issues(agent, "h", standalone=True) == []
    inline = {"trigger": "Stop", "action": {"type": "command", "command": "x"}}
    assert kiro_rules.hook_entry_issues(inline, "h", standalone=False) == []


# --- agents --------------------------------------------------------------------


def test_legacy_keys_are_errors_naming_the_v3_replacement() -> None:
    legacy = {"name": "a", "allowedTools": ["read"], "toolsSettings": {}}
    issues = agent(legacy)
    assert {(i.rule, i.severity) for i in issues} == {("agent-legacy-key", "error")}
    message = " ".join(i.message for i in issues)
    assert "use permissions instead" in message
    assert "/upgrade-agent" in message
    assert "commit the result" in message
    rules = {"rules": [{"capability": "fs_read", "effect": "allow"}]}
    assert agent({"name": "a", "permissions": rules}) == []
    # /upgrade-agent keeps allowedTools and toolsSettings next to permissions.
    assert agent({**legacy, "permissions": rules, "kiroUpgrade": {"revision": 2}}) == []
    still = agent({**legacy, "toolAliases": {}, "permissions": rules})
    assert [i.message.split(" ")[0] for i in still] == ["toolAliases"]


def test_unknown_agent_key_is_an_error_in_json_and_a_warning_in_markdown() -> None:
    data = {"name": "a", "colour": "blue", "welcomeMessage": "hi", "includePowers": True}
    (issue,) = agent(data)
    assert (issue.rule, issue.severity) == ("agent-unknown-key", "error")
    assert "'colour'" in issue.message
    (warning,) = agent(data, markdown=True)
    assert warning.severity == "warning"


def test_agent_legacy_tools_permissions_and_resources() -> None:
    data = {
        "name": "a",
        "tools": ["fs_read", "execute_bash", "use_aws", "read"],
        "toolAliases": {},
        "keyboardShortcut": "ctrl+a",
        "permissions": {
            "rules": [
                {"capability": "teleport", "effect": "allow"},
                {"capability": "shell", "effect": "maybe"},
                {"effect": "deny"},
                {"capability": "mcp", "effect": "ask", "match": [1]},
                "nope",
            ]
        },
        "resources": [
            "file:///Users/someone/.kiro/steering/a.md",
            "https://example.com",
            {"type": "knowledgeBase", "indexType": "huge"},
            7,
        ],
        "hooks": [
            {"trigger": "Stop", "action": {"type": "command", "command": "x"}},
            {"trigger": "agentSpawn", "action": {"type": "command"}},
            "bad",
        ],
    }
    found = rules_of(agent(data))
    assert {
        "agent-use-aws",
        "agent-legacy-key",
        "agent-permission-capability",
        "agent-permissions",
        "agent-machine-path",
        "agent-resource-scheme",
        "agent-knowledge-base",
        "hook-trigger",
        "hook-action",
        "hook-schema",
    } <= found
    assert "agent-permissions" in rules_of(agent({"permissions": []}))
    legacy = rules_of(agent({"hooks": {"agentSpawn": [], "Stop": []}}))
    assert legacy == {"hook-legacy-file"}


def test_agent_profile_checks_markdown_front_matter(config: Config) -> None:
    md = "---\nname: r\nallowedTools: [read]\n---\nYou review.\n"
    assert "agent-legacy-key" in lint_rules(config, "r.md", "agent", md)
    assert "agent-legacy-key" not in lint_rules(config, "prompts/r.md", "agent", md)


def test_real_upgraded_agent_is_clean(config: Config) -> None:
    """The universal format written by `/upgrade-agent` (Kiro CLI 3.5) passes."""
    agent = {
        "name": "groovy",
        "tools": ["@groovyscope", "code", "glob", "grep", "read", "shell", "write"],
        "allowedTools": ["read", "grep"],
        "toolsSettings": {"shell": {"allowedCommands": ["git status"]}},
        "includeMcpJson": True,
        "resources": ["file://.kiro/steering/*.md", "file://AGENTS.md"],
        "permissions": {
            "rules": [
                {"capability": "shell", "match": ["git status"], "effect": "allow"},
                {"capability": "mcp", "match": ["groovyscope/*"], "effect": "allow"},
            ]
        },
        "kiroUpgrade": {"revision": 2, "fingerprint": "sha256:x", "source": "sha256:y"},
    }
    errors = [r for r in lint_rules(config, "groovy.json", "agent", agent) if r.startswith("agent")]
    assert errors == []


# --- settings ------------------------------------------------------------------


def test_mcp_secrets_oauth_and_shapes(config: Config) -> None:
    good = {
        "mcpServers": {
            "gl": {
                "command": "uvx",
                "env": {"GITLAB_TOKEN": "${GITLAB_TOKEN}", "GIT_TERMINAL_PROMPT": "0"},
                "autoApprove": ["status"],
                "timeoutMs": 5000,
            },
            "remote": {
                "url": "https://mcp.example.com",
                "headers": {"Authorization": "Bearer ${TOKEN}"},
                "oauth": {"mode": "browser", "clientMetadataUrl": "https://example.com/c.json"},
            },
        }
    }
    assert kiro_rules.mcp_issues(good) == []
    bad = {
        "mcpServers": {
            "gl": {
                "command": "uvx",
                "env": {"GITLAB_TOKEN": "glabc", "API_KEY": "abc"},
                "headers": {"Authorization": "Bearer abc"},
                "timeout": 0,
                "autoApprove": "status",
                "disabled": "no",
                "oauth": {
                    "mode": "magic",
                    "clientSecret": "s3cret",
                    "clientMetadataUrl": "https://example.com/c.json",
                },
            },
            "x": {"url": "https://x", "oauth": {"clientMetadataUrl": "http://insecure"}},
            "y": {"url": "https://y", "oauth": "browser"},
            "z": "nope",
        }
    }
    found = kiro_rules.mcp_issues(bad)
    assert {"mcp-literal-secret", "mcp-oauth", "mcp-timeout", "mcp-shape"} == rules_of(found)
    assert sum(i.rule == "mcp-literal-secret" for i in found) == 4
    assert "mcp-literal-secret" in lint_rules(config, "mcp.json", "settings", bad)
    assert kiro_rules.mcp_issues([]) == []


def test_sandbox_rules(config: Config) -> None:
    assert kiro_rules.sandbox_issues({"enabled": True, "networkAccess": "DEFAULT_BLOCKED"}) == []
    found = rules_of(
        kiro_rules.sandbox_issues({"enabled": "yes", "networkAccess": "OPEN", "extra": 1})
    )
    assert found == {"sandbox-shape", "sandbox-network-access", "sandbox-unknown-key"}
    loosen = kiro_rules.sandbox_issues({"enabled": False})
    assert rules_of(loosen) == {"sandbox-cannot-loosen"}
    assert rules_of(kiro_rules.sandbox_issues([])) == {"sandbox-shape"}
    assert "sandbox-network-access" in lint_rules(
        config, "sandbox.json", "settings", {"networkAccess": "x"}
    )


def test_cli_settings_rules(config: Config) -> None:
    assert kiro_rules.cli_settings_issues({"chat.defaultAgent": "groovy"}) == []
    flat = kiro_rules.cli_settings_issues({"chat.agentEngine": "v3"})
    nested = kiro_rules.cli_settings_issues({"chat": {"enableAutoAgentUpgrade": False}})
    assert rules_of(flat) == rules_of(nested) == {"cli-settings-global-only"}
    assert rules_of(kiro_rules.cli_settings_issues("x")) == {"cli-settings-shape"}
    assert "cli-settings-global-only" in lint_rules(
        config, "cli.json", "settings", {"app.warnOnElevatedPrivileges": True}
    )


# --- workflows -----------------------------------------------------------------


def recipe(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Review loop",
        "description": "Review until clean",
        "inputs": {"target": "what to review"},
        "steps": [
            {"type": "step", "id": "plan", "agent": "planner", "prompt": "Plan {{target}}"},
            {
                "type": "repeat",
                "id": "loop",
                "maxIterations": 3,
                "onMaxIterations": "abort",
                "steps": [
                    {
                        "type": "parallel",
                        "id": "checks",
                        "joinPolicy": "allSettled",
                        "branches": [
                            {"type": "step", "id": "lint", "agent": "a", "prompt": "lint"},
                            {"type": "watch", "id": "ci", "handler": "github-checks"},
                        ],
                    }
                ],
            },
        ],
    }
    base.update(fields)
    return base


def test_workflow_valid_recipe(config: Config) -> None:
    assert kiro_rules.workflow_issues(recipe(), "review-loop.workflow.json") == []
    assert lint_rules(config, "review-loop.workflow.json", "workflow", recipe()) == set()
    as_yaml = yaml.safe_dump(recipe())
    assert lint_rules(config, "review-loop.workflow.yaml", "workflow", as_yaml) == set()


def test_workflow_rules(config: Config) -> None:
    bad_steps = [
        {"type": "step", "id": "a"},
        {"type": "step", "id": "a", "agent": "x", "prompt": "y"},
        {"type": "teleport", "id": "b"},
        {"type": "repeat", "id": "r", "maxIterations": 0, "steps": [], "stopWhen": "x"},
        {
            "type": "repeat",
            "id": "r2",
            "maxIterations": 2,
            "onMaxIterations": "pause",
            "steps": [],
            "stopWhen": "x",
            "stopCondition": {},
        },
        {"type": "parallel", "id": "p", "branches": "no"},
        {"type": "watch", "id": "w"},
        {"type": "sequence", "steps": []},
        "nope",
    ]
    found = kiro_rules.workflow_issues(
        recipe(name="Review", steps=bad_steps, inputs={"a": 1}, extra=True), "x.workflow.json"
    )
    assert {
        "workflow-schema",
        "workflow-duplicate-id",
        "workflow-repeat",
        "workflow-unknown-key",
        "workflow-file-name",
    } == rules_of(found)
    assert rules_of(kiro_rules.workflow_issues({"steps": []}, "x.workflow.json")) == {
        "workflow-schema"
    }
    assert rules_of(kiro_rules.workflow_issues([], "x.workflow.json")) == {"workflow-schema"}
    assert "yaml-syntax" in lint_rules(config, "x.workflow.yml", "workflow", "a: [")
    assert "json-syntax" in lint_rules(config, "x.workflow.json", "workflow", "{")
    assert lint_rules(config, "README.md", "workflow", "# Workflows\n") == set()


def test_workflow_depth_and_size() -> None:
    node: dict[str, Any] = {"type": "step", "id": "leaf", "agent": "a", "prompt": "p"}
    for level in range(9):
        node = {"type": "sequence", "id": f"s{level}", "steps": [node]}
    deep = kiro_rules.workflow_issues(recipe(name="deep", steps=[node]), "deep.workflow.json")
    assert rules_of(deep) == {"workflow-depth"}
    many = [{"type": "step", "id": f"s{i}", "agent": "a", "prompt": "p"} for i in range(51)]
    big = kiro_rules.workflow_issues(recipe(name="big", steps=many), "big.workflow.json")
    assert rules_of(big) == {"workflow-size"}


# --- .kiroignore -----------------------------------------------------------------


def test_kiroignore_rules(config: Config) -> None:
    good = "# secrets\n.env*\n*.pem\nbuild/**/cache\n!keep.pem\n"
    assert kiro_rules.kiroignore_issues(good, root_file=True) == []
    found = kiro_rules.kiroignore_issues("foo\\\nsrc/**.py\na**/b\n", root_file=True)
    assert rules_of(found) == {"kiroignore-syntax", "kiroignore-secrets"}
    assert sum(i.rule == "kiroignore-secrets" for i in found) == 2
    assert kiro_rules.kiroignore_issues("docs/\n", root_file=False) == []
    assert "kiroignore-secrets" in lint_rules(config, ".kiroignore", "kiroignore", "docs/\n")
    assert "kiroignore-secrets" not in lint_rules(config, "src/.kiroignore", "kiroignore", "x\n")
    assert "encoding-utf8" in lint_rules(config, ".kiroignore", "kiroignore", "a\0b")


def test_enums_match_kiro_cli_3_5() -> None:
    """Values checked against the Kiro docs and the harness bundled with Kiro CLI 3.5."""
    evaluate = {"capability": "workflow", "effect": "evaluate"}
    sessions = {"capability": "sessions", "effect": "allow"}
    assert agent({"permissions": {"rules": [evaluate, sessions]}}) == []
    for legacy in ("fileEdited", "AfterFileEdit", "agentSpawn"):
        issues = kiro_rules.hook_entry_issues(hook(trigger=legacy), "h", standalone=True)
        assert [(i.rule, i.severity) for i in issues] == [("hook-trigger", "error")]
        assert "PostFileSave" in issues[0].message
    for key in ("acp.agentEngine", "auth.portalUrl", "chat.uiMode", "chat.terminalTitle"):
        assert rules_of(kiro_rules.cli_settings_issues({key: "x"})) == {"cli-settings-global-only"}


def test_workflow_limits_count_step_nodes_only() -> None:
    watches = [{"type": "watch", "id": f"w{i}", "handler": "github-pr"} for i in range(60)]
    assert kiro_rules.workflow_issues(recipe(name="w", steps=watches), "w.workflow.json") == []
    steps = [{"type": "step", "id": f"s{i}", "agent": "a", "prompt": "p"} for i in range(51)]
    (issue,) = kiro_rules.workflow_issues(recipe(name="s", steps=steps), "s.workflow.json")
    assert (issue.rule, issue.severity) == ("workflow-size", "error")


def test_no_v1_or_v2_support_remains() -> None:
    """Only the MCP SDK's own version and the V3 hook schema value "v1" may appear."""
    import re
    from pathlib import Path

    import steersync

    pattern = re.compile(r"v2|V2|Classic|\bV1\b|2\.x")
    allowed = ("SDK v2",)
    found = [
        f"{path.name}:{number}: {line.strip()}"
        for path in Path(steersync.__file__).parent.rglob("*.py")
        for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1)
        if pattern.search(line) and not any(a in line for a in allowed)
    ]
    assert found == []


def test_agent_resource_budget(config: Config, tmp_path: Any) -> None:
    steering = tmp_path / ".kiro" / "steering"
    steering.mkdir(parents=True)
    limits = config.lint.kiro_limits
    for index in range(limits.agent_file_resources_max + 1):
        (steering / f"s{index:02d}.md").write_text("# S\n")
    (steering / "big.md").write_text("x" * (limits.file_chars_truncate + 1))
    (steering / "big2.md").write_text("x" * (limits.file_chars_truncate + 1))  # over the total
    (tmp_path / "huge.bin").write_bytes(b"x" * (limits.file_bytes_skip + 1))
    data = {"name": "a", "resources": ["file://.kiro/steering/*.md", "file://huge.bin"]}
    doc = document_from_bytes("a.json", json.dumps(data).encode())
    found = [
        f
        for f in lint_document(doc, config.lint, profile="agent", workspace_root=tmp_path)
        if f.rule == "agent-resource-budget"
    ]
    messages = " | ".join(f.message for f in found)
    assert {f.severity for f in found} == {"warning"}
    assert f"Kiro loads at most {limits.agent_file_resources_max}" in messages
    assert "big.md has" in messages
    assert "huge.bin is" in messages
    assert "total" in messages
    assert not [
        f
        for f in lint_document(doc, config.lint, profile="agent")
        if f.rule == "agent-resource-budget"
    ]
