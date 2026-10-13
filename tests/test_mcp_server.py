from __future__ import annotations

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
        "prompts",
        "agents-md",
    ]
    assert mcp_server.push_kiro_config(message="via mcp")["status"] == "pushed"


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
    }
