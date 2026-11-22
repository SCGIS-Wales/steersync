"""The MCP server over the protocol, through the SDK's in-process client."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp import types as mcp_types

from steersync import mcp_server, mcp_tools
from tests.conftest import PROJECT_SLUG

Elicitation = Callable[[Any, mcp_types.ElicitRequestParams], Awaitable[mcp_types.ElicitResult]]


@pytest.fixture
def in_project(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, central: Path
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    return project


def call(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    elicitation: Elicitation | None = None,
    progress: list[tuple[float, float | None, str | None]] | None = None,
) -> mcp_types.CallToolResult:
    async def run() -> mcp_types.CallToolResult:
        async def on_progress(value: float, total: float | None, message: str | None) -> None:
            if progress is not None:
                progress.append((value, total, message))

        client = Client(mcp_server.build_server(), elicitation_callback=elicitation)  # type: ignore[arg-type]
        async with client:
            return await client.call_tool(
                name,
                arguments or {},
                progress_callback=on_progress if progress is not None else None,  # type: ignore[arg-type]
            )

    return asyncio.run(run())


def structured(result: mcp_types.CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    content: dict[str, Any] = result.structured_content
    return content


def test_tools_are_typed_and_annotated() -> None:
    async def run() -> list[mcp_types.Tool]:
        async with Client(mcp_server.build_server()) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(run())}
    assert set(tools) == set(mcp_server.TOOL_SPECS)
    for tool in tools.values():
        assert tool.title
        assert tool.output_schema is not None, tool.name
        assert tool.annotations is not None
    assert tools["validate_kiro_config"].annotations.read_only_hint is True  # type: ignore[union-attr]
    assert tools["push_kiro_config"].annotations.read_only_hint is False  # type: ignore[union-attr]
    assert tools["pull_kiro_config"].annotations.idempotent_hint is True  # type: ignore[union-attr]
    pull_args = tools["pull_kiro_config"].input_schema["properties"]
    assert "approval" not in pull_args
    assert "ctx" not in pull_args
    assert "force" in pull_args
    assert pull_args["collections"]["description"].startswith("Limit to these collections")
    assert "ok" in tools["validate_kiro_config"].output_schema["properties"]  # type: ignore[index]
    assert set(mcp_server.READ_ONLY_TOOLS) >= {"doctor_kiro_config", "kiro_v3_status"}


def test_tools_round_trip(in_project: Path) -> None:
    progress: list[tuple[float, float | None, str | None]] = []
    pulled = structured(call("pull_kiro_config", progress=progress))
    assert pulled["scopes"]["workspace"]["steering"]["plan"]["add"] == ["python.md"]
    assert progress[0][2] == "refreshing the central repository"
    assert progress[-1][2] is not None
    assert progress[-1][2].startswith("applied")
    (in_project / ".kiro" / "steering" / "api.md").write_text("# API\n\nRules.\n")
    diff = structured(call("diff_kiro_config", {"cwd": str(in_project)}))
    assert diff["project"] == PROJECT_SLUG
    assert diff["workspace"]["steering"]["add"] == ["api.md"]
    assert "global" in diff
    lint = structured(call("validate_kiro_config"))
    assert lint["ok"]
    assert lint["checked"] == 2
    pushed = structured(call("push_kiro_config", {"dry_run": True}))
    assert pushed["status"] == "dry-run"
    assert pushed["plan"]["add"] == ["steering/api.md"]
    assert "kiroignore" in pushed["collections"]
    push_progress: list[tuple[float, float | None, str | None]] = []
    done = structured(call("push_kiro_config", {"message": "via mcp"}, progress=push_progress))
    assert done["status"] == "pushed"
    assert [p[0] for p in push_progress] == [0, 1, 2, 3, 4]
    collections = structured(call("list_kiro_collections"))
    assert collections["workspace"] == str(in_project.resolve())
    doctor = structured(call("doctor_kiro_config"))
    assert doctor["project"] == PROJECT_SLUG
    assert doctor["harness"]["cli_path"] is None
    assert "steering" in doctor["directories"]
    assert structured(call("kiro_v3_status"))["needs_reminder"] is False


def test_failures_are_error_results(
    in_project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEERSYNC_REMOTE", str(tmp_path / "missing.git"))
    (in_project / ".kiro" / "steering").mkdir(parents=True)
    (in_project / ".kiro" / "steering" / "x.md").write_text("# X\n")
    for name in ("pull_kiro_config", "diff_kiro_config", "push_kiro_config"):
        result = call(name)
        assert result.is_error, name
        assert result.content
    monkeypatch.chdir(tmp_path)
    result = call("validate_kiro_config")
    assert result.is_error
    text = result.content[0]
    assert isinstance(text, mcp_types.TextContent)
    assert "git repository" in text.text


def test_forced_pull_without_elicitation_needs_confirm(in_project: Path) -> None:
    refused = call("pull_kiro_config", {"force": True})
    assert refused.is_error
    assert "confirm" in str(refused.content)
    assert not call("pull_kiro_config", {"force": True, "dry_run": True}).is_error
    assert not call("pull_kiro_config", {"force": True, "confirm": True}).is_error


def elicit(action: str, overwrite: bool = True) -> tuple[Elicitation, list[str]]:
    asked: list[str] = []

    async def answer(context: Any, params: mcp_types.ElicitRequestParams) -> mcp_types.ElicitResult:
        del context
        asked.append(getattr(params, "message", ""))
        content = {"overwrite": overwrite} if action == "accept" else None
        return mcp_types.ElicitResult(action=action, content=content)  # type: ignore[arg-type]

    return answer, asked


def test_forced_pull_asks_through_elicitation(in_project: Path) -> None:
    accept, asked = elicit("accept")
    assert not call("pull_kiro_config", {"force": True}, elicitation=accept).is_error
    assert "Overwrite" in asked[0]
    unticked, _ = elicit("accept", overwrite=False)
    assert call("pull_kiro_config", {"force": True}, elicitation=unticked).is_error
    decline, asked_again = elicit("decline")
    assert call("pull_kiro_config", {"force": True}, elicitation=decline).is_error
    assert asked_again
    confirmed, never = elicit("accept")
    assert not call(
        "pull_kiro_config", {"force": True, "confirm": True}, elicitation=confirmed
    ).is_error
    assert never == []


def test_progress_bridge_never_fails_the_operation() -> None:
    class Broken:
        async def report_progress(self, *_: object) -> None:
            raise RuntimeError("client went away")

    report = mcp_server._progress_bridge(Broken())  # type: ignore[arg-type]
    report(1, 2, "outside a worker thread")  # no event loop: swallowed


def test_tool_logic_without_the_protocol(in_project: Path) -> None:
    report = mcp_tools.pull(dry_run=True)
    assert report.dry_run
    assert report.scopes["workspace"]["steering"].written == []


def test_unexpanded_placeholders_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kiro passes ${VAR} through literally when the variable is unset or not approved."""
    environ = {
        "STEERSYNC_REMOTE": "${STEERSYNC_REMOTE}",
        "GITLAB_TOKEN": " ${GITLAB_TOKEN} ",
        "HTTPS_PROXY": "http://proxy:3128",
        "NOTE": "costs ${AMOUNT} today",
        "PATH": "/usr/bin",
    }
    assert sorted(mcp_server.drop_unexpanded_placeholders(environ)) == [
        "GITLAB_TOKEN",
        "STEERSYNC_REMOTE",
    ]
    assert environ == {
        "HTTPS_PROXY": "http://proxy:3128",
        "NOTE": "costs ${AMOUNT} today",
        "PATH": "/usr/bin",
    }


def test_serve_drops_placeholders_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    class Fake:
        def run(self, transport: str) -> None:
            started.append(transport)

    monkeypatch.setenv("STEERSYNC_REMOTE", "${STEERSYNC_REMOTE}")
    monkeypatch.setattr(mcp_server, "build_server", Fake)
    mcp_server.serve()
    assert started == ["stdio"]
    import os

    assert "STEERSYNC_REMOTE" not in os.environ
