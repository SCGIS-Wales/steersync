"""The workspace registry: every repository steersync has worked in on this machine.

It lives in ``$XDG_STATE_HOME/steersync/workspaces.json`` (see :func:`paths.registry_path`),
never inside a repository, and holds only local paths and timestamps: no URLs, no
tokens. Commands that resolve a workspace register it themselves, so a user never has to;
``steersync fleet scan`` finds repositories set up before the registry existed.

Writes are atomic and serialised with the same operating system lock as the cache, so
two steersync processes never lose each other's entries. A damaged file is set aside as
``workspaces.json.corrupt-<UTC timestamp>`` and a fresh registry started. Registering is
best effort: a failure is logged and never fails the command that triggered it.
"""

import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from steersync.locking import exclusive_lock
from steersync.paths import registry_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 10
Source = Literal["init", "setup", "command", "scan", "manual"]


class Entry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    added_at: str
    last_seen_at: str
    source: Source


class Registry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, Entry] = {}

    def paths(self) -> list[Path]:
        return [Path(key) for key in sorted(self.entries)]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _key(path: Path) -> str:
    return str(path.expanduser().resolve())


def _lock(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def load(path: Path) -> Registry:
    """Read the registry; a missing file is empty, a damaged one is set aside."""
    if not path.is_file():
        return Registry()
    try:
        registry = Registry.model_validate(json.loads(path.read_text("utf-8")))
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            path.replace(target)
        except OSError:
            target = path
        log.warning(
            "the workspace registry %s is damaged (%s); set aside as %s", path, exc, target.name
        )
        return Registry()
    if registry.schema_version != SCHEMA_VERSION:
        log.warning("ignoring workspace registry %s with unknown schema version", path)
        return Registry()
    return registry


def _save(path: Path, registry: Registry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = registry.model_dump_json(indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _update(path: Path, change: Callable[[Registry], Registry]) -> Registry:
    with exclusive_lock(_lock(path), timeout=LOCK_TIMEOUT_SECONDS):
        updated = change(load(path))
        _save(path, updated)
        return updated


def upsert(path: Path, roots: Iterable[Path], source: Source) -> Registry:
    """Add or refresh ``roots``; an existing entry keeps its ``added_at`` and ``source``."""
    now = _now()

    def change(registry: Registry) -> Registry:
        entries = dict(registry.entries)
        for root in roots:
            key = _key(root)
            old = entries.get(key)
            entries[key] = Entry(
                path=key,
                added_at=old.added_at if old else now,
                last_seen_at=now,
                source=old.source if old else source,
            )
        return Registry(entries=entries)

    return _update(path, change)


def remove(path: Path, roots: Iterable[Path]) -> tuple[Registry, list[str]]:
    """Drop ``roots``; returns the registry and the keys that were present."""
    removed: list[str] = []

    def change(registry: Registry) -> Registry:
        entries = dict(registry.entries)
        for root in roots:
            key = _key(root)
            if entries.pop(key, None) is not None:
                removed.append(key)
        return Registry(entries=entries)

    return _update(path, change), removed


def is_work_tree(path: Path) -> bool:
    """A directory with a ``.git`` directory or file (worktrees and submodules)."""
    return path.is_dir() and (path / ".git").exists()


def prune(path: Path) -> tuple[Registry, list[str]]:
    """Drop entries whose path no longer exists or is no longer a git work tree."""
    pruned: list[str] = []

    def change(registry: Registry) -> Registry:
        keep = {}
        for key, entry in registry.entries.items():
            if is_work_tree(Path(key)):
                keep[key] = entry
            else:
                pruned.append(key)
        return Registry(entries=keep)

    return _update(path, change), pruned


def register(env: Mapping[str, str], root: Path | None, source: Source) -> None:
    """Best effort self registration from a command; never raises."""
    if root is None:
        return
    try:
        upsert(registry_path(env), [root], source)
    except Exception as exc:
        log.debug("could not register %s in the workspace registry: %s", root, exc)
