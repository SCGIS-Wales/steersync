"""Shared fixtures: isolated HOME, git identity, a central bare repository and a project clone."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from steersync.config import Config, load_config

PROJECT_REMOTE = "ssh://git@gitlab.example.com/acme/platform/api.git"
PROJECT_SLUG = "acme/platform/api"

CentralCommit = Callable[..., str]

GLOBAL_DOC = "---\ninclusion: always\n---\n# Global rules\n\nBe concise.\n"
PROJECT_DOC = '---\ninclusion: fileMatch\nfileMatchPattern: "**/*.py"\n---\n# Python\n\nUse ruff.\n'
SKILL_DOC = "---\nname: venafi-tpp-websdk\ndescription: Venafi TPP WebSDK runbook\n---\n# Venafi\n\nSteps.\n"
HOOK_DOC = (
    '{"version": "v1", "hooks": [{"name": "tests", "trigger": "Stop", '
    '"action": {"type": "command", "command": "./gradlew test"}}]}\n'
)
AGENT_DOC = '{"name": "entra", "description": "Entra agent", "tools": ["read"]}\n'


def central_global(work: Path, collection: str = "steering") -> Path:
    """Directory of a collection under ``global/`` in the central work tree."""
    return work / "global" / collection


def central_project(work: Path, collection: str = "steering") -> Path:
    """Directory of a collection under ``projects/<slug>/`` in the central work tree."""
    return work / "projects" / PROJECT_SLUG / collection


def git(*args: str, cwd: Path, env: Mapping[str, str]) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, env=dict(env), capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A minimal environment with an isolated HOME and git identity."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test User\n\temail = test.user@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "USER": "tester",
    }
    for key in ("KIRO_HOME", "STEERSYNC_CONFIG", "STEERSYNC_REMOTE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def central(tmp_path: Path, env: dict[str, str]) -> Path:
    """A bare central repository seeded with global and project steering."""
    bare = tmp_path / "central.git"
    work = tmp_path / "central-work"
    git("init", "-q", "--bare", str(bare), cwd=tmp_path, env=env)
    git("clone", "-q", str(bare), str(work), cwd=tmp_path, env=env)
    central_global(work).mkdir(parents=True)
    (central_global(work) / "rules.md").write_text(GLOBAL_DOC, encoding="utf-8")
    central_project(work).mkdir(parents=True)
    (central_project(work) / "python.md").write_text(PROJECT_DOC, encoding="utf-8")
    git("add", "-A", cwd=work, env=env)
    git("commit", "-qm", "seed", cwd=work, env=env)
    git("push", "-q", "origin", "main", cwd=work, env=env)
    return bare


@pytest.fixture
def central_commit(
    tmp_path: Path, env: dict[str, str]
) -> Callable[[Callable[[Path], None], str], str]:
    """Return a helper that mutates the central work tree, commits and pushes."""
    work = tmp_path / "central-work"

    def _commit(mutate: Callable[[Path], None], message: str = "update") -> str:
        git("pull", "-q", "origin", "main", cwd=work, env=env)
        mutate(work)
        git("add", "-A", cwd=work, env=env)
        git("commit", "-qm", message, cwd=work, env=env)
        git("push", "-q", "origin", "main", cwd=work, env=env)
        return git("rev-parse", "HEAD", cwd=work, env=env)

    return _commit


@pytest.fixture
def project(tmp_path: Path, env: dict[str, str]) -> Path:
    """A project clone whose origin points at a fake GitLab URL and whose push remote is local."""
    bare = tmp_path / "project.git"
    work = tmp_path / "project"
    git("init", "-q", "--bare", str(bare), cwd=tmp_path, env=env)
    git("clone", "-q", str(bare), str(work), cwd=tmp_path, env=env)
    git("remote", "set-url", "origin", PROJECT_REMOTE, cwd=work, env=env)
    git("remote", "add", "local", str(bare), cwd=work, env=env)
    (work / "README.md").write_text("# project\n", encoding="utf-8")
    git("add", "-A", cwd=work, env=env)
    git("commit", "-qm", "init", cwd=work, env=env)
    return work


@pytest.fixture
def config_file(tmp_path: Path, central: Path) -> Path:
    path = tmp_path / "steersync.yaml"
    path.write_text(
        f"remote:\n  url: {central}\n  allow_local: true\n"
        f"paths:\n  cache_dir: {tmp_path / 'cache'}\n"
        "git:\n  retry_backoff_seconds: 0\n  retry_max_backoff_seconds: 0\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(config_file: Path, env: dict[str, str]) -> Config:
    return load_config(env=env, explicit=config_file)
