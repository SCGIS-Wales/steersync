"""Resilience and GitLab workflow: offline pulls, proposal warnings, merge requests, bootstrap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import gitlab, gitops, mcp_server, sync
from steersync.cli import main
from steersync.config import Config, load_config
from steersync.errors import GitError, GitLabError
from steersync.hooks import merge_mcp_config, write_kiro_hook
from steersync.paths import kiro_home
from tests.conftest import PROJECT_SLUG, CentralCommit, central_global, central_project, git


@pytest.fixture
def in_project(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, central: Path
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    return project


def _write_steering(project: Path, name: str, body: str) -> None:
    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    (steering / name).write_text(body)


# --- offline fallback ----------------------------------------------------------


def test_pull_falls_back_to_cache_when_remote_is_unreachable(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = sync.pull(config, workspace_root=project, env=env)
    assert not first.offline
    (project / ".kiro" / "steering" / "python.md").unlink()

    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("fatal: unable to access: Could not resolve host: gitlab.example.com")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    result = sync.pull(config, workspace_root=project, env=env)
    assert result.offline
    assert result.commit == first.commit
    assert (project / ".kiro" / "steering" / "python.md").exists()
    status = sync.status(config, workspace_root=project, env=env)
    assert status.remote_commit == first.commit
    with pytest.raises(GitError, match="resolve host"):
        sync.push(config, workspace_root=project, env=env)


def test_offline_fallback_respects_configuration_and_permanent_errors(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\nsync:\n  offline_fallback: false\n"
    )
    config = load_config(env=env, explicit=path)
    sync.pull(config, workspace_root=project, env=env)

    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("fatal: unable to access: Could not resolve host: gitlab.example.com")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    with pytest.raises(GitError):
        sync.pull(config, workspace_root=project, env=env)
    enabled = config

    def denied(*args: object, **kwargs: object) -> None:
        raise GitError("fatal: Authentication failed")

    monkeypatch.setattr(gitops, "ensure_cache_clone", denied)
    with pytest.raises(GitError, match="Authentication"):
        sync.pull(enabled, workspace_root=project, env=env)


def test_pull_without_cache_still_fails_when_unreachable(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("fatal: the remote end hung up unexpectedly")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    with pytest.raises(GitError):
        sync.pull(config, workspace_root=project, env=env)


# --- proposal branches ---------------------------------------------------------


def test_push_warns_about_other_unmerged_proposals(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    _write_steering(project, "a.md", "# A\n\nOne.\n")
    first = sync.push(config, workspace_root=project, env=env)
    assert first.status == "pushed"
    assert first.existing_branches == ()
    _write_steering(project, "a.md", "# A\n\nTwo.\n")
    second = sync.push(config, workspace_root=project, env=env, dry_run=True)
    assert second.status == "dry-run"
    assert second.existing_branches == (first.branch,)
    pushed = sync.push(config, workspace_root=project, env=env)
    assert pushed.existing_branches == (first.branch,)
    again = sync.push(config, workspace_root=project, env=env)
    assert again.status == "already-proposed"
    assert again.existing_branches == (first.branch,)
    status = sync.status(config, workspace_root=project, env=env)
    assert set(status.proposals) == {first.branch, pushed.branch}
    assert all(name.startswith(f"kiro-config/{PROJECT_SLUG}/") for name in status.proposals)


def test_existing_proposal_warning_can_be_disabled(
    env: dict[str, str], project: Path, central: Path, tmp_path: Path
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\nbranch:\n  warn_existing: false\n"
    )
    config = load_config(env=env, explicit=path)
    _write_steering(project, "a.md", "# A\n\nOne.\n")
    sync.push(config, workspace_root=project, env=env)
    _write_steering(project, "a.md", "# A\n\nTwo.\n")
    assert sync.push(config, workspace_root=project, env=env).existing_branches == ()
    assert sync.status(config, workspace_root=project, env=env).proposals == ()


def test_user_id_sources(env: dict[str, str], project: Path, tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("branch:\n  user_from: [email, os]\n")
    config = load_config(env=env, explicit=path)
    assert gitops.current_user_id(config, project, env) == "test-user"
    path.write_text("branch:\n  user_from: [name]\n")
    config = load_config(env=env, explicit=path)
    assert gitops.current_user_id(config, project, env) == "test-user"
    assert gitops.current_user_id(config, project, {**env, "USER": "Dejan Gregor!"}) == "test-user"
    path.write_text("branch:\n  user_from: [os]\n")
    config = load_config(env=env, explicit=path)
    assert (
        gitops.current_user_id(config, project, {**env, "USER": "Dejan.Gregor_99"})
        == "dejan-gregor-99"
    )
    assert gitops.current_user_id(config, project, {**env, "USER": "---"}) == "user"
    path.write_text("branch:\n  user_from: []\n")
    with pytest.raises(Exception, match="user_from"):
        load_config(env=env, explicit=path)


# --- merge requests ------------------------------------------------------------


class FakeGitLab:
    """Stand in for the API: remembers created merge requests per branch."""

    def __init__(self) -> None:
        self.created: dict[str, gitlab.MergeRequest] = {}

    def ensure(
        self,
        client: gitlab.GitLabClient,
        config: Config,
        *,
        source_branch: str,
        title: str,
        description: str = "",
    ) -> tuple[gitlab.MergeRequest, bool]:
        del config, description
        if source_branch in self.created:
            return self.created[source_branch], False
        mr = gitlab.MergeRequest(
            len(self.created) + 1,
            f"https://gitlab.example.com/platform/kiro-config/-/merge_requests/{len(self.created) + 1}",
            source_branch,
            client.target_branch,
            title,
            "opened",
        )
        self.created[source_branch] = mr
        return mr, True

    def open(
        self, client: gitlab.GitLabClient, *, source_branches: list[str] | None = None
    ) -> list[gitlab.MergeRequest]:
        del client
        found = list(self.created.values())
        if source_branches is not None:
            found = [mr for mr in found if mr.source_branch in source_branches]
        return found


@pytest.fixture
def fake_gitlab(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], tmp_path: Path) -> FakeGitLab:
    fake = FakeGitLab()
    monkeypatch.setattr(gitlab, "ensure_merge_request", fake.ensure)
    monkeypatch.setattr(gitlab, "open_merge_requests", fake.open)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    env["GITLAB_TOKEN"] = "glpat-test"
    return fake


@pytest.fixture
def gitlab_config(env: dict[str, str], central: Path, tmp_path: Path) -> Config:
    path = tmp_path / "gl.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\n"
        "gitlab:\n  api_url: https://gitlab.example.com/api/v4\n  project: platform/kiro-config\n"
    )
    return load_config(env=env, explicit=path)


def test_push_opens_merge_request_through_api(
    gitlab_config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    fake_gitlab: FakeGitLab,
) -> None:
    _write_steering(project, "a.md", "# A\n\nOne.\n")
    pushed = sync.push(gitlab_config, workspace_root=project, env=env)
    assert pushed.status == "pushed"
    assert pushed.merge_request is not None
    assert pushed.merge_request.endswith("/merge_requests/1")
    assert pushed.merge_request_note == "merge request created"
    again = sync.push(gitlab_config, workspace_root=project, env=env)
    assert again.status == "already-proposed"
    assert again.merge_request == pushed.merge_request
    assert again.merge_request_note == "merge request already open"


def test_push_without_token_reports_why(
    gitlab_config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    _write_steering(project, "a.md", "# A\n\nOne.\n")
    pushed = sync.push(gitlab_config, workspace_root=project, env=env)
    assert pushed.merge_request is None
    assert "no GitLab token" in pushed.merge_request_note


def test_push_survives_api_failure(
    gitlab_config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env["GITLAB_TOKEN"] = "glpat-test"

    def boom(*args: object, **kwargs: object) -> None:
        raise GitLabError("GitLab API POST merge_requests failed with HTTP 403")

    monkeypatch.setattr(gitlab, "ensure_merge_request", boom)
    _write_steering(project, "a.md", "# A\n\nOne.\n")
    pushed = sync.push(gitlab_config, workspace_root=project, env=env)
    assert pushed.status == "pushed"
    assert pushed.merge_request is None
    assert "HTTP 403" in pushed.merge_request_note


def test_merge_request_command_and_tool(
    gitlab_config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    fake_gitlab: FakeGitLab,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_steering(project, "a.md", "# A\n\nOne.\n")
    with pytest.raises(GitLabError, match="not on the central remote"):
        sync.merge_request(gitlab_config, workspace_root=project, env=env)
    pushed = sync.push(gitlab_config, workspace_root=project, env=env)
    assert pushed.branch is not None
    fake_gitlab.created.clear()
    result = sync.merge_request(gitlab_config, workspace_root=project, env=env)
    assert result.created
    assert result.branch == pushed.branch
    assert result.project == PROJECT_SLUG
    assert result.others == ()
    again = sync.merge_request(gitlab_config, workspace_root=project, env=env, branch=pushed.branch)
    assert not again.created
    _write_steering(project, "a.md", "# A\n\nTwo.\n")
    second = sync.push(gitlab_config, workspace_root=project, env=env)
    mr = sync.merge_request(gitlab_config, workspace_root=project, env=env, title="custom")
    assert mr.branch == second.branch
    assert len(mr.others) == 1
    assert mr.others[0].startswith(pushed.branch)

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(tmp_path / "gl.yaml"))
    runner = CliRunner()
    cli = runner.invoke(main, ["mr", "--json"])
    assert cli.exit_code == 0, cli.output
    data = json.loads(cli.stdout)
    assert data["url"].endswith("/merge_requests/2")
    text = runner.invoke(main, ["mr", "--branch", pushed.branch])
    assert "merge request already open" in text.output
    assert "other open merge requests" in text.output
    tool = mcp_server.raise_merge_request()
    assert tool["ok"]
    assert tool["branch"] == second.branch
    assert len(tool["others"]) == 1
    monkeypatch.delenv("GITLAB_TOKEN")
    failed = runner.invoke(main, ["mr"])
    assert failed.exit_code == 9
    assert "GITLAB_TOKEN" in failed.output
    assert mcp_server.raise_merge_request()["ok"] is False


def test_merge_request_needs_something_to_propose(
    gitlab_config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    fake_gitlab: FakeGitLab,
) -> None:
    sync.pull(gitlab_config, workspace_root=project, env=env, include_global=False)
    with pytest.raises(GitLabError, match="nothing to propose"):
        sync.merge_request(gitlab_config, workspace_root=project, env=env)


# --- bootstrap: auto load in any repository --------------------------------------


def test_init_writes_session_hook_and_mcp_entry(runner_project: tuple[CliRunner, Path]) -> None:
    runner, project = runner_project
    before = runner.invoke(main, ["lint", "--json"])
    rules = {f["rule"] for f in json.loads(before.stdout)["findings"]}
    assert {"autoload-hook-missing", "autoload-mcp-missing"} <= rules
    result = runner.invoke(main, ["init", "--mcp-command", "/usr/local/bin/uvx"])
    assert result.exit_code == 0, result.output
    hook = json.loads((project / ".kiro" / "hooks" / "steersync.json").read_text())
    assert hook["hooks"][0]["trigger"] == "SessionStart"
    assert "steersync pull" in hook["hooks"][0]["action"]["command"]
    mcp = json.loads((project / ".kiro" / "settings" / "mcp.json").read_text())
    entry = mcp["mcpServers"]["steersync"]
    assert entry["command"] == "/usr/local/bin/uvx"
    assert entry["env"]["GITLAB_TOKEN"] == "${GITLAB_TOKEN}"
    after = runner.invoke(main, ["lint", "--json"])
    rules = {f["rule"] for f in json.loads(after.stdout)["findings"]}
    assert not rules & {"autoload-hook-missing", "autoload-mcp-missing"}
    second = runner.invoke(main, ["init"])
    assert "unchanged: " in second.output
    assert "kept: " in second.output
    skipped = runner.invoke(main, ["init", "--no-kiro"])
    assert "SessionStart" not in skipped.output


@pytest.fixture
def runner_project(in_project: Path) -> tuple[CliRunner, Path]:
    return CliRunner(), in_project


def test_merge_mcp_config_keeps_other_servers(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "extra": 1}))
    entry = {"command": "uvx", "args": ["steersync", "mcp"]}
    assert merge_mcp_config(path, entry) == "written"
    data = json.loads(path.read_text())
    assert set(data["mcpServers"]) == {"other", "steersync"}
    assert data["extra"] == 1
    assert merge_mcp_config(path, entry) == "unchanged"
    assert merge_mcp_config(path, {**entry, "command": "/abs/uvx"}) == "kept"
    assert merge_mcp_config(path, {**entry, "command": "/abs/uvx"}, force=True) == "updated"
    path.write_text("[]")
    assert merge_mcp_config(path, entry) == "written"
    path.write_text("{broken")
    with pytest.raises(ValueError, match="not valid JSON"):
        merge_mcp_config(path, entry)
    hooks = tmp_path / "hooks"
    assert write_kiro_hook(hooks) == "written"
    assert write_kiro_hook(hooks) == "unchanged"
    assert write_kiro_hook(hooks, timeout=10) == "updated"


def test_init_refuses_broken_mcp_json(runner_project: tuple[CliRunner, Path]) -> None:
    runner, project = runner_project
    settings = project / ".kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "mcp.json").write_text("{broken")
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 1
    assert "not valid JSON" in result.output


def test_mcp_config_command_uses_configured_token_variable(
    runner_project: tuple[CliRunner, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = runner_project
    path = tmp_path / "t.yaml"
    path.write_text("gitlab:\n  token_env: GL_API_TOKEN\n")
    monkeypatch.setenv("STEERSYNC_CONFIG", str(path))
    out = json.loads(runner.invoke(main, ["mcp-config"]).stdout)
    assert out["mcpServers"]["steersync"]["env"]["GL_API_TOKEN"] == "${GL_API_TOKEN}"
    doctor = runner.invoke(main, ["doctor"])
    assert "gitlab token: GL_API_TOKEN not set" in doctor.output


def test_pull_best_effort_bounds_git_time(
    runner_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = runner_project
    seen: list[int] = []
    original = sync.pull

    def spy(config: Config, **kwargs: object) -> object:
        seen.append(config.git.timeout_seconds)
        return original(config, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sync, "pull", spy)
    assert runner.invoke(main, ["pull", "--best-effort"]).exit_code == 0
    assert seen == [60]


# --- prompts and AGENTS.md ----------------------------------------------------------


def test_prompts_and_agents_md_round_trip(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def seed(work: Path) -> None:
        (central_project(work, "prompts")).mkdir(parents=True)
        (central_project(work, "prompts") / "code-review.md").write_text("# Review\n\nCheck.\n")
        (central_global(work, "prompts")).mkdir(parents=True)
        (central_global(work, "prompts") / "standup.md").write_text("# Standup\n\nSummarise.\n")
        (central_project(work, "agents-md")).mkdir(parents=True)
        (central_project(work, "agents-md") / "AGENTS.md").write_text("# Agents\n\nRules.\n")

    central_commit(seed, "prompts")
    result = sync.pull(config, workspace_root=project, env=env)
    by_key = {(r.scope, r.collection): r for r in result.results}
    assert by_key["workspace", "prompts"].plan.add == ("code-review.md",)
    assert by_key["global", "prompts"].plan.add == ("standup.md",)
    assert by_key["workspace", "agents-md"].plan.add == ("AGENTS.md",)
    assert (project / ".kiro" / "prompts" / "code-review.md").exists()
    assert (kiro_home(env) / "prompts" / "standup.md").exists()
    assert (project / "AGENTS.md").read_text().startswith("# Agents")
    (project / ".kiro" / "prompts" / ("x" * 60 + ".md")).write_text("# Long\n\nText.\n")
    pushed = sync.push(config, workspace_root=project, env=env, collections=["prompts"])
    assert pushed.status == "pushed"
    assert pushed.lint is not None
    assert "prompt-name-length" in {f.rule for f in pushed.lint.warnings}
    excluded = project / ".git" / "info" / "exclude"
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config.source_file))
    CliRunner().invoke(main, ["init", "--no-kiro"])
    text = excluded.read_text()
    assert "/.kiro/prompts/" in text
    assert "/AGENTS.md" not in text
    assert "/./" not in text
    assert git("status", "--porcelain", "--", "AGENTS.md", cwd=project, env=env).startswith("??")


def test_lint_paths_outside_collections_use_steering_profile(
    runner_project: tuple[CliRunner, Path],
) -> None:
    runner, project = runner_project
    docs = project / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n\nText.\n")
    (project / "AGENTS.md").write_text("# Agents\n\nRules.\n")
    report = json.loads(runner.invoke(main, ["lint", "--json", str(docs)]).stdout)
    assert report["checked"] == 1
    agents = json.loads(runner.invoke(main, ["lint", "--json", str(project / "AGENTS.md")]).stdout)
    assert agents["checked"] == 1
    assert agents["errors"] == 0
