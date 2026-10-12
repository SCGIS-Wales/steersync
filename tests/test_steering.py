from __future__ import annotations

import json
from pathlib import Path

import pytest

from steersync.errors import UnsafePathError
from steersync.paths import ensure_within
from steersync.steering import (
    apply_plan,
    discover_steering_files,
    file_digest,
    file_references,
    lock_collections,
    lock_hashes,
    parse_front_matter,
    plan_changes,
    read_document,
    read_lock,
    replace_directory_contents,
    snapshot_digest,
    snapshot_directory,
    snapshot_hashes,
    validate_relative_path,
    write_lock,
)


def test_parse_front_matter_variants() -> None:
    fm = parse_front_matter(
        "---\ninclusion: fileMatch\nfileMatchPattern: ['a', 'b']\n---\n# T\n\nbody\n"
    )
    assert fm.present
    assert not fm.leading_blank
    assert fm.error is None
    assert fm.metadata == {"inclusion": "fileMatch", "fileMatchPattern": ["a", "b"]}
    assert fm.body.startswith("# T")

    absent = parse_front_matter("# Just markdown\n")
    assert not absent.present
    assert absent.metadata == {}

    blank = parse_front_matter("\n---\ninclusion: manual\n---\n# T\n")
    assert blank.leading_blank
    assert not blank.present
    assert blank.metadata == {}

    broken = parse_front_matter("---\ninclusion: [unclosed\n---\n# T\n")
    assert broken.present
    assert broken.error
    assert "invalid YAML" in broken.error

    crlf = parse_front_matter("---\r\ninclusion: manual\r\n---\r\n# T\r\n")
    assert crlf.present
    assert crlf.metadata["inclusion"] == "manual"

    scalar = parse_front_matter("---\njust a string\n---\n# T\n")
    assert scalar.present
    assert scalar.metadata == {}


