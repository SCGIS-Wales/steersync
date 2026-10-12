"""Configuration loading and validation.

Configuration is layered: packaged defaults, then an optional YAML file, then
environment variables. Everything is validated with pydantic before use.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from steersync.errors import ConfigError

ENV_CONFIG = "STEERSYNC_CONFIG"
ENV_REMOTE = "STEERSYNC_REMOTE"
ENV_DEFAULT_BRANCH = "STEERSYNC_DEFAULT_BRANCH"
ENV_CACHE_DIR = "STEERSYNC_CACHE_DIR"
ENV_LOG_LEVEL = "STEERSYNC_LOG_LEVEL"
ENV_LOG_FORMAT = "STEERSYNC_LOG_FORMAT"
ENV_STRICT = "STEERSYNC_STRICT"
ENV_GIT_BINARY = "STEERSYNC_GIT_BINARY"

CONFIG_FILE_NAMES: tuple[str, ...] = ("steersync.yaml", "steersync.yml")

_SCP_LIKE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.\-\[\]]+:(?![/\\]{2})[^\s]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_ALLOWED_PLACEHOLDERS = frozenset({"default_branch", "project", "user", "hash"})
_COLLECTION_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

LintProfile = Literal["steering", "markdown", "skill", "agent", "hooks", "json", "generic"]
LINT_PROFILES: tuple[str, ...] = (
    "steering",
    "markdown",
    "skill",
    "agent",
    "hooks",
    "json",
    "generic",
)
# Deprecated ``paths`` keys and the collection setting that replaced each one.
_LEGACY_PATH_KEYS: dict[str, tuple[str, str]] = {
    "workspace_steering": ("steering", "workspace_path"),
    "global_steering": ("steering", "global_path"),
}

log = logging.getLogger(__name__)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ForbiddenPattern(_Strict):
    """A regular expression that must not appear in a steering document."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    pattern: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1, max_length=200)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"invalid regular expression: {exc}"
            raise ValueError(msg) from exc
        return value


class PyMarkdownConfig(_Strict):
    enabled: bool = True
    severity: Literal["error", "warning"] = "warning"
    disabled_rules: tuple[str, ...] = ()


class LintConfig(_Strict):
    max_file_bytes: int = Field(default=65536, ge=1024, le=10_000_000)
    require_h1: bool = True
    allowed_extra_keys: tuple[str, ...] = ("x-steersync",)
    file_reference_check: bool = True
    pymarkdown: PyMarkdownConfig = PyMarkdownConfig()
    forbidden_patterns: tuple[ForbiddenPattern, ...] = ()


class RemoteConfig(_Strict):
    url: str = ""
    default_branch: str = Field(default="main", min_length=1, max_length=200)
    global_dir: str = Field(default="global", min_length=1)
    projects_dir: str = Field(default="projects", min_length=1)
    allowed_hosts: tuple[str, ...] = ()
    allow_local: bool = False
    push_options: tuple[str, ...] = ()

    @field_validator("global_dir", "projects_dir")
    @classmethod
    def _relative_dir(cls, value: str) -> str:
        parts = Path(value).parts
        if Path(value).is_absolute() or ".." in parts or not parts:
            msg = "must be a relative directory inside the repository"
            raise ValueError(msg)
        return value.strip("/")

    @field_validator("push_options")
    @classmethod
    def _push_options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for option in value:
            if "\n" in option or "\r" in option or not option.strip():
                msg = "push options must be single non empty lines"
                raise ValueError(msg)
            unknown = set(_PLACEHOLDER_RE.findall(option)) - _ALLOWED_PLACEHOLDERS
            if unknown:
                msg = f"unknown placeholder(s) in push option: {sorted(unknown)}"
                raise ValueError(msg)
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            if not _HOST_RE.match(host):
                msg = f"invalid host name: {host!r}"
                raise ValueError(msg)
        return tuple(h.lower() for h in value)


class GitConfig(_Strict):
    binary: str = Field(default="git", min_length=1)
    timeout_seconds: int = Field(default=120, ge=5, le=3600)
    clone_depth: int = Field(default=1, ge=1, le=1000)
    retries: int = Field(default=2, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=2.0, ge=0, le=60)
    retry_max_backoff_seconds: float = Field(default=30.0, ge=0, le=600)

    @field_validator("binary")
    @classmethod
    def _binary(cls, value: str) -> str:
        if value.startswith("-") or any(c in value for c in "\n\r\0"):
            msg = "invalid git binary"
            raise ValueError(msg)
        return value


