"""Path resolution for Kiro directories, collection targets, locks and the clone cache."""

import hashlib
import sys
from collections.abc import Mapping
from pathlib import Path

from steersync.config import CollectionConfig, Config
from steersync.errors import UnsafePathError

DEFAULT_WORKSPACE_STEERING = ".kiro/steering"
DEFAULT_GLOBAL_STEERING = "steering"


def kiro_home(env: Mapping[str, str]) -> Path:
    """Return the Kiro home directory honouring ``KIRO_HOME`` like kiro-cli does."""
    override = env.get("KIRO_HOME")
    if override:
        return Path(override).expanduser()
    return Path(env.get("HOME", "~")).expanduser() / ".kiro"


def global_collection_dir(collection: CollectionConfig, env: Mapping[str, str]) -> Path | None:
    """Return the global target directory of a collection, or ``None`` without a global scope."""
    if collection.global_path is None:
        return None
    return kiro_home(env) / collection.global_path


def workspace_collection_dir(collection: CollectionConfig, workspace_root: Path) -> Path | None:
    """Return the workspace target directory of a collection, or ``None`` without one."""
    if collection.workspace_path is None:
        return None
    return workspace_root / collection.workspace_path


def central_collection_dir(base: Path, collection: CollectionConfig) -> Path:
    """Return the collection directory under a central ``global/`` or project directory."""
    return ensure_within(base, Path(collection.central_dir or ""))


def global_steering_dir(config: Config, env: Mapping[str, str]) -> Path:
    """Return the global steering directory (the ``steering`` collection or its default)."""
    steering = config.collections.get("steering")
    relative = (
        steering.global_path
        if steering is not None and steering.global_path is not None
        else DEFAULT_GLOBAL_STEERING
    )
    return kiro_home(env) / relative


def workspace_steering_dir(config: Config, workspace_root: Path) -> Path:
    """Return the workspace steering directory (the ``steering`` collection or its default)."""
    steering = config.collections.get("steering")
    relative = (
        steering.workspace_path
        if steering is not None and steering.workspace_path is not None
        else DEFAULT_WORKSPACE_STEERING
    )
    return workspace_root / relative


def global_lock_path(config: Config, env: Mapping[str, str]) -> Path:
    return kiro_home(env) / config.paths.global_lock


# Where steersync 0.6 and earlier kept the workspace record, inside the work tree.
LEGACY_WORKSPACE_LOCK = Path(".kiro") / "steersync.lock.json"


def workspace_lock_path(git_dir: Path) -> Path:
    """The workspace's last sync record, kept under the git directory, never in the work
    tree. A linked worktree has its own git directory, so it gets its own record."""
    return git_dir / "steersync" / "lock.json"


def state_root(env: Mapping[str, str]) -> Path:
    """Machine local state that is not configuration and not cache: the workspace registry
    and pull backups. ``$XDG_STATE_HOME/steersync`` (default ``~/.local/state/steersync``),
    ``%LOCALAPPDATA%\\steersync`` on Windows."""
    if sys.platform == "win32" and env.get("LOCALAPPDATA"):  # pragma: no cover - Windows
        return Path(env["LOCALAPPDATA"]) / "steersync"
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "steersync"
    return Path(env.get("HOME", "~")).expanduser() / ".local" / "state" / "steersync"


def registry_path(env: Mapping[str, str]) -> Path:
    """Every workspace steersync has seen on this machine (never inside a repository)."""
    return state_root(env) / "workspaces.json"


def backup_root(env: Mapping[str, str]) -> Path:
    """Copies of local files taken before a forced pull overwrites them."""
    return state_root(env) / "backups"


def cache_root(config: Config, env: Mapping[str, str]) -> Path:
    raw = config.paths.cache_dir
    if raw.startswith("~"):
        home = env.get("HOME")
        return Path(home) / raw[2:] if home else Path(raw).expanduser()
    return Path(raw)


def cache_repo_dir(config: Config, env: Mapping[str, str], remote_url: str) -> Path:
    """Return a stable cache directory for a remote URL."""
    digest = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:16]
    return cache_root(config, env) / "repos" / digest


def ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and ensure it stays inside ``base``."""
    base_resolved = base.resolve()
    target = (
        (base_resolved / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    if target != base_resolved and base_resolved not in target.parents:
        msg = f"path {candidate} escapes {base}"
        raise UnsafePathError(msg)
    return target
