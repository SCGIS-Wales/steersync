"""fleet push, pull and restore, typed overwrite confirmation, and the interactive overview."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from steersync import fleet, gitops, sync
from steersync.cli import main
from steersync.cli_fleet_actions import (
    DETAILS,
    KEEP,
    OVERWRITE,
    PULL,
    PUSH,
    QUIT,
    Prompts,
    expected_word,
    overview,
)
from steersync.errors import GitError, GitLabError
from steersync.fleet import Outcome, RepoStatus
from tests.conftest import STEERING, by_name, git

LOCAL = STEERING.format(n=9)


def status(repos: dict[str, Path]) -> dict[str, dict]:  # type: ignore[type-arg]
    result = CliRunner().invoke(main, ["fleet", "status", "--json"])
    return by_name(json.loads(result.stdout), repos)


def branches(central: Path, env: dict[str, str]) -> list[str]:
    out = git(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/kiro-config/", cwd=central, env=env
    )
    return out.splitlines()


def test_push_two_repositories(
    fleet_env: dict[str, Path],
    central: Path,
    env: dict[str, str],
) -> None:
    before = branches(central, env)
    result = CliRunner().invoke(
        main,
        [
            "fleet",
            "push",
            "--repo",
            str(fleet_env["ahead"]),
            "--repo",
            str(fleet_env["both"]),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("done") == 2
    new = sorted(set(branches(central, env)) - set(before))
    assert [b.split("/")[2] for b in new] == ["ahead", "both"]
    assert status(fleet_env)["ahead"]["state"] == "proposed"


def test_push_needs_a_selection_and_confirmation(fleet_env: dict[str, Path]) -> None:
    assert CliRunner().invoke(main, ["fleet", "push"]).exit_code == 2
    unknown = CliRunner().invoke(main, ["fleet", "push", "--repo", "/nowhere"])
    assert "not registered" in unknown.output
    cancelled = CliRunner().invoke(main, ["fleet", "push", "--all"], input="n\n")
    assert "nothing was pushed" in cancelled.output
    assert status(fleet_env)["ahead"]["state"] == "local-ahead"
    dry = CliRunner().invoke(main, ["fleet", "push", "--state", "local-ahead", "--dry-run"])
    assert dry.exit_code == 0
    assert "dry-run" in dry.output


def test_pull_keeps_local_edits_by_default(fleet_env: dict[str, Path]) -> None:
    result = CliRunner().invoke(main, ["fleet", "pull", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert "locally edited file(s) kept" in result.output
    rules = fleet_env["both"] / ".kiro" / "steering" / "rules.md"
    assert rules.read_text() == LOCAL
    rows = status(fleet_env)
    assert rows["behind"]["state"] == "in-sync"
    assert rows["both"]["state"] == "diverged"


def test_typed_overwrite_backs_up_then_restore(fleet_env: dict[str, Path]) -> None:
    rules = fleet_env["both"] / ".kiro" / "steering" / "rules.md"
    refused = CliRunner().invoke(
        main,
        ["fleet", "pull", "--repo", str(fleet_env["both"]), "--overwrite-local"],
        input="yes please\ny\n",
    )
    assert "overwrite cancelled" in refused.output
    assert rules.read_text() == LOCAL
    result = CliRunner().invoke(
        main,
        ["fleet", "pull", "--repo", str(fleet_env["both"]), "--overwrite-local"],
        input="overwrite\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert rules.read_text() == STEERING.format(n=3)
    assert "backup:" in result.output
    listed = CliRunner().invoke(main, ["fleet", "restore", "--list"]).output
    stamp = listed.split()[0]
    restored = CliRunner().invoke(main, ["fleet", "restore", stamp, "--yes"])
    assert restored.exit_code == 0, restored.output
    assert rules.read_text() == LOCAL


def test_overwrite_many_needs_the_count() -> None:
    assert expected_word(1) == "overwrite"
    assert expected_word(3) == "overwrite 3"


def test_offline_remote_refuses_push_and_pull(
    fleet_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("git fetch failed (128): ssh: Could not resolve hostname central")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    result = CliRunner().invoke(main, ["fleet", "push", "--state", "local-ahead", "--yes"])
    assert "offline" in result.output
    assert "skipped" in result.output
    pulled = CliRunner().invoke(main, ["fleet", "pull", "--state", "central-ahead", "--yes"])
    assert "pull is disabled" in pulled.output


def test_one_failure_does_not_stop_the_batch(
    fleet_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_push = sync.push

    def flaky(config, *, workspace_root, **kwargs):  # type: ignore[no-untyped-def]
        if workspace_root.name.endswith("ahead"):
            msg = "GitLab API GET merge_requests failed with HTTP 503 after 4 attempts over 21s"
            raise GitLabError(msg)
        return real_push(config, workspace_root=workspace_root, **kwargs)

    monkeypatch.setattr(sync, "push", flaky)
    result = CliRunner().invoke(
        main,
        [
            "fleet",
            "push",
            "--repo",
            str(fleet_env["ahead"]),
            "--repo",
            str(fleet_env["both"]),
            "--yes",
        ],
    )
    assert result.exit_code == 5
    assert "HTTP 503 after 4 attempts" in result.output
    assert f"rerun: cd {fleet_env['ahead'].resolve()} && steersync push" in result.output
    assert "done" in result.output


class Scripted:
    """Answers questions in order and records what was asked."""

    def __init__(self, answers: list[object]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def take(self, message: str) -> Any:
        self.asked.append(message)
        return self.answers.pop(0)

    def prompts(self) -> Prompts:
        def select(message: str, choices: list[str]) -> Any:
            del choices
            return self.take(message)

        def checkbox(message: str, choices: object) -> Any:
            del choices
            return self.take(message)

        def confirm(message: str, default: bool) -> Any:
            del default
            return self.take(message)

        return Prompts(select=select, checkbox=checkbox, text=self.take, confirm=confirm)


def test_interactive_overview_flows(fleet_env: dict[str, Path]) -> None:
    import os

    from steersync import registry
    from steersync.paths import registry_path

    entries = registry.load(registry_path(os.environ)).paths()
    report = fleet.collect(entries, os.environ)
    path = {name: str(p.resolve()) for name, p in fleet_env.items()}
    shown: list[str] = []
    pushed: list[list[str]] = []
    pulled: list[tuple[list[str], set[str]]] = []
    script = Scripted(
        [
            DETAILS,
            path["both"],
            PUSH,
            [path["ahead"]],
            True,
            PULL,
            [path["behind"], path["both"]],
            OVERWRITE,
            "overwrite",
            True,
            PULL,
            [path["both"]],
            KEEP,
            False,
            QUIT,
        ]
    )

    def fake_push(rows: Sequence[RepoStatus]) -> tuple[list[Outcome], bool]:
        pushed.append([r.path for r in rows])
        return [], False

    def fake_pull(rows: Sequence[RepoStatus], overwrite: set[str]) -> tuple[list[Outcome], bool]:
        pulled.append(([r.path for r in rows], overwrite))
        return [], False

    outcomes = overview(
        script.prompts(),
        lambda: report,
        lambda _: None,
        lambda row: shown.append(row.path),
        fake_push,
        fake_pull,
    )
    assert outcomes == []
    assert shown == [path["both"]]
    assert pushed == [[path["ahead"]]]
    assert [(sorted(rows), overwrite) for rows, overwrite in pulled] == [
        (sorted([path["behind"], path["both"]]), {path["both"]})
    ]
    assert any("local edits that also changed centrally" in q for q in script.asked)


def test_fleet_warnings_use_the_steersync_log_format(
    fleet_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("git fetch failed (128): ssh: connect to host central: No route to host")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    result = CliRunner().invoke(main, ["fleet", "status", "--refresh"])
    assert "WARNING:steersync" not in result.output
