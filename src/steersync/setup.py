"""Building blocks of ``steersync setup``: checks, URL handling and the files it writes.

Every function here is plain logic so it can be tested without a terminal; the
command in :mod:`steersync.cli` asks the questions and calls them. Nothing writes
without the caller asking for it, and every write keeps a backup of what it replaces.
"""

import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import yaml

from steersync import gitops
from steersync.config import (
    CONFIG_FILE_NAMES,
    Config,
    deep_merge,
    default_config_text,
    remote_host,
)
from steersync.errors import ConfigError, GitError, SteersyncError

MIN_GIT = (2, 18)
DEFAULT_BRANCH_FALLBACK = "master"
_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_WEB_SUFFIX_RE = re.compile(r"/-/.*$")


class GitCheck(NamedTuple):
    path: str | None
    version: str | None
    ok: bool
    problem: str | None


class Candidate(NamedTuple):
    """A git URL to try for what the user typed, and whether it is a local path."""

    url: str
    local: bool


class RemoteCheck(NamedTuple):
    url: str
    reachable: bool
    head_branch: str | None
    branches: tuple[str, ...]
    error: str | None


def check_git(binary: str = "git") -> GitCheck:
    """Is git installed and new enough (2.18 or later)?"""
    path = shutil.which(binary)
    if path is None:
        return GitCheck(None, None, False, f"{binary!r} was not found on PATH; install git first")
    config = Config.model_validate({"git": {"binary": binary}})
    try:
        output = gitops.git_version(config)
    except SteersyncError as exc:
        return GitCheck(path, None, False, str(exc))
    match = _GIT_VERSION_RE.search(output)
    if match is None:
        return GitCheck(path, output, False, f"cannot read the git version from {output!r}")
    version = (int(match.group(1)), int(match.group(2)))
    if version < MIN_GIT:
        return GitCheck(path, output, False, f"{output} is too old; steersync needs git 2.18+")
    return GitCheck(path, output, True, None)


def candidate_urls(text: str) -> list[Candidate]:
    """Git URLs worth trying for what the user typed, most likely first.

    A browser address (``https://gitlab.example.com/group/project`` or a page inside it)
    becomes the SSH URL followed by the HTTPS clone URL; SSH and ``.git`` URLs are kept as
    they are; an existing local directory is a local repository (tests and evaluation).
    """
    raw = text.strip().strip("'\"")
    if not raw:
        return []
    if Path(raw).expanduser().is_dir():
        return [Candidate(str(Path(raw).expanduser().resolve()), local=True)]
    if raw.startswith("file://"):
        return [Candidate(raw, local=True)]
    parts = urlsplit(raw)
    if parts.scheme in {"http", "https"} and parts.hostname:
        path = _WEB_SUFFIX_RE.sub("", parts.path).strip("/")
        path = path.removesuffix(".git")
        if not path:
            return []
        host = parts.hostname
        port = f":{parts.port}" if parts.port else ""
        return [
            Candidate(f"git@{host}:{path}.git", local=False),
            Candidate(f"https://{host}{port}/{path}.git", local=False),
        ]
    return [Candidate(raw, local=False)]


def probe_remote(config: Config, url: str, env: Mapping[str, str]) -> RemoteCheck:
    """Ask the remote for its branches and its default branch with ``git ls-remote``."""
    git_env = gitops.git_environment(env)
    try:
        result = gitops.run_git(
            config, ["ls-remote", "--symref", "--", url], env=git_env, check=False, timeout=45
        )
    except GitError as exc:
        return RemoteCheck(url, False, None, (), str(exc))
    if result.returncode != 0:
        detail = gitops.redact_url((result.stderr or result.stdout).strip())
        return RemoteCheck(url, False, None, (), detail or f"git exited {result.returncode}")
    head: str | None = None
    branches: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            head = line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
        fields = line.split("\t")
        if len(fields) == 2 and fields[1].startswith("refs/heads/"):
            branches.append(fields[1].removeprefix("refs/heads/"))
    return RemoteCheck(url, True, head, tuple(branches), None)


