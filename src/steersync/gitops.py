"""Thin, safe wrapper around the git binary.

All calls use argument lists, never a shell, and run with interactive prompts
disabled so hooks and MCP servers fail fast instead of hanging.
"""

import getpass
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from steersync.config import Config, validate_remote_url
from steersync.errors import GitError
from steersync.retry import RetryEvent, RetryPolicy, backoff_delay

log = logging.getLogger(__name__)

ZERO_OID_RE = re.compile(r"^0{40}(0{24})?$")
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9._/-]+")
_SCRUBBED_ENV_PREFIXES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE")
_CREDENTIAL_IN_URL = re.compile(r"(?<=://)[^/@\s]+@")
_TRANSIENT_RE = re.compile(
    r"could not resolve host|connection (?:timed out|reset|refused|closed)|operation timed out"
    r"|timed out after|early eof|the remote end hung up|rpc failed|unexpected disconnect"
    r"|http(?:/[0-9.]+)? 5\d\d|returned error: 5\d\d|error: 5\d\d|temporary failure"
    r"|ssh: connect to host|gnutls_handshake|ssl_read|tls connection was non-properly terminated"
    # ssh's own ConnectTimeout, and routes that come and go on Wi-Fi or VPN
    r"|connection to \S+ port \d+ timed out|no route to host|network is unreachable",
    re.IGNORECASE | re.MULTILINE,
)
# A server that does not support push options drops the connection, so git also prints
# "the remote end hung up"; that rejection is permanent and must not be retried.
_PERMANENT_RE = re.compile(r"push options", re.IGNORECASE)


class GitResult(NamedTuple):
    """Result of a git invocation."""

    returncode: int
    stdout: str
    stderr: str


class PrePushRef(NamedTuple):
    """One line of the pre-push hook stdin contract from githooks(5)."""

    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str

    @property
    def is_delete(self) -> bool:
        return bool(ZERO_OID_RE.match(self.local_oid))


def redact_url(url: str) -> str:
    """Strip user info from URLs before they reach logs."""
    return _CREDENTIAL_IN_URL.sub("***@", url)


def is_transient(message: str) -> bool:
    """Return True when a git error message looks like a temporary network or server fault."""
    if _PERMANENT_RE.search(message):
        return False
    return bool(_TRANSIENT_RE.search(message))


def git_retry_policy(config: Config, retries: int) -> RetryPolicy:
    """The shared retry policy for a git network call allowing ``retries`` retries."""
    return RetryPolicy(
        attempts=retries + 1,
        base_delay=config.git.retry_backoff_seconds,
        max_delay=config.git.retry_max_backoff_seconds,
    )


# Fail fast when a host is unreachable (a firewalled port 22 otherwise hangs until the git
# timeout, then again on every retry) and notice a connection that stalls mid transfer.
DEFAULT_SSH_COMMAND = (
    "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
)


