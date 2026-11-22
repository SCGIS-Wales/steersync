"""Shared fixtures: isolated HOME, git identity, a central bare repository and a project clone."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import registry
from steersync.cli import main
from steersync.config import Config, load_config
from steersync.paths import registry_path

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


# File modes, execute bits, POSIX shell hooks and unprivileged symlinks do not exist on
# Windows; the behaviour they cover is exercised on Linux and macOS.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file semantics")


@pytest.fixture(autouse=True)
def _isolated_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never touch the developer's workspace registry or backups."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def _no_local_kiro_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of a kiro-cli installed on the developer's machine."""
    monkeypatch.setattr("steersync.kiro_version.CLI_BINARY", "kiro-cli-not-installed-in-tests")
    monkeypatch.setenv("STEERSYNC_NO_V3_REMINDER", "1")


def lock_file(project: Path) -> Path:
    """The workspace's last sync record, under the git directory."""
    return project / ".git" / "steersync" / "lock.json"


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
    for key in (
        "KIRO_HOME",
        "STEERSYNC_CONFIG",
        "STEERSYNC_REMOTE",
        "XDG_CONFIG_HOME",
        "LOCALAPPDATA",
    ):
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


# --- several repositories for steersync fleet ------------------------------------------------

STEERING = "---\ninclusion: always\n---\n# Rules\n\nVersion {n}.\n"


def make_project(base: Path, slug: str, env: dict[str, str]) -> Path:
    repo = base / slug.replace("/", "-")
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(repo), cwd=base, env=env)
    git("remote", "add", "origin", f"ssh://git@gitlab.example.com/{slug}.git", cwd=repo, env=env)
    (repo / "README.md").write_text("# x\n")
    git("add", "-A", cwd=repo, env=env)
    git("commit", "-qm", "init", cwd=repo, env=env)
    return repo


def seed(slugs: list[str]):  # type: ignore[no-untyped-def]
    def mutate(work: Path) -> None:
        for slug in slugs:
            steering = work / "projects" / slug / "steering"
            steering.mkdir(parents=True, exist_ok=True)
            (steering / "rules.md").write_text(STEERING.format(n=1))

    return mutate


def edit_central(slug: str, n: int):  # type: ignore[no-untyped-def]
    def mutate(work: Path) -> None:
        (work / "projects" / slug / "steering" / "rules.md").write_text(STEERING.format(n=n))

    return mutate


@pytest.fixture
def fleet_env(
    tmp_path: Path,
    env: dict[str, str],
    config_file: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    monkeypatch.setenv("STEERSYNC_NO_V3_REMINDER", "1")
    slugs = ["acme/ahead", "acme/behind", "acme/both", "acme/proposed", "acme/same"]
    central_commit(seed(slugs), "seed fleet")
    base = tmp_path / "code"
    repos = {slug.split("/")[1]: make_project(base, slug, env) for slug in slugs}
    runner = CliRunner()
    for repo in repos.values():
        monkeypatch.chdir(repo)
        assert runner.invoke(main, ["pull", "--no-global"]).exit_code == 0
    steering = Path(".kiro") / "steering"
    (repos["ahead"] / steering / "new.md").write_text("# New\n\nLocal.\n")
    central_commit(edit_central("acme/behind", 2), "behind")
    (repos["both"] / steering / "rules.md").write_text(STEERING.format(n=9))
    central_commit(edit_central("acme/both", 3), "both")
    (repos["proposed"] / steering / "extra.md").write_text("# Extra\n\nProposed.\n")
    monkeypatch.chdir(repos["proposed"])
    assert runner.invoke(main, ["push"]).exit_code == 0
    gone = make_project(base, "acme/gone", env)
    plain = base / "plain"
    plain.mkdir()
    git("init", "-q", str(plain), cwd=base, env=env)
    registry.upsert(registry_path(dict(os.environ)), [gone, plain], "manual")
    shutil.rmtree(gone)
    monkeypatch.chdir(tmp_path)
    return repos


def by_name(document: dict, repos: dict[str, Path]) -> dict[str, dict]:  # type: ignore[type-arg]
    names = {str(p.resolve()): n for n, p in repos.items()}
    return {names.get(r["path"], r["path"]): r for r in document["repositories"]}