def test_discover_skips_hidden_and_non_markdown(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.md").write_text("# B\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "c.md").write_text("# C\n")
    (tmp_path / ".dot.md").write_text("# D\n")
    (tmp_path / "notes.txt").write_text("x")
    found = [p.relative_to(tmp_path).as_posix() for p in discover_steering_files(tmp_path)]
    assert found == ["a.md", "nested/b.md"]
    assert discover_steering_files(tmp_path / "missing") == []


def test_discover_follows_file_symlinks_only(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.md").write_text("# L\n")
    steering = tmp_path / "steering"
    steering.mkdir()
    (steering / "linked.md").symlink_to(outside / "linked.md")
    (steering / "dirlink").symlink_to(outside)
    found = [p.name for p in discover_steering_files(steering)]
    assert found == ["linked.md"]


@pytest.mark.parametrize(
    "bad", ["", "/abs.md", "../up.md", "a/../../b.md", ".git/x.md", "a\\b.md", "a\0b.md"]
)
def test_validate_relative_path_rejects(bad: str) -> None:
    with pytest.raises(UnsafePathError):
        validate_relative_path(bad)


def test_validate_relative_path_accepts() -> None:
    assert validate_relative_path("team/api.md") == "team/api.md"


def test_ensure_within(tmp_path: Path) -> None:
    assert ensure_within(tmp_path, Path("a/b.md")) == (tmp_path / "a/b.md").resolve()
    assert ensure_within(tmp_path, Path()) == tmp_path.resolve()
    with pytest.raises(UnsafePathError):
        ensure_within(tmp_path, Path("../escape.md"))
    with pytest.raises(UnsafePathError):
        ensure_within(tmp_path, tmp_path.parent / "other")


def test_snapshot_and_digests(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_bytes(b"# A\n")
    (tmp_path / "b.md").write_bytes(b"# B\n")
    snap = snapshot_directory(tmp_path)
    assert snap == {"a.md": b"# A\n", "b.md": b"# B\n"}
    assert snapshot_digest(snap) == snapshot_digest({"b.md": b"# B\n", "a.md": b"# A\n"})
    assert snapshot_digest(snap) != snapshot_digest({"a.md": b"# A\n", "b.md": b"# C\n"})
    assert snapshot_digest(snap) != snapshot_digest({"a.md": b"# A\n# B\n"})
    assert snapshot_hashes(snap)["a.md"] == file_digest(b"# A\n")


def test_file_references() -> None:
    text = "see #[[file:src/app.py]] and #[[file:src/app.py:10-20]] and #[[folder:docs]] #[[file:x.md:7]]"
    assert file_references(text) == [
        ("file", "src/app.py"),
        ("file", "src/app.py"),
        ("folder", "docs"),
        ("file", "x.md"),
    ]


def test_plan_changes_matrix() -> None:
    source = {
        "same.md": b"s",
        "changed.md": b"new",
        "edited.md": b"remote",
        "added.md": b"a",
        "gone-edit.md": b"r",
    }
    target = {
        "same.md": b"s",
        "changed.md": b"old",
        "edited.md": b"mine",
        "removed.md": b"old",
        "local.md": b"l",
        "gone-mod.md": b"mod",
    }
    lock = {
        "same.md": file_digest(b"s"),
        "changed.md": file_digest(b"old"),
        "edited.md": file_digest(b"old"),
        "removed.md": file_digest(b"old"),
        "gone-mod.md": file_digest(b"orig"),
    }
    plan = plan_changes(source, target, lock)
    assert plan.add == ("added.md", "gone-edit.md")
    assert plan.update == ("changed.md",)
    assert plan.delete == ("removed.md",)
    assert plan.conflict == ("edited.md", "gone-mod.md")
    assert plan.unchanged == ("same.md",)
    assert plan.local_only == ("local.md",)
    assert plan.change_count == 4
    assert not plan.is_empty
    no_prune = plan_changes(source, target, lock, prune=False)
    assert no_prune.delete == ()
    assert "removed.md" in no_prune.local_only
    assert plan_changes({}, {}, {}).is_empty


def test_apply_plan_writes_and_removes(tmp_path: Path) -> None:
    target = tmp_path / "steering"
    target.mkdir()
    (target / "old.md").write_bytes(b"old")
    (target / "nested").mkdir()
    (target / "nested" / "gone.md").write_bytes(b"gone")
    (target / "mine.md").write_bytes(b"mine")
    source = {"new/doc.md": b"# New\n", "old.md": b"updated", "mine.md": b"theirs"}
    lock = {
        "old.md": file_digest(b"old"),
        "nested/gone.md": file_digest(b"gone"),
        "mine.md": file_digest(b"orig"),
    }
    plan = plan_changes(source, target_snapshot := snapshot_directory(target), lock)
    assert target_snapshot
    touched = apply_plan(target, source, plan)
    assert sorted(touched) == ["nested/gone.md", "new/doc.md", "old.md"]
    assert (target / "new" / "doc.md").read_bytes() == b"# New\n"
    assert (target / "old.md").read_bytes() == b"updated"
    assert not (target / "nested").exists()
    assert (target / "mine.md").read_bytes() == b"mine"
    assert not list(target.glob(".*.tmp"))
    forced = apply_plan(target, source, plan, force=True)
    assert "mine.md" in forced
    assert (target / "mine.md").read_bytes() == b"theirs"


def test_apply_plan_force_removes_conflicting_deletions(tmp_path: Path) -> None:
    target = tmp_path / "steering"
    target.mkdir()
    (target / "gone-mod.md").write_bytes(b"mod")
    plan = plan_changes({}, snapshot_directory(target), {"gone-mod.md": file_digest(b"orig")})
    assert plan.conflict == ("gone-mod.md",)
    apply_plan(target, {}, plan, force=True)
    assert not (target / "gone-mod.md").exists()


def test_replace_directory_contents(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_bytes(b"k")
    (tmp_path / "drop.md").write_bytes(b"d")
    replace_directory_contents(tmp_path, {"keep.md": b"k2", "add.md": b"a"})
    assert snapshot_directory(tmp_path) == {"keep.md": b"k2", "add.md": b"a"}


def test_lock_round_trip(tmp_path: Path) -> None:
    lock = tmp_path / "lock.json"
    assert read_lock(lock) == {"version": 2, "collections": {}}
    write_lock(
        lock,
        remote="r",
        commit="c",
        collections={"steering": {"b.md": "2", "a.md": "1"}, "skills": {"s/SKILL.md": "3"}},
    )
    data = read_lock(lock)
    assert data["remote"] == "r"
    assert data["commit"] == "c"
    assert data["version"] == 2
    assert list(data["collections"]["steering"]["files"]) == ["a.md", "b.md"]
    assert lock_hashes(data) == {"a.md": "1", "b.md": "2"}
    assert lock_hashes(data, "skills") == {"s/SKILL.md": "3"}
    assert lock_hashes(data, "missing") == {}
    assert lock_collections(data) == {
        "skills": {"s/SKILL.md": "3"},
        "steering": {"a.md": "1", "b.md": "2"},
    }
    lock.write_text("{not json")
    assert read_lock(lock)["collections"] == {}
    lock.write_text(json.dumps({"files": "nope"}))
    assert read_lock(lock)["collections"] == {}
    lock.write_text(json.dumps([1, 2]))
    assert read_lock(lock)["collections"] == {}
    assert lock_hashes({"collections": "nope"}) == {}
    assert lock_collections({"collections": {"x": {"files": "nope"}, "y": 3}}) == {}


def test_lock_version_one_is_read_as_steering(tmp_path: Path) -> None:
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"version": 1, "remote": "r", "commit": "c", "files": {"a.md": "1"}})
    )
    data = read_lock(lock)
    assert data["version"] == 2
    assert data["remote"] == "r"
    assert lock_hashes(data) == {"a.md": "1"}
    assert lock_hashes(data, "specs") == {}


def test_read_document(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_bytes(b"---\ninclusion: manual\n---\n# D\n")
    doc = read_document(tmp_path, "doc.md")
    assert doc.front_matter is not None
    assert doc.front_matter.metadata["inclusion"] == "manual"
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe")
    assert read_document(tmp_path, "bin.md").text is None
