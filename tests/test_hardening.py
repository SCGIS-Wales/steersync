"""Tests for reliability, resiliency and security hardening."""

from __future__ import annotations

import json
import logging
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from steersync import cli, gitops, mcp_server, paths, sync
from steersync.config import Config, load_config
from steersync.errors import GitError, LimitExceededError, LockTimeoutError, UnsafePathError
from steersync.locking import exclusive_lock
from steersync.paths import global_steering_dir, workspace_lock_path
from steersync.steering import snapshot_directory, write_lock
from tests.conftest import CentralCommit, central_global, central_project, git

PUSH_OPTIONS_REJECTED = (
    "fatal: the receiving end does not support push options\n"
    "fatal: the remote end hung up unexpectedly"
)

# Locking


def test_exclusive_lock_serialises_and_times_out(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "repo.lock"
    with (
        exclusive_lock(lock, timeout=1),
        pytest.raises(LockTimeoutError, match="timed out"),
        exclusive_lock(lock, timeout=0.3, poll_interval=0.05),
    ):
        pass  # pragma: no cover
    with exclusive_lock(lock, timeout=1):
        assert lock.exists()


def test_concurrent_pulls_share_cache_safely(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    errors: list[Exception] = []

    def worker() -> None:
        try:
            sync.pull(config, workspace_root=project, env=env)
        except Exception as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (project / ".kiro" / "steering" / "python.md").exists()


def test_cache_lock_contention_raises(
    env: dict[str, str], project: Path, central: Path, tmp_path: Path
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\nsync:\n  lock_timeout_seconds: 1\n"
    )
    config = load_config(env=env, explicit=path)
    cache_dir = paths.cache_repo_dir(config, env, str(central))
    with (
        exclusive_lock(cache_dir.with_name(cache_dir.name + ".lock"), timeout=1),
        pytest.raises(LockTimeoutError),
    ):
        sync.pull(config, workspace_root=project, env=env)


# Retries


class _FakeRun:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        code, err = outcome
        return subprocess.CompletedProcess(command, code, "", err)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays


def test_run_git_retries_transient_errors(
    config: Config, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    fake = _FakeRun(
        [
            (128, "fatal: unable to access: Could not resolve host: gitlab.example.com"),
            subprocess.TimeoutExpired(cmd="git", timeout=5),
            (0, ""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    result = gitops.run_git(config, ["fetch"], retries=2)
    assert result.returncode == 0
    assert len(fake.calls) == 3
    assert len(no_sleep) == 2
    assert all(call["stdin"] is subprocess.DEVNULL for call in fake.calls)


def test_run_git_does_not_retry_permanent_errors(
    config: Config, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    fake = _FakeRun([(128, "fatal: Authentication failed for 'https://h/a.git'")])
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(GitError, match="Authentication failed"):
        gitops.run_git(config, ["fetch"], retries=3)
    assert len(fake.calls) == 1
    assert no_sleep == []


def test_run_git_gives_up_after_retries(
    config: Config, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    fake = _FakeRun([subprocess.TimeoutExpired(cmd="git", timeout=5)] * 3)
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(GitError, match="timed out"):
        gitops.run_git(config, ["push"], retries=2)
    assert len(fake.calls) == 3


def test_run_git_passes_input_when_given(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun([(0, "")])
    monkeypatch.setattr(subprocess, "run", fake)
    gitops.run_git(config, ["hash-object", "--stdin"], stdin="data")
    assert fake.calls[0]["input"] == "data"
    assert fake.calls[0]["stdin"] is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("fatal: unable to access 'x': Could not resolve host: h", True),
        ("error: RPC failed; HTTP 502 curl 22", True),
        ("fatal: the remote end hung up unexpectedly", True),
        ("ssh: connect to host h port 22: Connection refused", True),
        ("git fetch timed out after 120s", True),
        ("fatal: Authentication failed", False),
        ("fatal: repository 'x' not found", False),
        ("git@h: Permission denied (publickey).", False),
        (PUSH_OPTIONS_REJECTED, False),
    ],
)
def test_is_transient(message: str, expected: bool) -> None:
    assert gitops.is_transient(message) is expected


def test_backoff_delay_is_capped(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("git:\n  retry_backoff_seconds: 2\n  retry_max_backoff_seconds: 5\n")
    config = load_config(env=env, explicit=path)
    assert 2 <= gitops.backoff_delay(config, 0) <= 2.5
    assert 5 <= gitops.backoff_delay(config, 10) <= 6.25


# Self healing cache


def test_corrupt_cache_is_recreated(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache" / "repo"
    git_env = gitops.git_environment(env)
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    (cache / ".git" / "HEAD").write_text("garbage\n")
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=git_env)
    assert (cache / "global" / "steering" / "rules.md").exists()
    assert gitops.head_commit(config, cache, env=git_env)


def test_half_written_cache_directory_is_replaced(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache" / "repo"
    cache.mkdir(parents=True)
    (cache / "leftover").write_text("partial clone")
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=env)
    assert not (cache / "leftover").exists()
    assert (cache / "global" / "steering" / "rules.md").exists()


def test_stale_index_lock_is_removed(
    config: Config, env: dict[str, str], central: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache" / "repo"
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=env)
    (cache / ".git" / "index.lock").write_text("")
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=env)
    assert not (cache / ".git" / "index.lock").exists()


def test_transient_refresh_failure_keeps_cache(
    config: Config,
    env: dict[str, str],
    central: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache" / "repo"
    gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=env)

    def offline(*args: object, **kwargs: object) -> None:
        raise GitError("git fetch failed (128): Could not resolve host: h")

    monkeypatch.setattr(gitops, "_refresh_clone", offline)
    with pytest.raises(GitError, match="resolve host"):
        gitops.ensure_cache_clone(config, url=str(central), cache_dir=cache, env=env)
    assert (cache / ".git").is_dir()


def test_cache_operations_do_not_run_git_hooks(
    config: Config, env: dict[str, str], project: Path, central: Path, tmp_path: Path
) -> None:
    sync.pull(config, workspace_root=project, env=env)
    cache = paths.cache_repo_dir(config, env, str(central))
    marker = tmp_path / "hook-ran"
    hook = cache / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    sync.pull(config, workspace_root=project, env=env)
    (project / ".kiro" / "steering" / "new.md").write_text("# New\n\nText.\n")
    assert sync.push(config, workspace_root=project, env=env).status == "pushed"
    assert not marker.exists()


def test_failed_push_leaves_cache_on_default_branch(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / ".kiro" / "steering").mkdir(parents=True)
    (project / ".kiro" / "steering" / "x.md").write_text("# X\n\nText.\n")

    def refuse(*args: object, **kwargs: object) -> None:
        raise GitError("git push failed (1): pre-receive hook declined")

    monkeypatch.setattr(gitops, "push_branch", refuse)
    with pytest.raises(GitError, match="declined"):
        sync.push(config, workspace_root=project, env=env)
    cache = paths.cache_repo_dir(config, env, str(central))
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=cache, env=env) == "main"
    assert git("status", "--porcelain", cwd=cache, env=env) == ""


# Content safety


def test_central_symlinks_are_never_followed(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    central_commit: CentralCommit,
    tmp_path: Path,
) -> None:
    secret = tmp_path / "home" / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True)
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")

    def plant(work: Path) -> None:
        (central_global(work) / "evil.md").symlink_to(secret)
        (central_project(work) / "evil.md").symlink_to("../../../../../global/steering/rules.md")

    central_commit(plant, "plant symlinks")
    result = sync.pull(config, workspace_root=project, env=env)
    assert not (global_steering_dir(config, env) / "evil.md").exists()
    assert not (project / ".kiro" / "steering" / "evil.md").exists()
    assert all("evil.md" not in r.plan.add for r in result.results)


def test_central_directory_symlink_is_rejected(
    config: Config,
    env: dict[str, str],
    project: Path,
    central: Path,
    central_commit: CentralCommit,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("# Leak\n")

    def plant(work: Path) -> None:
        git("rm", "-rq", "global", cwd=work, env={**env})
        (work / "global").symlink_to(outside)

    central_commit(plant, "global becomes a symlink")
    with pytest.raises(UnsafePathError):
        sync.pull(config, workspace_root=None, env=env)


def test_limits_on_central_and_local_content(
    env: dict[str, str], project: Path, central: Path, tmp_path: Path
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\nsync:\n  max_files: 1\n  max_total_bytes: 2048\n"
    )
    config = load_config(env=env, explicit=path)
    assert sync.pull(config, workspace_root=project, env=env).change_count == 2
    steering = project / ".kiro" / "steering"
    (steering / "a.md").write_text("# A\n")
    with pytest.raises(LimitExceededError, match="max_files"):
        sync.push(config, workspace_root=project, env=env)
    (steering / "a.md").unlink()
    (steering / "python.md").write_text("# Big\n\n" + "x" * 4096)
    with pytest.raises(LimitExceededError, match="max_total_bytes"):
        sync.push(config, workspace_root=project, env=env, run_lint=False)


def test_written_files_are_world_readable(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    sync.pull(config, workspace_root=project, env=env)
    mode = (project / ".kiro" / "steering" / "python.md").stat().st_mode & 0o777
    assert mode == 0o644


def test_lock_file_never_stores_credentials(tmp_path: Path) -> None:
    lock = tmp_path / "lock.json"
    write_lock(
        lock,
        remote="https://user:glpat-secret@gitlab.example.com/a.git",
        commit=None,
        collections={},
    )
    data = json.loads(lock.read_text())
    assert data["remote"] == "https://gitlab.example.com/a.git"
    assert "secret" not in lock.read_text()


def test_password_in_remote_url_is_warned(
    env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    config = load_config(env={**env, "STEERSYNC_REMOTE": "https://u:p@gitlab.example.com/a.git"})
    with caplog.at_level(logging.WARNING):
        assert sync._central_url(config).endswith("/a.git")
    assert "credential helper" in caplog.text


def test_snapshot_skips_symlinks_only_when_asked(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("# Real\n")
    steering = tmp_path / "steering"
    steering.mkdir()
    (steering / "link.md").symlink_to(target)
    assert "link.md" in snapshot_directory(steering)
    assert snapshot_directory(steering, follow_symlinks=False) == {}


def test_pull_lock_records_redacted_remote(
    config: Config, env: dict[str, str], project: Path, central: Path
) -> None:
    sync.pull(config, workspace_root=project, env=env)
    assert json.loads(workspace_lock_path(config, project).read_text())["remote"] == str(central)


# CLI safety


@pytest.fixture
def in_project(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, central: Path
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    return project


def test_pull_best_effort(
    in_project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("STEERSYNC_REMOTE", str(tmp_path / "missing.git"))
    result = runner.invoke(cli.main, ["pull", "--best-effort"])
    assert result.exit_code == 0
    assert "steering pull skipped" in result.output
    assert runner.invoke(cli.main, ["pull"]).exit_code == 3
    bad = tmp_path / "bad.yaml"
    bad.write_text("remote: [")
    assert runner.invoke(cli.main, ["--config", str(bad), "pull", "--best-effort"]).exit_code == 0
    assert runner.invoke(cli.main, ["--config", str(bad), "pull"]).exit_code == 2


def test_hook_mode_survives_unexpected_errors(
    in_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    (in_project / ".kiro" / "steering").mkdir(parents=True)
    (in_project / ".kiro" / "steering" / "x.md").write_text("# X\n")

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(sync, "push", explode)
    hooked = runner.invoke(cli.main, ["push", "--from-hook"], input="a 1 b 2\n")
    assert hooked.exit_code == 0
    assert "unexpected error" in hooked.output
    direct = runner.invoke(cli.main, ["push"])
    assert direct.exit_code == 1
    assert isinstance(direct.exception, RuntimeError)

    def disk_full(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sync, "push", disk_full)
    assert runner.invoke(cli.main, ["push", "--from-hook"], input="a 1 b 2\n").exit_code == 0
    failed = runner.invoke(cli.main, ["push"])
    assert failed.exit_code == 1
    assert "No space left" in failed.output


def test_hook_budget_bounds_git(config: Config) -> None:
    bounded = cli._hook_budget(config)
    assert bounded.git.timeout_seconds == min(
        config.git.timeout_seconds, config.hook.timeout_seconds
    )
    assert bounded.git.retries <= 1
    assert config.git.timeout_seconds == 120


@pytest.mark.parametrize(
    ("error", "code", "text"),
    [
        (KeyboardInterrupt(), 130, "interrupted"),
        (OSError("disk on fire"), 1, "disk on fire"),
        (RuntimeError("boom"), 1, "unexpected error: RuntimeError: boom"),
    ],
)
def test_run_entry_point_handles_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    code: int,
    text: str,
) -> None:
    def fail(**kwargs: object) -> None:
        raise error

    monkeypatch.setattr(cli, "main", fail)
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == code
    assert text in capsys.readouterr().err


def test_run_entry_point_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "main", lambda **kwargs: calls.append(kwargs))
    cli.run()
    assert calls == [{"standalone_mode": True}]


def test_mcp_tools_report_configuration_errors(
    in_project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEERSYNC_CONFIG", str(tmp_path / "missing.yaml"))
    for tool in (
        mcp_server.pull_steering,
        mcp_server.push_steering,
        mcp_server.diff_steering,
        mcp_server.lint_steering,
    ):
        outcome = tool()
        assert outcome["ok"] is False
        assert "missing" in outcome["error"]