class BranchConfig(_Strict):
    template: str = Field(default="steering/{project}/{user}-{hash}", min_length=1)
    hash_length: int = Field(default=8, ge=6, le=64)

    @field_validator("template")
    @classmethod
    def _template(cls, value: str) -> str:
        names = set(_PLACEHOLDER_RE.findall(value))
        unknown = names - {"project", "user", "hash"}
        if unknown:
            msg = f"unknown placeholder(s) in branch template: {sorted(unknown)}"
            raise ValueError(msg)
        if "{hash}" not in value:
            msg = "branch template must contain {hash} so identical pushes dedupe"
            raise ValueError(msg)
        return value


def _relative_path(value: str) -> str:
    """Validate a path that must stay inside its base directory."""
    if Path(value).is_absolute() or ".." in Path(value).parts or not value.strip():
        msg = "must be a relative path without parent references"
        raise ValueError(msg)
    return value.strip().strip("/")


class CollectionConfig(_Strict):
    """One kind of Kiro content that is synced as a unit.

    A collection maps a directory in the workspace (and optionally one under the
    Kiro home) onto a directory in the central repository, and says which files
    inside it are synced and how they are validated.
    """

    enabled: bool = True
    description: str = Field(default="", max_length=200)
    # Directory relative to the workspace root; ``null`` means the collection has no
    # workspace scope (it is only synced globally).
    workspace_path: str | None = None
    # Directory relative to the Kiro home (``KIRO_HOME`` or ``~/.kiro``); ``null`` means
    # the collection has no global scope.
    global_path: str | None = None
    # Directory under ``global/`` and ``projects/<slug>/`` in the central repository.
    # Defaults to the collection name.
    central_dir: str | None = None
    # Glob patterns relative to the collection directory. ``**`` spans directories.
    include: tuple[str, ...] = ("**/*",)
    exclude: tuple[str, ...] = ()
    # Limit discovery to this many directory levels (1 = only the top level).
    max_depth: int | None = Field(default=None, ge=1, le=64)
    lint: LintProfile = "generic"
    # Overrides ``lint.max_file_bytes`` for this collection.
    max_file_bytes: int | None = Field(default=None, ge=1024, le=100_000_000)
    # Add the workspace directory to ``.git/info/exclude`` on ``steersync init``.
    git_exclude: bool = True

    @field_validator("workspace_path", "global_path", "central_dir")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_path(value)

    @field_validator("include", "exclude")
    @classmethod
    def _patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if not pattern.strip() or pattern.startswith(("/", "\\")) or "\\" in pattern:
                msg = f"invalid glob pattern {pattern!r}: patterns are relative posix globs"
                raise ValueError(msg)
            if ".." in PurePosixPath(pattern).parts or "\0" in pattern:
                msg = f"invalid glob pattern {pattern!r}: parent references are not allowed"
                raise ValueError(msg)
        return value

    @field_validator("include")
    @classmethod
    def _not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            msg = "include must list at least one glob pattern"
            raise ValueError(msg)
        return value


class PathsConfig(_Strict):
    workspace_lock: str = ".kiro/steersync.lock.json"
    global_lock: str = "steersync.lock.json"
    cache_dir: str = "~/.cache/steersync"

    @field_validator("workspace_lock", "global_lock")
    @classmethod
    def _relative(cls, value: str) -> str:
        return _relative_path(value)


class SyncConfig(_Strict):
    prune: bool = True
    strict_pull: bool = False
    lock_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_files: int = Field(default=1000, ge=1, le=100_000)
    max_total_bytes: int = Field(default=10_485_760, ge=1024, le=1_073_741_824)


class HookConfig(_Strict):
    strict: bool = False
    timeout_seconds: int = Field(default=60, ge=5, le=3600)


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["text", "json"] = "text"


