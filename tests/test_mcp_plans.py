"""MCP plan and fix tools: propose, one approval, stale plans; lint JSON and SARIF output."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from mcp import types as mcp_types

from steersync.cli import main
from tests.conftest import git
from tests.test_mcp_server import Elicitation, call, structured


@pytest.fixture
def in_project(
    project: Path, config_file: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "api.md").write_bytes(
        b"\xef\xbb\xbf---\ninclusion: Always\n---\n\n# API\n\nRules.\n"
    )
    (steering / "py.md").write_text(
        '---\ninclusion: fileMatch\nfileMatchPattern: "src/**, tests/**"\n---\n\n# Py\n\nTypes.\n'
    )
    git("add", "-A", cwd=project, env=env)
    git("commit", "-qm", "kiro", cwd=project, env=env)
    return project


def answer(content: dict[str, Any] | None, action: str = "accept") -> tuple[Elicitation, list[Any]]:
    asked: list[Any] = []

    async def reply(context: Any, params: mcp_types.ElicitRequestParams) -> mcp_types.ElicitResult:
        asked.append(params)
        return mcp_types.ElicitResult(action=action, content=content)  # type: ignore[arg-type]

    return reply, asked


# --- fixes ---------------------------------------------------------------------------------------


def test_propose_then_apply_fixes_with_one_question(in_project: Path) -> None:
    proposal = structured(call("propose_fixes"))
    assert [f["path"] for f in proposal["fixes"]] == [".kiro/steering/api.md"]
    assert [o["rule"] for o in proposal["offers"]] == ["fm-filematch-comma"]
    choice = proposal["offers"][0]["choice"]
    reply, asked = answer({"apply_safe": True, "unsafe": [choice]})
    applied = structured(call("apply_fixes", {"plan_id": proposal["plan_id"]}, elicitation=reply))
    assert len(asked) == 1
    assert sorted(applied["applied"]) == [".kiro/steering/api.md", ".kiro/steering/py.md"]
    assert applied["undo"].startswith("steersync restore ")
    py = (in_project / ".kiro" / "steering" / "py.md").read_text(encoding="utf-8")
    assert 'fileMatchPattern: ["src/**", "tests/**"]' in py


def test_fixes_without_a_way_to_ask_need_confirm(in_project: Path) -> None:
    proposal = structured(call("propose_fixes"))
    refused = call("apply_fixes", {"plan_id": proposal["plan_id"]})
    assert refused.is_error
    assert "confirm=true" in str(refused.content)
    assert (in_project / ".kiro" / "steering" / "api.md").read_bytes().startswith(b"\xef\xbb\xbf")
    confirmed = structured(call("apply_fixes", {"plan_id": proposal["plan_id"], "confirm": True}))
    assert confirmed["applied"] == [".kiro/steering/api.md"]
    declined, _ = answer(None, "decline")
    again = structured(call("propose_fixes"))
    assert call("apply_fixes", {"plan_id": again["plan_id"]}, elicitation=declined).is_error


def test_stale_fix_plan_is_refused(in_project: Path) -> None:
    proposal = structured(call("propose_fixes"))
    api = in_project / ".kiro" / "steering" / "api.md"
    api.write_bytes(api.read_bytes().replace(b"Rules.", b"More rules."))
    stale = call("apply_fixes", {"plan_id": proposal["plan_id"], "confirm": True})
    assert stale.is_error
    assert "stale_plan" in str(stale.content)
    assert call("apply_fixes", {"plan_id": "nope", "confirm": True}).is_error


# --- plans -----------------------------------------------------------------------------------------


def test_plan_setup_then_apply(in_project: Path) -> None:
    plan = structured(call("plan_setup"))
    ids = {s["id"]: s for s in plan["steps"]}
    assert {"config.save", "mcp.register", "hook.session", "repo.exclude"} <= set(ids)
    reply, asked = answer({"approve": True, "steps": ["hook.session"]})
    applied = structured(call("apply_plan", {"plan_id": plan["plan_id"]}, elicitation=reply))
    assert len(asked) == 1
    assert applied["applied"] == ["hook.session"]
    assert applied["results"][0]["ok"] is True
    # The machine changed: the old plan_id is stale now.
    stale = call("apply_plan", {"plan_id": plan["plan_id"], "confirm": True})
    assert stale.is_error
    assert "stale_plan" in str(stale.content)
    no, _ = answer({"approve": False, "steps": []})
    fresh = structured(call("plan_setup"))
    assert call("apply_plan", {"plan_id": fresh["plan_id"]}, elicitation=no).is_error
    confirmed = structured(
        call(
            "apply_plan",
            {"plan_id": fresh["plan_id"], "confirm": True, "step_ids": ["repo.exclude"]},
        )
    )
    assert confirmed["applied"] == ["repo.exclude"]
    unknown = call("apply_plan", {"plan_id": fresh["plan_id"], "confirm": True, "step_ids": ["x"]})
    assert unknown.is_error


def test_plan_fleet(fleet_env: dict[str, Path]) -> None:
    plan = structured(call("plan_fleet", {"action": "push", "states": ["local-ahead"]}))
    assert [s["id"].split(":")[0] for s in plan["steps"]] == ["push"]
    applied = structured(call("apply_plan", {"plan_id": plan["plan_id"], "confirm": True}))
    assert applied["results"][0]["ok"] is True
    assert applied["backup_id"] is None  # pushing changes no local file


def test_new_tools_are_registered_and_read_only_where_they_should_be() -> None:
    from steersync import hooks, mcp_server

    assert {"plan_setup", "plan_fleet", "propose_fixes"} <= set(mcp_server.READ_ONLY_TOOLS)
    assert "apply_plan" not in mcp_server.READ_ONLY_TOOLS
    assert "apply_fixes" not in mcp_server.READ_ONLY_TOOLS
    assert "propose_fixes" in hooks.AUTO_APPROVE_TOOLS


# --- lint output -----------------------------------------------------------------------------------


def test_lint_json_and_sarif(in_project: Path) -> None:
    import jsonschema

    document = json.loads(CliRunner().invoke(main, ["lint", "--format", "json"]).stdout)
    assert document["version"] == 1
    assert document["summary"]["errors"] == document["errors"]
    fixed = json.loads(CliRunner().invoke(main, ["lint", "--diff", "--format", "json"]).stdout)
    assert fixed["fixes"][0]["path"] == ".kiro/steering/api.md"
    assert fixed["fixes"][0]["applied"] is False
    sarif = json.loads(CliRunner().invoke(main, ["lint", "--diff", "--format", "sarif"]).stdout)
    schema = json.loads(
        (Path(__file__).parent / "data" / "sarif-2.1.0.schema.json").read_text(encoding="utf-8")
    )
    # The schema's own draft, without its metaschema check: one SARIF pattern uses an
    # inline (?i) flag that Python's re rejects, in a property steersync never writes.
    jsonschema.Draft7Validator(schema).validate(sarif)
    run = sarif["runs"][0]
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert {"file-bom", "fm-filematch-comma"} <= rule_ids
    with_fix = [r for r in run["results"] if "fixes" in r]
    assert with_fix
    assert with_fix[0]["fixes"][0]["properties"]["safety"] == "safe"
