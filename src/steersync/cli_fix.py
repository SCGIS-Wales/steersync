"""``steersync lint --fix`` and ``--diff``: targets, the unsafe choice, apply, report.

Exit codes: 0 when nothing at or above ``lint.fail_level`` remains, 4 when findings remain
or ``--diff`` found pending changes, 11 when a fix failed verification (that file is left
untouched).
"""

import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

import click

from steersync import gitops
from steersync.config import CollectionConfig, Config
from steersync.fixes import engine
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
        found = lint_document(doc, config.lint, root, profile=target.profile)  # type: ignore[arg-type]
        return Counter(f.rule for f in found)

    return relint


def _secret(config: Config, root: Path | None) -> Callable[[engine.Target, str], bool]:
    def secret(target: engine.Target, text: str) -> bool:
        doc = document_from_bytes(target.label, text.encode("utf-8"))
        found = lint_document(doc, config.lint, root, profile=target.profile)  # type: ignore[arg-type]
        return any(is_secret_rule(f.rule) for f in found)

    return secret


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
) -> int:
    """Plan, choose, apply (or print diffs) and return the fix part of the exit code."""
    common = {
        "relint": _relint(config, root),
        "secret": _secret(config, root),
        "dirty": _dirty(config, root),
        "select": select,
        "ignore": ignore,
        "allow_dirty": allow_dirty,
    }
    plan = engine.plan_fixes(found, config.lint, all_unsafe=unsafe_fixes, **common)  # type: ignore[arg-type]
    if plan.offers and interaction.interactive and not diff_only and not unsafe_fixes:
        chosen = choose(plan.offers)
        if chosen:
            plan = engine.plan_fixes(found, config.lint, unsafe=chosen, **common)  # type: ignore[arg-type]
    for note in plan.skipped:
        click.echo(f"skipped {note}", err=True)
    for failure in plan.failed:
        click.echo(f"FIX FAILED {failure} (file left untouched)", err=True)
    if plan.offers and not unsafe_fixes:
        click.echo("unsafe fixes available (rerun with --unsafe-fixes to apply them):", err=True)
        for offer in plan.offers:
            click.echo(f"  {offer.rule} · {offer.label} · {offer.summary}", err=True)
    if diff_only:
        for fix in plan.fixes:
            click.echo(fix.diff, nl=False)
        if plan.failed:
            return EXIT_FIX_FAILED
        return EXIT_FINDINGS if plan.fixes else 0
    paths = {t.label: t.path for t in found}
    backup_id, changed = engine.apply_fixes(plan.fixes, paths, backup_root(os.environ))
    applied = [f for f in plan.fixes if f.path not in changed]
    for label in changed:
        click.echo(f"skipped {label}: changed while fixing; nothing written", err=True)
    if applied:
        click.echo(f"fixed {len(applied)} file(s)")
        if show_fixes:
            for fix in applied:
                click.echo(f"  {fix.path}: {', '.join(fix.rules)}")
        if backup_id and config.ux.show_undo_hint:
            click.echo(f"Undo with: steersync restore {backup_id}")
    elif not plan.failed:
        click.echo("nothing to fix")
    return EXIT_FIX_FAILED if plan.failed else 0


def exit_with(fix_code: int, report: LintReport, fail_level: str) -> None:
    if fix_code:
        sys.exit(fix_code)
    if fails(report, fail_level):
        sys.exit(EXIT_FINDINGS)
