"""steersync fleet: status across many repositories, classification, offline, JSON."""

import json
import shutil
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

from steersync import fleet, gitops, registry, sync
from steersync.cli import main
from steersync.errors import GitError
from steersync.fleet import Counts, classify
from steersync.paths import registry_path
from tests.conftest import CentralCommit, git

STEERING = "---\ninclusion: always\n---\n# Rules\n\nVersion {n}.\n"


def make_project(base: Path, slug: str, env: dict[str, str]) -> Path:
    repo = base / slug.replace("/", "-")
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(repo), cwd=base, env=env)
    git("remote", "add", "origin", f"ssh://git@gitlab.example.com/{slug}.git", cwd=repo, env=env)
    (repo / "README.md").write_text("# x\n")
    git("add", "-A", cwd=repo, env=env)
    git("commit", "-qm", "init", cwd=repo, env=env)
    return repo


def seed(slugs: list[str]):  # type: ignore[no-untyped-def]
    def mutate(work: Path) -> None:
        for slug in slugs:
            steering = work / "projects" / slug / "steering"
            steering.mkdir(parents=True, exist_ok=True)
            (steering / "rules.md").write_text(STEERING.format(n=1))

    return mutate


def edit_central(slug: str, n: int):  # type: ignore[no-untyped-def]
    def mutate(work: Path) -> None:
        (work / "projects" / slug / "steering" / "rules.md").write_text(STEERING.format(n=n))

    return mutate


@pytest.fixture
def fleet_env(
    tmp_path: Path,
    env: dict[str, str],
    config_file: Path,
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    monkeypatch.setenv("STEERSYNC_NO_V3_REMINDER", "1")
    slugs = ["acme/ahead", "acme/behind", "acme/both", "acme/proposed", "acme/same"]
    central_commit(seed(slugs), "seed fleet")
    base = tmp_path / "code"
    repos = {slug.split("/")[1]: make_project(base, slug, env) for slug in slugs}
    runner = CliRunner()
    for repo in repos.values():
        monkeypatch.chdir(repo)
        assert runner.invoke(main, ["pull", "--no-global"]).exit_code == 0
    steering = Path(".kiro") / "steering"
    (repos["ahead"] / steering / "new.md").write_text("# New\n\nLocal.\n")
    central_commit(edit_central("acme/behind", 2), "behind")
    (repos["both"] / steering / "rules.md").write_text(STEERING.format(n=9))
    central_commit(edit_central("acme/both", 3), "both")
    (repos["proposed"] / steering / "extra.md").write_text("# Extra\n\nProposed.\n")
    monkeypatch.chdir(repos["proposed"])
    assert runner.invoke(main, ["push"]).exit_code == 0
    gone = make_project(base, "acme/gone", env)
    plain = base / "plain"
    plain.mkdir()
    git("init", "-q", str(plain), cwd=base, env=env)
    registry.upsert(registry_path(dict(__import__("os").environ)), [gone, plain], "manual")
    shutil.rmtree(gone)
    monkeypatch.chdir(tmp_path)
    return repos


def by_name(document: dict, repos: dict[str, Path]) -> dict[str, dict]:  # type: ignore[type-arg]
    names = {str(p.resolve()): n for n, p in repos.items()}
    return {names.get(r["path"], r["path"]): r for r in document["repositories"]}


def test_status_classifies_every_state(fleet_env: dict[str, Path], tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["fleet", "status", "--json"])
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


def test_status_table_and_filter(fleet_env: dict[str, Path]) -> None:
    result = CliRunner().invoke(main, ["fleet", "status", "--no-refresh"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    for word in ("local-ahead", "central-ahead", "diverged", "proposed", "in-sync", "missing"):
        assert word in result.output
    assert "1 both" in result.output
    assert "need attention" in result.output
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
    rules.write_bytes(rules.read_bytes().replace(b"\n", b"\r\n"))
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
