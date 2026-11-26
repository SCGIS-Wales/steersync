"""File discovery, front matter parsing, snapshots, change plans and lock files.

The module is named after steering, its first use, but every function works on
any collection of files described by a :class:`FileSpec`.
"""

import functools
import glob
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple

import frontmatter
import yaml

from steersync.errors import LimitExceededError, UnsafePathError
from steersync.paths import ensure_within

log = logging.getLogger(__name__)

LOCK_VERSION = 2
FILE_MODE = 0o644
EXECUTABLE_MODE = 0o755
DEFAULT_COLLECTION = "steering"
_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+@")
STEERING_SUFFIX = ".md"
INCLUSION_MODES: tuple[str, ...] = ("always", "fileMatch", "manual", "auto")
KNOWN_KEYS: frozenset[str] = frozenset({"inclusion", "fileMatchPattern", "name", "description"})
FILE_REFERENCE_RE = re.compile(r"#\[\[(file|folder):([^\]\n]+?)\]\]")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

Snapshot = dict[str, bytes]
InclusionMode = Literal["always", "fileMatch", "manual", "auto"]


class FileSpec(NamedTuple):
    """Which files inside a collection directory take part in a sync."""

    include: tuple[str, ...] = ("**/*",)
    exclude: tuple[str, ...] = ()
    max_depth: int | None = None

    def literal_names(self) -> frozenset[str]:
        """File names spelled out literally at the end of include patterns.

        A hidden file is only synced when an include pattern names it exactly
        (``**/.kiroignore``), never through a wildcard.
        """
        return frozenset(
            name
            for pattern in self.include
            if not any(c in (name := PurePosixPath(pattern).name) for c in "*?[")
        )

    def excludes_directory(self) -> Callable[[str], bool]:
        """Return a predicate telling whether everything under a directory is excluded."""
        excludes = [_glob_regex(p) for p in self.exclude]
        return lambda relative: any(r.match(f"{relative}/{_PROBE}") for r in excludes)

    def matcher(self) -> Callable[[str], bool]:
        """Return a predicate over posix relative paths."""
        includes = [_glob_regex(p) for p in self.include]
        excludes = [_glob_regex(p) for p in self.exclude]
        return lambda relative: (
            any(r.match(relative) for r in includes)
            and not any(r.match(relative) for r in excludes)
        )


STEERING_SPEC = FileSpec(include=(f"**/*{STEERING_SUFFIX}",))
# A file name no real file has, used to test whether a directory pattern excludes a tree.
_PROBE = "\0steersync-probe"


@functools.cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(glob.translate(pattern, recursive=True, include_hidden=True))


class FrontMatter(NamedTuple):
    """Parsed front matter of a steering document."""

    present: bool
    leading_blank: bool
    metadata: dict[str, Any]
    body: str
    error: str | None
    error_line: int | None = None  # line in the file, when the error can be placed


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


def discover_files(
    directory: Path, spec: FileSpec = STEERING_SPEC, *, follow_symlinks: bool = True
) -> list[Path]:
    """Return the files under ``directory`` selected by ``spec`` sorted by relative path.

    Hidden directories are skipped, and so are hidden files unless an include
    pattern names them literally (``**/.kiroignore``). Directories whose whole tree
    is excluded (``**/node_modules/**``) are not entered, so a workspace wide
    collection such as nested ``AGENTS.md`` stays fast. Symlinked directories are
    never entered, to match the Kiro IDE. Symlinked files are included when
    ``follow_symlinks`` is true (kiro-cli follows them). Content from the central
    repository is read with ``follow_symlinks=False`` so a malicious link such as
    ``global/x.md -> ~/.ssh/id_rsa`` cannot copy local secrets into the sync.
    """
    if not directory.is_dir():
        return []
    matches = spec.matcher()
    pruned = spec.excludes_directory()
    hidden_ok = spec.literal_names()
    found: list[Path] = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        here = Path(root).relative_to(directory)
        depth = len(here.parts)
        if spec.max_depth is not None and depth + 1 >= spec.max_depth:
            dirs[:] = []
        else:
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not pruned((here / d).as_posix())
            )
        for name in sorted(files):
            if name.startswith(".") and name not in hidden_ok:
                continue
            path = Path(root) / name
            if not matches(path.relative_to(directory).as_posix()):
                continue
            if path.is_symlink() and not follow_symlinks:
                log.warning("ignoring symlink %s in central repository content", path)
                continue
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(directory).as_posix())


