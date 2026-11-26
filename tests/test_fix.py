"""steersync lint --fix: fixers, verification, the engine, the golden corpus and the CLI."""

import json
import os
import shutil
from collections import Counter
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from hypothesis import given, settings
from hypothesis import strategies as st

from steersync import cli_fix
from steersync.cli import main
from steersync.config import Config, default_config_text
from steersync.fixes import diffing, engine, frontmatter_fixes, json_fixes, registry
from steersync.fixes.model import FileContext, Fixer, NonConvergentFixError, VerificationError
from steersync.fixes.verify import verify
from steersync.interaction import Interaction
from tests.conftest import git

CONFIG = Config.model_validate(yaml.safe_load(default_config_text()))
LINT = CONFIG.lint
GOLDEN = Path(__file__).parent / "golden" / "fix"
CTX = FileContext(".kiro/steering/a.md", "steering", "steering")
SAFE = [f for f, s in registry.catalogue(LINT.fix) if s == "safe"]


def no_change(text: str) -> str:
    return text


def counts(_: str) -> Counter[str]:
    return Counter()


# --- fixers ------------------------------------------------------------------------------------


def test_front_matter_fixers() -> None:
    assert frontmatter_fixes.strip_bom("\ufeffx", CTX) == "x"
    assert frontmatter_fixes.to_lf("a\r\nb\r\n", CTX) == "a\nb\n"
    assert frontmatter_fixes.leading_blank("\n \n---\na: 1\n---\n", CTX) == "---\na: 1\n---\n"
    assert frontmatter_fixes.leading_blank("# No front matter\n", CTX) == "# No front matter\n"
    by_id = {f.rule_id: f for f in frontmatter_fixes.FIXERS}

    def run(rule: str, text: str, ctx: FileContext = CTX) -> str:
        return by_id[rule].apply(text, ctx)

    assert "fileMatchPattern: x" in run("fm-key-case", "---\nfilematchpattern: x\n---\n")
    both = "---\nfileMatchPattern: a\nfile_match_pattern: b\n---\n"
    assert run("fm-key-case", both) == both  # never overwrite the real key
    assert "inclusion: always" in run("fm-inclusion-case", "---\ninclusion: Always\n---\n")
    assert "inclusion: 'manual'" in run("fm-inclusion-case", "---\ninclusion: 'MANUAL'\n---\n")
    assert "inclusion: manual" in run("fm-inclusion-typo", "---\ninclusion: manuel\n---\n")
    ambiguous = "---\ninclusion: xyz\n---\n"
    assert run("fm-inclusion-typo", ambiguous) == ambiguous
    assert 'fileMatchPattern: "**/*.go"' in run(
        "fm-quote-glob", "---\nfileMatchPattern: **/*.go\n---\n"
    )
    split = run("fm-filematch-comma", '---\nfileMatchPattern: "a/**, b/**"\n---\n')
    assert 'fileMatchPattern: ["a/**", "b/**"]' in split
    named = run("fm-auto-name", "---\ninclusion: auto\ndescription: d\n---\n# T\n")
    assert "name: a\n" in named
    moved = run("fm-move-to-metadata", "---\ninclusion: manual\nowner: x\ntags: [a, b]\n---\n# T\n")
    assert yaml.safe_load(moved.split("---")[1]) == {
        "inclusion": "manual",
        "metadata": {"owner": "x", "tags": "a,b"},
    }
    skill = FileContext("skills/x/SKILL.md", "skill", "x")
    assert "name: release-notes" in run("skill-name-case", "---\nname: Release_Notes\n---\n", skill)
    assert json_fixes.json_format('{"a":1}', FileContext("a.json", "agent")) == '{\n  "a": 1\n}\n'
    assert json_fixes.json_format("{broken", FileContext("a.json", "agent")) == "{broken"
    assert json_fixes.json_format("x", FileContext("a.md", "agent")) == "x"


def test_registry_classes_from_config() -> None:
    fix = LINT.fix.model_copy(
        update={"unfixable": ("json",), "extend_safe_fixes": ("fm-quote-glob",)}
    )
    classes = {f.rule_id: s for f, s in registry.catalogue(fix)}
    assert "json-format" not in classes
    assert classes["fm-quote-glob"] == "safe"
    assert classes["fm-filematch-comma"] == "unsafe"
    only = registry.catalogue(LINT.fix, select=("fm-",), ignore=("fm-move",))
    assert {f.rule_id for f, _ in only} >= {"fm-key-case"}
    assert "fm-move-to-metadata" not in {f.rule_id for f, _ in only}
    assert registry.safety_of("unknown-fix", LINT.fix) == "never"


