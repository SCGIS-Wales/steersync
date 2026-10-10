from __future__ import annotations

from pathlib import Path

import pytest

from steersync import gitops
from steersync.config import Config, load_config
from steersync.errors import GitError
from tests.conftest import PROJECT_REMOTE, PROJECT_SLUG, CentralCommit, git


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ssh://git@gitlab.example.com:2222/acme/platform/api.git", "acme/platform/api"),
        ("https://gitlab.example.com/acme/platform/api.git", "acme/platform/api"),
        ("https://user:token@gitlab.example.com/acme/api", "acme/api"),
        ("git@github.com:SCGIS-Wales/steersync.git", "SCGIS-Wales/steersync"),
        ("file:///srv/git/steering.git", "steering"),
        ("/srv/git/steering.git/", "steering"),
    ],
)
def test_project_path_from_url(url: str, expected: str) -> None:
    assert gitops.project_path_from_url(url) == expected


def test_project_path_rejects_empty() -> None:
    with pytest.raises(GitError):
        gitops.project_path_from_url("https://gitlab.example.com/")


def test_project_slug_sanitises() -> None:
    assert gitops.project_slug(PROJECT_REMOTE) == PROJECT_SLUG
    assert gitops.project_slug("git@h:Acme Corp/My Repo.lock.git") == "acme-corp/my-repo"
    assert gitops.project_slug("git@h:../../etc/passwd") == "etc/passwd"
    with pytest.raises(GitError):
        gitops.project_slug("git@h:.../...")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("steering/acme/user-abc", "steering/acme/user-abc"),
        ("a//b", "a/b"),
        ("a/.hidden/b.lock", "a/hidden/b"),
        ("bad name~^:?*[\\", "bad-name"),
        ("x@{y}", "x-y"),
        ("a..b", "a.b"),
    ],
)
def test_sanitise_ref(raw: str, expected: str) -> None:
    assert gitops.sanitise_ref(raw) == expected


def test_build_branch_name_is_valid_ref(config: Config, env: dict[str, str]) -> None:
    name = gitops.build_branch_name(
        config, project="acme/platform/api", user="dejan", content_hash="0123456789abcdef"
    )
    assert name == "steering/acme/platform/api/dejan-01234567"
    assert gitops.check_ref_format(config, name, env=env)
    assert not gitops.check_ref_format(config, "bad..name", env=env)