def discover_steering_files(directory: Path, *, follow_symlinks: bool = True) -> list[Path]:
    """Return steering markdown files under ``directory`` sorted by relative path."""
    return discover_files(directory, STEERING_SPEC, follow_symlinks=follow_symlinks)


def snapshot_directory(
    directory: Path, spec: FileSpec = STEERING_SPEC, *, follow_symlinks: bool = True
) -> Snapshot:
    """Read every selected file under ``directory`` into memory keyed by relative path."""
    result: Snapshot = {}
    for path in discover_files(directory, spec, follow_symlinks=follow_symlinks):
        relative = validate_relative_path(path.relative_to(directory).as_posix())
        result[relative] = path.read_bytes()
    return result


def executable_paths(directory: Path, relatives: Iterable[str]) -> frozenset[str]:
    """Return the relative paths whose file has an execute bit set (skill scripts)."""
    executable: set[str] = set()
    for relative in relatives:
        path = directory / relative
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if mode & 0o111:
            executable.add(relative)
    return frozenset(executable)


def enforce_limits(snapshot: Mapping[str, bytes], *, max_files: int, max_total_bytes: int) -> None:
    """Refuse snapshots that are larger than the configured safety limits."""
    if len(snapshot) > max_files:
        msg = f"{len(snapshot)} files exceed sync.max_files ({max_files})"
        raise LimitExceededError(msg)
    total = sum(len(data) for data in snapshot.values())
    if total > max_total_bytes:
        msg = f"{total} bytes exceed sync.max_total_bytes ({max_total_bytes})"
        raise LimitExceededError(msg)


def comparable(data: bytes) -> bytes:
    """Content as compared and hashed: text with CRLF line endings normalised to LF.

    A checkout with ``core.autocrlf`` or a Windows editor must not look like a change.
    Binary content (any NUL byte) is left exactly as it is; files on disk are never
    rewritten, only their hash is computed this way.
    """
    if b"\0" in data or b"\r\n" not in data:
        return data
    return data.replace(b"\r\n", b"\n")


def file_digest(data: bytes) -> str:
    return hashlib.sha256(comparable(data)).hexdigest()


def snapshot_digest(snapshot: Mapping[str, bytes]) -> str:
    """Return a stable digest for an entire snapshot (paths and content)."""
    hasher = hashlib.sha256()
    for relative in sorted(snapshot):
        data = comparable(snapshot[relative])
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(len(data)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\n")
    return hasher.hexdigest()


def snapshot_hashes(snapshot: Mapping[str, bytes]) -> dict[str, str]:
    return {relative: file_digest(data) for relative, data in snapshot.items()}


_PLAIN_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]+(\S.*)$")