# --- verification ------------------------------------------------------------------------------


def test_verify_rejects_changes_of_meaning() -> None:
    fixer = SAFE[0]
    base = "---\ninclusion: manual\n---\n\n# T\n\nSee #[[file:a.md]].\n\n```py\nx = 1\n```\n"
    verify(base, base.replace("\n# T", "\n# T"), "a.md", [fixer], refix=no_change, relint=counts)
    cases = {
        "front matter data": base.replace("manual", "always"),
        "rendered Markdown": base.replace("# T", "## T"),
        "reference": base.replace("a.md]]", "b.md]]"),
        "fenced code": base.replace("x = 1", "x = 2"),
        "valid YAML": base.replace("inclusion: manual", "inclusion: [manual"),
    }
    for reason, broken in cases.items():
        with pytest.raises(VerificationError, match=reason.split()[-1]):
            verify(base, broken, "a.md", [fixer], refix=no_change, relint=counts)
    with pytest.raises(VerificationError, match="JSON data"):
        verify('{"a": 1}', '{"a": 2}', "a.json", [fixer], refix=no_change, relint=counts)
    with pytest.raises(VerificationError, match="no longer parses"):
        verify('{"a": 1}', "{", "a.json", [fixer], refix=no_change, relint=counts)
    with pytest.raises(VerificationError, match="idempotent"):
        verify(base, base, "a.md", [fixer], refix=lambda t: t + "x", relint=counts)

    def worse(text: str) -> Counter[str]:
        return Counter({"fm-yaml": 1}) if text != base else Counter()

    changed = base.replace("# T\n", "# T \n")
    with pytest.raises(VerificationError, match="fm-yaml"):
        verify(base, changed, "a.md", [fixer], refix=no_change, relint=worse)


def test_non_convergent_fixers_stop() -> None:
    flip = Fixer("flip", frozenset({"steering"}), lambda t, _: "b" if t == "a" else "a", "x")
    with pytest.raises(NonConvergentFixError, match="did not settle after 3 passes"):
        engine.fix_text("a", [flip], CTX, 3)


@settings(max_examples=60, deadline=None)
@given(st.text(alphabet="ab -#:\n\r\ufeff*,", max_size=60))
def test_safe_fixes_are_idempotent(text: str) -> None:
    fixed, _ = engine.fix_text(text, SAFE, CTX, 100)
    assert engine.fix_text(fixed, SAFE, CTX, 100)[0] == fixed


def test_diff_has_git_prefixes() -> None:
    diff = diffing.unified("x.md", "a\n", "b\n")
    assert diff.startswith("--- a/x.md\n+++ b/x.md\n")


# --- the golden corpus ---------------------------------------------------------------------------


def _target(root: Path, path: Path) -> engine.Target:
    profile = "agent" if "/agents/" in path.as_posix() else "steering"
    return engine.Target(path, path.relative_to(root).as_posix(), profile)


@pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()))
def test_golden_fix(case: str, tmp_path: Path) -> None:
    """input/ fixed (with case.json's unsafe choice) must equal expected/ and diff.patch;
    a second run changes nothing. STEERSYNC_UPDATE_GOLDEN=1 regenerates."""
    folder = GOLDEN / case
    meta = json.loads((folder / "case.json").read_text())
    work = tmp_path / "ws"
    shutil.copytree(folder / "input", work)
    found = [_target(work, p) for p in sorted(work.rglob("*")) if p.is_file()]
    relint = cli_fix._relint(CONFIG, work)
    secret = cli_fix._secret(CONFIG, work)
    plan = engine.plan_fixes(
        found, LINT, relint=relint, secret=secret, dirty=lambda _: False, all_unsafe=meta["unsafe"]
    )
    assert plan.failed == ()
    engine.apply_fixes(plan.fixes, {t.label: t.path for t in found}, tmp_path / "backups")
    diff = "".join(f.diff for f in plan.fixes)
    if os.environ.get("STEERSYNC_UPDATE_GOLDEN") == "1":
        shutil.rmtree(folder / "expected", ignore_errors=True)
        shutil.copytree(work, folder / "expected")
        (folder / "diff.patch").write_bytes(diff.encode())
    for expected in sorted((folder / "expected").rglob("*")):
        if expected.is_file():
            actual = work / expected.relative_to(folder / "expected")
            assert actual.read_bytes() == expected.read_bytes(), expected
    assert diff.encode() == (folder / "diff.patch").read_bytes()
    again = engine.plan_fixes(
        found, LINT, relint=relint, secret=secret, dirty=lambda _: False, all_unsafe=meta["unsafe"]
    )
    assert again.fixes == ()