def test_build_branch_name_rejects_empty(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("branch:\n  template: '{hash}'\n")
    config = load_config(env=env, explicit=path)
    with pytest.raises(GitError):
        gitops.build_branch_name(config, project="p", user="u", content_hash="........")


def test_render_push_options(config: Config) -> None:
    options = gitops.render_push_options(
        config, project="acme/api", user="dejan", content_hash="abcdef0123"
    )
    assert options[0] == "merge_request.create"
    assert "merge_request.target=main" in options
    assert "merge_request.title=steering: update acme/api from dejan" in options


def test_parse_pre_push_stdin() -> None:
    zero = "0" * 40
    text = f"refs/heads/main abc123 refs/heads/main def456\n(delete) {zero} refs/heads/old 1234\nbroken line\n"
    refs = gitops.parse_pre_push_stdin(text)
    assert len(refs) == 2
    assert not refs[0].is_delete
    assert refs[1].is_delete


def test_redact_url() -> None:
    assert (
        gitops.redact_url("https://user:glpat-secret@gitlab.example.com/a.git")
        == "https://***@gitlab.example.com/a.git"
    )
    assert gitops.redact_url("git@host:a/b.git") == "git@host:a/b.git"


def test_git_environment_hardening(tmp_path: Path) -> None:
    env = gitops.git_environment(
        {"GIT_DIR": "/x", "SSH_AUTH_SOCK": "/s", "HTTPS_PROXY": "p"}, hooks_dir=tmp_path
    )
    assert "GIT_DIR" not in env
    assert env["SSH_AUTH_SOCK"] == "/s"
    assert env["HTTPS_PROXY"] == "p"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["STEERSYNC_ACTIVE"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path)
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_run_git_missing_binary(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("git:\n  binary: definitely-not-git-xyz\n")
    config = load_config(env=env, explicit=path)
    with pytest.raises(GitError, match="not found"):
        gitops.git_version(config, env=env)


def test_run_git_failure_raises(config: Config, env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(GitError, match="failed"):
        gitops.run_git(config, ["rev-parse", "HEAD"], cwd=tmp_path, env=env)
    assert gitops.repo_toplevel(config, tmp_path, env=env) is None


def test_run_git_timeout(
    config: Config, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    def boom(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(GitError, match="timed out"):
        gitops.run_git(config, ["status"], cwd=tmp_path, env=env)


def test_repo_helpers(config: Config, env: dict[str, str], project: Path) -> None:
    assert (
        gitops.repo_toplevel(
            config, project / "sub" if (project / "sub").exists() else project, env=env
        )
        == project
    )
    assert gitops.remote_url(config, project, env=env) == PROJECT_REMOTE
    assert gitops.remote_url(config, project, "nope", env=env) is None
    assert gitops.git_dir(config, project, env=env) == project / ".git"
    assert gitops.hooks_dir(config, project, env=env) == project / ".git" / "hooks"
    assert gitops.current_user_id(config, project, env) == "test.user"
    assert (
        gitops.current_user_id(config, None, {**env, "GIT_CONFIG_GLOBAL": "/dev/null"}) == "tester"
    )
    assert gitops.git_version(config, env=env).startswith("git version")


def test_hooks_dir_honours_core_hookspath(
    config: Config, env: dict[str, str], project: Path
) -> None:
    git("config", "core.hooksPath", ".githooks", cwd=project, env=env)
    assert gitops.hooks_dir(config, project, env=env) == project / ".githooks"


def test_write_info_exclude_is_idempotent(
    config: Config, env: dict[str, str], project: Path
) -> None:
    assert gitops.write_info_exclude(config, project, "/.kiro/steering/", env=env) is True
    assert gitops.write_info_exclude(config, project, "/.kiro/steering/", env=env) is False
    content = (project / ".git" / "info" / "exclude").read_text()
    assert content.count("/.kiro/steering/") == 1


def test_cache_clone_and_refresh(
    config: Config,
    env: dict[str, str],
    central: Path,
    tmp_path: Path,
    central_commit: CentralCommit,
) -> None:
    cache = tmp_path / "cache" / "repo"
    git_env = gitops.git_environment(env)
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    first = gitops.head_commit(config, cache, env=git_env)
    (cache / "junk.txt").write_text("dirty")
    new = central_commit(lambda w: (w / "global" / "extra.md").write_text("# Extra\n"), "extra")
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    assert gitops.head_commit(config, cache, env=git_env) == new != first
    assert not (cache / "junk.txt").exists()
    assert (cache / "global" / "extra.md").exists()


def test_cache_clone_updates_remote_url(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache" / "repo"
    git_env = gitops.git_environment(env)
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    git("remote", "set-url", "origin", "/nonexistent", cwd=cache, env=env)
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    assert gitops.remote_url(config, cache, env=git_env) == str(central)


def test_remote_branch_exists(config: Config, env: dict[str, str], central: Path) -> None:
    git_env = gitops.git_environment(env)
    assert gitops.remote_branch_exists(config, str(central), "main", env=git_env)
    assert not gitops.remote_branch_exists(config, str(central), "nope", env=git_env)
    assert gitops.remote_head_oid(config, str(central), "main", env=git_env)
    assert gitops.remote_head_oid(config, str(central), "nope", env=git_env) is None
    with pytest.raises(GitError):
        gitops.remote_branch_exists(
            config, str(central.parent / "missing.git"), "main", env=git_env
        )


def test_commit_and_push_branch(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache" / "repo"
    git_env = gitops.git_environment(env)
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    gitops.create_branch(config, cache, "feature/x", "origin/main", env=git_env)
    assert (
        gitops.commit_paths(
            config,
            cache,
            paths=["global"],
            message="noop",
            author_name="a",
            author_email="a@b",
            env=git_env,
        )
        is None
    )
    (cache / "global" / "new.md").write_text("# New\n")
    commit = gitops.commit_paths(
        config,
        cache,
        paths=["global"],
        message="add",
        author_name="a",
        author_email="a@b",
        env=git_env,
    )
    assert commit
    result, delivered = gitops.push_branch(
        config,
        cache,
        url=str(central),
        branch="feature/x",
        push_options=["merge_request.create"],
        env=git_env,
    )
    assert result.returncode == 0
    assert delivered is False
    assert gitops.remote_branch_exists(config, str(central), "feature/x", env=git_env)
    _, delivered = gitops.push_branch(
        config, cache, url=str(central), branch="feature/x", push_options=[], env=git_env
    )
    assert delivered is False
    with pytest.raises(GitError, match="push failed"):
        gitops.push_branch(
            config,
            cache,
            url=str(central.parent / "missing.git"),
            branch="feature/x",
            push_options=[],
            env=git_env,
        )


def test_push_options_accepted_when_advertised(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    git("config", "receive.advertisePushOptions", "true", cwd=central, env=env)
    cache = tmp_path / "cache" / "repo"
    git_env = gitops.git_environment(env)
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    gitops.create_branch(config, cache, "feature/y", "origin/main", env=git_env)
    (cache / "global" / "new.md").write_text("# New\n")
    gitops.commit_paths(
        config,
        cache,
        paths=["global"],
        message="add",
        author_name="a",
        author_email="a@b",
        env=git_env,
    )
    _, delivered = gitops.push_branch(
        config,
        cache,
        url=str(central),
        branch="feature/y",
        push_options=["merge_request.create"],
        env=git_env,
    )
    assert delivered is True
