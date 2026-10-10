"""Steering document discovery, front matter parsing, snapshots and change plans."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple

import frontmatter
import yaml

from steersync.errors import UnsafePathError
from steersync.paths import ensure_within

log = logging.getLogger(__name__)

LOCK_VERSION = 1
STEERING_SUFFIX = ".md"
INCLUSION_MODES: tuple[str, ...] = ("always", "fileMatch", "manual", "auto")
KNOWN_KEYS: frozenset[str] = frozenset({"inclusion", "fileMatchPattern", "name", "description"})
FILE_REFERENCE_RE = re.compile(r"#\[\[(file|folder):([^\]\n]+?)\]\]")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

Snapshot = dict[str, bytes]
InclusionMode = Literal["always", "fileMatch", "manual", "auto"]


class FrontMatter(NamedTuple):
    """Parsed front matter of a steering document."""

    present: bool
    leading_blank: bool
    metadata: dict[str, Any]
    body: str
    error: str | None


class SteeringDocument(NamedTuple):
    relative_path: str
    raw: bytes
    text: str | None
    front_matter: FrontMatter | None


class ChangePlan(NamedTuple):
    """Result of comparing a source snapshot with a target snapshot."""

    add: tuple[str, ...]
    update: tuple[str, ...]
    delete: tuple[str, ...]
    conflict: tuple[str, ...]
    unchanged: tuple[str, ...]
    local_only: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.update or self.delete)

    @property
    def change_count(self) -> int:
        return len(self.add) + len(self.update) + len(self.delete)


def validate_relative_path(relative: str) -> str:
    """Reject paths that could escape a target directory or hide from discovery."""
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not relative or "\0" in relative or "\\" in relative:
        msg = f"unsafe steering path: {relative!r}"
        raise UnsafePathError(msg)
    if any(part in {"..", ""} or part.startswith(".git") for part in posix.parts):
        msg = f"unsafe steering path: {relative!r}"
        raise UnsafePathError(msg)
    return posix.as_posix()


def discover_steering_files(directory: Path) -> list[Path]:
    """Return steering markdown files under ``directory`` sorted by relative path.

    Hidden directories are skipped. Symlinked files are followed because kiro-cli
    follows them; symlinked directories are not, to match the Kiro IDE.
    """
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        found.extend(
            Path(root) / name
            for name in sorted(files)
            if name.endswith(STEERING_SUFFIX) and not name.startswith(".")
        )
    return found


def snapshot_directory(directory: Path) -> Snapshot:
    """Read every steering file under ``directory`` into memory keyed by relative path."""
    result: Snapshot = {}
    for path in discover_steering_files(directory):
        relative = validate_relative_path(path.relative_to(directory).as_posix())
        result[relative] = path.read_bytes()
    return result


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_digest(snapshot: Mapping[str, bytes]) -> str:
    """Return a stable digest for an entire snapshot (paths and content)."""
    hasher = hashlib.sha256()
    for relative in sorted(snapshot):
        data = snapshot[relative]
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(len(data)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\n")
    return hasher.hexdigest()


def snapshot_hashes(snapshot: Mapping[str, bytes]) -> dict[str, str]:
    return {relative: file_digest(data) for relative, data in snapshot.items()}


def parse_front_matter(text: str) -> FrontMatter:
    """Parse YAML front matter the way Kiro reads it.

    Front matter must start on the very first byte. A leading blank line means
    Kiro treats the whole file as ``inclusion: always`` with no metadata.
    """
    leading_blank = text[:1] in {"\n", "\r"} and text.lstrip("\r\n").startswith("---")
    if leading_blank:
        return FrontMatter(False, True, {}, text, None)
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return FrontMatter(False, False, {}, text, None)
    try:
        post = frontmatter.loads(text)
    except (yaml.YAMLError, ValueError) as exc:
        return FrontMatter(
            True, False, {}, text[match.end() :], f"invalid YAML front matter: {exc}"
        )
    metadata = post.metadata if isinstance(post.metadata, dict) else {}
    return FrontMatter(True, False, dict(metadata), post.content, None)


def read_document(directory: Path, relative: str) -> SteeringDocument:
    path = ensure_within(directory, Path(relative))
    raw = path.read_bytes()
    return document_from_bytes(relative, raw)


def document_from_bytes(relative: str, raw: bytes) -> SteeringDocument:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return SteeringDocument(relative, raw, None, None)
    return SteeringDocument(relative, raw, text, parse_front_matter(text))


def file_references(text: str) -> list[tuple[str, str]]:
    """Return ``(kind, target)`` pairs for ``#[[file:...]]`` and ``#[[folder:...]]`` references."""
    refs: list[tuple[str, str]] = []
    for kind, target in FILE_REFERENCE_RE.findall(text):
        path = target.strip()
        if kind == "file":
            path = re.sub(r":\d+(-\d+)?$", "", path)
        refs.append((kind, path))
    return refs