# --- the CLI ---------------------------------------------------------------------------------------


@pytest.fixture
def workspace(
    project: Path, config_file: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    steering = project / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "api.md").write_bytes(
        b"\xef\xbb\xbf---\ninclusion: Always\n---\n\n# API\n\nRules.\n"
    )
    (steering / "py.md").write_text(
        '---\ninclusion: fileMatch\nfileMatchPattern: "src/**, tests/**"\n---\n\n# Py\n\nTypes.\n'
    )
    git("add", "-A", cwd=project, env=env)
    git("commit", "-qm", "kiro", cwd=project, env=env)
    return project


def test_diff_writes_nothing_and_exits_4(workspace: Path) -> None:
    api = workspace / ".kiro" / "steering" / "api.md"
    before = api.read_bytes()
    result = CliRunner().invoke(main, ["lint", "--diff"])
    assert result.exit_code == 4
    assert "+inclusion: always" in result.output
    assert api.read_bytes() == before


def test_fix_safe_then_unsafe_and_undo(workspace: Path, env: dict[str, str]) -> None:
    api = workspace / ".kiro" / "steering" / "api.md"
    py = workspace / ".kiro" / "steering" / "py.md"
    before = api.read_bytes()
    result = CliRunner().invoke(main, ["lint", "--fix", "--show-fixes"])
    assert "fixed 1 file(s)" in result.output
    assert "file-bom, fm-inclusion-case" in result.output
    assert "unsafe fixes available" in result.output
    assert "fm-filematch-comma · .kiro/steering/py.md" in result.output
    assert api.read_text().startswith("---\ninclusion: always\n")
    assert '"src/**, tests/**"' in py.read_text()  # unsafe: listed, not applied
    stamp = result.output.split("Undo with: steersync restore ")[1].split()[0]
    git("commit", "-qam", "safe fixes", cwd=workspace, env=env)
    unsafe = CliRunner().invoke(main, ["lint", "--fix", "--unsafe-fixes"])
    assert 'fileMatchPattern: ["src/**", "tests/**"]' in py.read_text()
    assert "fixed 1 file(s)" in unsafe.output
    restored = CliRunner().invoke(main, ["restore", stamp, "--yes"])
    assert restored.exit_code == 0, restored.output
    assert api.read_bytes() == before


def test_dirty_files_are_refused_unless_allowed(workspace: Path) -> None:
    api = workspace / ".kiro" / "steering" / "api.md"
    api.write_bytes(api.read_bytes() + b"\nMore.\n")
    refused = CliRunner().invoke(main, ["lint", "--fix"])
    assert "uncommitted changes (--allow-dirty to fix anyway)" in refused.output
    assert api.read_bytes().startswith(b"\xef\xbb\xbf")
    allowed = CliRunner().invoke(main, ["lint", "--fix", "--allow-dirty"])
    assert "fixed 1 file(s)" in allowed.output
    assert not api.read_bytes().startswith(b"\xef\xbb\xbf")


def test_failed_verification_exits_11_and_leaves_the_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from steersync.fixes import verify as verify_module

    def reject(*_: object, **__: object) -> None:
        raise VerificationError("the rendered Markdown would change")

    monkeypatch.setattr(verify_module, "verify", reject)
    api = workspace / ".kiro" / "steering" / "api.md"
    before = api.read_bytes()
    result = CliRunner().invoke(main, ["lint", "--fix"])
    assert result.exit_code == 11
    assert "FIX FAILED .kiro/steering/api.md" in result.output
    assert api.read_bytes() == before


