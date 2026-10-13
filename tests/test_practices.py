"""Best practice warnings: every rule is objective and taken from the Kiro documentation."""

from __future__ import annotations

import json
from pathlib import Path

from steersync import practices
from steersync.config import Config, load_config
from steersync.lint import lint_document, lint_paths, lint_snapshot, lint_workspace
from steersync.steering import STEERING_SPEC, document_from_bytes


def rules(config: Config, name: str, profile: str, text: str | bytes, **kwargs: object) -> set[str]:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    found = lint_document(document_from_bytes(name, raw), config.lint, profile=profile, **kwargs)  # type: ignore[arg-type]
    return {f.rule for f in found}


def test_steering_practices(config: Config) -> None:
    good = "---\ninclusion: always\n---\n# Product\n\nShort.\n"
    assert rules(config, "product.md", "steering", good) == set()
    assert "steering-numeric-prefix" in rules(config, "08-custom-claims.md", "steering", good)
    assert "steering-file-name" in rules(config, "Custom_Claims.md", "steering", good)
    assert "steering-globs-key" in rules(
        config, "tf.md", "steering", "---\ninclusion: fileMatch\nglobs: ['*.tf']\n---\n# T\n"
    )
    assert "steering-filematch-prefix" in rules(
        config,
        "tf.md",
        "steering",
        "---\ninclusion: fileMatch\nfileMatchPattern: ['/src/**/*.tf']\n---\n# T\n",
    )
    assert "steering-auto-description" in rules(
        config, "x.md", "steering", "---\ninclusion: auto\nname: x\ndescription: Rules\n---\n# T\n"
    )
    big = "---\ninclusion: always\n---\n# Big\n\n" + "x" * 20_000 + "\n"
    assert "steering-always-size" in rules(config, "big.md", "steering", big)
    assert "steering-always-size" not in rules(
        config, "big.md", "steering", big.replace("always", "manual")
    )
    assert "steering-document-control-key" in rules(
        config, "x.md", "steering", "---\ninclusion: manual\nowner: me\n---\n# T\n"
    )


def test_steering_collection_practices(config: Config) -> None:
    snapshot = {"product.md": b"# P\n", "tech.md": b"# T\n", "structure.md": b"# S\n"}
    report = lint_snapshot(snapshot, config.lint, profile="steering")
    assert "steering-foundation-missing" not in {f.rule for f in report.findings}
    partial = lint_snapshot({"product.md": b"# P\n"}, config.lint, profile="steering")
    finding = next(f for f in partial.findings if f.rule == "steering-foundation-missing")
    assert "tech.md, structure.md" in finding.message
    assert finding.path == "."
    heavy = {f"rule-{i}.md": b"# R\n\n" + b"y" * 20_000 for i in range(3)}
    heavy.update(snapshot)
    found = {f.rule for f in lint_snapshot(heavy, config.lint, profile="steering").findings}
    assert "steering-always-budget" in found
    assert lint_snapshot({}, config.lint, profile="steering").findings == ()


