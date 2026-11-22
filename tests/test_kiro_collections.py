"""The collections added for Kiro IDE 1.5 / CLI 3.5: workflows, sandbox, kiroignore,
nested AGENTS.md, opt in cli.json, whole spec directories and agent backups."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from steersync import sync
from steersync.config import CollectionConfig, Config, load_config
from steersync.errors import ConfigError
from steersync.steering import FileSpec, discover_files
from tests.conftest import CentralCommit, central_project

WORKFLOW = {
    "name": "release",
    "steps": [{"type": "step", "id": "s", "agent": "default", "prompt": "Ship it"}],
}


def rel(directory: Path, spec: FileSpec) -> list[str]:
    return [p.relative_to(directory).as_posix() for p in discover_files(directory, spec)]


def test_hidden_files_need_a_literal_pattern(tmp_path: Path) -> None:
    (tmp_path / ".kiroignore").write_text("x\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / ".kiroignore").write_text("y\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / ".kiroignore").write_text("z\n")
    assert rel(tmp_path, FileSpec(("**/.kiroignore",))) == [".kiroignore", "pkg/.kiroignore"]
    assert rel(tmp_path, FileSpec(("**/*",))) == []


def test_excluded_trees_are_not_walked(tmp_path: Path) -> None:
    for folder in ("node_modules/pkg", "src/node_modules/x", "src/app", "build"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "AGENTS.md").write_text("# A\n")
    (tmp_path / "AGENTS.md").write_text("# Root\n")
    spec = FileSpec(("**/AGENTS.md",), ("**/node_modules/**", "**/build/**"))
    assert rel(tmp_path, spec) == ["AGENTS.md", "src/app/AGENTS.md"]


def test_collections_share_a_directory_only_with_disjoint_literal_names(
    env: dict[str, str], tmp_path: Path
) -> None:
    defaults = load_config(env=env)
    shared = [n for n, c in defaults.collections.items() if c.workspace_path == ".kiro/settings"]
    assert shared == ["settings", "sandbox", "cli-settings"]
    enabled = tmp_path / "c.yaml"
    enabled.write_text("collections:\n  cli-settings:\n    enabled: true\n")
    assert "cli-settings" in load_config(env=env, explicit=enabled).enabled_collections()
    clash = tmp_path / "clash.yaml"
    clash.write_text(
        "collections:\n  extra:\n    workspace_path: .kiro/settings\n    include: ['mcp.json']\n"
    )
    with pytest.raises(ConfigError, match="share the same workspace_path"):
        load_config(env=env, explicit=clash)
    wildcard = tmp_path / "wild.yaml"
    wildcard.write_text(
        "collections:\n  extra:\n    workspace_path: .kiro/settings\n    include: ['*.json']\n"
    )
    with pytest.raises(ConfigError, match="share the same workspace_path"):
        load_config(env=env, explicit=wildcard)
    assert CollectionConfig(workspace_path=".", include=("**/*.md",)).literal_file_names() is None
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "collections": {
                    "a": {"workspace_path": "x", "central_dir": "same", "include": ["a.json"]},
                    "b": {"workspace_path": "x", "central_dir": "same", "include": ["b.json"]},
                }
            }
        )


def seed(work: Path) -> None:
    workflows = central_project(work, "workflows")
    workflows.mkdir(parents=True)
    (workflows / "release.workflow.json").write_text(json.dumps(WORKFLOW))
    sandbox = central_project(work, "sandbox")
    sandbox.mkdir(parents=True)
    (sandbox / "sandbox.json").write_text('{"enabled": true, "networkAccess": "DEFAULT_BLOCKED"}')
    ignore = central_project(work, "kiroignore")
    (ignore / "services" / "billing").mkdir(parents=True)
    (ignore / ".kiroignore").write_text(".env*\n*.pem\n")
    (ignore / "services" / "billing" / ".kiroignore").write_text("fixtures/\n")
    agents_md = central_project(work, "agents-md")
    (agents_md / "services" / "billing").mkdir(parents=True)
    (agents_md / "AGENTS.md").write_text("# Root\n\nRules.\n")
    (agents_md / "services" / "billing" / "AGENTS.md").write_text("# Billing\n\nRules.\n")
    specs = central_project(work, "specs") / "tls"
    specs.mkdir(parents=True)
    (specs / "bugfix.md").write_text("# Bug\n\nDetails.\n")
    (specs / ".config.kiro").write_text("hidden metadata stays out\n")
    (specs / "spec.json").write_text('{"workflow": "bugfix"}\n')


def test_pull_and_push_new_collections(
    config: Config, env: dict[str, str], project: Path, central_commit: CentralCommit
) -> None:
    central_commit(seed, "seed kiro 3.5 collections")
    result = sync.pull(config, workspace_root=project, env=env)
    by_key = {(r.scope, r.collection): r for r in result.results}
    assert by_key["workspace", "workflows"].plan.add == ("release.workflow.json",)
    assert by_key["workspace", "sandbox"].plan.add == ("sandbox.json",)
    assert by_key["workspace", "kiroignore"].plan.add == (
        ".kiroignore",
        "services/billing/.kiroignore",
    )
    assert by_key["workspace", "agents-md"].plan.add == (
        "AGENTS.md",
        "services/billing/AGENTS.md",
    )
    assert by_key["workspace", "specs"].plan.add == ("tls/bugfix.md", "tls/spec.json")
    assert ("workspace", "cli-settings") not in by_key
    assert (project / "services" / "billing" / ".kiroignore").read_text() == "fixtures/\n"
    assert (project / ".kiro" / "settings" / "sandbox.json").exists()
    # The settings collection shares .kiro/settings without touching sandbox.json.
    assert by_key["workspace", "settings"].plan.delete == ()

    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "a.json").write_text('{"name": "a", "tools": ["read"]}\n')
    (agents / "a.json.bak").write_text('{"name": "a", "allowedTools": ["*"]}\n')
    (project / "node_modules" / "dep").mkdir(parents=True)
    (project / "node_modules" / "dep" / "AGENTS.md").write_text("# Vendored\n")
    pushed = sync.push(config, workspace_root=project, env=env, dry_run=True)
    assert pushed.plan is not None
    assert "agents/a.json" in pushed.plan.add
    assert not any(path.endswith(".bak") for path in pushed.plan.add)
    assert not any("node_modules" in path for path in pushed.plan.add)


def test_lint_explicit_paths_outside_the_workspace(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from steersync.cli import main

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    outside = tmp_path / "home-agents"
    outside.mkdir()
    (outside / "old.json").write_text('{"name": "old", "allowedTools": ["read"]}')
    (outside / "old.json.bak").write_text("{")
    runner = CliRunner()
    by_profile = runner.invoke(main, ["lint", "--json", "--profile", "agent", str(outside)])
    data = json.loads(by_profile.stdout)
    assert data["checked"] == 1
    assert {f["rule"] for f in data["findings"]} == {"agent-v2-format"}
    single = runner.invoke(main, ["lint", "--json", str(outside / "old.json")])
    assert json.loads(single.stdout)["checked"] == 1
