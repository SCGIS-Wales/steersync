"""steersync fleet: status across many repositories, classification, offline, JSON."""

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

from steersync import fleet, gitops, sync
from steersync.cli import main
from steersync.errors import GitError
from steersync.fleet import Counts, classify
from tests.conftest import STEERING, CentralCommit, by_name, make_project, seed


def test_status_classifies_every_state(fleet_env: dict[str, Path], tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["fleet", "status", "--global", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    rows = by_name(document, fleet_env)
    assert rows["ahead"]["state"] == "local-ahead"
    assert rows["ahead"]["push"] == {"add": 1, "update": 0, "delete": 0}
    assert rows["behind"]["state"] == "central-ahead"
    assert rows["behind"]["pull"] == {"add": 0, "update": 1, "delete": 0}
    assert rows["both"]["state"] == "diverged"
    assert rows["both"]["conflicts"] == ["steering/rules.md"]
    assert rows["proposed"]["state"] == "proposed"
    assert rows["proposed"]["proposal"].startswith("kiro-config/acme/proposed/")
    assert rows["same"]["state"] == "in-sync"
    states = {r["path"]: r["state"] for r in document["repositories"]}
    assert states[str((tmp_path / "code" / "acme-gone").resolve())] == "missing"
    assert states[str((tmp_path / "code" / "plain").resolve())] == "unconfigured"
    assert states["~/.kiro"] == "central-ahead"  # global/steering/rules.md not pulled here
    assert document["remotes"][0]["offline"] is False
    assert document["remotes"][0]["commit"]
    order = [r["state"] for r in document["repositories"] if r["path"] != "~/.kiro"]
    assert order.index("diverged") < order.index("local-ahead") < order.index("in-sync")
    assert document["repositories"][-1]["path"] == "~/.kiro"


def test_global_row_is_opt_in(fleet_env: dict[str, Path], config_file: Path) -> None:
    def paths(*args: str) -> list[str]:
        result = CliRunner().invoke(main, ["fleet", *args, "--json"])
        assert result.exit_code == 0, result.output
        return [r["path"] for r in json.loads(result.stdout)["repositories"]]

    assert "~/.kiro" not in paths("status")
    assert "~/.kiro" in paths("status", "--global")
    assert "~/.kiro" in paths("--global", "status")  # on the group as well
    config_file.write_text(config_file.read_text() + "\nfleet:\n  include_global: true\n")
    assert "~/.kiro" in paths("status")
    assert "~/.kiro" not in paths("status", "--no-global")


def test_push_by_name_includes_the_global_row(fleet_env: dict[str, Path]) -> None:
    result = CliRunner().invoke(main, ["fleet", "pull", "--repo", "~/.kiro", "--plan", "--json"])
    assert result.exit_code == 0, result.output
    assert [s["id"] for s in json.loads(result.stdout)["steps"]] == ["pull:~/.kiro"]
    nothing = CliRunner().invoke(main, ["fleet", "pull", "--all", "--plan", "--json"])
    assert all("~/.kiro" not in s["id"] for s in json.loads(nothing.stdout)["steps"])


def test_status_table_and_filter(fleet_env: dict[str, Path]) -> None:
    result = CliRunner().invoke(main, ["fleet", "status", "--no-refresh"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    for word in ("local-ahead", "central-ahead", "diverged", "proposed", "in-sync", "missing"):
        assert word in result.output
    assert "1 both" in result.output
    assert "4 need attention" in result.output  # diverged, ahead, behind, missing
    only = json.loads(
        CliRunner().invoke(main, ["fleet", "status", "--json", "--state", "diverged"]).stdout
    )
    assert [r["state"] for r in only["repositories"]] == ["diverged"]
    # Without a terminal, bare `steersync fleet` prints the table and never prompts.
    bare = CliRunner().invoke(main, ["fleet"])
    assert bare.exit_code == 0
    assert "local-ahead" in bare.output


def test_offline_remote_is_visible(
    fleet_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("git fetch failed (128): ssh: Could not resolve hostname central")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    document = json.loads(
        CliRunner().invoke(main, ["fleet", "status", "--json", "--refresh"]).stdout
    )
    assert document["remotes"][0]["offline"] is True
    assert document["remotes"][0]["cache_age_seconds"] is not None
    rows = by_name(document, fleet_env)
    assert rows["ahead"]["offline"] is True
    table = CliRunner().invoke(main, ["fleet", "status", "--refresh"]).output
    assert "offline" in table


def test_error_in_one_repository_does_not_stop_others(
    fleet_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sync.compare_workspace

    def broken(config, snapshot, root, env, *, project):  # type: ignore[no-untyped-def]
        if project == "acme/same":
            raise RuntimeError("disk on fire")
        return original(config, snapshot, root, env, project=project)

    monkeypatch.setattr(sync, "compare_workspace", broken)
    result = CliRunner().invoke(main, ["fleet", "status", "--json"])
    assert result.exit_code == 5
    rows = by_name(json.loads(result.stdout), fleet_env)
    assert rows["same"]["state"] == "error"
    assert "disk on fire" in rows["same"]["error"]
    assert rows["ahead"]["state"] == "local-ahead"


@pytest.mark.parametrize("width", [72, 80, 100, 120, 160])
def test_table_never_crops_the_note(fleet_env: dict[str, Path], width: int) -> None:
    import io
    import os

    from rich.console import Console

    from steersync import fleet_ui, registry
    from steersync.paths import registry_path

    report = fleet.collect(registry.load(registry_path(os.environ)).paths(), os.environ)
    # Plain text even when FORCE_COLOR is set (as in CI): escape codes are not columns.
    console = Console(file=io.StringIO(), width=width, force_terminal=False, color_system=None)
    console.print(fleet_ui.table(report, width=width))
    lines = console.file.getvalue().splitlines()  # type: ignore[attr-defined]
    assert all(len(line) <= width for line in lines)
    if width < 120:
        return  # narrow: rich folds every column, including the headings
    start = lines[0].index("Note")
    notes = "".join(line[start:].strip() for line in lines[2:])
    proposed = next(r for r in report.repositories if r.state == "proposed")
    assert proposed.proposal
    assert proposed.proposal in notes


@pytest.mark.parametrize(
    ("push", "pull", "conflicts", "proposed", "state"),
    [
        (Counts(), Counts(), 0, False, "in-sync"),
        (Counts(1), Counts(), 0, False, "local-ahead"),
        (Counts(1), Counts(), 0, True, "proposed"),
        (Counts(), Counts(0, 1), 0, False, "central-ahead"),
        (Counts(), Counts(), 1, False, "diverged"),
        (Counts(0, 0, 1), Counts(1), 0, False, "diverged"),
    ],
)
def test_classify_table(
    push: Counts, pull: Counts, conflicts: int, proposed: bool, state: str
) -> None:
    assert classify(push, pull, conflicts, proposed=proposed) == state


counts = st.builds(Counts, st.integers(0, 3), st.integers(0, 3), st.integers(0, 3))


@given(counts, counts, st.integers(0, 3), st.booleans())
def test_classify_is_total_and_in_sync_means_nothing_pending(
    push: Counts, pull: Counts, conflicts: int, proposed: bool
) -> None:
    state = classify(push, pull, conflicts, proposed=proposed)
    assert state in fleet.STATE_ORDER
    if state == "in-sync":
        assert not push.any()
        assert not pull.any()
        assert conflicts == 0


@pytest.mark.benchmark
def test_status_of_thirty_repositories_is_fast(
    tmp_path: Path,
    env: dict[str, str],
    config_file: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    slugs = [f"bench/repo{i}" for i in range(30)]
    central_commit(seed(slugs), "bench")
    repos = [make_project(tmp_path / "code", slug, env) for slug in slugs]
    for repo in repos:
        steering = repo / ".kiro" / "steering"
        steering.mkdir(parents=True)
        (steering / "rules.md").write_text(STEERING.format(n=1))
    import os

    fleet.collect(repos, dict(os.environ), refresh=True)  # warm the cache
    started = time.monotonic()
    report = fleet.collect(repos, dict(os.environ), refresh=False)
    took = time.monotonic() - started
    assert len([r for r in report.repositories if not r.is_global()]) == 30
    assert {r.state for r in report.repositories if not r.is_global()} == {"in-sync"}
    assert took < 15, f"collect took {took:.1f}s for 30 repositories"


def test_crlf_checkout_is_in_sync(fleet_env: dict[str, Path]) -> None:
    """Task E: line ending conversion alone never makes a repository local ahead."""
    rules = fleet_env["same"] / ".kiro" / "steering" / "rules.md"
    # From LF first: a Windows runner with core.autocrlf may have checked it out as CRLF.
    lf = rules.read_bytes().replace(b"\r\n", b"\n")
    rules.write_bytes(lf.replace(b"\n", b"\r\n"))
    rows = by_name(
        json.loads(CliRunner().invoke(main, ["fleet", "status", "--json"]).stdout), fleet_env
    )
    assert rows["same"]["state"] == "in-sync"


def test_comparable_normalises_text_only() -> None:
    from steersync.steering import comparable, file_digest

    assert comparable(b"a\r\nb\r\n") == b"a\nb\n"
    assert file_digest(b"a\r\nb") == file_digest(b"a\nb")
    binary = b"\x00\r\n\x01"
    assert comparable(binary) == binary
    assert file_digest(b"a\rb") != file_digest(b"a\nb")  # lone CR is content