class Config(_Strict):
    """Fully validated steersync configuration."""

    remote: RemoteConfig = RemoteConfig()
    git: GitConfig = GitConfig()
    branch: BranchConfig = BranchConfig()
    paths: PathsConfig = PathsConfig()
    collections: dict[str, CollectionConfig] = {}
    sync: SyncConfig = SyncConfig()
    lint: LintConfig = LintConfig()
    hook: HookConfig = HookConfig()
    logging: LoggingConfig = LoggingConfig()
    source_file: Path | None = None

    @field_validator("collections", mode="before")
    @classmethod
    def _default_central_dir(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        filled: dict[str, Any] = {}
        for name, raw in value.items():
            needs_default = isinstance(raw, dict) and raw.get("central_dir") is None
            filled[name] = {**raw, "central_dir": name} if needs_default else raw
        return filled

    @field_validator("collections")
    @classmethod
    def _collections(cls, value: dict[str, CollectionConfig]) -> dict[str, CollectionConfig]:
        seen: dict[str, dict[str, str]] = {
            "central_dir": {},
            "workspace_path": {},
            "global_path": {},
        }
        for name, collection in value.items():
            if not _COLLECTION_NAME_RE.match(name):
                msg = f"invalid collection name {name!r}: use lowercase letters, digits and dashes"
                raise ValueError(msg)
            if collection.workspace_path is None and collection.global_path is None:
                msg = f"collection {name!r} needs a workspace_path or a global_path"
                raise ValueError(msg)
            if not collection.enabled:
                continue
            for key, used in seen.items():
                path = getattr(collection, key)
                if path is None:
                    continue
                if path in used:
                    msg = f"collections {used[path]!r} and {name!r} share the same {key} {path!r}"
                    raise ValueError(msg)
                used[path] = name
        return value

    def enabled_collections(self) -> dict[str, CollectionConfig]:
        """Return the enabled collections in configuration order."""
        return {name: c for name, c in self.collections.items() if c.enabled}

    def select_collections(self, names: Sequence[str] | None) -> dict[str, CollectionConfig]:
        """Return the enabled collections named in ``names`` (all of them when empty)."""
        enabled = self.enabled_collections()
        if not names:
            return enabled
        unknown = [n for n in names if n not in self.collections]
        if unknown:
            msg = f"unknown collection(s): {', '.join(sorted(unknown))}"
            raise ConfigError(msg)
        disabled = [n for n in names if n not in enabled]
        if disabled:
            msg = f"collection(s) disabled in configuration: {', '.join(sorted(disabled))}"
            raise ConfigError(msg)
        return {name: enabled[name] for name in dict.fromkeys(names)}


def default_config_text() -> str:
    """Return the packaged default configuration as YAML text."""
    return resources.files("steersync.resources").joinpath("default_config.yaml").read_text("utf-8")


def _load_yaml(text: str, origin: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"{origin}: invalid YAML: {exc}"
        raise ConfigError(msg) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        msg = f"{origin}: top level must be a mapping"
        raise ConfigError(msg)
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` merged in. Nested mappings merge recursively."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _set_nested(data: dict[str, Any], dotted: str, value: Any) -> None:  # noqa: ANN401
    node = data
    *parents, leaf = dotted.split(".")
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[leaf] = value


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    mapping = {
        ENV_REMOTE: "remote.url",
        ENV_DEFAULT_BRANCH: "remote.default_branch",
        ENV_CACHE_DIR: "paths.cache_dir",
        ENV_LOG_LEVEL: "logging.level",
        ENV_LOG_FORMAT: "logging.format",
        ENV_GIT_BINARY: "git.binary",
    }
    overrides: dict[str, Any] = {}
    for env_name, dotted in mapping.items():
        raw = env.get(env_name)
        if raw is not None and raw != "":
            value: Any = raw.upper() if dotted == "logging.level" else raw
            _set_nested(overrides, dotted, value)
    strict = env.get(ENV_STRICT)
    if strict is not None:
        _set_nested(overrides, "hook.strict", strict.strip().lower() in {"1", "true", "yes", "on"})
    return overrides


def find_config_file(
    workspace_root: Path | None,
    env: Mapping[str, str],
    explicit: Path | None = None,
) -> Path | None:
    """Locate the configuration file using the documented precedence."""
    if explicit is not None:
        if not explicit.is_file():
            msg = f"configuration file not found: {explicit}"
            raise ConfigError(msg)
        return explicit
    from_env = env.get(ENV_CONFIG)
    if from_env:
        candidate = Path(from_env).expanduser()
        if not candidate.is_file():
            msg = f"{ENV_CONFIG} points to a missing file: {candidate}"
            raise ConfigError(msg)
        return candidate
    candidates: list[Path] = []
    if workspace_root is not None:
        candidates.extend(workspace_root / ".kiro" / name for name in CONFIG_FILE_NAMES)
    xdg = env.get("XDG_CONFIG_HOME")
    config_home = (
        Path(xdg).expanduser() if xdg else Path(env.get("HOME", "~")).expanduser() / ".config"
    )
    candidates.extend(config_home / "steersync" / name for name in ("config.yaml", "config.yml"))
    return next((c for c in candidates if c.is_file()), None)


def load_config(
    *,
    workspace_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    explicit: Path | None = None,
) -> Config:
    """Load, merge and validate configuration."""
    environment = os.environ if env is None else env
    data = _load_yaml(default_config_text(), "packaged defaults")
    source = find_config_file(workspace_root, environment, explicit)
    if source is not None:
        try:
            text = source.read_text("utf-8")
        except OSError as exc:
            msg = f"cannot read configuration file {source}: {exc}"
            raise ConfigError(msg) from exc
        data = deep_merge(data, _load_yaml(text, str(source)))
    data = deep_merge(data, _env_overrides(environment))
    data = _migrate_legacy_paths(data, str(source) if source else "configuration")
    data["source_file"] = source
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        msg = f"invalid configuration: {details}"
        raise ConfigError(msg) from exc


def _migrate_legacy_paths(data: dict[str, Any], origin: str) -> dict[str, Any]:
    """Map the pre 0.2 ``paths.*_steering`` keys onto the steering collection."""
    paths_section = data.get("paths")
    if not isinstance(paths_section, dict):
        return data
    legacy = {k: v for k, v in paths_section.items() if k in _LEGACY_PATH_KEYS}
    if not legacy:
        return data
    migrated = dict(data)
    migrated["paths"] = {k: v for k, v in paths_section.items() if k not in _LEGACY_PATH_KEYS}
    collections = dict(migrated.get("collections") or {})
    for key, value in legacy.items():
        collection, setting = _LEGACY_PATH_KEYS[key]
        existing = collections.get(collection)
        target = dict(existing) if isinstance(existing, dict) else {}
        target[setting] = value
        collections[collection] = target
        log.warning(
            "%s: paths.%s is deprecated; use collections.%s.%s", origin, key, collection, setting
        )
    migrated["collections"] = collections
    return migrated


def validate_remote_url(
    url: str, *, allowed_hosts: tuple[str, ...] = (), allow_local: bool = False
) -> str:
    """Validate a git remote URL and return it unchanged.

    Only ssh://, https://, git@host:path and (optionally) local file URLs are accepted.
    Anything that could be interpreted as a git option or a helper transport is rejected.
    """
    candidate = url.strip()
    if not candidate:
        msg = "remote URL is empty; set remote.url in configuration or STEERSYNC_REMOTE"
        raise ConfigError(msg)
    if candidate.startswith("-") or any(c in candidate for c in "\n\r\0 \t"):
        msg = "remote URL contains illegal characters"
        raise ConfigError(msg)
    if "::" in candidate:
        msg = "remote helper transports (ext::, fd::) are not allowed"
        raise ConfigError(msg)
    host = _remote_host(candidate, allow_local=allow_local)
    if allowed_hosts and host and host.lower() not in allowed_hosts:
        msg = f"remote host {host!r} is not in remote.allowed_hosts"
        raise ConfigError(msg)
    return candidate


def _remote_host(url: str, *, allow_local: bool) -> str | None:
    parts = urlsplit(url)
    if parts.scheme in {"ssh", "https", "git+ssh", "ssh+git"}:
        if not parts.hostname:
            msg = f"remote URL has no host: {url}"
            raise ConfigError(msg)
        return parts.hostname
    if parts.scheme == "file" or url.startswith("/"):
        if not allow_local:
            msg = "local remotes are disabled; set remote.allow_local to true to permit them"
            raise ConfigError(msg)
        return None
    if parts.scheme in {"http", "git", "ftp", "ftps"}:
        msg = f"insecure remote scheme {parts.scheme!r} is not allowed"
        raise ConfigError(msg)
    if _SCP_LIKE.match(url):
        return url.split(":", 1)[0].rsplit("@", 1)[-1]
    msg = f"unsupported remote URL: {url}"
    raise ConfigError(msg)


def remote_host(url: str) -> str | None:
    """Return the host of a validated remote URL, or ``None`` for local remotes."""
    return _remote_host(url, allow_local=True)