def explain_unreachable(url: str, error: str) -> str:
    """A next step for the most common reasons ``git ls-remote`` fails."""
    host = gitops_host(url)
    lowered = error.lower()
    if "host key verification failed" in lowered:
        return (
            f"git does not know {host}'s host key yet; run `ssh -T git@{host}` once and accept it"
        )
    if "permission denied" in lowered or "publickey" in lowered:
        return f"{host} refused your SSH key; check `ssh -T git@{host}` or use the HTTPS URL"
    if "authentication failed" in lowered or "could not read username" in lowered:
        return "HTTPS needs a credential helper or a token; or use the SSH URL"
    if "could not resolve host" in lowered:
        return f"{host} could not be resolved; check the address and your VPN or proxy"
    if "timed out" in lowered or "port 22" in lowered:
        return (
            f"{host} did not answer; if your network blocks SSH on port 22, use the HTTPS URL "
            "(or ssh://git@altssh.gitlab.com:443/... on GitLab.com)"
        )
    if any(
        phrase in lowered
        for phrase in ("not found", "could not be found", "does not appear to be a git repo")
    ):
        return "the project does not exist or you have no access to it"
    return "check the address and that `git ls-remote <url>` works in your terminal"


def gitops_host(url: str) -> str:
    """The host of ``url`` for messages, or the URL itself when it has none."""
    try:
        host = remote_host(url)
    except ConfigError:
        host = None
    return host or url


def suggested_branch(check: RemoteCheck) -> str:
    """The branch to offer: the remote's default branch, else master."""
    return check.head_branch or DEFAULT_BRANCH_FALLBACK


def user_config_path(env: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/steersync/config.yaml`` (default ``~/.config/steersync``)."""
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path(env.get("HOME", "~")).expanduser() / ".config"
    return base / "steersync" / "config.yaml"


def project_config_path(root: Path) -> Path | None:
    """The project configuration file when one exists (it wins over the user file)."""
    return next(
        (root / ".kiro" / n for n in CONFIG_FILE_NAMES if (root / ".kiro" / n).is_file()), None
    )


def updated_user_config(
    existing: Mapping[str, Any],
    *,
    url: str,
    branch: str,
    host: str | None,
    local: bool,
    extra_collections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """``existing`` with the central repository (and any extra collections) set.

    Everything else in the file is kept. The result is validated against the full
    configuration model, so setup never writes a file steersync would then refuse.
    """
    data: dict[str, Any] = dict(existing)
    remote = dict(data.get("remote") or {})
    remote["url"] = url
    remote["default_branch"] = branch
    if host:
        hosts = [h for h in remote.get("allowed_hosts") or [] if isinstance(h, str)]
        remote["allowed_hosts"] = [*hosts, host] if host.lower() not in hosts else hosts
    if local:
        remote["allow_local"] = True
    data["remote"] = remote
    if extra_collections:
        collections = dict(data.get("collections") or {})
        for name, spec in extra_collections.items():
            collections.setdefault(name, dict(spec))
        data["collections"] = collections
    defaults = yaml.safe_load(default_config_text())
    Config.model_validate(deep_merge(defaults, data))
    return data


def write_user_config(path: Path, data: Mapping[str, Any]) -> Path | None:
    """Write the configuration, keeping the previous file as ``config.yaml.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.is_file():
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text("utf-8"), encoding="utf-8")
    header = (
        "# Written by `steersync setup`. Run it again to change the central repository;\n"
        "# `steersync config default` shows every other setting.\n"
    )
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(header + yaml.safe_dump(dict(data), sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return backup


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text("utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        msg = f"{path} is not a YAML mapping; fix or remove it, then run setup again"
        raise ConfigError(msg)
    return loaded


def unsynced_kiro_folders(root: Path, config: Config) -> list[str]:
    """Folders directly under ``.kiro`` that no collection syncs (``notes``, ``plan``)."""
    kiro = root / ".kiro"
    if not kiro.is_dir():
        return []
    covered = {
        _top_two(c.workspace_path)
        for c in config.collections.values()
        if c.workspace_path and c.workspace_path.startswith(".kiro/")
    }
    found = []
    for child in sorted(kiro.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (".kiro", child.name) in covered:
            continue
        if any(f.is_file() and not f.name.startswith(".") for f in child.rglob("*")):
            found.append(child.name)
    return found


def _top_two(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.strip("/").split("/") if part)[:2]


def extra_collection(folder: str) -> dict[str, Any]:
    """The collection definition for an extra ``.kiro/<folder>`` of notes or plans."""
    return {
        "description": f"Extra Kiro folder .kiro/{folder} (added by steersync setup)",
        "workspace_path": f".kiro/{folder}",
        "global_path": None,
        "include": ["**/*"],
        "exclude": ["**/*.bak", "**/*.tmp"],
        "lint": "markdown",
    }
