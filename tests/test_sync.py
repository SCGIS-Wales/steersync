from __future__ import annotations

import json
from pathlib import Path

import pytest

from steersync import gitops, paths, sync
from steersync.config import Config, load_config
from steersync.errors import ConfigError, LintError, SyncConflictError
from steersync.paths import global_lock_path, global_steering_dir, workspace_lock_path
from tests.conftest import (
    GLOBAL_DOC,
    PROJECT_DOC,
    PROJECT_SLUG,
    CentralCommit,
    central_global,
    central_project,
    git,
)


def test_pull_applies_global_and_workspace(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    result = sync.pull(config, workspace_root=project, env=env)
    assert {r.scope for r in result.results} == {"global", "workspace"}
    assert {r.collection for r in result.results if r.scope == "global"} == {
        "steering",
        "skills",
        "agents",
        "hooks",
        "prompts",
    }
    assert (global_steering_dir(config, env) / "rules.md").read_text() == GLOBAL_DOC
    assert (project / ".kiro" / "steering" / "python.md").read_text() == PROJECT_DOC
    assert result.change_count == 2
    assert not result.conflicts
    lock = json.loads(workspace_lock_path(config, project).read_text())
    assert lock["commit"] == result.commit
    assert "python.md" in lock["collections"]["steering"]["files"]
    assert lock["collections"]["specs"]["files"] == {}
    global_lock = json.loads(global_lock_path(config, env).read_text())
    assert global_lock["collections"]["steering"]["files"].keys() == {"rules.md"}
    again = sync.pull(config, workspace_root=project, env=env)
    assert again.change_count == 0


def test_pull_dry_run_and_scopes(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    result = sync.pull(config, workspace_root=project, env=env, dry_run=True, include_global=False)
    assert {r.scope for r in result.results} == {"workspace"}
    assert result.results[0].collection == "steering"
    assert result.results[0].plan.add == ("python.md",)
    assert not (project / ".kiro" / "steering").exists()
    assert not workspace_lock_path(config, project).exists()


def test_pull_without_workspace_or_origin(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    result = sync.pull(config, workspace_root=None, env=env)
    assert {r.scope for r in result.results} == {"global"}
    git("remote", "remove", "origin", cwd=project, env=env)
    result = sync.pull(config, workspace_root=project, env=env, include_global=False)
    assert result.results == ()
    with_url = sync.pull(
        config,
        workspace_root=project,
        env=env,
        include_global=False,
        remote_url="git@h:acme/platform/api.git",
    )
    assert with_url.results[0].plan.add == ("python.md",)


def test_pull_keeps_local_edits_unless_forced(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    sync.pull(config, workspace_root=project, env=env)
    local = project / ".kiro" / "steering" / "python.md"
    local.write_text("# Mine\n")
    central_commit(lambda w: (central_project(w) / "python.md").write_text("# Theirs\n"))
    result = sync.pull(config, workspace_root=project, env=env, include_global=False)
    assert result.conflicts == ("workspace:steering:python.md",)
    assert local.read_text() == "# Mine\n"
    with pytest.raises(SyncConflictError):
        sync.raise_on_conflicts(result)
    again = sync.pull(config, workspace_root=project, env=env, include_global=False)
    assert again.conflicts == ("workspace:steering:python.md",)
    forced = sync.pull(config, workspace_root=project, env=env, include_global=False, force=True)
    assert forced.conflicts == ("workspace:steering:python.md",)
    assert local.read_text() == "# Theirs\n"
    assert (
        sync.pull(config, workspace_root=project, env=env, include_global=False).change_count == 0
    )
    sync.raise_on_conflicts(
        forced if not forced.conflicts else sync.pull(config, workspace_root=project, env=env)
    )


def test_pull_prunes_removed_files_and_updates(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    central_commit: CentralCommit,
    tmp_path: Path,
) -> None:
    sync.pull(config, workspace_root=project, env=env)
    local_only = project / ".kiro" / "steering" / "local.md"
    local_only.write_text("# Local\n")

    def mutate(work: Path) -> None:
        (central_project(work) / "python.md").unlink()
        (central_project(work) / "team" / "style.md").parent.mkdir(parents=True)
        (central_project(work) / "team" / "style.md").write_text("# Style\n")
        (central_global(work) / "rules.md").write_text("# Global rules\n\nUpdated.\n")

    central_commit(mutate, "restructure")
    result = sync.pull(config, workspace_root=project, env=env)
    ws = next(r for r in result.results if r.scope == "workspace" and r.collection == "steering")
    assert ws.plan.delete == ("python.md",)
    assert ws.plan.add == ("team/style.md",)
    assert ws.plan.local_only == ("local.md",)
    assert not (project / ".kiro" / "steering" / "python.md").exists()
    assert (project / ".kiro" / "steering" / "team" / "style.md").exists()
    assert local_only.exists()
    gl = next(r for r in result.results if r.scope == "global" and r.collection == "steering")
    assert gl.plan.update == ("rules.md",)
    no_prune_file = tmp_path / "np.yaml"
    no_prune_file.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\npaths:\n  cache_dir: {tmp_path / 'cache'}\nsync:\n  prune: false\n"
    )
    no_prune = load_config(env=env, explicit=no_prune_file)
    central_commit(lambda w: (central_project(w) / "team" / "style.md").unlink(), "drop")
    result = sync.pull(no_prune, workspace_root=project, env=env, include_global=False)
    assert result.results[0].plan.delete == ()
    assert (project / ".kiro" / "steering" / "team" / "style.md").exists()


def test_pull_strict_rejects_bad_remote_content(
    config: Config, env: dict[str, str], project: Path, central: Path, central_commit: CentralCommit
) -> None:
    central_commit(
        lambda w: (central_global(w) / "bad.md").write_text("---\ninclusion: nope\n---\n# Bad\n")
    )
    result = sync.pull(config, workspace_root=None, env=env)
    assert not result.results[0].lint.ok
    assert (global_steering_dir(config, env) / "bad.md").exists()
    with pytest.raises(LintError):
        sync.pull(config, workspace_root=None, env=env, strict=True)


def test_pull_rejects_invalid_remote(env: dict[str, str], tmp_path: Path) -> None:
    config = load_config(env={**env, "STEERSYNC_REMOTE": "ext::evil"})
    with pytest.raises(ConfigError):
        sync.pull(config, workspace_root=None, env=env)


def test_push_creates_branch_then_dedupes(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "api.md").write_text("---\ninclusion: always\n---\n# API\n\nUse REST.\n")
    dry = sync.push(config, workspace_root=project, env=env, dry_run=True)
    assert dry.status == "dry-run"
    assert dry.branch
    assert dry.plan is not None
    assert dry.plan.add == ("steering/api.md",)
    git_env = gitops.git_environment(env)
    assert not gitops.remote_branch_exists(config, str(central), dry.branch, env=git_env)

    pushed = sync.push(config, workspace_root=project, env=env)
    assert pushed.status == "pushed"
    assert pushed.branch == dry.branch
    assert pushed.commit
    assert pushed.branch.startswith(f"kiro-config/{PROJECT_SLUG}/tester-")
    assert pushed.existing_branches == ()
    assert pushed.merge_request is None
    assert gitops.remote_branch_exists(config, str(central), pushed.branch, env=git_env)
    assert "push options not accepted" in pushed.message
    show = git("show", "--stat", "--format=%an <%ae>%n%s", pushed.branch, cwd=central, env=env)
    assert "Test User <test.user@example.com>" in show
    assert f"kiro-config: update {PROJECT_SLUG} from tester" in show
    assert f"projects/{PROJECT_SLUG}/steering/api.md" in show
    assert "python.md" not in git(
        "ls-tree",
        "-r",
        "--name-only",
        pushed.branch,
        "--",
        f"projects/{PROJECT_SLUG}",
        cwd=central,
        env=env,
    )
    assert pushed.collections == (
        "steering",
        "specs",
        "skills",
        "agents",
        "hooks",
        "settings",
        "prompts",
        "agents-md",
    )
    assert pushed.plan is not None
    assert pushed.plan.add == ("steering/api.md",)

    again = sync.push(config, workspace_root=project, env=env)
    assert again.status == "already-proposed"
    assert again.branch == pushed.branch

    (steering / "api.md").write_text("---\ninclusion: always\n---\n# API\n\nUse GraphQL.\n")
    second = sync.push(config, workspace_root=project, env=env, message="custom message")
    assert second.status == "pushed"
    assert second.branch is not None
    assert second.branch != pushed.branch
    assert git("log", "-1", "--format=%s", second.branch, cwd=central, env=env) == "custom message"
    assert (
        git(
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            cwd=paths.cache_repo_dir(config, env, str(central)),
            env=env,
        )
        == "main"
    )


def test_push_reports_up_to_date_and_nothing(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    assert sync.push(config, workspace_root=project, env=env).status == "nothing-to-push"
    sync.pull(config, workspace_root=project, env=env, include_global=False)
    result = sync.push(config, workspace_root=project, env=env)
    assert result.status == "up-to-date"
    assert result.plan is not None
    assert result.plan.is_empty


def test_push_global_scope(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    target = global_steering_dir(config, env)
    target.mkdir(parents=True)
    (target / "rules.md").write_text("# Global rules\n\nChanged.\n")
    result = sync.push(config, workspace_root=project, env=env, scope="global")
    assert result.status == "pushed"
    assert result.project == "global"
    assert result.branch
    assert result.branch.startswith("kiro-config/global/")
    assert "global/steering/rules.md" in git("show", "--stat", result.branch, cwd=central, env=env)


def test_push_lint_gate(config: Config, env: dict[str, str], project: Path, central: Path) -> None:
    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "bad.md").write_text("# Bad\n\nAKIAIOSFODNN7EXAMPLE\n")
    with pytest.raises(LintError):
        sync.push(config, workspace_root=project, env=env)
    assert sync.push(config, workspace_root=project, env=env, run_lint=False).status == "pushed"


def test_push_skips_central_repository_itself(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    work = tmp_path / "central-work"
    (work / ".kiro" / "steering").mkdir(parents=True)
    (work / ".kiro" / "steering" / "x.md").write_text("# X\n")
    result = sync.push(config, workspace_root=work, env=env)
    assert result.status == "skipped"


def test_push_requires_project(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    git("remote", "remove", "origin", cwd=project, env=env)
    (project / ".kiro" / "steering").mkdir(parents=True)
    (project / ".kiro" / "steering" / "x.md").write_text("# X\n")
    with pytest.raises(ConfigError, match="project"):
        sync.push(config, workspace_root=project, env=env)
    result = sync.push(
        config,
        workspace_root=project,
        env=env,
        remote_url="https://gitlab.example.com/other/thing.git",
    )
    assert result.status == "pushed"
    assert result.project == "other/thing"


def test_push_rejects_invalid_branch(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / ".kiro" / "steering").mkdir(parents=True)
    (project / ".kiro" / "steering" / "x.md").write_text("# X\n")
    monkeypatch.setattr(gitops, "check_ref_format", lambda *a, **k: False)
    with pytest.raises(ConfigError, match="valid git ref"):
        sync.push(config, workspace_root=project, env=env)


def test_status(config: Config, env: dict[str, str], project: Path, central: Path) -> None:
    result = sync.status(config, workspace_root=project, env=env)
    assert result.project == PROJECT_SLUG
    assert result.remote_commit
    assert result.workspace is not None
    assert result.workspace["steering"].delete == ("python.md",)
    assert result.workspace["specs"].is_empty
    assert result.global_["steering"].add == ("rules.md",)
    assert "specs" not in result.global_
    assert result.lint is not None
    assert result.lint.checked == 0
    no_ws = sync.status(config, workspace_root=None, env=env, run_lint=False)
    assert no_ws.workspace is None
    assert no_ws.project is None
    assert no_ws.lint is None
    git("remote", "remove", "origin", cwd=project, env=env)
    assert sync.status(config, workspace_root=project, env=env).workspace is None


def test_same_remote() -> None:
    assert sync._same_remote("git@h:a/b.git", "ssh://git@h/a/b")
    assert not sync._same_remote("git@h:a/b.git", "git@other:a/b.git")
    assert not sync._same_remote("", "git@h:a/b.git")
