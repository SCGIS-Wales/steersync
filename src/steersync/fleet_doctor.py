"""``steersync fleet doctor``: read only health checks with an explicit repair switch.

Checks the registry, every registered work tree, each central cache clone (``git fsck
--connectivity-only`` and its branch), each last sync record, the cache locks and the
backup store. ``repair`` performs only safe repairs: rebuild a damaged cache clone from
its remote (it holds no user data), set aside a damaged registry or sync record, forget
missing registry entries and prune expired backups. It never touches a work tree.
"""

import contextlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from steersync import backups, discovery, gitops, registry, sync
from steersync.config import Config, load_config
from steersync.errors import LockTimeoutError, SteersyncError
from steersync.locking import exclusive_lock
from steersync.paths import backup_root, cache_repo_dir, global_lock_path, registry_path
from steersync.steering import lock_problem, read_lock


class Check(NamedTuple):
    check: str
    subject: str
    ok: bool
    detail: str
    repaired: bool = False


def _git_ok(config: Config, args: list[str], cwd: Path, env: Mapping[str, str]) -> tuple[bool, str]:
    result = gitops.run_git(config, args, cwd=cwd, env=gitops.git_environment(env), check=False)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def _remotes(paths: list[Path], env: Mapping[str, str]) -> dict[str, Config]:
    found: dict[str, Config] = {}
    configs: list[Config] = []
    with contextlib.suppress(SteersyncError):
        configs.append(load_config(workspace_root=None, env=env))
    for path in paths:
        if registry.is_work_tree(path):
            try:
                configs.append(load_config(workspace_root=path, env=env))
            except SteersyncError:
                continue
    for config in configs:
        if config.remote.url:
            try:
                found.setdefault(sync.central_url(config), config)
            except SteersyncError:
                continue
    return found


def _cache_check(url: str, config: Config, env: Mapping[str, str], repair: bool) -> Check:
    subject = gitops.redact_url(url)
    cache = cache_repo_dir(config, env, url)
    if not (cache / ".git").is_dir():
        return Check("cache clone", subject, True, "not cloned yet (created on first use)")
    healthy, detail = _git_ok(config, ["fsck", "--connectivity-only", "--no-progress"], cache, env)
    problem = None if healthy else f"git fsck failed: {detail[-200:]}"
    if problem is None:
        on_branch, head = _git_ok(config, ["symbolic-ref", "--short", "HEAD"], cache, env)
        if not on_branch or head != config.remote.default_branch:
            problem = f"HEAD is {head or 'detached'}, expected {config.remote.default_branch}"
    if problem is None:
        return Check("cache clone", subject, True, f"healthy at {cache}")
    if not repair:
        return Check("cache clone", subject, False, problem)
    try:
        with sync.central_lock(config, env, url):
            shutil.rmtree(cache, ignore_errors=True)
            sync.refresh_central(config, env, url, refresh=True)
    except SteersyncError as exc:
        return Check("cache clone", subject, False, f"{problem}; rebuild failed: {exc}")
    return Check("cache clone", subject, True, f"{problem}; rebuilt from the remote", True)


def _lock_check(url: str, config: Config, env: Mapping[str, str]) -> Check:
    cache = cache_repo_dir(config, env, url)
    lock = cache.with_name(cache.name + ".lock")
    subject = gitops.redact_url(url)
    if not lock.exists():
        return Check("cache lock", subject, True, "free")
    try:
        with exclusive_lock(lock, timeout=0):
            pass
    except LockTimeoutError:
        return Check("cache lock", subject, True, "held by a running steersync process")
    return Check(
        "cache lock",
        subject,
        True,
        "free (an operating system lock, released when its process exits)",
    )


def _record_check(label: str, path: Path, repair: bool) -> Check:
    problem = lock_problem(path)
    if problem is None:
        return Check("sync record", label, True, "readable" if path.exists() else "none yet")
    if not repair:
        return Check("sync record", label, False, f"{path}: {problem}")
    read_lock(path)  # sets the damaged record aside
    return Check("sync record", label, True, f"{problem}; set aside", True)


def run(env: Mapping[str, str] | None = None, *, repair: bool = False) -> list[Check]:
    """Every check, in a stable order; repairs only when ``repair`` is set."""
    environment = os.environ if env is None else env
    checks: list[Check] = []
    reg_path = registry_path(environment)
    problem = registry.problem(reg_path)
    if problem and repair:
        registry.load(reg_path)  # sets the damaged file aside
        checks.append(Check("registry", str(reg_path), True, f"{problem}; set aside", True))
    elif problem:
        checks.append(Check("registry", str(reg_path), False, problem))
    else:
        checks.append(Check("registry", str(reg_path), True, "readable"))
    entries = registry.load(reg_path).paths() if problem is None or repair else []
    missing = [str(p) for p in entries if not registry.is_work_tree(p)]
    if missing and repair:
        registry.prune(reg_path)
        checks.append(
            Check(
                "work trees",
                f"{len(entries)} registered",
                True,
                f"forgot {len(missing)} missing: {', '.join(missing)}",
                True,
            )
        )
    elif missing:
        checks.append(
            Check(
                "work trees",
                f"{len(entries)} registered",
                False,
                f"missing: {', '.join(missing)} (steersync fleet prune)",
            )
        )
    else:
        checks.append(Check("work trees", f"{len(entries)} registered", True, "all present"))
    for path in entries:
        git_dir = discovery.git_dir_of(path)
        if git_dir is not None:
            checks.append(_record_check(str(path), git_dir / "steersync" / "lock.json", repair))
    remotes = _remotes(entries, environment)
    for url, config in remotes.items():
        checks.append(_cache_check(url, config, environment, repair))
        checks.append(_lock_check(url, config, environment))
    try:
        machine = load_config(workspace_root=None, env=environment)
    except SteersyncError:
        machine = None
    if machine is not None:
        checks.append(_record_check("~/.kiro", global_lock_path(machine, environment), repair))
        root = backup_root(environment)
        limits = machine.fleet
        would = backups.prune(
            root, keep_days=limits.backup_keep_days, max_bytes=limits.backup_max_bytes, dry_run=True
        )
        use = backups.usage(root)
        summary = f"{use['backups']} backups, {use['bytes']} bytes"
        if would.removed and repair:
            backups.prune(
                root, keep_days=limits.backup_keep_days, max_bytes=limits.backup_max_bytes
            )
            checks.append(
                Check("backups", str(root), True, f"{summary}; pruned {len(would.removed)}", True)
            )
        elif would.removed:
            checks.append(
                Check(
                    "backups",
                    str(root),
                    False,
                    f"{summary}; {len(would.removed)} past their age or size limit",
                )
            )
        else:
            checks.append(Check("backups", str(root), True, summary))
    return checks