def test_practices_can_be_disabled(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("lint:\n  practices: false\n")
    off = load_config(env=env, explicit=path)
    assert rules(off, "08-x.md", "steering", "# T\n\nText.\n") == set()
    assert lint_snapshot({"x.md": b"# X\n\nText.\n"}, off.lint, profile="steering").findings == ()


def test_practice_limits_are_configurable(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("lint:\n  practice_limits:\n    always_file_bytes: 1024\n")
    tight = load_config(env=env, explicit=path)
    text = "# T\n\n" + "x" * 1500 + "\n"
    assert "steering-always-size" in rules(tight, "t.md", "steering", text)


def test_agent_practices(config: Config, tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "entra.md").write_text("# Prompt\n")
    good = json.dumps(
        {
            "name": "entra",
            "prompt": "file://./prompts/entra.md",
            "tools": ["read", "write"],
            "resources": ["file://.kiro/steering/product.md", "skill://.kiro/skills/*/SKILL.md"],
            "permissions": {"rules": []},
        }
    )
    assert rules(config, "entra.json", "agent", good, directory=tmp_path) == set()
    bad = json.dumps(
        {
            "name": "entra",
            "prompt": "file://./prompts/missing.md",
            "tools": ["*"],
            "allowedTools": ["*"],
            "resources": ["file://.kiro/steering/**/*.md", "file://src/main/**/*.groovy"],
            "toolsSettings": {},
            "hooks": {"postToolUse": {"matcher": "fs_write", "command": "fmt"}},
        }
    )
    found = rules(config, "entra.json", "agent", bad, directory=tmp_path)
    assert {
        "agent-tools-wildcard",
        "agent-allowed-wildcard",
        "agent-steering-glob",
        "agent-source-glob",
        "agent-prompt-file-missing",
        "agent-hooks-object-form",
        "agent-inline-hooks",
        "agent-permissions-missing",
        "agent-deprecated-key",
    } <= found
    markdown = "---\nname: reviewer\ntools: ['*']\n---\nYou review code.\n"
    assert "agent-tools-wildcard" in rules(config, "reviewer.md", "agent", markdown)
    assert "agent-tools-wildcard" not in rules(config, "prompts/reviewer.md", "agent", markdown)


def test_hook_practices(config: Config) -> None:
    hooks = {
        "version": "v1",
        "hooks": [
            {
                "name": "a",
                "trigger": "Stop",
                "matcher": "x",
                "action": {"type": "command", "command": "t"},
                "timeout": 900,
            },
            {"name": "b", "when": {"type": "fileEdited"}, "then": {"type": "runCommand"}},
        ],
    }
    found = rules(config, "h.json", "hooks", json.dumps(hooks))
    assert {"hook-timeout", "hook-matcher-ignored", "hook-when-then"} <= found
    legacy = rules(config, "fmt.kiro.hook", "hooks", json.dumps({"name": "fmt", "when": {}}))
    assert "hook-legacy-format" in legacy
    assert "hook-schema" not in legacy
    assert "json-syntax" in rules(config, "broken.kiro.hook", "hooks", "{")


def test_skill_practices(config: Config) -> None:
    long_desc = "d" * 1100
    text = f"---\nname: Venafi_Runbook\ndescription: {long_desc}\n---\n# V\n" + "line\n" * 600
    found = rules(config, "venafi/SKILL.md", "skill", text)
    assert {"skill-name-format", "skill-description-length", "skill-body-length"} <= found
    report = lint_snapshot(
        {"SKILL.md": b"---\nname: x\ndescription: y\n---\n# X\n"}, config.lint, profile="skill"
    )
    assert "skill-misplaced" in {f.rule for f in report.findings}


def test_settings_and_prompt_practices(config: Config) -> None:
    mcp = json.dumps(
        {
            "mcpServers": {
                "ok": {"command": "uvx", "args": ["steersync", "mcp"]},
                "local": {"command": "/home/dejan/.local/bin/steersync"},
                "broken": {"args": []},
            }
        }
    )
    found = rules(config, "mcp.json", "settings", mcp)
    assert {"mcp-machine-path", "mcp-server-command"} <= found
    assert "mcp-shape" in rules(config, "mcp.json", "settings", "[]")
    assert "lsp-shape" in rules(config, "lsp.json", "settings", '{"servers": {}}')
    assert rules(config, "lsp.json", "settings", '{"languages": {"groovy": {}}}') == set()
    assert "prompt-name-length" in rules(config, "p" * 60 + ".md", "prompt", "# P\n\nText.\n")
    assert rules(config, "code-review.md", "prompt", "# Review\n\nText.\n") == set()


def test_workspace_autoload_findings(config: Config, tmp_path: Path) -> None:
    report = lint_workspace(
        tmp_path, config.lint, hooks_dir=".kiro/hooks", settings_dir=".kiro/settings"
    )
    assert {f.rule for f in report.findings} == {"autoload-hook-missing", "autoload-mcp-missing"}
    hooks = tmp_path / ".kiro" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "steersync.json").write_text(
        json.dumps(
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "steersync-pull",
                        "trigger": "SessionStart",
                        "action": {"type": "command", "command": "steersync pull --quiet"},
                    }
                ],
            }
        )
    )
    settings = tmp_path / ".kiro" / "settings"
    settings.mkdir()
    (settings / "mcp.json").write_text(json.dumps({"mcpServers": {"steersync": {"command": "x"}}}))
    clean = lint_workspace(
        tmp_path, config.lint, hooks_dir=".kiro/hooks", settings_dir=".kiro/settings"
    )
    assert clean.findings == ()
    (hooks / "broken.json").write_text("{")
    assert (
        practices.workspace_findings(
            tmp_path, hooks_dir=".kiro/hooks", settings_dir=".kiro/settings"
        )
        == []
    )


def test_lint_paths_reports_collection_practices(config: Config, tmp_path: Path) -> None:
    steering = tmp_path / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "product.md").write_text("# P\n\nText.\n")
    report = lint_paths([steering], config.lint, tmp_path, profile="steering", spec=STEERING_SPEC)
    finding = next(f for f in report.findings if f.rule == "steering-foundation-missing")
    assert finding.path == ".kiro/steering"
    ad_hoc = lint_paths([steering], config.lint, tmp_path, profile="steering")
    assert "steering-foundation-missing" not in {f.rule for f in ad_hoc.findings}
