"""Tests for syncing several collections (steering, specs, skills, agents, hooks, settings)."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import mcp_server, sync
from steersync.cli import main
from steersync.config import Config, load_config
from steersync.errors import ConfigError, LintError
from steersync.paths import (
    global_collection_dir,
    global_lock_path,
    kiro_home,
    workspace_collection_dir,
    workspace_lock_path,
)
from steersync.steering import FileSpec, discover_files, read_lock
from tests.conftest import (
    AGENT_DOC,
    HOOK_DOC,
    PROJECT_SLUG,
    SKILL_DOC,
    CentralCommit,
    central_global,
    central_project,
    git,
)


def seed_collections(work: Path) -> None:
    """Add one file per non steering collection to the central project and global dirs."""
    specs = central_project(work, "specs") / "tls-rotation"
    specs.mkdir(parents=True)
    (specs / "design.md").write_text("# Design\n\nStaged rotation.\n")
    (specs / "tasks.md").write_text("# Tasks\n\n- [ ] one\n")
    skill = central_project(work, "skills") / "venafi-tpp-websdk"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_DOC)
    script = skill / "scripts" / "renew.sh"
    script.write_text("#!/bin/sh\necho renew\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (skill / "references").mkdir()
    (skill / "references" / "runbook.md").write_text("# Runbook\n\nFull body.\n")
    agents = central_project(work, "agents")
    (agents / "prompts").mkdir(parents=True)
    (agents / "entra.json").write_text(AGENT_DOC)
    (agents / "prompts" / "entra.md").write_text("# Prompt\n\nYou develop Entra resources.\n")
    hooks = central_project(work, "hooks")
    hooks.mkdir(parents=True)
    (hooks / "tests-on-stop.json").write_text(HOOK_DOC)
    settings = central_project(work, "settings")
    settings.mkdir(parents=True)
    (settings / "lsp.json").write_text('{"languages": {"groovy": {"command": "groovy-lsp"}}}\n')
    (settings / "groovy-classpath.txt").write_text("not synced\n")
    global_skill = central_global(work, "skills") / "shared-skill"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text(
        "---\nname: shared-skill\ndescription: Shared\n---\n# Shared\n\nBody.\n"
    )
    global_hooks = central_global(work, "hooks")
    global_hooks.mkdir(parents=True)
    (global_hooks / "steersync.json").write_text(HOOK_DOC)


def test_pull_applies_every_collection(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    central_commit(seed_collections, "seed collections")
    result = sync.pull(config, workspace_root=project, env=env)
    by_key = {(r.scope, r.collection): r for r in result.results}
    assert by_key["workspace", "specs"].plan.add == (
        "tls-rotation/design.md",
        "tls-rotation/tasks.md",
    )
    assert by_key["workspace", "skills"].plan.add == (
        "venafi-tpp-websdk/SKILL.md",
        "venafi-tpp-websdk/references/runbook.md",
        "venafi-tpp-websdk/scripts/renew.sh",
    )
    assert by_key["workspace", "agents"].plan.add == ("entra.json", "prompts/entra.md")
    assert by_key["workspace", "hooks"].plan.add == ("tests-on-stop.json",)
    assert by_key["workspace", "settings"].plan.add == ("lsp.json",)
    assert by_key["global", "skills"].plan.add == ("shared-skill/SKILL.md",)
    assert by_key["global", "hooks"].plan.add == ("steersync.json",)
    assert ("global", "specs") not in by_key
    assert ("global", "settings") not in by_key
    kiro = project / ".kiro"
    assert (kiro / "specs" / "tls-rotation" / "design.md").exists()
    assert (kiro / "settings" / "lsp.json").exists()
    assert not (kiro / "settings" / "groovy-classpath.txt").exists()
    script = kiro / "skills" / "venafi-tpp-websdk" / "scripts" / "renew.sh"
    assert script.stat().st_mode & stat.S_IXUSR
    assert (kiro / "skills" / "venafi-tpp-websdk" / "SKILL.md").stat().st_mode & 0o777 == 0o644
    assert (kiro_home(env) / "skills" / "shared-skill" / "SKILL.md").exists()
    assert (kiro_home(env) / "hooks" / "steersync.json").exists()
    lock = read_lock(workspace_lock_path(config, project))
    assert set(lock["collections"]) == {
        "steering",
        "specs",
        "skills",
        "agents",
        "hooks",
        "settings",
        "prompts",
        "agents-md",
    }
    assert "venafi-tpp-websdk/scripts/renew.sh" in lock["collections"]["skills"]["files"]
    global_lock = read_lock(global_lock_path(config, env))
    assert set(global_lock["collections"]) == {"steering", "skills", "agents", "hooks", "prompts"}
    assert sync.pull(config, workspace_root=project, env=env).change_count == 0


def test_pull_can_be_limited_to_collections(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    central_commit(seed_collections, "seed collections")
    result = sync.pull(config, workspace_root=project, env=env, collections=["specs", "hooks"])
    assert {(r.scope, r.collection) for r in result.results} == {
        ("workspace", "specs"),
        ("workspace", "hooks"),
        ("global", "hooks"),
    }
    assert not (project / ".kiro" / "steering").exists()
    lock = read_lock(workspace_lock_path(config, project))
    assert set(lock["collections"]) == {"specs", "hooks"}
    with pytest.raises(ConfigError, match="unknown collection"):
        sync.pull(config, workspace_root=project, env=env, collections=["nope"])


def test_pull_keeps_lock_entries_of_other_collections(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    central_commit(seed_collections, "seed collections")
    sync.pull(config, workspace_root=project, env=env)
    (project / ".kiro" / "specs" / "tls-rotation" / "design.md").write_text("# Mine\n")
    central_commit(
        lambda w: (central_project(w, "specs") / "tls-rotation" / "design.md").write_text(
            "# Theirs\n"
        ),
        "edit",
    )
    only_steering = sync.pull(config, workspace_root=project, env=env, collections=["steering"])
    assert only_steering.change_count == 0
    lock = read_lock(workspace_lock_path(config, project))
    assert "tls-rotation/design.md" in lock["collections"]["specs"]["files"]
    result = sync.pull(config, workspace_root=project, env=env, collections=["specs"])
    assert result.conflicts == ("workspace:specs:tls-rotation/design.md",)
    assert (project / ".kiro" / "specs" / "tls-rotation" / "design.md").read_text() == "# Mine\n"


def test_push_proposes_every_collection_in_one_branch(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    kiro = project / ".kiro"
    (kiro / "steering").mkdir(parents=True)
    (kiro / "steering" / "api.md").write_text("# API\n\nREST.\n")
    (kiro / "specs" / "mfa").mkdir(parents=True)
    (kiro / "specs" / "mfa" / "tasks.md").write_text("# Tasks\n\n- [ ] stamp\n")
    skill = kiro / "skills" / "venafi-tpp-websdk"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_DOC)
    script = skill / "scripts" / "renew.sh"
    script.write_text("#!/bin/sh\necho renew\n")
    script.chmod(0o755)
    (kiro / "hooks").mkdir()
    (kiro / "hooks" / "tests-on-stop.json").write_text(HOOK_DOC)
    (kiro / "settings").mkdir()
    (kiro / "settings" / "lsp.json").write_text('{"languages": {}}\n')
    (kiro / "settings" / "groovy-classpath.txt").write_text("ignored\n")

    result = sync.push(config, workspace_root=project, env=env)
    assert result.status == "pushed", result.message
    assert result.plan is not None
    assert result.plan.add == (
        "hooks/tests-on-stop.json",
        "settings/lsp.json",
        "skills/venafi-tpp-websdk/SKILL.md",
        "skills/venafi-tpp-websdk/scripts/renew.sh",
        "specs/mfa/tasks.md",
        "steering/api.md",
    )
    assert result.plan.delete == ("steering/python.md",)
    assert result.branch is not None
    tree = git(
        "ls-tree", "-r", result.branch, "--", f"projects/{PROJECT_SLUG}", cwd=central, env=env
    )
    assert "100755 blob" in tree
    assert "scripts/renew.sh" in tree
    assert "groovy-classpath.txt" not in tree
    assert "python.md" not in tree
    assert sync.push(config, workspace_root=project, env=env).status == "already-proposed"

    (kiro / "steering" / "api.md").write_text("# API\n\nGraphQL.\n")
    limited = sync.push(config, workspace_root=project, env=env, collections=["specs"])
    assert limited.status == "pushed"
    assert limited.collections == ("specs",)
    assert limited.plan is not None
    assert limited.plan.add == ("specs/mfa/tasks.md",)
    assert limited.branch != result.branch
    steering_only = sync.push(config, workspace_root=project, env=env, collections=["steering"])
    assert steering_only.status == "pushed"
    assert steering_only.plan is not None
    # The first proposal is still unmerged, so the default branch only has python.md.
    assert steering_only.plan.add == ("steering/api.md",)
    assert steering_only.plan.delete == ("steering/python.md",)
    assert steering_only.branch is not None
    tree = git("ls-tree", "-r", "--name-only", steering_only.branch, cwd=central, env=env)
    assert "specs/mfa/tasks.md" not in tree


def test_push_lint_uses_each_collection_profile(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    kiro = project / ".kiro"
    (kiro / "hooks").mkdir(parents=True)
    (kiro / "hooks" / "broken.json").write_text("{not json")
    with pytest.raises(LintError, match="lint error"):
        sync.push(config, workspace_root=project, env=env)
    (kiro / "hooks" / "broken.json").write_text(HOOK_DOC)
    (kiro / "skills" / "bad").mkdir(parents=True)
    (kiro / "skills" / "bad" / "SKILL.md").write_text("# No front matter\n")
    with pytest.raises(LintError):
        sync.push(config, workspace_root=project, env=env)
    assert sync.push(config, workspace_root=project, env=env, collections=["hooks"]).status == (
        "pushed"
    )


def test_push_reports_nothing_for_empty_collections(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    result = sync.push(config, workspace_root=project, env=env, collections=["specs", "hooks"])
    assert result.status == "nothing-to-push"
    assert result.collections == ("specs", "hooks")


def test_status_reports_every_collection(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    central_commit(seed_collections, "seed collections")
    (project / ".kiro" / "hooks").mkdir(parents=True)
    (project / ".kiro" / "hooks" / "fmt.json").write_text(HOOK_DOC)
    result = sync.status(config, workspace_root=project, env=env)
    assert result.workspace is not None
    assert set(result.workspace) == {
        "steering",
        "specs",
        "skills",
        "agents",
        "hooks",
        "settings",
        "prompts",
        "agents-md",
    }
    assert result.workspace["hooks"].add == ("fmt.json",)
    assert result.workspace["hooks"].delete == ("tests-on-stop.json",)
    assert set(result.global_) == {"steering", "skills", "agents", "hooks", "prompts"}
    assert result.global_["skills"].add == ("shared-skill/SKILL.md",)
    assert result.lint is not None
    assert result.lint.checked == 1
    limited = sync.status(config, workspace_root=project, env=env, collections=["specs"])
    assert limited.workspace is not None
    assert set(limited.workspace) == {"specs"}
    assert limited.global_ == {}


def test_legacy_flat_layout_is_reported(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    central_commit: CentralCommit,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def flatten(work: Path) -> None:
        git("mv", "global/steering/rules.md", "global/rules.md", cwd=work, env=env)

    central_commit(flatten, "legacy layout")
    with caplog.at_level(logging.WARNING, logger="steersync.sync"):
        result = sync.pull(config, workspace_root=None, env=env)
    assert result.change_count == 0
    assert any("move them to steering/" in record.message for record in caplog.records)


def test_version_one_lock_is_upgraded_on_pull(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    first = sync.pull(config, workspace_root=project, env=env, include_global=False)
    lock_path = workspace_lock_path(config, project)
    data = json.loads(lock_path.read_text())
    legacy = {
        "version": 1,
        "remote": data["remote"],
        "commit": data["commit"],
        "files": data["collections"]["steering"]["files"],
    }
    lock_path.write_text(json.dumps(legacy))
    central_commit(lambda w: (central_project(w) / "python.md").write_text("# Theirs\n"), "edit")
    result = sync.pull(config, workspace_root=project, env=env, include_global=False)
    steering = next(r for r in result.results if r.collection == "steering")
    assert steering.plan.update == ("python.md",)
    assert not result.conflicts
    assert json.loads(lock_path.read_text())["version"] == 2
    assert first.commit != result.commit


def test_custom_collection_and_disabled_defaults(
    env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit, tmp_path: Path
) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\n"
        "collections:\n"
        "  specs:\n    enabled: false\n"
        "  skills:\n    enabled: false\n"
        "  agents:\n    enabled: false\n"
        "  hooks:\n    enabled: false\n"
        "  settings:\n    enabled: false\n"
        "  prompts:\n    enabled: false\n"
        "  agents-md:\n"
        "    description: Root AGENTS.md\n"
        "    workspace_path: .\n"
        "    include: ['AGENTS.md']\n"
        "    max_depth: 1\n"
        "    lint: markdown\n"
        "    git_exclude: false\n"
    )
    config = load_config(env=env, explicit=path)
    assert list(config.enabled_collections()) == ["steering", "agents-md"]
    assert config.collections["agents-md"].central_dir == "agents-md"

    def seed(work: Path) -> None:
        target = central_project(work, "agents-md")
        target.mkdir(parents=True)
        (target / "AGENTS.md").write_text("# Agents\n\nRules for every surface.\n")
        (target / "ignored.md").write_text("# Ignored\n")

    central_commit(seed, "agents.md")
    result = sync.pull(config, workspace_root=project, env=env)
    assert {(r.scope, r.collection) for r in result.results} == {
        ("global", "steering"),
        ("workspace", "steering"),
        ("workspace", "agents-md"),
    }
    assert (project / "AGENTS.md").read_text().startswith("# Agents")
    assert not (project / "ignored.md").exists()
    (project / "AGENTS.md").write_text("# Agents\n\nChanged locally.\n")
    pushed = sync.push(config, workspace_root=project, env=env, collections=["agents-md"])
    assert pushed.status == "pushed"
    assert pushed.plan is not None
    assert pushed.plan.update == ("agents-md/AGENTS.md",)
    # Files the collection's patterns do not select are never touched centrally.
    assert pushed.plan.delete == ()
    assert pushed.branch is not None
    tree = git("ls-tree", "-r", "--name-only", pushed.branch, cwd=central, env=env)
    assert f"projects/{PROJECT_SLUG}/agents-md/ignored.md" in tree


def test_discover_files_patterns_and_depth(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "lsp.json").write_text("{}")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep").mkdir()
    (tmp_path / "nested" / "b.json").write_text("{}")
    (tmp_path / "nested" / "deep" / "c.json").write_text("{}")
    (tmp_path / "nested" / "deep" / "c.md").write_text("# C")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.json").write_text("{}")

    def names(spec: FileSpec) -> list[str]:
        return [p.relative_to(tmp_path).as_posix() for p in discover_files(tmp_path, spec)]

    assert names(FileSpec(("**/*.json",))) == [
        "__pycache__/x.json",
        "a.json",
        "lsp.json",
        "nested/b.json",
        "nested/deep/c.json",
    ]
    assert names(FileSpec(("**/*.json",), ("**/__pycache__/**",))) == [
        "a.json",
        "lsp.json",
        "nested/b.json",
        "nested/deep/c.json",
    ]
    assert names(FileSpec(("lsp.json",))) == ["lsp.json"]
    assert names(FileSpec(("*.json",))) == ["a.json", "lsp.json"]
    assert names(FileSpec(("**/*",), max_depth=1)) == ["a.json", "lsp.json"]
    assert names(FileSpec(("**/*",), max_depth=2)) == [
        "__pycache__/x.json",
        "a.json",
        "lsp.json",
        "nested/b.json",
    ]
    assert names(FileSpec(("**/*.md",))) == ["nested/deep/c.md"]


def test_cli_collection_options(
    project: Path,
    config_file: Path,
    central: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    runner = CliRunner()
    central_commit(seed_collections, "seed collections")
    listing = runner.invoke(main, ["config", "collections"])
    assert listing.exit_code == 0, listing.output
    assert "skills: Agent Skills" in listing.output
    assert "global:    (none)" in listing.output
    as_json = runner.invoke(main, ["config", "collections", "--json"])
    rows = {row["name"]: row for row in json.loads(as_json.output)}
    assert rows["settings"]["global_dir"] is None
    assert rows["skills"]["lint"] == "skill"

    pull = runner.invoke(main, ["pull", "-c", "skills", "-c", "specs"])
    assert pull.exit_code == 0, pull.output
    assert "workspace/skills" in pull.output
    assert "+ venafi-tpp-websdk/SKILL.md" in pull.output
    assert "workspace/steering" not in pull.output
    bad = runner.invoke(main, ["pull", "--collection", "nope"])
    assert bad.exit_code == 2
    assert "unknown collection" in bad.output

    status = runner.invoke(main, ["status", "--json", "-c", "hooks"])
    data = json.loads(status.output)
    assert list(data["workspace"]) == ["hooks"]
    assert list(data["global"]) == ["hooks"]
    assert data["lint"]["checked"] == 0

    (project / ".kiro" / "hooks").mkdir()
    (project / ".kiro" / "hooks" / "fmt.json").write_text(HOOK_DOC)
    push = runner.invoke(main, ["push", "--json", "-c", "hooks", "--dry-run"])
    assert push.exit_code == 0, push.output
    pushed = json.loads(push.output)
    assert pushed["collections"] == ["hooks"]
    assert pushed["plan"]["add"] == ["hooks/fmt.json"]

    doctor = runner.invoke(main, ["doctor"])
    assert "collections: steering, specs, skills, agents, hooks, settings, prompts" in doctor.output
    assert "workspace skills:" in doctor.output
    assert "global hooks:" in doctor.output


def test_cli_lint_covers_collections(
    project: Path, config_file: Path, central: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    runner = CliRunner()
    kiro = project / ".kiro"
    (kiro / "steering").mkdir(parents=True)
    (kiro / "steering" / "ok.md").write_text("# OK\n\nfine\n")
    (kiro / "hooks").mkdir()
    (kiro / "hooks" / "bad.json").write_text('{"version": "v1", "hooks": [{"name": "x"}]}\n')
    result = runner.invoke(main, ["lint"])
    assert result.exit_code == 4
    assert ".kiro/hooks/bad.json" in result.output
    assert "[hook-schema]" in result.output
    assert runner.invoke(main, ["lint", "-c", "steering"]).exit_code == 0
    assert runner.invoke(main, ["lint", "-c", "hooks"]).exit_code == 4
    # Explicit paths pick the profile of the collection that contains them (pre-commit).
    explicit = runner.invoke(main, ["lint", "--json", str(kiro / "hooks")])
    assert json.loads(explicit.output)["errors"] == 2
    explicit = runner.invoke(
        main, ["lint", "--json", str(kiro / "hooks" / "bad.json"), str(kiro / "steering" / "ok.md")]
    )
    data = json.loads(explicit.output)
    assert data["checked"] == 2
    assert {f["rule"] for f in data["findings"]} == {"hook-schema"}
    explicit = runner.invoke(main, ["lint", "--json", "-c", "hooks", str(kiro / "hooks")])
    assert json.loads(explicit.output)["errors"] == 2
    outside = project / "README.md"
    explicit = runner.invoke(main, ["lint", "--json", str(outside)])
    assert json.loads(explicit.output)["checked"] == 1
    explicit = runner.invoke(
        main, ["lint", "--json", "--profile", "json", str(kiro / "hooks" / "bad.json")]
    )
    assert json.loads(explicit.output)["errors"] == 0
    assert runner.invoke(main, ["lint", "-c", "nope"]).exit_code == 2


def test_mcp_tools_accept_collections(
    project: Path,
    config_file: Path,
    central: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    central_commit(seed_collections, "seed collections")
    pulled = mcp_server.pull_kiro_config(collections=["skills"])
    assert pulled["ok"], pulled
    assert list(pulled["scopes"]["workspace"]) == ["skills"]
    assert list(pulled["scopes"]["global"]) == ["skills"]
    diff = mcp_server.diff_kiro_config(collections=["specs"])
    assert diff["ok"]
    assert list(diff["workspace"]) == ["specs"]
    assert diff["global"] == {}
    lint = mcp_server.validate_kiro_config(collections=["skills"])
    assert lint["ok"]
    assert lint["checked"] == 3
    assert mcp_server.validate_kiro_config(collections=["nope"])["ok"] is False
    pushed = mcp_server.push_kiro_config(dry_run=True, collections=["skills"])
    assert pushed["ok"]
    assert pushed["status"] == "up-to-date"
    assert pushed["collections"] == ["skills"]


def test_path_helpers(config: Config, env: dict[str, str], tmp_path: Path) -> None:
    settings = config.collections["settings"]
    assert global_collection_dir(settings, env) is None
    assert workspace_collection_dir(settings, tmp_path) == tmp_path / ".kiro" / "settings"
    skills = config.collections["skills"]
    assert global_collection_dir(skills, env) == kiro_home(env) / "skills"