def test_interactive_unsafe_choice(workspace: Path) -> None:
    py = workspace / ".kiro" / "steering" / "py.md"
    asked: list[list[engine.Offer]] = []

    def choose(offers: object) -> dict[str, frozenset[str]]:
        asked.append(list(offers))  # type: ignore[call-overload]
        return {".kiro/steering/py.md": frozenset({"fm-filematch-comma"})}

    found = [_target(workspace, p) for p in sorted((workspace / ".kiro").rglob("*.md"))]
    code = cli_fix.run_fix(
        CONFIG,
        workspace,
        found,
        Interaction(True, False, False, False, "text"),
        diff_only=False,
        unsafe_fixes=False,
        show_fixes=False,
        select=(),
        ignore=(),
        allow_dirty=False,
        choose=choose,
    )
    assert code == 0
    assert [o.rule for o in asked[0]] == ["fm-filematch-comma"]
    assert 'fileMatchPattern: ["src/**", "tests/**"]' in py.read_text()


def test_fail_level(workspace: Path) -> None:
    from steersync.lint import LintFinding, LintReport

    report = LintReport((LintFinding("a", "x", "warning", "m"),), 1)
    assert not cli_fix.fails(report, "error")
    assert cli_fix.fails(report, "warning")
    assert CliRunner().invoke(main, ["lint", "--fix"]).exit_code in {0, 4}


def test_fixer_edge_cases() -> None:
    by_id = {f.rule_id: f for f in frontmatter_fixes.FIXERS}
    plain = "# No front matter\n"
    for rule in ("fm-auto-name", "fm-move-to-metadata", "fm-key-case"):
        assert by_id[rule].apply(plain, CTX) == plain
    manual = "---\ninclusion: manual\n---\n# T\n"
    assert by_id["fm-auto-name"].apply(manual, CTX) == manual
    named = "---\ninclusion: auto\nname: x\n---\n# T\n"
    assert by_id["fm-auto-name"].apply(named, CTX) == named
    move = by_id["fm-move-to-metadata"].apply
    for unchanged in (
        "---\n[broken\n---\n# T\n",  # invalid YAML is another fixer's job
        "---\n- a list\n---\n# T\n",
        "---\ninclusion: manual\n---\n# T\n",  # nothing to move
        "---\ninclusion: manual\nowner: x\nmetadata: [a]\n---\n# T\n",  # metadata not a map
    ):
        assert move(unchanged, CTX) == unchanged
    merged = move("---\nowner: x\nmetadata:\n  team: y\n---\n# T\n", CTX)
    assert yaml.safe_load(merged.split("---")[1]) == {"metadata": {"team": "y", "owner": "x"}}


def test_engine_guards(tmp_path: Path) -> None:
    unreadable = tmp_path / "bin.md"
    unreadable.write_bytes(b"\xff\xfe")
    leak = tmp_path / "leak.md"
    leak.write_text("﻿# Leak\n", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("x")
    found = [
        engine.Target(unreadable, "bin.md", "steering"),
        engine.Target(leak, "leak.md", "steering"),
        engine.Target(other, "other.txt", "no-such-profile"),
    ]
    plan = engine.plan_fixes(
        found,
        LINT,
        relint=lambda *_: Counter(),
        secret=lambda t, _: t.label == "leak.md",
        dirty=lambda _: False,
    )
    assert plan.fixes == ()
    assert any("cannot be read as UTF-8" in s for s in plan.skipped)
    assert any("possible secret" in s for s in plan.skipped)
    assert engine.apply_fixes([], {}, tmp_path) == (None, [])


def test_a_file_edited_during_the_run_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("﻿# A\n", encoding="utf-8")
    target = engine.Target(path, "a.md", "steering")
    plan = engine.plan_fixes(
        [target],
        LINT,
        relint=lambda *_: Counter(),
        secret=lambda *_: False,
        dirty=lambda _: False,
    )
    path.write_text("﻿# A, edited meanwhile\n", encoding="utf-8")
    stamp, changed = engine.apply_fixes(plan.fixes, {"a.md": path}, tmp_path / "backups")
    assert stamp is not None
    assert changed == ["a.md"]
    assert path.read_text() == "﻿# A, edited meanwhile\n"


def test_atomic_write_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.md"
    path.write_text("old")

    def broken(self: Path, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        engine._write_atomic(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]
