"""Rules of the release decision script used by the CI workflow."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_release.py"
spec = importlib.util.spec_from_file_location("prepare_release", SCRIPT)
assert spec is not None
assert spec.loader is not None
prepare_release = importlib.util.module_from_spec(spec)
sys.modules["prepare_release"] = prepare_release
spec.loader.exec_module(prepare_release)

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something new.

### Fixed

- Something broken.

## [0.2.0] - 2026-10-12

### Added

- Old.
"""


def test_version_helpers() -> None:
    assert prepare_release.parse_version('x = 1\n__version__ = "1.2.3"\n') == (1, 2, 3)
    with pytest.raises(ValueError, match="semantic"):
        prepare_release.parse_version('__version__ = "1.2"\n')
    assert prepare_release.bump_version((1, 2, 3), "major") == (2, 0, 0)
    assert prepare_release.bump_version((1, 2, 3), "minor") == (1, 3, 0)
    assert prepare_release.bump_version((1, 2, 3), "patch") == (1, 2, 4)
    assert prepare_release.bump_version((1, 2, 3), "skip") == (1, 2, 3)
    assert prepare_release.format_version((0, 3, 0)) == "0.3.0"
    assert (
        prepare_release.set_version('__version__ = "0.2.0"\n', "0.3.0") == '__version__ = "0.3.0"\n'
    )
    with pytest.raises(ValueError, match="exactly one"):
        prepare_release.set_version("nothing\n", "1.0.0")


@pytest.mark.parametrize(
    ("labels", "title", "expected"),
    [
        (["release:major"], "docs: x", "major"),
        (["bug", "Release:Patch"], "feat: x", "patch"),
        (["release:skip", "release:major"], "feat!: x", "skip"),
        ([], "feat: add collections", "minor"),
        ([], "feat(cli)!: drop flag", "major"),
        ([], "fix: retry", "patch"),
        ([], "Something with BREAKING CHANGE inside", "major"),
        ([], "docs: readme", "skip"),
        ([], "ci: pin actions", "skip"),
        ([], "Plain title", "minor"),
    ],
)
def test_decide_bump(labels: list[str], title: str, expected: str) -> None:
    bump, reason = prepare_release.decide_bump(labels, title, CHANGELOG)
    assert bump == expected
    assert reason


def test_changelog_drives_the_bump_without_labels_or_prefix() -> None:
    patch_only = CHANGELOG.replace("### Added\n\n- Something new.\n\n", "", 1)
    assert prepare_release.decide_bump([], "Plain", patch_only)[0] == "patch"
    empty = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-10-12\n\n- Old.\n"
    bump, reason = prepare_release.decide_bump([], "Plain", empty)
    assert bump == "skip"
    assert "empty Unreleased" in reason
    assert prepare_release.unreleased_section("# No sections\n") == ""


def test_move_unreleased() -> None:
    moved = prepare_release.move_unreleased(CHANGELOG, "0.3.0", dt.date(2026, 10, 13))
    assert "## [Unreleased]\n\n## [0.3.0] - 2026-10-13\n\n### Added\n\n- Something new." in moved
    assert moved.count("## [Unreleased]") == 1
    assert moved.index("## [0.3.0]") < moved.index("## [0.2.0]")
    assert prepare_release.unreleased_section(moved) == ""
    empty = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-10-12\n\n- Old.\n"
    with pytest.raises(ValueError, match="empty"):
        prepare_release.move_unreleased(empty, "0.2.1", dt.date(2026, 10, 13))
    filled = prepare_release.move_unreleased(
        empty, "0.2.1", dt.date(2026, 10, 13), fallback_note="Fix the retry"
    )
    assert "## [0.2.1] - 2026-10-13\n\n### Changed\n\n- Fix the retry\n\n## [0.2.0]" in filled
    with pytest.raises(ValueError, match="Unreleased"):
        prepare_release.move_unreleased("# Nothing\n", "1.0.0", dt.date(2026, 1, 1))


def test_cli_decide_and_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    version_file = tmp_path / "__init__.py"
    version_file.write_text('"""doc"""\n\n__version__ = "0.2.0"\n')
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG)
    readme = tmp_path / "README.md"
    readme.write_text("VERSION=0.2.0\nrev: v0.2.0\n")
    code = prepare_release.main(
        [
            "--version-file",
            str(version_file),
            "--changelog",
            str(changelog),
            "decide",
            "--labels",
            "release:minor,bug",
            "--title",
            "fix: x",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "bump=minor" in out
    assert "version=0.3.0" in out
    assert "current=0.2.0" in out
    code = prepare_release.main(
        [
            "--version-file",
            str(version_file),
            "--changelog",
            str(changelog),
            "apply",
            "--version",
            "0.3.0",
            "--date",
            "2026-10-13",
            "--replace",
            f"{readme}::0.2.0::0.3.0",
        ]
    )
    assert code == 0
    assert '__version__ = "0.3.0"' in version_file.read_text()
    assert "## [0.3.0] - 2026-10-13" in changelog.read_text()
    assert readme.read_text() == "VERSION=0.3.0\nrev: v0.3.0\n"
    broken = prepare_release.main(
        [
            "--version-file",
            str(version_file),
            "--changelog",
            str(changelog),
            "apply",
            "--version",
            "0.4.0",
        ]
    )
    assert broken == 1
    assert "empty" in capsys.readouterr().err
