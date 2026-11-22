"""Task A: no forced overwrite without a verified backup; restore puts it back."""

import errno
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from steersync import backups, sync
from steersync.backups import BackupError
from steersync.config import Config
from steersync.paths import backup_root
from tests.conftest import CentralCommit, central_project, posix_only

MINE = "# Mine\n\nLocal edit.\n"


@pytest.fixture
def edited(
    config: Config, env: dict[str, str], project: Path, central_commit: CentralCommit
) -> Path:
    """A local edit and a central edit of the same file: only a forced pull overwrites it."""
    sync.pull(config, workspace_root=project, env=env, include_global=False)
    local = project / ".kiro" / "steering" / "python.md"
    local.write_text(MINE)
    local.chmod(0o640)  # Windows keeps only the read only bit; compare with what it reports
    central_commit(lambda w: (central_project(w) / "python.md").write_text("# Theirs\n"), "edit")
    return local


def force(config: Config, env: dict[str, str], project: Path) -> sync.PullResult:
    return sync.pull(
        config, workspace_root=project, env=dict(os.environ) | env, include_global=False, force=True
    )


def test_forced_pull_backs_up_then_restore_reproduces_bytes_and_modes(
    config: Config, env: dict[str, str], project: Path, edited: Path
) -> None:
    mode = stat.S_IMODE(edited.stat().st_mode)  # 0o640, or what Windows keeps of it
    result = force(config, env, project)
    assert edited.read_text() == "# Theirs\n"
    (directory,) = result.backups
    backup = backups.load(Path(directory))
    assert [item.path for item in backup.items] == [".kiro/steering/python.md"]
    manifest = json.loads((Path(directory) / "manifest.json").read_text())
    assert manifest["items"][0]["mode"] == mode
    restored = backups.restore(backup)
    assert restored == [".kiro/steering/python.md"]
    assert edited.read_text() == MINE
    assert stat.S_IMODE(edited.stat().st_mode) == mode
    assert backups.listing(backup_root(dict(os.environ)))[0].directory == backup.directory


@posix_only
def test_read_only_backup_directory_blocks_the_overwrite(
    config: Config, env: dict[str, str], project: Path, edited: Path
) -> None:
    root = backup_root(dict(os.environ))
    root.mkdir(parents=True)
    root.chmod(0o500)
    try:
        with pytest.raises(BackupError, match="not touched"):
            force(config, env, project)
    finally:
        root.chmod(0o700)
    assert edited.read_text() == MINE


def test_disk_full_part_way_blocks_the_overwrite(
    config: Config,
    env: dict[str, str],
    project: Path,
    edited: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = project / ".kiro" / "steering" / "second.md"
    central_commit(lambda w: (central_project(w) / "second.md").write_text("# Second\n"), "add")
    sync.pull(config, workspace_root=project, env=env, include_global=False)
    second.write_text("# Second, mine\n")
    central_commit(lambda w: (central_project(w) / "second.md").write_text("# Second v2\n"), "v2")
    real_copy = shutil.copy2
    calls: list[object] = []

    def copy_until_full(source: Path, destination: Path) -> object:
        calls.append(source)
        if len(calls) > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(source, destination)

    monkeypatch.setattr(shutil, "copy2", copy_until_full)
    with pytest.raises(BackupError, match="No space left"):
        force(config, env, project)
    assert len(calls) == 2
    assert edited.read_text() == MINE
    assert second.read_text() == "# Second, mine\n"


def test_file_changed_between_backup_and_apply_aborts(
    config: Config,
    env: dict[str, str],
    project: Path,
    edited: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_take = backups.take

    def take_then_edit(*args: object, **kwargs: object) -> backups.Backup:
        taken = real_take(*args, **kwargs)  # type: ignore[arg-type]
        edited.write_text("# Edited again while backing up\n")
        return taken

    monkeypatch.setattr(backups, "take", take_then_edit)
    with pytest.raises(BackupError, match="changed after the backup"):
        force(config, env, project)
    assert edited.read_text() == "# Edited again while backing up\n"


def test_tampered_backup_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "a").mkdir(parents=True)
    (workspace / "a" / "f.md").write_text("one")
    backup = backups.take(tmp_path / "backups", workspace, ["a/f.md"], project="acme/x")
    (backup.directory / "files" / "a" / "f.md").write_text("two")
    with pytest.raises(BackupError, match="does not match"):
        backups.restore(backup)
    (backup.directory / "files" / "a" / "f.md").unlink()
    with pytest.raises(BackupError, match="holds 0 files"):
        backups.verify(backup)
    with pytest.raises(BackupError, match="manifest"):
        backups.load(tmp_path / "nowhere")


def test_prune_by_age_and_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    root = tmp_path / "backups"
    for stamp, size in (
        ("20260101T000000000000Z", 10),
        ("20261120T000000000000Z", 300),
        ("20261121T000000000000Z", 300),
    ):
        (root / stamp / "x").mkdir(parents=True)
        (root / stamp / "x" / "blob").write_bytes(b"x" * size)
    now = datetime(2026, 11, 22, tzinfo=UTC)
    result = backups.prune(root, keep_days=14, max_bytes=400, now=now)
    assert result.removed == ("20260101T000000000000Z", "20261120T000000000000Z")
    assert [p.name for p in root.iterdir()] == ["20261121T000000000000Z"]
