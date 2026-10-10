from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import __version__
from steersync.cli import main
from steersync.config import Config
from tests.conftest import PROJECT_SLUG, CentralCommit, git


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def in_project(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, central: Path
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    return project


def test_version_and_help(runner: CliRunner) -> None:
    assert __version__ in runner.invoke(main, ["--version"]).output
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in (
        "pull",
        "push",
        "status",
        "lint",
        "install-hook",
        "init",
        "kiro-hook",
        "mcp-config",
        "mcp",
        "config",
        "doctor",
    ):
        assert command in result.output


def test_config_commands(runner: CliRunner, in_project: Path) -> None:
    show = runner.invoke(main, ["config", "show"])
    assert show.exit_code == 0
    assert json.loads(show.output)["remote"]["allow_local"] is True
    assert runner.invoke(main, ["config", "validate"]).output.startswith("ok:")
    default = runner.invoke(main, ["config", "default"])
    assert "forbidden_patterns" in default.output


def test_config_validate_failure(
    runner: CliRunner, in_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEERSYNC_REMOTE", "http://insecure.example.com/x.git")
    result = runner.invoke(main, ["config", "validate"])
    assert result.exit_code == 2


def test_config_error_exit_code(runner: CliRunner, in_project: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("remote: [")
    result = runner.invoke(main, ["--config", str(bad), "doctor"])
    assert result.exit_code == 2


def test_doctor(runner: CliRunner, in_project: Path) -> None:
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert f"project slug: {PROJECT_SLUG}" in result.output
    assert "origin: ssh://***@gitlab.example.com" in result.output


def test_doctor_outside_repo(
    runner: CliRunner, tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "not inside a git repository" in result.output
    assert runner.invoke(main, ["lint"]).exit_code == 1
    assert runner.invoke(main, ["init"]).exit_code == 1
    assert runner.invoke(main, ["install-hook"]).exit_code == 1
    assert runner.invoke(main, ["kiro-hook", "--write"]).exit_code == 1
    push = runner.invoke(main, ["push"])
    assert push.exit_code == 1
    assert runner.invoke(main, ["push", "--from-hook"]).exit_code == 0


def test_init_and_install_hook(runner: CliRunner, in_project: Path) -> None:
    result = runner.invoke(main, ["init", "--write-config", "--hook"])
    assert result.exit_code == 0, result.output
    assert (in_project / ".kiro" / "steersync.yaml").exists()
    assert (in_project / ".kiro" / "steering").is_dir()
    assert "/.kiro/steering/" in (in_project / ".git" / "info" / "exclude").read_text()
    assert (in_project / ".git" / "hooks" / "pre-push").exists()
    second = runner.invoke(main, ["init", "--write-config"])
    assert "exists:" in second.output
    assert "unchanged:" in second.output
    hook = runner.invoke(main, ["install-hook"])
    assert "unchanged" in hook.output
    (in_project / ".git" / "hooks" / "pre-push").write_text(
        "#!/bin/sh\n# managed by steersync old\n"
    )
    assert runner.invoke(main, ["install-hook"]).exit_code == 1
    assert "updated" in runner.invoke(main, ["install-hook", "--force"]).output


def test_pull_push_status_round_trip(
    runner: CliRunner, in_project: Path, central: Path, env: dict[str, str]
) -> None:
    pull = runner.invoke(main, ["pull"])
    assert pull.exit_code == 0, pull.output
    assert "+ python.md" in pull.output
    assert "applied 2 change(s)" in pull.output
    (in_project / ".kiro" / "steering" / "api.md").write_text(
        "---\ninclusion: manual\n---\n# API\n\nRules.\n"
    )
    status = runner.invoke(main, ["status"])
    assert status.exit_code == 0
    assert "+ api.md" in status.output
    status_json = runner.invoke(main, ["status", "--json", "--no-lint"])
    assert json.loads(status_json.output)["workspace"]["add"] == ["api.md"]
    push = runner.invoke(main, ["push"])
    assert push.exit_code == 0, push.output
    assert "pushed:" in push.output
    push_json = runner.invoke(main, ["push", "--json"])
    data = json.loads(push_json.output)
    assert data["status"] == "already-proposed"
    assert data["branch"].startswith("steering/")
    assert runner.invoke(main, ["-q", "push", "--dry-run"]).exit_code == 0
    pull_json = runner.invoke(main, ["pull", "--json", "--no-global"])
    assert json.loads(pull_json.output)["scopes"]["workspace"]["plan"]["local_only"] == ["api.md"]
    assert runner.invoke(main, ["--quiet", "pull", "--dry-run"]).exit_code == 0


def test_pull_conflict_message(
    runner: CliRunner, in_project: Path, central: Path, central_commit: CentralCommit
) -> None:
    runner.invoke(main, ["pull"])
    (in_project / ".kiro" / "steering" / "python.md").write_text("# Mine\n")
    central_commit(lambda w: (w / "projects" / PROJECT_SLUG / "python.md").write_text("# Theirs\n"))
    result = runner.invoke(main, ["pull"])
    assert result.exit_code == 0
    assert "locally modified" in result.output
    forced = runner.invoke(main, ["pull", "--force"])
    assert forced.exit_code == 0
    assert (in_project / ".kiro" / "steering" / "python.md").read_text() == "# Theirs\n"


def test_pull_failure_exit_code(
    runner: CliRunner, in_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEERSYNC_REMOTE", str(in_project.parent / "missing.git"))
    result = runner.invoke(main, ["pull"])
    assert result.exit_code == 3
    assert runner.invoke(main, ["status"]).exit_code == 3


def test_push_from_hook_never_blocks(
    runner: CliRunner, in_project: Path, monkeypatch: pytest.MonkeyPatch, central: Path
) -> None:
    (in_project / ".kiro" / "steering").mkdir(parents=True)
    (in_project / ".kiro" / "steering" / "bad.md").write_text("# Bad\n\nAKIAIOSFODNN7EXAMPLE\n")
    result = runner.invoke(
        main, ["push", "--from-hook"], input="refs/heads/main 1 refs/heads/main 2\n"
    )
    assert result.exit_code == 0
    assert "steering sync skipped" in result.output
    strict = runner.invoke(
        main, ["push", "--from-hook", "--strict"], input="refs/heads/main 1 refs/heads/main 2\n"
    )
    assert strict.exit_code == 4
    monkeypatch.setenv("STEERSYNC_STRICT", "1")
    assert runner.invoke(main, ["push", "--from-hook"], input="x 1 y 2\n").exit_code == 4
    monkeypatch.delenv("STEERSYNC_STRICT")
    deletes = runner.invoke(
        main, ["push", "--from-hook"], input=f"(delete) {'0' * 40} refs/heads/old 1\n"
    )
    assert deletes.exit_code == 0
    assert deletes.output == ""
    monkeypatch.setenv("STEERSYNC_ACTIVE", "1")
    assert runner.invoke(main, ["push", "--from-hook"]).output == ""


def test_push_from_hook_uses_pre_commit_remote(
    runner: CliRunner,
    in_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    central: Path,
    env: dict[str, str],
) -> None:
    (in_project / ".kiro" / "steering").mkdir(parents=True)
    (in_project / ".kiro" / "steering" / "ok.md").write_text("# OK\n\nfine\n")
    monkeypatch.setenv("PRE_COMMIT_REMOTE_URL", "https://gitlab.example.com/hooked/repo.git")
    result = runner.invoke(
        main, ["push", "--from-hook"], input="refs/heads/main 1 refs/heads/main 2\n"
    )
    assert result.exit_code == 0, result.output
    branches = git("branch", "--list", "steering/hooked/repo/*", cwd=central, env=env)
    assert "steering/hooked/repo/" in branches


def test_push_bad_config_from_hook(runner: CliRunner, in_project: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("remote: [")
    assert runner.invoke(main, ["--config", str(bad), "push", "--from-hook"]).exit_code == 0
    assert runner.invoke(main, ["--config", str(bad), "push"]).exit_code == 2


def test_lint_command(runner: CliRunner, in_project: Path) -> None:
    steering = in_project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "ok.md").write_text("# OK\n\nfine\n")
    assert runner.invoke(main, ["lint"]).exit_code == 0
    (steering / "bad.md").write_text("---\ninclusion: nope\n---\n# Bad\n")
    result = runner.invoke(main, ["lint"])
    assert result.exit_code == 4
    assert "[inclusion-value]" in result.output
    as_json = runner.invoke(main, ["lint", "--json", str(steering / "bad.md")])
    assert json.loads(as_json.output)["errors"] == 1
    assert runner.invoke(main, ["-q", "lint"]).exit_code == 4


def test_kiro_hook_and_mcp_config(runner: CliRunner, in_project: Path) -> None:
    v3 = runner.invoke(main, ["kiro-hook"])
    assert json.loads(v3.output)["hooks"][0]["trigger"] == "SessionStart"
    v2 = runner.invoke(main, ["kiro-hook", "--engine", "v2"])
    assert "agentSpawn" in json.loads(v2.output)["hooks"]
    written = runner.invoke(main, ["kiro-hook", "--write"])
    assert written.exit_code == 0
    assert (in_project / ".kiro" / "hooks" / "steersync.json").exists()
    assert runner.invoke(main, ["kiro-hook", "--engine", "v2", "--write"]).exit_code == 1
    mcp = runner.invoke(
        main, ["mcp-config", "--remote", "git@h:a/b.git", "--command", "/usr/local/bin/uvx"]
    )
    data = json.loads(mcp.output)["mcpServers"]["steersync"]
    assert data["command"] == "/usr/local/bin/uvx"
    assert data["env"]["STEERSYNC_REMOTE"] == "git@h:a/b.git"


def test_mcp_command_reports_missing_extra(
    runner: CliRunner, in_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from steersync import mcp_server
    from steersync.errors import SteersyncError

    def boom() -> None:
        raise SteersyncError("needs extra")

    monkeypatch.setattr(mcp_server, "serve", boom)
    assert runner.invoke(main, ["mcp"]).exit_code == 1


def test_json_logging_option(
    runner: CliRunner, in_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEERSYNC_REMOTE", str(in_project.parent / "missing.git"))
    result = runner.invoke(main, ["--log-format", "json", "--log-level", "debug", "pull"])
    assert result.exit_code == 3
    assert '"level": "ERROR"' in result.output


def test_config_fixture_type(config: Config) -> None:
    assert config.remote.allow_local is True
