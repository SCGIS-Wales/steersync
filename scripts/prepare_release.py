"""Decide and prepare a release from a merged pull request.

Used by the CI workflow on every push to ``main``. The decision is deterministic:

1. A version in ``src/steersync/__init__.py`` that has no GitHub release yet is
   released as it is (a manual bump in the pull request wins).
2. Otherwise the bump comes from the merged pull request, first match wins:
   a ``release:major|minor|patch|skip`` label, a Conventional Commits prefix in
   the title (``feat!:`` or ``BREAKING CHANGE`` is major, ``feat:`` minor,
   ``fix:`` ``perf:`` ``refactor:`` ``deps:`` patch, ``docs:`` ``chore:`` ``ci:``
   ``test:`` ``style:`` ``build:`` skip), then the ``## [Unreleased]`` section of
   ``CHANGELOG.md`` (``### Added`` or ``### Removed`` present is minor, any other
   content patch, empty skip).
3. ``apply`` rewrites the version file and moves the Unreleased notes under the
   new version with today's date, adding the pull request title as the only
   note when the section was empty.

Every function is pure apart from ``apply`` so the rules are unit tested.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Literal

Bump = Literal["major", "minor", "patch", "skip"]

VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
UNRELEASED_RE = re.compile(
    r"^## \[Unreleased\][^\n]*\n(?P<body>.*?)(?=^## \[|\Z)",
    re.DOTALL | re.MULTILINE,
)
LABEL_PREFIX = "release:"
TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:")
TITLE_BUMPS: dict[str, Bump] = {
    "feat": "minor",
    "fix": "patch",
    "perf": "patch",
    "refactor": "patch",
    "deps": "patch",
    "revert": "patch",
    "docs": "skip",
    "chore": "skip",
    "ci": "skip",
    "test": "skip",
    "style": "skip",
    "build": "skip",
}
MINOR_HEADINGS = ("### Added", "### Removed")


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION_RE.search(text)
    if match is None:
        msg = "cannot read a semantic __version__ from the version file"
        raise ValueError(msg)
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def bump_version(version: tuple[int, int, int], bump: Bump) -> tuple[int, int, int]:
    major, minor, patch = version
    if bump == "major":
        return major + 1, 0, 0
    if bump == "minor":
        return major, minor + 1, 0
    if bump == "patch":
        return major, minor, patch + 1
    return version


def unreleased_section(changelog: str) -> str:
    """Return the body of the Unreleased section, stripped, or an empty string."""
    match = UNRELEASED_RE.search(changelog)
    return match.group("body").strip() if match else ""


def bump_from_labels(labels: list[str]) -> Bump | None:
    wanted = {f"{LABEL_PREFIX}{name}" for name in ("major", "minor", "patch", "skip")}
    found = [label.strip().lower() for label in labels if label.strip().lower() in wanted]
    if not found:
        return None
    order: list[Bump] = ["skip", "major", "minor", "patch"]
    for bump in order:
        if f"{LABEL_PREFIX}{bump}" in found:
            return bump
    return None  # pragma: no cover - every match is in order


def bump_from_title(title: str) -> Bump | None:
    text = title.strip()
    if "BREAKING CHANGE" in text:
        return "major"
    match = TITLE_RE.match(text)
    if match is None:
        return None
    if match.group("bang"):
        return "major"
    return TITLE_BUMPS.get(match.group("type"))


def bump_from_changelog(changelog: str) -> Bump:
    body = unreleased_section(changelog)
    if not body:
        return "skip"
    if any(heading in body for heading in MINOR_HEADINGS):
        return "minor"
    return "patch"


def decide_bump(labels: list[str], title: str, changelog: str) -> tuple[Bump, str]:
    """Return the bump and a one line reason."""
    from_label = bump_from_labels(labels)
    if from_label is not None:
        return from_label, f"label {LABEL_PREFIX}{from_label}"
    from_title = bump_from_title(title)
    if from_title is not None:
        return from_title, f"pull request title prefix ({title.split(':', 1)[0]})"
    from_changelog = bump_from_changelog(changelog)
    if from_changelog == "skip":
        return "skip", "no release label, no typed title and an empty Unreleased section"
    return from_changelog, "Unreleased section of CHANGELOG.md"


def move_unreleased(
    changelog: str, version: str, date: dt.date, *, fallback_note: str | None = None
) -> str:
    """Move the Unreleased notes under ``## [version] - date`` and leave Unreleased empty."""
    match = UNRELEASED_RE.search(changelog)
    if match is None:
        msg = "CHANGELOG.md has no '## [Unreleased]' section"
        raise ValueError(msg)
    body = match.group("body").strip()
    if not body:
        if not fallback_note:
            msg = "the Unreleased section is empty and no fallback note was given"
            raise ValueError(msg)
        body = f"### Changed\n\n- {fallback_note.strip()}"
    replacement = f"## [Unreleased]\n\n## [{version}] - {date.isoformat()}\n\n{body}\n\n"
    return changelog[: match.start()] + replacement + changelog[match.end() :]


def set_version(text: str, version: str) -> str:
    updated, count = VERSION_RE.subn(f'__version__ = "{version}"', text)
    if count != 1:
        msg = "version file must contain exactly one __version__ line"
        raise ValueError(msg)
    return updated


def _labels(raw: str) -> list[str]:
    return [item for item in raw.split(",") if item.strip()]


def cmd_decide(args: argparse.Namespace) -> int:
    changelog = Path(args.changelog).read_text("utf-8")
    current = parse_version(Path(args.version_file).read_text("utf-8"))
    bump, reason = decide_bump(_labels(args.labels), args.title, changelog)
    new_version = format_version(bump_version(current, bump))
    print(f"bump={bump}")
    print(f"reason={reason}")
    print(f"current={format_version(current)}")
    print(f"version={new_version}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    version_path = Path(args.version_file)
    changelog_path = Path(args.changelog)
    version_path.write_text(set_version(version_path.read_text("utf-8"), args.version), "utf-8")
    date = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.UTC).date()
    changelog_path.write_text(
        move_unreleased(
            changelog_path.read_text("utf-8"),
            args.version,
            date,
            fallback_note=args.fallback_note or None,
        ),
        "utf-8",
    )
    for extra in args.replace or []:
        path, old, new = extra.split("::", 2)
        target = Path(path)
        target.write_text(target.read_text("utf-8").replace(old, new), "utf-8")
    print(f"applied={args.version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0] if __doc__ else "")
    parser.add_argument("--version-file", default="src/steersync/__init__.py")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    commands = parser.add_subparsers(dest="command", required=True)
    decide = commands.add_parser("decide", help="print the bump for a merged pull request")
    decide.add_argument("--labels", default="", help="comma separated pull request labels")
    decide.add_argument("--title", default="", help="pull request title")
    decide.set_defaults(func=cmd_decide)
    apply = commands.add_parser("apply", help="write the version and move the changelog")
    apply.add_argument("--version", required=True)
    apply.add_argument("--date", default="", help="ISO date, default today (UTC)")
    apply.add_argument("--fallback-note", default="", help="note when Unreleased is empty")
    apply.add_argument(
        "--replace",
        action="append",
        help="extra literal replacement as path::old::new, e.g. README.md::0.1.0::0.2.0",
    )
    apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
