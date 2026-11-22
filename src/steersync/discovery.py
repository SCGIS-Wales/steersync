"""Find repositories on disk where steersync has been set up (read only).

``scan`` walks the given roots to ``max_depth``, stops descending at a repository, and
skips dependency and virtual environment folders and hidden directories. A repository
counts as set up when any marker is present: the last sync record under its git
directory, the steersync pre-push hook, steersync entries in ``info/exclude``, or a
``.kiro/steersync.yaml``.
"""

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from steersync.hooks import HOOK_MARKER

SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target"})
EXCLUDE_MARKERS = ("/.kiro/steering/", "/.kiro/skills/", "/.kiro/agents/")


def git_dir_of(work_tree: Path) -> Path | None:
    """The git directory of a work tree: ``.git`` itself, or where a ``.git`` file points."""
    dot_git = work_tree / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text("utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.removeprefix("gitdir:").strip())
            return target if target.is_absolute() else (work_tree / target).resolve()
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return ""


def is_initialised(work_tree: Path) -> bool:
    """Does this repository carry any sign that steersync was set up in it?"""
    if (work_tree / ".kiro" / "steersync.yaml").is_file():
        return True
    git_dir = git_dir_of(work_tree)
    if git_dir is None:
        return False
    if (git_dir / "steersync" / "lock.json").is_file() or (
        work_tree / ".kiro" / "steersync.lock.json"  # written by steersync 0.6 and earlier
    ).is_file():
        return True
    if HOOK_MARKER in _read(git_dir / "hooks" / "pre-push"):
        return True
    common = git_dir.parent.parent if git_dir.parent.name == "worktrees" else git_dir
    exclude = _read(common / "info" / "exclude")
    return any(marker in exclude for marker in EXCLUDE_MARKERS)


def _walk(root: Path, max_depth: int) -> Iterator[Path]:
    if not root.is_dir():
        return
    stack: list[tuple[Path, int]] = [(root.resolve(), 0)]
    while stack:
        here, depth = stack.pop()
        if (here / ".git").exists():
            yield here
            continue
        if depth >= max_depth:
            continue
        try:
            children = sorted(os.scandir(here), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for child in children:
            if child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            if child.is_dir(follow_symlinks=False):
                stack.append((Path(child.path), depth + 1))


def scan(roots: Iterable[Path], max_depth: int) -> list[Path]:
    """Repositories under ``roots`` (at most ``max_depth`` levels down) set up for steersync."""
    found: dict[str, Path] = {}
    for root in roots:
        for repo in _walk(root.expanduser(), max_depth):
            if is_initialised(repo):
                found.setdefault(str(repo), repo)
    return [found[key] for key in sorted(found)]
