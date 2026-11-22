from pathlib import Path

import pytest

from steersync import mcp_server
from tests.conftest import PROJECT_SLUG


@pytest.fixture
def in_project(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, central: Path
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    return project


def test_tools_round_trip(in_project: Path) -> None:
    pulled = mcp_server.pull_kiro_config()
    assert pulled["ok"]
    assert pulled["scopes"]["workspace"]["steering"]["plan"]["add"] == ["python.md"]
    (in_project / ".kiro" / "steering" / "api.md").write_text("# API\n\nRules.\n")
    diff = mcp_server.diff_kiro_config(str(in_project))
    assert diff["ok"]
    assert diff["project"] == PROJECT_SLUG
    assert diff["workspace"]["steering"]["add"] == ["api.md"]
    lint = mcp_server.validate_kiro_config()
    assert lint["ok"]
    assert lint["checked"] == 2
    pushed = mcp_server.push_kiro_config(dry_run=True)
    assert pushed["ok"]
    assert pushed["status"] == "dry-run"
    assert pushed["plan"]["add"] == ["steering/api.md"]
    assert pushed["collections"] == [
        "steering",
        "specs",
        "skills",
        "agents",
        "hooks",
        "settings",
        "sandbox",
        "workflows",
        "prompts",
        "agents-md",
        "kiroignore",
    ]
    assert mcp_server.push_kiro_config(message="via mcp")["status"] == "pushed"


def test_force_pull_needs_confirmation(in_project: Path) -> None:
    refused = mcp_server.pull_kiro_config(force=True)
    assert refused["ok"] is False
    assert "confirm" in refused["error"]
    assert mcp_server.pull_kiro_config(force=True, dry_run=True)["ok"]
    assert mcp_server.pull_kiro_config(force=True, confirm=True)["ok"]


def test_tools_report_errors(
    in_project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEERSYNC_REMOTE", str(tmp_path / "missing.git"))
    (in_project / ".kiro" / "steering").mkdir(parents=True)
    (in_project / ".kiro" / "steering" / "x.md").write_text("# X\n")
    assert mcp_server.pull_kiro_config()["ok"] is False
    assert mcp_server.diff_kiro_config()["ok"] is False
    assert mcp_server.push_kiro_config()["ok"] is False
    monkeypatch.chdir(tmp_path)
    assert "git repository" in mcp_server.push_kiro_config()["error"]
    assert "git repository" in mcp_server.validate_kiro_config()["error"]


def test_build_server_registers_tools(in_project: Path) -> None:
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == {
        "pull_kiro_config",
        "push_kiro_config",
        "raise_merge_request",
        "diff_kiro_config",
        "validate_kiro_config",
        "list_kiro_collections",
        "kiro_v3_status",
    }
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    assert tools["validate_kiro_config"].annotations.read_only_hint is True
    assert tools["push_kiro_config"].annotations.read_only_hint is False
    assert tools["pull_kiro_config"].annotations.idempotent_hint is True
    assert tools["kiro_v3_status"].title == "Kiro CLI V3 status"
