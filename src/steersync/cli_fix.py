"""``steersync lint --fix`` and ``--diff``: targets, the unsafe choice, apply, report.

Exit codes: 0 when nothing at or above ``lint.fail_level`` remains, 4 when findings remain
or ``--diff`` found pending changes, 11 when a fix failed verification (that file is left
untouched).
"""

import functools
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple, cast

import click

from steersync import gitops
from steersync.config import CollectionConfig, Config, LintProfile
from steersync.fixes import engine
from steersync.fixes.model import FileFix
from steersync.interaction import Interaction
from steersync.lint import LintReport, is_secret_rule, lint_document
from steersync.paths import backup_root, workspace_collection_dir
from steersync.steering import FileSpec, discover_files, document_from_bytes

EXIT_FINDINGS = 4
EXIT_FIX_FAILED = 11
SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}


def fails(report: LintReport, level: str) -> bool:
    """True when a finding is at or above ``lint.fail_level``."""
    threshold = SEVERITY_ORDER[level]
    return any(SEVERITY_ORDER[f.severity] >= threshold for f in report.findings)


def _label(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _files(path: Path, collection: CollectionConfig | None) -> list[Path]:
    if path.is_file():
        return [path]
    spec = (
        FileSpec(collection.include, collection.exclude, collection.max_depth)
        if collection
        else None
    )
    return list(discover_files(path, spec) if spec else discover_files(path))


def collection_for_profile(config: Config, profile: str | None) -> CollectionConfig | None:
    """The first collection using ``profile``, so its file patterns apply to a directory."""
    if profile is None:
        return None
    return next((c for c in config.collections.values() if c.lint == profile), None)


def collection_for_path(config: Config, root: Path | None, path: Path) -> str | None:
    """Return the enabled collection whose workspace directory contains ``path``."""
    if root is None:
        return None
    resolved = path.resolve()
    root_resolved = root.resolve()
    best: tuple[int, str] | None = None
    for name, collection in config.enabled_collections().items():
        target = workspace_collection_dir(collection, root)
        if target is None:
            continue
        base = target.resolve()
        if base == root_resolved:
            # A collection rooted at the workspace (AGENTS.md, .kiroignore) owns only the
            # files its patterns select, never every path under the repository.
            spec = FileSpec(collection.include, collection.exclude, collection.max_depth)
            if (
                resolved.is_file()
                and base in resolved.parents
                and spec.matcher()(resolved.relative_to(base).as_posix())
            ):
                best = best or (0, name)
            continue
        if resolved == base or base in resolved.parents:
            depth = len(base.parts)
            if best is None or depth > best[0]:
                best = (depth, name)
    return best[1] if best else None


def lookup(
    config: Config, root: Path | None, collections: Sequence[str], profile: str | None
) -> Callable[[Path], CollectionConfig | None]:
    """The collection an explicit path belongs to (``--collection`` forces one)."""
    forced = config.collections[collections[0]] if collections else None

    def find(path: Path) -> CollectionConfig | None:
        if forced is not None:
            return forced
        name = collection_for_path(config, root, path)
        return config.collections[name] if name else collection_for_profile(config, profile)

    return find


def targets(
    config: Config,
    root: Path | None,
    paths: Sequence[Path],
    collections: Sequence[CollectionConfig],
    collection_for: Callable[[Path], CollectionConfig | None],
    profile: str | None,
) -> list[engine.Target]:
    """Every file lint would check, with the profile it is checked with."""
    found: list[engine.Target] = []
    if paths:
        for path in paths:
            collection = collection_for(path)
            chosen = profile or (collection.lint if collection else "steering")
            found += [engine.Target(f, _label(f, root), chosen) for f in _files(path, collection)]
        return found
    for collection in collections:
        directory = workspace_collection_dir(collection, root) if root else None
        if directory is None or not directory.is_dir():
            continue
        found += [
            engine.Target(f, _label(f, root), collection.lint)
            for f in _files(directory, collection)
        ]
    del config
    return found


def _relint(config: Config, root: Path | None) -> engine.Relint:
    def relint(target: engine.Target, text: str) -> Counter[str]:
        doc = document_from_bytes(target.label, text.encode("utf-8"))
        found = lint_document(doc, config.lint, root, profile=cast("LintProfile", target.profile))
        return Counter(f.rule for f in found)

    return relint


def _secret(config: Config, root: Path | None) -> Callable[[engine.Target, str], bool]:
    def secret(target: engine.Target, text: str) -> bool:
        doc = document_from_bytes(target.label, text.encode("utf-8"))
        found = lint_document(doc, config.lint, root, profile=cast("LintProfile", target.profile))
        return any(is_secret_rule(f.rule) for f in found)

    return secret


def _severities(config: Config, root: Path | None, target: engine.Target) -> Counter[str]:
    """How many errors, warnings and infos ``target`` has now."""
    doc = document_from_bytes(target.label, target.path.read_bytes())
    found = lint_document(doc, config.lint, root, profile=cast("LintProfile", target.profile))
    return Counter(f.severity for f in found)


def _dirty(config: Config, root: Path | None) -> Callable[[Path], bool]:
    def dirty(path: Path) -> bool:
        if root is None:
            return False
        result = gitops.run_git(
            config, ["status", "--porcelain", "--", str(path)], cwd=root, check=False
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    return dirty


def _choose_unsafe(offers: Sequence[engine.Offer]) -> dict[str, frozenset[str]]:
    """One checkbox, every unsafe fix unchecked; returns the chosen rules per file."""
    import questionary  # noqa: PLC0415 - interface library, loaded on demand

    choices = [
        questionary.Choice(f"{o.rule} · {o.label} · {o.summary}", value=(o.label, o.rule))
        for o in offers
    ]
    picked = (
        questionary.checkbox(
            "Unsafe fixes change how a file behaves. Select the ones to apply:", choices=choices
        ).ask()
        or []
    )
    chosen: dict[str, set[str]] = {}
    for label, rule in picked:
        chosen.setdefault(label, set()).add(rule)
    return {label: frozenset(rules) for label, rules in chosen.items()}


class FixOutcome(NamedTuple):
    code: int
    fixes: tuple[FileFix, ...]
    applied: bool


def run_fix(
    config: Config,
    root: Path | None,
    found: Sequence[engine.Target],
    interaction: Interaction,
    *,
    diff_only: bool,
    unsafe_fixes: bool,
    show_fixes: bool,
    select: Sequence[str],
    ignore: Sequence[str],
    allow_dirty: bool,
    choose: Callable[[Sequence[engine.Offer]], dict[str, frozenset[str]]] = _choose_unsafe,
    machine: bool = False,
) -> FixOutcome:
    """Plan, choose, apply (or print diffs); the fix part of the exit code and the fixes.

    With ``machine`` output (JSON, SARIF) every message goes to stderr.
    """

    def say(text: str, *, err: bool = False, nl: bool = True) -> None:
        click.echo(text, err=err or machine, nl=nl)

    plan_for = functools.partial(
        engine.plan_fixes,
        found,
        config.lint,
        relint=_relint(config, root),
        secret=_secret(config, root),
        dirty=_dirty(config, root),
        select=select,
        ignore=ignore,
        allow_dirty=allow_dirty,
    )
    plan = plan_for(all_unsafe=unsafe_fixes)
    if plan.offers and interaction.interactive and not diff_only and not unsafe_fixes:
        chosen = choose(plan.offers)
        if chosen:
            plan = plan_for(unsafe=chosen)
    for note in plan.skipped:
        say(f"skipped {note}", err=True)
    for failure in plan.failed:
        say(f"FIX FAILED {failure} (file left untouched)", err=True)
    if plan.offers and not unsafe_fixes:
        say("unsafe fixes available (rerun with --unsafe-fixes to apply them):", err=True)
        for offer in plan.offers:
            say(f"  {offer.rule} · {offer.label} · {offer.summary}", err=True)
    failed_code = EXIT_FIX_FAILED if plan.failed else 0
    if diff_only:
        if not machine:
            for fix in plan.fixes:
                say(fix.diff, nl=False)
        code = failed_code or (EXIT_FINDINGS if plan.fixes else 0)
        return FixOutcome(code, plan.fixes, False)
    paths = {t.label: t.path for t in found}
    backup_id, changed = engine.apply_fixes(plan.fixes, paths, backup_root(os.environ))
    applied = tuple(f for f in plan.fixes if f.path not in changed)
    for label in changed:
        say(f"skipped {label}: changed while fixing; nothing written", err=True)
    if applied:
        say(f"fixed {len(applied)} file(s)")
        if show_fixes:
            for fix in applied:
                say(f"  {fix.path}: {', '.join(fix.rules)}")
        if backup_id and config.ux.show_undo_hint:
            say(f"Undo with: steersync restore {backup_id}")
    elif not plan.failed:
        say("nothing to fix")
    return FixOutcome(failed_code, applied, True)


def exit_with(fix_code: int, report: LintReport, fail_level: str) -> None:
    if fix_code:
        sys.exit(fix_code)
    if fails(report, fail_level):
        sys.exit(EXIT_FINDINGS)
