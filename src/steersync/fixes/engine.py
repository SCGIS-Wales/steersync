"""Collect, choose, fix to a fixpoint, verify, then write atomically after one backup.

The pipeline is pure up to :func:`apply_fixes`:

1. :func:`plan_fixes` reads each target and leaves out files with a possible secret, files
   with uncommitted changes (unless allowed) and fixes classed ``never``.
2. :func:`fix_text` applies the chosen fixers in catalogue order until nothing changes, at
   most ``lint.fix.max_passes`` times.
3. :func:`steersync.fixes.verify.verify` proves the result keeps the file's meaning.
4. :func:`apply_fixes` rechecks each file's hash just before writing it.
"""

import hashlib
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from steersync import backups
from steersync.config import LintConfig
from steersync.fixes import diffing, registry, verify
from steersync.fixes.model import (
    FileContext,
    FileFix,
    Fixer,
    NonConvergentFixError,
    Safety,
    VerificationError,
)


class Target(NamedTuple):
    path: Path
    label: str  # path shown to the user, relative to the workspace when possible
    profile: str


class Offer(NamedTuple):
    """An unsafe fix the user may choose: one rule in one file."""

    label: str
    rule: str
    summary: str


class FixPlan(NamedTuple):
    fixes: tuple[FileFix, ...]  # verified, ready to write
    offers: tuple[Offer, ...]  # unsafe fixes not chosen yet
    failed: tuple[str, ...]  # verification or convergence failures; files left alone
    skipped: tuple[str, ...]  # files left out, with the reason


Relint = Callable[[Target, str], Counter[str]]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fix_text(
    text: str, fixers: Sequence[Fixer], ctx: FileContext, max_passes: int
) -> tuple[str, tuple[str, ...]]:
    """Apply ``fixers`` in order until a pass changes nothing; the rules that changed it."""
    applied: list[str] = []
    current = text
    for _ in range(max_passes):
        before = current
        for fixer in fixers:
            changed = fixer.apply(current, ctx)
            if changed != current:
                applied.append(fixer.rule_id)
                current = changed
        if current == before:
            return current, tuple(dict.fromkeys(applied))
    msg = f"{ctx.path}: the fixes did not settle after {max_passes} passes"
    raise NonConvergentFixError(msg)


def _context(target: Target) -> FileContext:
    return FileContext(target.label, target.profile, target.path.parent.name)


def _fixers_for(
    target: Target, catalogue: Sequence[tuple[Fixer, Safety]]
) -> list[tuple[Fixer, Safety]]:
    return [(f, s) for f, s in catalogue if target.profile in f.profiles]


def _build(
    target: Target,
    original: str,
    fixers: Sequence[Fixer],
    max_passes: int,
    relint: Relint,
) -> tuple[str, tuple[str, ...]]:
    ctx = _context(target)
    fixed, rules = fix_text(original, fixers, ctx, max_passes)
    if fixed == original:
        return fixed, ()
    used = [f for f in fixers if f.rule_id in rules]
    verify.verify(
        original,
        fixed,
        target.label,
        used,
        refix=lambda text: fix_text(text, fixers, ctx, max_passes)[0],
        relint=lambda text: relint(target, text),
    )
    return fixed, rules


def _changes_rendering(fixer: Fixer, text: str, target: Target) -> bool:
    if not target.label.endswith(".md"):
        return False
    after = fixer.apply(text, _context(target))
    return after != text and not verify.renders_same(text, after)


def plan_fixes(
    targets: Sequence[Target],
    config: LintConfig,
    *,
    relint: Relint,
    secret: Callable[[Target, str], bool],
    dirty: Callable[[Path], bool],
    unsafe: Mapping[str, frozenset[str]] | None = None,
    all_unsafe: bool = False,
    select: Sequence[str] = (),
    ignore: Sequence[str] = (),
    allow_dirty: bool = False,
) -> FixPlan:
    """Every fix to make, the unsafe fixes on offer, and what was left alone and why.

    ``unsafe`` maps a file label to the unsafe rules chosen for it; ``all_unsafe`` chooses
    every unsafe fix (``--unsafe-fixes``).
    """
    catalogue = registry.catalogue(config.fix, select=select, ignore=ignore)
    chosen_unsafe = unsafe or {}
    fixes: list[FileFix] = []
    offers: list[Offer] = []
    failed: list[str] = []
    skipped: list[str] = []
    for target in targets:
        candidates = _fixers_for(target, catalogue)
        if not candidates:
            continue
        try:
            original = target.path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(f"{target.label}: cannot be read as UTF-8 ({exc})")
            continue
        if secret(target, original):
            skipped.append(f"{target.label}: possible secret; never fixed automatically")
            continue
        if config.fix.require_clean_git and not allow_dirty and dirty(target.path):
            skipped.append(f"{target.label}: uncommitted changes (--allow-dirty to fix anyway)")
            continue
        safe = [f for f, s in candidates if s == "safe"]
        risky = [f for f, s in candidates if s == "unsafe"]
        # Safe after verification: demoted to unsafe when it would change the rendering.
        demoted = [f for f in safe if f.demotable and _changes_rendering(f, original, target)]
        safe = [f for f in safe if f not in demoted]
        risky = [*risky, *demoted]
        wanted = chosen_unsafe.get(target.label, frozenset())
        # A demoted fix the user chose may change the rendering: that is why it was offered.
        picked = [
            replace(f, changes_markdown=True) if f in demoted else f
            for f in risky
            if all_unsafe or f.rule_id in wanted
        ]
        chosen_ids = {f.rule_id for f in [*safe, *picked]}
        ordered = [
            next(p for p in [*safe, *picked] if p.rule_id == f.rule_id)
            for f, _ in candidates
            if f.rule_id in chosen_ids
        ]
        try:
            fixed, rules = _build(target, original, ordered, config.fix.max_passes, relint)
            if not all_unsafe:
                for fixer in risky:
                    if fixer.rule_id in chosen_ids:
                        continue
                    _, trial_rules = fix_text(
                        fixed, [fixer], _context(target), config.fix.max_passes
                    )
                    if trial_rules:
                        offers.append(Offer(target.label, fixer.rule_id, fixer.summary))
        except (VerificationError, NonConvergentFixError) as exc:
            failed.append(f"{target.label}: {exc}")
            continue
        if rules:
            safety: Safety = "unsafe" if any(f.rule_id in rules for f in picked) else "safe"
            fixes.append(
                FileFix(
                    target.label,
                    sha256(original),
                    original,
                    fixed,
                    rules,
                    safety,
                    diffing.unified(target.label, original, fixed),
                )
            )
    return FixPlan(tuple(fixes), tuple(offers), tuple(failed), tuple(skipped))


def _write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        Path(temporary).chmod(mode)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def apply_fixes(
    fixes: Sequence[FileFix], paths: Mapping[str, Path], backup_root: Path
) -> tuple[str | None, list[str]]:
    """One backup for the run, then each file rechecked and written; the backup id and the
    files that changed under us (left alone)."""
    if not fixes:
        return None, []
    backup = backups.take_files(backup_root, [paths[f.path] for f in fixes], label="lint --fix")
    changed: list[str] = []
    for fix in fixes:
        target = paths[fix.path]
        if sha256(target.read_text("utf-8")) != fix.original_sha256:
            changed.append(fix.path)
            continue
        _write_atomic(target, fix.fixed)
    return backup.stamp, changed
