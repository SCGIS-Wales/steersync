"""steersync fleet doctor: read only checks, safe repairs, exit codes."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import backups, gitops
from steersync.cli import main
from steersync.config import Config
from steersync.paths import backup_root, cache_repo_dir, registry_path
from tests.conftest import git, lock_file


@pytest.fixture
def pulled(
    project: Path, config_file: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    monkeypatch.setenv("STEERSYNC_NO_V3_REMINDER", "1")
    assert CliRunner().invoke(main, ["pull", "--no-global"]).exit_code == 0
    return project


def doctor(*args: str) -> tuple[int, list[dict]]:  # type: ignore[type-arg]
    result = CliRunner().invoke(main, ["fleet", "doctor", "--json", *args])
    return result.exit_code, json.loads(result.stdout)


def problems(checks: list[dict]) -> list[str]:  # type: ignore[type-arg]
    return [c["check"] for c in checks if not c["ok"]]


def test_healthy(pulled: Path) -> None:
    code, checks = doctor()
    assert code == 0, checks
    assert {c["check"] for c in checks} >= {
        "registry",
        "work trees",
        "sync record",
        "cache clone",
        "cache lock",
        "backups",
    }
    text = CliRunner().invoke(main, ["fleet", "doctor"]).output
    assert "ok" in text


def test_damaged_cache_clone_is_rebuilt(
    pulled: Path, config: Config, central: Path, env: dict[str, str]
) -> None:
    cache = cache_repo_dir(config, dict(os.environ), str(central))
    git("checkout", "-q", "--detach", cwd=cache, env=env)
    gitops.remove_tree(cache / ".git" / "objects" / "pack")
    for loose in (cache / ".git" / "objects").glob("[0-9a-f][0-9a-f]"):
        gitops.remove_tree(loose)
    code, checks = doctor()
    assert code == 5
    assert "cache clone" in problems(checks)
    code, checks = doctor("--repair")
    assert code == 0, checks
    assert any(c["check"] == "cache clone" and c["repaired"] for c in checks)
    status = CliRunner().invoke(main, ["fleet", "status", "--json", "--no-refresh"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["repositories"][0]["state"] == "in-sync"


def test_damaged_records_are_reported_then_set_aside(pulled: Path) -> None:
    lock_file(pulled).write_text("{truncated")
    reg = registry_path(dict(os.environ))
    code, checks = doctor()
    assert code == 5
    assert "sync record" in problems(checks)
    assert lock_file(pulled).read_text() == "{truncated"  # read only without --repair
    code, checks = doctor("--repair")
    assert code == 0
    assert list(lock_file(pulled).parent.glob("lock.json.corrupt-*"))
    reg.write_text("not json")
    code, checks = doctor()
    assert code == 5
    assert "registry" in problems(checks)
    code, _ = doctor("--repair")
    assert code == 0
    assert list(reg.parent.glob("workspaces.json.corrupt-*"))


def test_missing_work_trees_and_old_backups(
    pulled: Path, tmp_path: Path, env: dict[str, str]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", str(other), cwd=tmp_path, env=env)
    assert CliRunner().invoke(main, ["fleet", "add", str(other)]).exit_code == 0
    gitops.remove_tree(other)
    root = backup_root(dict(os.environ))
    old = root / "20200101T000000000000Z" / "x"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text("{}")
    code, checks = doctor()
    assert code == 5
    assert {"work trees", "backups"} <= set(problems(checks))
    code, checks = doctor("--repair")
    assert code == 0, checks
    assert not old.exists()
    assert backups.usage(root)["backups"] == 0