def plan_changes(
    source: Mapping[str, bytes],
    target: Mapping[str, bytes],
    lock_hashes: Mapping[str, str],
    *,
    prune: bool = True,
) -> ChangePlan:
    """Plan how to make ``target`` match ``source`` while respecting local edits.

    ``lock_hashes`` records what the target looked like after the last sync. A
    target file that differs from both the lock and the source was edited locally
    and is reported as a conflict instead of being overwritten.
    """
    add: list[str] = []
    update: list[str] = []
    delete: list[str] = []
    conflict: list[str] = []
    unchanged: list[str] = []
    local_only: list[str] = []
    target_hashes = snapshot_hashes(target)
    for relative in sorted(source):
        source_hash = file_digest(source[relative])
        current = target_hashes.get(relative)
        if current is None:
            add.append(relative)
        elif current == source_hash:
            unchanged.append(relative)
        elif lock_hashes.get(relative) == current:
            update.append(relative)
        else:
            conflict.append(relative)
    for relative in sorted(set(target) - set(source)):
        if relative not in lock_hashes:
            local_only.append(relative)
        elif lock_hashes[relative] == target_hashes[relative]:
            if prune:
                delete.append(relative)
            else:
                local_only.append(relative)
        else:
            conflict.append(relative)
    return ChangePlan(
        tuple(add),
        tuple(update),
        tuple(delete),
        tuple(sorted(conflict)),
        tuple(unchanged),
        tuple(local_only),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def apply_plan(
    target_dir: Path,
    source: Mapping[str, bytes],
    plan: ChangePlan,
    *,
    force: bool = False,
) -> list[str]:
    """Write adds and updates, remove deletes, and optionally overwrite conflicts.

    Returns the list of relative paths that were written or removed.
    """
    touched: list[str] = []
    writes = list(plan.add) + list(plan.update)
    if force:
        writes.extend(r for r in plan.conflict if r in source)
    for relative in writes:
        validate_relative_path(relative)
        path = ensure_within(target_dir, Path(relative))
        _atomic_write(path, source[relative])
        touched.append(relative)
    removals = list(plan.delete)
    if force:
        removals.extend(r for r in plan.conflict if r not in source)
    for relative in removals:
        validate_relative_path(relative)
        path = ensure_within(target_dir, Path(relative))
        path.unlink(missing_ok=True)
        _remove_empty_parents(target_dir, path.parent)
        touched.append(relative)
    return touched


def _remove_empty_parents(stop: Path, directory: Path) -> None:
    current = directory
    stop_resolved = stop.resolve()
    while current.resolve() != stop_resolved and stop_resolved in current.resolve().parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def replace_directory_contents(target_dir: Path, source: Mapping[str, bytes]) -> list[str]:
    """Make ``target_dir`` contain exactly ``source``. Used inside the cache clone."""
    existing = snapshot_directory(target_dir)
    plan = plan_changes(source, existing, snapshot_hashes(existing), prune=True)
    return apply_plan(target_dir, source, plan, force=True)


def read_lock(path: Path) -> dict[str, Any]:
    """Read a lock file; a missing or corrupt lock yields an empty record."""
    if not path.is_file():
        return {"version": LOCK_VERSION, "files": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError, json.JSONDecodeError:
        log.warning("ignoring unreadable lock file %s", path)
        return {"version": LOCK_VERSION, "files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        log.warning("ignoring malformed lock file %s", path)
        return {"version": LOCK_VERSION, "files": {}}
    return data


def lock_hashes(lock: Mapping[str, Any]) -> dict[str, str]:
    files = lock.get("files", {})
    return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}


def write_lock(path: Path, *, remote: str, commit: str | None, files: Mapping[str, str]) -> None:
    record = {
        "version": LOCK_VERSION,
        "remote": remote,
        "commit": commit,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": dict(sorted(files.items())),
    }
    _atomic_write(path, (json.dumps(record, indent=2, sort_keys=False) + "\n").encode("utf-8"))
