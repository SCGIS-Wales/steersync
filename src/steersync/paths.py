"""Path resolution for Kiro directories, locks and the clone cache."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from steersync.config import Config
from steersync.errors import UnsafePathError


def kiro_home(env: Mapping[str, str]) -> Path:
    """Return the Kiro home directory honouring ``KIRO_HOME`` like kiro-cli does."""
    override = env.get("KIRO_HOME")
    if override:
        return Path(override).expanduser()
    return Path(env.get("HOME", "~")).expanduser() / ".kiro"


def global_steering_dir(config: Config, env: Mapping[str, str]) -> Path:
    return kiro_home(env) / config.paths.global_steering


def global_lock_path(config: Config, env: Mapping[str, str]) -> Path:
    return kiro_home(env) / config.paths.global_lock


def workspace_steering_dir(config: Config, workspace_root: Path) -> Path:
    return workspace_root / config.paths.workspace_steering


def workspace_lock_path(config: Config, workspace_root: Path) -> Path:
    return workspace_root / config.paths.workspace_lock


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
