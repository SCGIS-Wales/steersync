"""Kiro CLI V3 harness detection and the reminder."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from steersync import kiro_version
from steersync.kiro_version import detect_harness, parse_setting, parse_version


def fake_runner(outputs: dict[str, tuple[int, str] | None]):  # type: ignore[no-untyped-def]
    def run(args: Sequence[str]) -> tuple[int, str] | None:
        return outputs.get(" ".join(args[1:]))

    return run


def which_found(_: str) -> str:
    return "/usr/local/bin/kiro-cli"


def which_missing(_: str) -> None:
    return None


def test_parsers() -> None:
    assert parse_version("kiro-cli 3.5.0") == (3, 5, 0)
    assert parse_version("unknown") is None
    assert parse_setting('"v3"') == "v3"
    assert parse_setting("v3 (global)") == "v3"
    assert parse_setting("true") is True
    assert parse_setting("false (workspace)") is False
    assert parse_setting("") is None
    assert parse_setting("[1]") is None


def test_v3_cli_needs_no_reminder(tmp_path: Path) -> None:
    run = fake_runner(
        {
            "--version": (0, "kiro-cli 3.5.0"),
            "settings -f json chat.agentEngine": (0, '"v3"'),
            "settings -f json chat.enableAutoAgentUpgrade": (0, "true"),
            "settings -f json chat.enableWorkflows": (0, "true"),
        }
    )
    status = detect_harness({"HOME": str(tmp_path)}, run=run, which=which_found)
    assert status.cli_version == "3.5.0"
    assert status.engine == "v3"
    assert status.engine_source == "kiro-cli"
    assert status.auto_upgrade is True
    assert status.workflows_enabled is True
    assert status.on_v3
    assert not status.needs_reminder
    assert status.to_dict()["message"] is None


def test_v2_engine_old_cli_and_v2_agents(tmp_path: Path) -> None:
    agents = tmp_path / "ws" / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    (agents / "md.md").write_text("---\nname: md\nhooks:\n  agentSpawn: []\n---\nBody\n")
    (agents / "ok.json").write_text(json.dumps({"name": "ok", "tools": ["read"]}))
    (agents / "broken.json").write_text("{")
    (agents / "notes.txt").write_text("x")
    run = fake_runner(
        {
            "--version": (0, "kiro-cli 2.37.0"),
            "settings -f json chat.agentEngine": (0, '"v2"'),
            "settings -f json chat.enableAutoAgentUpgrade": (0, "false"),
            "settings -f json acp.agentEngine": (0, '"v2"'),
        }
    )
    status = detect_harness(
        {"HOME": str(tmp_path)}, agent_dirs=[agents], run=run, which=which_found
    )
    assert status.acp_engine == "v2"
    assert any("acp.agentEngine" in reason for reason in status.reasons)
    assert "kiro-cli settings acp.agentEngine v3" in kiro_version.reminder_text(status)
    assert not status.on_v3
    assert status.needs_reminder
    assert [Path(p).name for p in status.v2_agents] == ["md.md", "old.json"]
    text = kiro_version.reminder_text(status)
    assert "predates 3.0.0" in text
    assert "kiro-cli settings chat.agentEngine v3" in text
    assert "/upgrade-agent" in text
    assert f"kiro-cli agent upgrade {agents}" in text
    assert "automatic agent upgrade is off" in text
    assert "Update kiro-cli" in text


def test_settings_file_fallback_and_default(tmp_path: Path) -> None:
    settings = tmp_path / "kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "cli.json").write_text(json.dumps({"chat": {"agentEngine": "v1"}}))
    env = {"HOME": str(tmp_path), "KIRO_HOME": str(tmp_path / "kiro")}
    failing = fake_runner({"--version": (0, "kiro-cli 3.1.0")})
    status = detect_harness(env, run=failing, which=which_found)
    assert status.engine == "v1"
    assert status.engine_source == "settings-file"
    assert "Classic" in status.reasons[0]
    (settings / "cli.json").write_text("{}")
    default = detect_harness(env, run=failing, which=which_found)
    assert default.engine is None
    assert default.engine_source == "default"
    assert default.on_v3


def test_missing_cli_is_ide_only(tmp_path: Path) -> None:
    status = detect_harness({"HOME": str(tmp_path)}, which=which_missing)
    assert status.cli_path is None
    assert not status.on_v3
    assert not status.needs_reminder


def test_default_runner_handles_missing_binary() -> None:
    assert kiro_version.default_runner(["/nonexistent/kiro-cli", "--version"]) is None
    code, out = kiro_version.default_runner(["git", "--version"]) or (1, "")
    assert code == 0
    assert "git" in out


def test_reminder_rate_limit(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert kiro_version.reminder_due(tmp_path, env, now=1_000_000)
    assert not kiro_version.reminder_due(tmp_path, env, now=1_000_100)
    assert kiro_version.reminder_due(tmp_path, env, now=1_000_000 + 86_400)
    assert not kiro_version.reminder_due(tmp_path / "x", {"STEERSYNC_NO_V3_REMINDER": "1"})
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert not kiro_version.reminder_due(blocked / "sub", env, now=5)


def test_cli_v3_status_and_doctor(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from steersync.cli import main

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "toolsSettings": {}}))
    runner = CliRunner()
    result = runner.invoke(main, ["v3-status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["needs_reminder"] is True
    assert data["v2_agents"][0].endswith("old.json")
    strict = runner.invoke(main, ["v3-status", "--strict"])
    assert strict.exit_code == 10
    assert "kiro-cli: not found" in strict.stdout
    assert "/upgrade-agent" in strict.stderr
    doctor = runner.invoke(main, ["doctor"])
    assert "V3 harness: no" in doctor.stdout
    assert "V2 format agent" in doctor.stdout


def test_pull_hook_prints_reminder_once(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from steersync.cli import main

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    monkeypatch.setenv("STEERSYNC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("STEERSYNC_NO_V3_REMINDER")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    runner = CliRunner()
    first = runner.invoke(main, ["pull", "--quiet", "--best-effort"])
    assert first.exit_code == 0
    assert "Kiro CLI V3" in first.stderr
    second = runner.invoke(main, ["pull", "--quiet", "--best-effort"])
    assert "Kiro CLI V3" not in second.stderr


def test_mcp_tool(project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from steersync import mcp_tools

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    status = mcp_tools.v3_status()
    assert status.cli_path is None
    assert status.v2_agents[0].endswith("old.json")
    monkeypatch.setenv("STEERSYNC_CONFIG", str(project / "missing.yaml"))
    assert mcp_tools.v3_status().v2_agents == []


def test_harness_lines_mention_disabled_workflows(tmp_path: Path) -> None:
    from steersync.cli import _harness_lines

    run = fake_runner({"--version": (0, "kiro-cli 3.5.0")})
    status = detect_harness({"HOME": str(tmp_path)}, run=run, which=which_found)
    assert status.workflows_enabled is None
    assert any("chat.enableWorkflows true" in line for line in _harness_lines(status))
    on = fake_runner(
        {"--version": (0, "kiro-cli 3.5.0"), "settings -f json chat.enableWorkflows": (0, "true")}
    )
    enabled = detect_harness({"HOME": str(tmp_path)}, run=on, which=which_found)
    assert not any("workflows" in line for line in _harness_lines(enabled))


def test_reminder_lists_only_the_steps_that_apply(tmp_path: Path) -> None:
    agents = tmp_path / "ws" / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    on_v3 = fake_runner(
        {"--version": (0, "kiro-cli 3.5.0"), "settings -f json chat.agentEngine": (0, '"v3"')}
    )
    status = detect_harness(
        {"HOME": str(tmp_path)}, agent_dirs=[agents], run=on_v3, which=which_found
    )
    text = kiro_version.reminder_text(status)
    assert "chat.agentEngine v3" not in text
    assert f"kiro-cli agent upgrade {agents}" in text
