"""Converting legacy hooks into standalone V3 hook files."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import kiro_rules, migrate
from steersync.cli import main


def test_convert_legacy_hook() -> None:
    data = {
        "name": "Lint on save",
        "description": "Run the linter",
        "enabled": True,
        "when": {"type": "fileEdited", "patterns": ["*.py", "*.pyi"]},
        "then": {"type": "askAgent", "prompt": "Lint the file"},
    }
    hooks, skipped = migrate.convert_legacy_hook(data, "lint")
    assert skipped == []
    assert [h["matcher"] for h in hooks] == ["*.py", "*.pyi"]
    assert hooks[0]["trigger"] == "PostFileSave"
    assert hooks[0]["action"] == {"type": "agent", "prompt": "Lint the file"}
    assert hooks[0]["name"] == "lint-on-save-1"
    single, _ = migrate.convert_legacy_hook(
        {"when": {"type": "agentStop"}, "then": {"type": "runCommand", "command": "make"}}, "stop"
    )
    assert single == [
        {"name": "stop", "trigger": "Stop", "action": {"type": "command", "command": "make"}}
    ]
    assert migrate.convert_legacy_hook({"when": {"type": "x"}}, "n")[1]
    assert migrate.convert_legacy_hook({"when": {"type": "manual"}, "then": {}}, "n")[1]
    for hook in hooks + single:
        assert kiro_rules.hook_entry_issues(hook, "h", standalone=True) == []


def test_convert_agent_hooks() -> None:
    hooks, skipped = migrate.convert_agent_hooks(
        {
            "agentSpawn": [{"command": "steersync pull", "timeout_ms": 1500}],
            "preToolUse": {"matcher": "fs_write", "command": "fmt"},
            "stop": [{"nothing": True}],
        },
        "Entra Agent",
    )
    assert hooks[0] == {
        "name": "entra-agent-sessionstart-1",
        "trigger": "SessionStart",
        "action": {"type": "command", "command": "steersync pull"},
        "timeout": 2,
    }
    assert hooks[1]["matcher"] == "fs_write"
    assert hooks[1]["trigger"] == "PreToolUse"
    assert skipped == ["Entra Agent: hooks.stop[0] has no command"]


@pytest.fixture
def legacy_workspace(project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    hooks = project / ".kiro" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "fmt.kiro.hook").write_text(
        json.dumps(
            {
                "name": "fmt",
                "when": {"type": "fileCreated", "patterns": ["*.ts"]},
                "then": {"type": "runCommand", "command": "prettier -w"},
            }
        )
    )
    (hooks / "broken.kiro.hook").write_text("{")
    (hooks / "odd.kiro.hook").write_text(json.dumps({"when": {"type": "nope"}}))
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "entra.json").write_text(
        json.dumps({"name": "entra", "hooks": {"agentSpawn": [{"command": "echo hi"}]}})
    )
    (agents / "plain.json").write_text(json.dumps({"name": "plain"}))
    return project


def test_cli_migrate_hooks(legacy_workspace: Path) -> None:
    runner = CliRunner()
    dry = runner.invoke(main, ["migrate-hooks"])
    assert dry.exit_code == 0
    assert "would write: .kiro/hooks/fmt.json" in dry.stdout
    assert "would write: .kiro/hooks/agent-entra.json" in dry.stdout
    assert "dry run" in dry.stdout
    assert not (legacy_workspace / ".kiro" / "hooks" / "fmt.json").exists()
    written = runner.invoke(main, ["migrate-hooks", "--write"])
    assert written.exit_code == 0
    assert "written: .kiro/hooks/fmt.json" in written.stdout
    assert "standalone hooks apply to every agent" in written.stderr
    assert "not valid JSON" in written.stderr
    data = json.loads((legacy_workspace / ".kiro" / "hooks" / "fmt.json").read_text())
    assert kiro_rules.hook_file_issues(data) == []
    again = runner.invoke(main, ["migrate-hooks", "--write"])
    assert "exists: .kiro/hooks/fmt.json" in again.stdout
    lint = runner.invoke(main, ["lint", "-c", "hooks", "--json"])
    assert json.loads(lint.stdout)["errors"] == 1  # only the broken legacy file


def test_cli_migrate_hooks_nothing(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    assert "no legacy hooks" in CliRunner().invoke(main, ["migrate-hooks"]).stdout
