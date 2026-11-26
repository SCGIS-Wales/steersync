"""Fixers and the fixes they produce."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Safety = Literal["safe", "unsafe", "never"]


@dataclass(frozen=True)
class FileContext:
    path: str  # relative path, for messages
    profile: str
    folder: str = ""  # name of the containing directory (skills)


@dataclass(frozen=True)
class Fixer:
    rule_id: str
    profiles: frozenset[str]
    apply: Callable[[str, FileContext], str]
    summary: str
    # Front matter keys the fixer may change; verification ignores them.
    changes_keys: frozenset[str] = field(default_factory=frozenset)
    # The fixer may change rendered Markdown (verification then skips the HTML check).
    changes_markdown: bool = False


@dataclass(frozen=True)
class FileFix:
    path: str
    original_sha256: str
    original: str
    fixed: str
    rules: tuple[str, ...]
    safety: Safety
    diff: str


class NonConvergentFixError(Exception):
    """Fixers kept changing the text past lint.fix.max_passes."""


class VerificationError(Exception):
    """A fix would change what the file means; nothing is written for it."""