def _execute(
    command: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    input: str | None,  # noqa: A002 - mirrors subprocess.run
    stdin: int | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` like ``subprocess.run``, but end its whole process group on timeout.

    git starts ssh (and credential helpers) as children; killing only git leaves them
    behind, holding the connection open. On POSIX the command runs in its own session so
    the group can be killed; on Windows the git process is killed.
    """
    posix = os.name == "posix"
    with subprocess.Popen(  # nosec B603 - argument list, no shell
        list(command),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE if input is not None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=posix,
    ) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process, posix=posix)
            process.communicate()
            raise
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _kill_group(process: subprocess.Popen[str], *, posix: bool) -> None:
    if posix:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            process.kill()
    else:  # pragma: no cover - Windows
        process.kill()


def git_environment(base: Mapping[str, str], *, hooks_dir: Path | None = None) -> dict[str, str]:
    """Build the environment for git subprocesses.

    Keeps the caller's SSH agent, credential helper and proxy settings, disables
    prompts, and removes variables that would redirect git at the wrong repository.
    """
    env = {k: v for k, v in base.items() if not k.startswith(_SCRUBBED_ENV_PREFIXES)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = env.get("GIT_SSH_COMMAND", DEFAULT_SSH_COMMAND)
    env["LC_ALL"] = "C"
    env["STEERSYNC_ACTIVE"] = "1"
    if hooks_dir is not None:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        env["GIT_CONFIG_VALUE_0"] = str(hooks_dir)
    return env


def run_git(
    config: Config,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: int | None = None,
    stdin: str | None = None,
    retries: int = 0,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> GitResult:
    """Run git with hardened defaults and return its output.

    ``retries`` re-runs the command after transient network or server failures
    (including timeouts) with exponential backoff. Only idempotent network
    operations should pass a non zero value. Standard input is never inherited:
    when the caller is an MCP stdio server, inheriting it would let git read the
    protocol stream.
    """
    base_env = os.environ if env is None else env
    command = [
        config.git.binary,
        "-c",
        "protocol.ext.allow=never",
        "-c",
        f"protocol.file.allow={'always' if config.remote.allow_local else 'never'}",
        "-c",
        "core.askPass=",
        *args,
    ]
    name = args[0] if args else ""
    log.debug("git %s (cwd=%s)", " ".join(redact_url(a) for a in args), cwd)
    attempt = 0
    while True:
        try:
            completed = _execute(
                command,
                cwd=cwd,
                env=base_env,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                timeout=timeout or config.git.timeout_seconds,
            )
        except FileNotFoundError as exc:
            msg = f"git binary not found: {config.git.binary}"
            raise GitError(msg) from exc
        except subprocess.TimeoutExpired as exc:
            if attempt < retries:
                attempt = _wait_before_retry(config, name, attempt, retries, "timed out", on_retry)
                continue
            msg = f"git {name} timed out after {exc.timeout:.0f}s"
            raise GitError(msg) from exc
        result = GitResult(completed.returncode, completed.stdout, completed.stderr)
        if result.returncode != 0 and attempt < retries and is_transient(result.stderr):
            attempt = _wait_before_retry(
                config, name, attempt, retries, result.stderr.strip(), on_retry
            )
            continue
        break
    if check and result.returncode != 0:
        detail = redact_url(without_advice(result.stderr) or result.stdout.strip())
        msg = f"git {name} failed ({result.returncode}): {detail}"
        raise GitError(msg)
    return result


# git's generic advice after a failed network call; the cause is on another line.
_ADVICE = (
    "fatal: could not read from remote repository",
    "please make sure you have the correct access rights",
    "and the repository exists",
)


def without_advice(output: str) -> str:
    """git's error output without the generic "could not read from remote" advice."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    useful = [line for line in lines if not line.lower().startswith(_ADVICE)]
    return "\n".join(useful or lines)


def failure_reason(stderr: str) -> str:
    """The line of git's error output that says what went wrong."""
    return (without_advice(stderr).splitlines() or ["unknown"])[0]


def _wait_before_retry(
    config: Config,
    name: str,
    attempt: int,
    retries: int,
    reason: str,
    on_retry: Callable[[RetryEvent], None] | None,
) -> int:
    delay = backoff_delay(git_retry_policy(config, retries), attempt)
    detail = redact_url(failure_reason(reason))
    log.warning(
        "git %s failed transiently (%s); retry %d in %.1fs", name, detail, attempt + 1, delay
    )
    if on_retry is not None:
        on_retry(RetryEvent(f"git {name}", attempt + 1, retries + 1, delay, detail))
    time.sleep(delay)
    return attempt + 1


def git_version(config: Config, env: Mapping[str, str] | None = None) -> str:
    return run_git(config, ["--version"], env=env).stdout.strip()


def repo_toplevel(config: Config, start: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """Return the working tree root containing ``start`` or ``None`` outside a repository."""
    result = run_git(config, ["rev-parse", "--show-toplevel"], cwd=start, env=env, check=False)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def git_dir(config: Config, repo: Path, env: Mapping[str, str] | None = None) -> Path:
    out = run_git(config, ["rev-parse", "--git-dir"], cwd=repo, env=env).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (repo / path).resolve()


def remote_url(
    config: Config, repo: Path, name: str = "origin", env: Mapping[str, str] | None = None
) -> str | None:
    """The URL configured for remote ``name``, before ``url.<base>.insteadOf`` rewriting.

    The project slug comes from this URL, so a mirror or protocol rewrite in the user's git
    configuration does not change which project directory is synced.
    """
    configured = run_git(
        config, ["config", "--get", f"remote.{name}.url"], cwd=repo, env=env, check=False
    )
    if configured.returncode == 0 and configured.stdout.strip():
        return configured.stdout.strip()
    result = run_git(config, ["remote", "get-url", name], cwd=repo, env=env, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def remote_for_url(
    config: Config, repo: Path, url: str, env: Mapping[str, str] | None = None
) -> str | None:
    """Name of the remote whose configured or rewritten URL is ``url``.

    git hands a pre-push hook the URL after ``insteadOf`` rewriting; this maps it back to
    the remote, whose configured URL then gives the project slug.
    """
    names = run_git(config, ["remote"], cwd=repo, env=env, check=False)
    if names.returncode != 0:
        return None
    for name in names.stdout.split():
        candidates = {remote_url(config, repo, name, env=env)}
        for args in (["remote", "get-url", name], ["remote", "get-url", "--push", name]):
            result = run_git(config, args, cwd=repo, env=env, check=False)
            if result.returncode == 0:
                candidates.add(result.stdout.strip())
        if url in candidates:
            return name
    return None


def config_value(
    config: Config, repo: Path | None, key: str, env: Mapping[str, str] | None = None
) -> str | None:
    result = run_git(config, ["config", "--get", key], cwd=repo, env=env, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def project_path_from_url(url: str) -> str:
    """Derive ``group/subgroup/project`` from any supported remote URL."""
    candidate = url.strip()
    parts = urlsplit(candidate)
    if parts.scheme in {"ssh", "https", "http", "git", "git+ssh", "ssh+git", "file"}:
        path = parts.path
    elif candidate.startswith("/"):
        path = candidate
    elif ":" in candidate:
        path = candidate.split(":", 1)[1]
    else:
        path = candidate
    path = path.strip("/")
    path = path.removesuffix(".git").rstrip("/")
    if parts.scheme == "file" or candidate.startswith("/"):
        path = path.rsplit("/", 1)[-1]
    if not path:
        msg = f"cannot derive a project path from remote URL {redact_url(url)}"
        raise GitError(msg)
    return path


def slugify_component(component: str) -> str:
    """Make a single path component safe for directory and ref names."""
    cleaned = _UNSAFE_COMPONENT.sub("-", component).strip("-.")
    cleaned = cleaned.removesuffix(".lock")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned.lower()


def project_slug(url: str) -> str:
    """Return a sanitised ``group/subgroup/project`` slug for a remote URL."""
    components = [slugify_component(c) for c in project_path_from_url(url).split("/")]
    kept = [c for c in components if c]
    if not kept:
        msg = f"remote URL {redact_url(url)} yields an empty project slug"
        raise GitError(msg)
    return "/".join(kept)


_USER_ID_RE = re.compile(r"[^a-z0-9-]+")


def _user_candidate(
    source: str, config: Config, repo: Path | None, env: Mapping[str, str]
) -> str | None:
    if source == "os":
        return env.get("USER") or env.get("LOGNAME") or env.get("USERNAME") or _login_name()
    if source == "email":
        email = config_value(config, repo, "user.email", env=env)
        return email.split("@", 1)[0] if email else None
    if source == "name":
        return config_value(config, repo, "user.name", env=env)
    return None


def _login_name() -> str | None:
    try:
        return getpass.getuser()
    except OSError, KeyError, ImportError:  # pragma: no cover - platform dependent
        return None


def current_user_id(config: Config, repo: Path | None, env: Mapping[str, str]) -> str:
    """Return a short identifier for the current user suitable for branch names.

    Sources are tried in the order of ``branch.user_from``: the operating system
    user name, the local part of the git ``user.email``, the git ``user.name``.
    The result is lowercase letters, digits and dashes only.
    """
    for source in config.branch.user_from:
        candidate = _user_candidate(source, config, repo, env)
        if not candidate:
            continue
        slug = _USER_ID_RE.sub("-", candidate.strip().lower()).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-")
        if slug:
            return slug
    return "user"


def sanitise_ref(name: str) -> str:
    """Sanitise a candidate ref name per git-check-ref-format rules."""
    cleaned = _UNSAFE_REF_CHARS.sub("-", name)
    cleaned = re.sub(r"/{2,}", "/", cleaned)
    cleaned = cleaned.replace("@{", "-")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    parts = [p.strip(".").removesuffix(".lock") for p in cleaned.split("/")]
    cleaned = "/".join(p for p in parts if p)
    return cleaned.strip("-/.")[:200]


def build_branch_name(config: Config, *, project: str, user: str, content_hash: str) -> str:
    """Render and sanitise the proposal branch name."""
    short_hash = content_hash[: config.branch.hash_length]
    rendered = config.branch.template.format(project=project, user=user, hash=short_hash)
    name = sanitise_ref(rendered)
    if not name or name == "@":
        msg = "branch template produced an invalid ref name"
        raise GitError(msg)
    return name


def check_ref_format(config: Config, name: str, env: Mapping[str, str] | None = None) -> bool:
    result = run_git(config, ["check-ref-format", "--branch", name], env=env, check=False)
    return result.returncode == 0


def render_push_options(config: Config, *, project: str, user: str, content_hash: str) -> list[str]:
    values = {
        "default_branch": config.remote.default_branch,
        "project": project,
        "user": user,
        "hash": content_hash[: config.branch.hash_length],
    }
    return [option.format(**values) for option in config.remote.push_options]


def remote_branch_exists(
    config: Config, url: str, branch: str, env: Mapping[str, str] | None = None
) -> bool:
    """Return True when ``refs/heads/<branch>`` exists on the remote."""
    result = run_git(
        config,
        ["ls-remote", "--exit-code", "--heads", "--", url, f"refs/heads/{branch}"],
        env=env,
        check=False,
        retries=config.git.retries,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    msg = f"ls-remote failed ({result.returncode}): {redact_url(without_advice(result.stderr))}"
    raise GitError(msg)


def list_remote_branches(
    config: Config,
    url: str,
    pattern: str,
    env: Mapping[str, str] | None = None,
    *,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> list[str]:
    """Return the remote branch names matching a glob such as ``kiro-config/acme/api/*``."""
    result = run_git(
        config,
        ["ls-remote", "--heads", "--", url, f"refs/heads/{pattern}"],
        env=env,
        retries=config.git.retries,
        on_retry=on_retry,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].startswith("refs/heads/"):
            names.append(fields[1].removeprefix("refs/heads/"))
    return sorted(names)


def remote_head_oid(
    config: Config, url: str, branch: str, env: Mapping[str, str] | None = None
) -> str | None:
    result = run_git(
        config,
        ["ls-remote", "--heads", "--", url, f"refs/heads/{branch}"],
        env=env,
        retries=config.git.retries,
    )
    first = result.stdout.strip().splitlines()[:1]
    return first[0].split()[0] if first else None


def ensure_cache_clone(
    config: Config,
    *,
    url: str,
    cache_dir: Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Clone the central repository into the cache or refresh an existing clone.

    The clone is shallow and single branch. On every call the default branch is
    reset to the remote tip so the cache never carries stale local state. A cache
    that cannot be refreshed for a non transient reason (corruption, an
    interrupted clone, a rewritten default branch) is deleted and cloned again.
    """
    validate_remote_url(
        url, allowed_hosts=config.remote.allowed_hosts, allow_local=config.remote.allow_local
    )
    if (cache_dir / ".git").is_dir():
        try:
            _refresh_clone(config, url=url, cache_dir=cache_dir, env=env)
        except GitError as exc:
            if is_transient(str(exc)):
                raise
            log.warning("cache clone %s is unusable (%s); recreating it", cache_dir, exc)
        else:
            return cache_dir
    remove_tree(cache_dir)
    cache_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        run_git(
            config,
            [
                "clone",
                "--quiet",
                "--depth",
                str(config.git.clone_depth),
                "--single-branch",
                "--branch",
                config.remote.default_branch,
                "--no-tags",
                "--",
                url,
                str(cache_dir),
            ],
            env=env,
            retries=config.git.retries,
        )
    except GitError as exc:
        if "not found in upstream" in str(exc):
            branch = config.remote.default_branch
            msg = (
                f"the central repository {redact_url(url)} has no branch {branch!r}. A new "
                "GitLab project is empty until its first commit: create it with "
                "'Initialize repository with a README' (or push one commit to "
                f"{branch!r}), or set remote.default_branch to the branch it uses"
            )
            raise GitError(msg) from exc
        raise
    _reset_to_remote(config, cache_dir=cache_dir, env=env)
    return cache_dir


def _refresh_clone(
    config: Config, *, url: str, cache_dir: Path, env: Mapping[str, str] | None
) -> None:
    branch = config.remote.default_branch
    stored = remote_url(config, cache_dir, env=env)
    if stored != url:
        run_git(config, ["remote", "set-url", "origin", "--", url], cwd=cache_dir, env=env)
    run_git(
        config,
        [
            "fetch",
            "--depth",
            str(config.git.clone_depth),
            "--prune",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        cwd=cache_dir,
        env=env,
        retries=config.git.retries,
    )
    _reset_to_remote(config, cache_dir=cache_dir, env=env)


def _reset_to_remote(config: Config, *, cache_dir: Path, env: Mapping[str, str] | None) -> None:
    branch = config.remote.default_branch
    lock = cache_dir / ".git" / "index.lock"
    if lock.exists():
        log.warning("removing stale %s left by an interrupted git process", lock)
        lock.unlink(missing_ok=True)
    run_git(
        config,
        ["checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}"],
        cwd=cache_dir,
        env=env,
    )
    run_git(config, ["clean", "-fdqx"], cwd=cache_dir, env=env)


def reset_cache_clone(
    config: Config, *, cache_dir: Path, env: Mapping[str, str] | None = None
) -> None:
    """Put an existing cache clone back on its cached default branch without fetching."""
    _reset_to_remote(config, cache_dir=cache_dir, env=env)


def _clear_readonly(function: Callable[..., object], path: str, _: BaseException) -> None:
    """``rmtree`` error handler: git writes its objects read only, which Windows refuses to
    delete, so clear the read only bit and retry once."""
    os.chmod(path, stat.S_IWRITE)  # noqa: PTH101 - rmtree hands over a str path
    function(path)


def remove_tree(path: Path) -> None:
    """Delete a file or directory tree, including git's read only objects on Windows."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, onexc=_clear_readonly)


def head_commit(config: Config, repo: Path, env: Mapping[str, str] | None = None) -> str:
    return run_git(config, ["rev-parse", "HEAD"], cwd=repo, env=env).stdout.strip()


def create_branch(
    config: Config, repo: Path, branch: str, start_point: str, env: Mapping[str, str] | None = None
) -> None:
    run_git(config, ["checkout", "--quiet", "-B", branch, start_point], cwd=repo, env=env)


def commit_paths(
    config: Config,
    repo: Path,
    *,
    paths: Sequence[str],
    message: str,
    author_name: str,
    author_email: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Stage ``paths`` and commit. Returns the new commit id or ``None`` if nothing changed."""
    run_git(config, ["add", "--all", "--", *paths], cwd=repo, env=env)
    staged = run_git(config, ["diff", "--cached", "--quiet"], cwd=repo, env=env, check=False)
    if staged.returncode == 0:
        return None
    run_git(
        config,
        [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "--quiet",
            "--no-verify",
            "--no-gpg-sign",
            "--message",
            message,
        ],
        cwd=repo,
        env=env,
    )
    return head_commit(config, repo, env=env)


def push_branch(
    config: Config,
    repo: Path,
    *,
    url: str,
    branch: str,
    push_options: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> tuple[GitResult, bool]:
    """Push ``branch`` to ``url`` with the given push options.

    Servers that do not advertise push options make git fail instead of ignoring
    them, so the push is retried once without options. The second element of the
    result says whether the options were delivered.
    """
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    args = ["push", "--quiet", "--no-verify"]
    for option in push_options:
        args.extend(["--push-option", option])
    args.extend(["--", url, refspec])
    result = run_git(config, args, cwd=repo, env=env, check=False, retries=config.git.retries)
    if result.returncode == 0:
        return result, bool(push_options)
    if push_options and "push options" in result.stderr:
        log.warning("remote does not accept push options; pushing without them")
        retry = run_git(
            config,
            ["push", "--quiet", "--no-verify", "--", url, refspec],
            cwd=repo,
            env=env,
            retries=config.git.retries,
        )
        return retry, False
    detail = redact_url(without_advice(result.stderr) or result.stdout.strip())
    msg = f"git push failed ({result.returncode}): {detail}"
    raise GitError(msg)


def parse_pre_push_stdin(text: str) -> list[PrePushRef]:
    """Parse the ref lines git feeds a pre-push hook on stdin."""
    refs: list[PrePushRef] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4:
            refs.append(PrePushRef(*fields))
    return refs


def write_info_exclude(
    config: Config, repo: Path, pattern: str, env: Mapping[str, str] | None = None
) -> bool:
    """Append ``pattern`` to ``.git/info/exclude``. Returns True when a change was made."""
    exclude = git_dir(config, repo, env=env) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text("utf-8").splitlines() if exclude.is_file() else []
    if pattern in existing:
        return False
    with exclude.open("a", encoding="utf-8") as handle:
        if existing and existing[-1] != "":
            handle.write("\n")
        handle.write(f"# added by steersync\n{pattern}\n")
    return True


def hooks_dir(config: Config, repo: Path, env: Mapping[str, str] | None = None) -> Path:
    """Return the effective hooks directory for ``repo`` honouring core.hooksPath."""
    custom = config_value(config, repo, "core.hooksPath", env=env)
    if custom:
        path = Path(custom).expanduser()
        return path if path.is_absolute() else repo / path
    return git_dir(config, repo, env=env) / "hooks"
