"""Verified backups of local files before a forced pull overwrites them.

``take`` copies every file that will be replaced or deleted into
``$XDG_STATE_HOME/steersync/backups/<UTC timestamp>/<project slug>/``, preserving
relative paths and modes, writes ``manifest.json`` (path, SHA-256, mode, size) and then
verifies the copy against the manifest. Any failure (disk full, permission denied, a
hash mismatch) raises :class:`BackupError` and the caller must not overwrite anything.
``verify_unchanged`` re-hashes the originals just before the overwrite, so a file edited
in between aborts it. ``restore`` verifies the backup before copying it back. There is
no way to skip a backup.
"""

import contextlib
import hashlib
import json
import shutil
import stat
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from steersync import gitops
from steersync.errors import SteersyncError

MANIFEST = "manifest.json"
SCHEMA_VERSION = 1


class BackupError(SteersyncError):
    """A backup could not be taken or verified; nothing was overwritten."""

    exit_code = 5


class Item(NamedTuple):
    path: str  # relative to the workspace root
    sha256: str
    mode: int
    size: int
    # The file did not exist before the run: restoring removes it again.
    created: bool = False


class Backup(NamedTuple):
    directory: Path  # .../backups/<stamp>/<slug>
    stamp: str
    project: str
    workspace: str
    items: tuple[Item, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _slug_dir(project: str) -> str:
    return project.replace("/", "__") or "workspace"


def take(
    root: Path,
    workspace: Path,
    relatives: Iterable[str],
    *,
    project: str,
    stamp: str | None = None,
    record_missing: bool = False,
) -> Backup:
    """Copy and verify ``relatives`` (paths under ``workspace``) before an overwrite.

    With ``record_missing`` a path that does not exist yet is recorded as created, so a
    restore removes the file the run is about to create.
    """
    when = stamp or stamp_now()
    target = root / when / _slug_dir(project)
    items: list[Item] = []
    try:
        target.mkdir(parents=True, exist_ok=False)
        for relative in sorted(set(relatives)):
            source = workspace / relative
            if not source.is_file():
                if record_missing and not source.exists():
                    items.append(Item(relative, "", 0, 0, created=True))
                continue
            destination = target / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            mode = stat.S_IMODE(source.stat().st_mode)
            items.append(Item(relative, _sha256(source), mode, source.stat().st_size))
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "project": project,
            "workspace": str(workspace),
            "created_at": when,
            "items": [item._asdict() for item in items],
        }
        (target / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        msg = f"backup to {target} failed ({exc}); local files were not touched"
        raise BackupError(msg) from exc
    backup = Backup(target, when, project, str(workspace), tuple(items))
    verify(backup)
    return backup


def verify(backup: Backup) -> None:
    """The copies exist with the recorded hash, mode and size, and nothing else is there."""
    files = backup.directory / "files"
    try:
        present = (
            {str(p.relative_to(files).as_posix()) for p in files.rglob("*") if p.is_file()}
            if files.exists()
            else set()
        )
        expected = {item.path for item in backup.items if not item.created}
        if present != expected:
            msg = f"backup {backup.directory} holds {len(present)} files, expected {len(expected)}"
            raise BackupError(msg)
        for item in backup.items:
            if item.created:
                continue
            copy = files / item.path
            if _sha256(copy) != item.sha256:
                raise BackupError(f"backup copy of {item.path} does not match its original")
            if stat.S_IMODE(copy.stat().st_mode) != item.mode:
                raise BackupError(f"backup copy of {item.path} lost its file mode")
            if copy.stat().st_size != item.size:
                raise BackupError(f"backup copy of {item.path} has the wrong size")
    except OSError as exc:
        raise BackupError(f"backup {backup.directory} cannot be verified: {exc}") from exc


def verify_unchanged(backup: Backup, workspace: Path) -> None:
    """The originals still match the backup (nothing changed between backup and apply)."""
    for item in backup.items:
        current = workspace / item.path
        try:
            if item.created:
                changed = current.exists()
            else:
                changed = not current.is_file() or _sha256(current) != item.sha256
        except OSError as exc:
            raise BackupError(f"cannot re-read {item.path}: {exc}") from exc
        if changed:
            msg = f"{item.path} changed after the backup was taken; nothing was overwritten"
            raise BackupError(msg)


def load(directory: Path) -> Backup:
    """Read a backup from its ``<stamp>/<slug>`` directory."""
    try:
        data = json.loads((directory / MANIFEST).read_text("utf-8"))
        items = tuple(Item(**item) for item in data["items"])
        return Backup(
            directory, str(data["created_at"]), str(data["project"]), str(data["workspace"]), items
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"backup {directory} has no readable manifest: {exc}") from exc


def listing(root: Path) -> list[Backup]:
    """Every readable backup, newest first."""
    found: list[Backup] = []
    if not root.is_dir():
        return found
    for stamp_dir in sorted(root.iterdir(), reverse=True):
        for slug_dir in sorted(stamp_dir.iterdir()) if stamp_dir.is_dir() else []:
            try:
                found.append(load(slug_dir))
            except BackupError:
                continue
    return found


def restore(backup: Backup, *, workspace: Path | None = None) -> list[str]:
    """Verify the backup, then copy every file back with its mode."""
    verify(backup)
    target = workspace or Path(backup.workspace)
    restored: list[str] = []
    for item in backup.items:
        destination = target / item.path
        if item.created:
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
                restored.append(item.path)
            continue
        source = backup.directory / "files" / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(item.mode)
        restored.append(item.path)
    return restored


def take_files(root: Path, paths: Iterable[Path], *, label: str) -> Backup:
    """Back up absolute paths anywhere (setup touches ~/.config, ~/.kiro and the repository).

    Paths that do not exist yet are recorded as created, so ``steersync restore`` removes
    them again. ``label`` names the run in ``steersync restore --list``.
    """
    absolute = [path.expanduser().absolute() for path in paths]
    anchor = Path(absolute[0].anchor) if absolute else Path("/")
    relatives = [path.relative_to(anchor).as_posix() for path in absolute]
    return take(root, anchor, relatives, project=label, record_missing=True)


class PruneResult(NamedTuple):
    removed: tuple[str, ...]
    kept_bytes: int


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def prune(
    root: Path,
    *,
    keep_days: int,
    max_bytes: int,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PruneResult:
    """Drop backups older than ``keep_days``, then the oldest until under ``max_bytes``."""
    if not root.is_dir():
        return PruneResult((), 0)
    current = now or datetime.now(UTC)
    stamps = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    removed: list[str] = []
    survivors: list[tuple[Path, int]] = []
    for path in stamps:
        try:
            created = datetime.strptime(path.name, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            created = current
        if current - created > timedelta(days=keep_days):
            removed.append(path.name)
        else:
            survivors.append((path, _size(path)))
    total = sum(size for _, size in survivors)
    while survivors and total > max_bytes:
        path, size = survivors.pop(0)
        removed.append(path.name)
        total -= size
    if not dry_run:
        for name in removed:
            with contextlib.suppress(OSError):  # a backup still in use is pruned next time
                gitops.remove_tree(root / name)
    return PruneResult(tuple(removed), total)


def usage(root: Path) -> Mapping[str, int]:
    """Number of backups and their total size."""
    if not root.is_dir():
        return {"backups": 0, "bytes": 0}
    stamps = [p for p in root.iterdir() if p.is_dir()]
    return {"backups": len(stamps), "bytes": sum(_size(p) for p in stamps)}