def explain_yaml_error(header: str, exc: Exception) -> tuple[str, int | None]:
    """A message saying what to change, and the line of the header it is on (1 based).

    The common mistake is a plain value containing ``": "`` (``status: Done. Decisions:
    none``), which YAML reads as a second key; the fix is to quote the value.
    """
    for index, line in enumerate(header.splitlines(), start=1):
        found = _PLAIN_KEY_RE.match(line)
        if not found:
            continue
        key, value = found.groups()
        if value[:1] in {'"', "'", "|", ">", "[", "{"} or ": " not in value:
            continue
        quoted = value.replace("\\", "\\\\").replace('"', '\\"')
        shown = quoted[:60] + ("..." if len(quoted) > 60 else "")
        message = (
            f"the value of `{key}` contains ': ', which YAML reads as another key; "
            f'put the value in quotes: {key}: "{shown}"'
        )
        return message, index
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    where: int | None = mark.line + 1 if mark is not None else None
    return f"invalid YAML front matter: {problem}", where


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
        message, line = explain_yaml_error(match.group(1), exc)
        # +1 for the opening --- line, so the number is a line of the file.
        return FrontMatter(
            True, False, {}, text[match.end() :], message, line + 1 if line else None
        )
    raw = yaml.safe_load(match.group(1)) if match.group(1).strip() else {}
    if raw is not None and not isinstance(raw, dict):
        return FrontMatter(
            True,
            False,
            {},
            post.content,
            "front matter is not a YAML mapping; write key: value lines",
            2,
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


def _atomic_write(path: Path, data: bytes, *, mode: int = FILE_MODE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(mode)
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
    executable: Collection[str] = (),
) -> list[str]:
    """Write adds and updates, remove deletes, and optionally overwrite conflicts.

    Files listed in ``executable`` are written with mode 0755 so skill scripts
    stay runnable. Returns the list of relative paths that were written or removed.
    """
    touched: list[str] = []
    writes = list(plan.add) + list(plan.update)
    if force:
        writes.extend(r for r in plan.conflict if r in source)
    for relative in writes:
        validate_relative_path(relative)
        path = ensure_within(target_dir, Path(relative))
        mode = EXECUTABLE_MODE if relative in executable else FILE_MODE
        _atomic_write(path, source[relative], mode=mode)
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


def replace_directory_contents(
    target_dir: Path,
    source: Mapping[str, bytes],
    spec: FileSpec = STEERING_SPEC,
    *,
    executable: Collection[str] = (),
) -> list[str]:
    """Make the files selected by ``spec`` in ``target_dir`` match ``source`` exactly.

    Used inside the cache clone. Files that ``spec`` does not select are left alone.
    """
    existing = snapshot_directory(target_dir, spec)
    plan = plan_changes(source, existing, snapshot_hashes(existing), prune=True)
    return apply_plan(target_dir, source, plan, force=True, executable=executable)


def _empty_lock() -> dict[str, Any]:
    return {"schema_version": LOCK_VERSION, "collections": {}}


def _valid_lock(data: object) -> bool:
    """The record has a supported schema version and well formed collections."""
    if not isinstance(data, dict):
        return False
    version = data.get("schema_version", data.get("version"))
    if version != LOCK_VERSION or isinstance(version, bool):
        return False
    collections = data.get("collections")
    if not isinstance(collections, dict):
        return False
    for entry in collections.values():
        files = entry.get("files") if isinstance(entry, dict) else None
        if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            return False
    return True


def _set_aside(path: Path) -> Path | None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(target)
    except OSError:
        return None
    return target


def lock_problem(path: Path) -> str | None:
    """Why the record at ``path`` would be set aside, without touching it (None: fine)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"unreadable: {exc}"
    return None if _valid_lock(data) else "fails validation (wrong types or schema version)"


def read_lock(path: Path) -> dict[str, Any]:
    """Read the last sync record; a missing or damaged record yields an empty one.

    A record that cannot be read, is not JSON or fails validation (wrong types, an
    unknown schema version) is renamed to ``<name>.corrupt-<UTC timestamp>`` for
    inspection, with one warning, and treated as absent. Without a record every local
    file that differs from the central copy counts as a local edit and is kept, so a
    damaged record can never cause an overwrite.
    """
    if not path.is_file():
        return _empty_lock()
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        data = None
    if _valid_lock(data):
        assert isinstance(data, dict)  # noqa: S101 - narrowed by _valid_lock
        return data
    moved = _set_aside(path)
    log.warning(
        "the last sync record %s is damaged; set aside as %s and ignored (locally changed "
        "files are kept)",
        path,
        moved.name if moved else "(could not rename it)",
    )
    return _empty_lock()


def lock_collections(lock: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Return every collection's recorded hashes from a lock record."""
    collections = lock.get("collections", {})
    if not isinstance(collections, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for name, entry in collections.items():
        files = entry.get("files") if isinstance(entry, dict) else None
        if isinstance(files, dict):
            result[str(name)] = {str(k): str(v) for k, v in files.items()}
    return result


def lock_hashes(lock: Mapping[str, Any], collection: str = DEFAULT_COLLECTION) -> dict[str, str]:
    """Return the recorded hashes of one collection."""
    return lock_collections(lock).get(collection, {})


def write_lock(
    path: Path,
    *,
    remote: str,
    commit: str | None,
    collections: Mapping[str, Mapping[str, str]],
) -> None:
    record = {
        "schema_version": LOCK_VERSION,
        "remote": _USERINFO_RE.sub("", remote),
        "commit": commit,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "collections": {
            name: {"files": dict(sorted(files.items()))}
            for name, files in sorted(collections.items())
        },
    }
    _atomic_write(path, (json.dumps(record, indent=2, sort_keys=False) + "\n").encode("utf-8"))
