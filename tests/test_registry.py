"""Workspace registry and discovery: every repository steersync has seen on this machine."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from steersync import discovery, registry
from steersync.cli import main
from steersync.hooks import HOOK_MARKER
from steersync.paths import registry_path, state_root
from tests.conftest import git


def make_repo(path: Path, env: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", str(path), cwd=path, env=env)
    return path


def test_state_lives_outside_repositories(env: dict[str, str], tmp_path: Path) -> None:
    assert state_root({"XDG_STATE_HOME": str(tmp_path)}) == tmp_path / "steersync"
    assert state_root({"HOME": "/home/x"}) == Path("/home/x/.local/state/steersync")
    assert registry_path({"XDG_STATE_HOME": "/s"}) == Path("/s/steersync/workspaces.json")


def test_upsert_round_trip_and_remove(tmp_path: Path, env: dict[str, str]) -> None:
    path = tmp_path / "state" / "workspaces.json"
    one = make_repo(tmp_path / "one", env)
    two = make_repo(tmp_path / "two", env)
    registry.upsert(path, [one], "init")
    first = registry.load(path).entries[str(one.resolve())]
    registry.upsert(path, [one, two], "command")
    again = registry.load(path).entries[str(one.resolve())]
    assert (again.added_at, again.source) == (first.added_at, "init")
    assert registry.load(path).paths() == sorted([one.resolve(), two.resolve()])
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert "://" not in path.read_text()
    _, removed = registry.remove(path, [one, tmp_path / "never"])
    assert removed == [str(one.resolve())]
    assert registry.load(path).paths() == [two.resolve()]


def test_prune_drops_missing_and_non_git_paths(tmp_path: Path, env: dict[str, str]) -> None:
    path = tmp_path / "workspaces.json"
    keep = make_repo(tmp_path / "keep", env)
    gone = make_repo(tmp_path / "gone", env)
    plain = tmp_path / "plain"
    plain.mkdir()
    registry.upsert(path, [keep, gone, plain], "scan")
    import shutil

    shutil.rmtree(gone)
    _, pruned = registry.prune(path)
    assert sorted(pruned) == sorted([str(gone.resolve()), str(plain.resolve())])
    assert registry.load(path).paths() == [keep.resolve()]


@pytest.mark.parametrize("content", ["{broken", "[]", '{"schema_version": 1, "entries": 3}'])
def test_damaged_registry_is_set_aside(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "workspaces.json"
    path.write_text(content)
    assert registry.load(path).entries == {}
    aside = list(tmp_path.glob("workspaces.json.corrupt-*"))
    assert len(aside) == 1
    assert aside[0].read_text() == content
    assert "damaged" in caplog.text


def test_concurrent_upserts_from_two_processes_keep_every_entry(tmp_path: Path) -> None:
    path = tmp_path / "workspaces.json"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from steersync import registry\n"
        "base, name, path = sys.argv[1:4]\n"
        "for i in range(25):\n"
        "    registry.upsert(Path(path), [Path(base) / f'{name}-{i}'], 'command')\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path), name, str(path)])
        for name in ("a", "b")
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    assert len(registry.load(path).entries) == 50


def test_register_never_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    registry.register({"XDG_STATE_HOME": str(blocked)}, tmp_path, "command")
    registry.register({}, None, "command")


def test_scan_finds_only_initialised_repositories(tmp_path: Path, env: dict[str, str]) -> None:
    root = tmp_path / "code"
    with_lock = make_repo(root / "a", env)
    (with_lock / ".git" / "steersync").mkdir()
    (with_lock / ".git" / "steersync" / "lock.json").write_text("{}")
    with_hook = make_repo(root / "group" / "b", env)
    (with_hook / ".git" / "hooks" / "pre-push").write_text(f"#!/bin/sh\n{HOOK_MARKER}\n")
    with_exclude = make_repo(root / "c", env)
    (with_exclude / ".git" / "info" / "exclude").write_text("/.kiro/steering/\n")
    with_config = make_repo(root / "d", env)
    (with_config / ".kiro").mkdir()
    (with_config / ".kiro" / "steersync.yaml").write_text("remote: {}\n")
    make_repo(root / "plain", env)
    make_repo(root / "node_modules" / "pkg", env)
    (root / "node_modules" / "pkg" / ".kiro").mkdir()
    (root / "node_modules" / "pkg" / ".kiro" / "steersync.yaml").write_text("")
    deep = make_repo(root / "x" / "y" / "z" / "w" / "deep", env)
    (deep / ".kiro").mkdir()
    (deep / ".kiro" / "steersync.yaml").write_text("")
    # A repository inside a repository is not searched.
    nested = make_repo(with_lock / "vendor" / "inner", env)
    (nested / ".kiro").mkdir()
    (nested / ".kiro" / "steersync.yaml").write_text("")
    found = discovery.scan([root], max_depth=4)
    assert found == sorted(p.resolve() for p in (with_lock, with_hook, with_exclude, with_config))
    assert deep.resolve() in discovery.scan([root], max_depth=6)


def test_worktree_git_file_is_followed(tmp_path: Path, env: dict[str, str]) -> None:
    main_repo = make_repo(tmp_path / "main", env)
    (main_repo / "f").write_text("x")
    git("add", "-A", cwd=main_repo, env=env)
    git("commit", "-qm", "x", cwd=main_repo, env=env)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), cwd=main_repo, env=env)
    git_dir = discovery.git_dir_of(linked)
    assert git_dir is not None
    assert git_dir.parent.name == "worktrees"
    (git_dir / "steersync").mkdir()
    (git_dir / "steersync" / "lock.json").write_text("{}")
    assert discovery.is_initialised(linked)


def test_fleet_cli_commands(
    project: Path, config_file: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    runner = CliRunner()
    assert "no repositories registered" in runner.invoke(main, ["fleet", "list"]).output
    assert runner.invoke(main, ["init"]).exit_code == 0
    listed = json.loads(runner.invoke(main, ["fleet", "list", "--json"]).output)
    assert [e["path"] for e in listed] == [str(project.resolve())]
    assert listed[0]["source"] == "init"
    other = make_repo(project.parent / "other", env)
    assert "registered" in runner.invoke(main, ["fleet", "add", str(other)]).output
    bad = runner.invoke(main, ["fleet", "add", str(project.parent)])
    assert bad.exit_code != 0
    assert "not a git work tree" in bad.output
    import shutil

    shutil.rmtree(other)
    assert "(missing)" in runner.invoke(main, ["fleet", "list"]).output
    assert "pruned" in runner.invoke(main, ["fleet", "prune"]).output
    assert "nothing to prune" in runner.invoke(main, ["fleet", "prune"]).output
    assert "removed" in runner.invoke(main, ["fleet", "remove", str(project)]).output
    scanned = runner.invoke(main, ["fleet", "scan", str(project.parent)])
    assert scanned.exit_code == 0
    assert str(project.resolve()) in scanned.output
    assert "1 repository found and registered" in scanned.output
    status = json.loads(runner.invoke(main, ["fleet", "status", "--json"]).stdout)
    assert str(project.resolve()) in [r["path"] for r in status["repositories"]]
    assert "Repository" in runner.invoke(main, ["fleet"]).output  # no terminal: the table


def test_workspace_commands_register_themselves(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    assert CliRunner().invoke(main, ["pull", "--no-global"]).exit_code == 0
    assert registry.load(registry_path(os.environ)).paths() == [project.resolve()]
