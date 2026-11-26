"""The Kiro V3 baseline check: kiro-cli version, agent engine and legacy agent files."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import kiro_version
from steersync.config import Config
from steersync.kiro_version import parse_setting, parse_version

LEGACY = Config().lint.legacy_agent_keys


def fake_runner(outputs: dict[str, tuple[int, str] | None]):  # type: ignore[no-untyped-def]
    def run(args: Sequence[str]) -> tuple[int, str] | None:
        return outputs.get(" ".join(args[1:]))

    return run


def which_found(_: str) -> str:
    return "/usr/local/bin/kiro-cli"


def which_missing(_: str) -> None:
    return None


def detect(env: Mapping[str, str], **kwargs: object) -> kiro_version.KiroStatus:
    return kiro_version.detect(
        env,
        min_cli_version="3.1.0",
        min_ide_version="1.4.86",
        legacy_keys=LEGACY,
        **kwargs,  # type: ignore[arg-type]
    )


def test_parsers() -> None:
    assert parse_version("kiro-cli 3.5.0") == (3, 5, 0)
    assert parse_version("unknown") is None
    assert parse_setting('"v3"') == "v3"
    assert parse_setting("v3 (global)") == "v3"
    assert parse_setting("true") is True
    assert parse_setting("false (workspace)") is False
    assert parse_setting("") is None
    assert parse_setting("[1]") is None


def test_supported_v3_cli(tmp_path: Path) -> None:
    run = fake_runner(
        {
            "--version": (0, "kiro-cli 3.5.0"),
            "settings -f json chat.agentEngine": (0, '"v3"'),
            "settings -f json chat.enableWorkflows": (0, "true"),
        }
    )
    status = detect({"HOME": str(tmp_path)}, run=run, which=which_found)
    assert status.cli_version == "3.5.0"
    assert status.engine == "v3"
    assert status.engine_source == "kiro-cli"
    assert status.workflows_enabled is True
    assert status.supported
    assert status.to_dict()["message"] is None


def test_old_cli_other_engine_and_legacy_agents(tmp_path: Path) -> None:
    agents = tmp_path / "ws" / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    (agents / "md.md").write_text("---\nname: md\ntoolAliases: {}\n---\nBody\n")
    (agents / "ok.json").write_text(json.dumps({"name": "ok", "tools": ["read"]}))
    (agents / "broken.json").write_text("{")
    (agents / "notes.txt").write_text("x")
    run = fake_runner(
        {
            "--version": (0, "kiro-cli 3.0.4"),
            "settings -f json chat.agentEngine": (0, '"v2"'),
        }
    )
    status = detect({"HOME": str(tmp_path)}, agent_dirs=[agents], run=run, which=which_found)
    assert not status.supported
    assert [Path(p).name for p in status.legacy_agents] == ["md.md", "old.json"]
    text = kiro_version.problem_text(status)
    assert "kiro-cli 3.0.4 is installed; steersync needs 3.1.0 or later" in text
    assert "Upgrade kiro-cli to 3.1.0 or later" in text
    assert "kiro-cli settings chat.agentEngine v3" in text
    assert "/upgrade-agent" in text
    assert "Kiro IDE 1.4.86 or later" in text


def test_settings_file_fallback_and_default(tmp_path: Path) -> None:
    settings = tmp_path / "kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "cli.json").write_text(json.dumps({"chat": {"agentEngine": "v1"}}))
    env = {"HOME": str(tmp_path), "KIRO_HOME": str(tmp_path / "kiro")}
    failing = fake_runner({"--version": (0, "kiro-cli 3.1.0")})
    status = detect(env, run=failing, which=which_found)
    assert status.engine == "v1"
    assert status.engine_source == "settings-file"
    assert "only the V3 harness" in status.problems[0]
    (settings / "cli.json").write_text("{}")
    default = detect(env, run=failing, which=which_found)
    assert default.engine is None
    assert default.engine_source == "default"
    assert default.supported


def test_missing_cli_is_ide_only(tmp_path: Path) -> None:
    status = detect({"HOME": str(tmp_path)}, which=which_missing)
    assert status.cli_path is None
    assert status.supported


def test_default_runner_handles_missing_binary() -> None:
    assert kiro_version.default_runner(["/nonexistent/kiro-cli", "--version"]) is None
    code, out = kiro_version.default_runner(["git", "--version"]) or (1, "")
    assert code == 0
    assert "git" in out


def test_check_rate_limit(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert kiro_version.check_due(tmp_path, env, now=1_000_000)
    assert not kiro_version.check_due(tmp_path, env, now=1_000_100)
    assert kiro_version.check_due(tmp_path, env, now=1_000_000 + 86_400)
    assert not kiro_version.check_due(tmp_path / "x", {"STEERSYNC_NO_KIRO_CHECK": "1"})
    blocked = tmp_path / "file"
    blocked.write_text("")
    assert not kiro_version.check_due(blocked / "sub", env, now=5)


def test_minimum_versions_come_from_config(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from steersync.cli import main

    monkeypatch.chdir(project)
    config = tmp_path / "c.yaml"
    config.write_text(
        "remote:\n  url: git@gitlab.example.com:acme/kiro-config.git\n"
        'kiro:\n  min_cli_version: "9.0.0"\n'
    )
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config))
    # git stands in for kiro-cli: `git --version` reports 2.x, below the 9.0.0 minimum.
    monkeypatch.setattr(kiro_version, "CLI_BINARY", "git")
    result = CliRunner().invoke(main, ["doctor", "--kiro", "--json"])
    data = json.loads(result.stdout)
    assert data["min_cli_version"] == "9.0.0"
    assert data["supported"] is False
    bad = tmp_path / "bad.yaml"
    bad.write_text('kiro:\n  min_cli_version: "3.1"\n')
    monkeypatch.setenv("STEERSYNC_CONFIG", str(bad))
    assert CliRunner().invoke(main, ["doctor", "--kiro"]).exit_code == 2


def test_doctor_kiro_strict_and_doctor(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from steersync.cli import main

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "toolsSettings": {}}))
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--kiro", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["supported"] is False
    assert data["legacy_agents"][0].endswith("old.json")
    strict = runner.invoke(main, ["doctor", "--kiro", "--strict"])
    assert strict.exit_code == 10
    assert "kiro-cli: not found" in strict.stdout
    assert "/upgrade-agent" in strict.stderr
    (agents / "old.json").write_text(json.dumps({"name": "old", "permissions": {"rules": []}}))
    assert runner.invoke(main, ["doctor", "--kiro", "--strict"]).exit_code == 0
    doctor = runner.invoke(main, ["doctor"])
    assert "kiro: supported" in doctor.stdout
    assert "supported: Kiro CLI 3.1.0+ (V3), Kiro IDE 1.4.86+" in doctor.stdout


def test_removed_commands_are_gone() -> None:
    from steersync.cli import main

    for command in ("v3-status", "migrate-hooks"):
        assert CliRunner().invoke(main, [command]).exit_code == 2


def test_pull_hook_reports_once_a_day(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from steersync.cli import main

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    monkeypatch.setenv("STEERSYNC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("STEERSYNC_NO_KIRO_CHECK", raising=False)
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    runner = CliRunner()
    first = runner.invoke(main, ["pull", "--quiet", "--best-effort"])
    assert first.exit_code == 0
    assert "below the version steersync supports" in first.stderr
    second = runner.invoke(main, ["pull", "--quiet", "--best-effort"])
    assert "below the version" not in second.stderr


def test_mcp_tool(project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from steersync import mcp_tools

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    status = mcp_tools.kiro_check()
    assert status.cli_path is None
    assert status.legacy_agents[0].endswith("old.json")
    assert status.supported is False
    monkeypatch.setenv("STEERSYNC_CONFIG", str(project / "missing.yaml"))
    assert mcp_tools.kiro_check().legacy_agents == []


def test_kiro_lines_mention_disabled_workflows(tmp_path: Path) -> None:
    from steersync.cli import _kiro_lines

    run = fake_runner({"--version": (0, "kiro-cli 3.5.0")})
    status = detect({"HOME": str(tmp_path)}, run=run, which=which_found)
    assert status.workflows_enabled is None
    assert any("chat.enableWorkflows true" in line for line in _kiro_lines(status))
    on = fake_runner(
        {"--version": (0, "kiro-cli 3.5.0"), "settings -f json chat.enableWorkflows": (0, "true")}
    )
    enabled = detect({"HOME": str(tmp_path)}, run=on, which=which_found)
    assert not any("workflows" in line for line in _kiro_lines(enabled))


def test_problem_text_lists_only_what_applies(tmp_path: Path) -> None:
    agents = tmp_path / "ws" / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "old.json").write_text(json.dumps({"name": "old", "allowedTools": ["read"]}))
    on_v3 = fake_runner(
        {"--version": (0, "kiro-cli 3.5.0"), "settings -f json chat.agentEngine": (0, '"v3"')}
    )
    status = detect({"HOME": str(tmp_path)}, agent_dirs=[agents], run=on_v3, which=which_found)
    text = kiro_version.problem_text(status)
    assert "chat.agentEngine v3" not in text
    assert "Upgrade kiro-cli" not in text
    assert "/upgrade-agent" in text
