"""fleet push, pull and restore, typed overwrite confirmation, and the interactive overview."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from steersync import fleet, fleet_plan, gitops, sync
from steersync.cli import main
from steersync.cli_fleet_actions import (
    DETAILS,
    PULL,
    PUSH,
    QUIT,
    Prompts,
    overview,
)
from steersync.errors import GitError, GitLabError
from steersync.fleet import RepoStatus
from steersync.plan import Plan
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
    # No terminal and no --yes: the plan, then exit 2 naming the flag; nothing pushed.
    asked = CliRunner().invoke(main, ["fleet", "push", "--all"])
    assert asked.exit_code == 2
    assert "Re-run with --yes to apply 2 steps" in asked.output
    assert status(fleet_env)["ahead"]["state"] == "local-ahead"
    planned = CliRunner().invoke(main, ["fleet", "push", "--state", "local-ahead", "--plan"])
    assert planned.exit_code == 0
    assert "push:" in planned.output
    assert "review" in planned.output
    assert status(fleet_env)["ahead"]["state"] == "local-ahead"


def test_pull_keeps_local_edits_by_default(fleet_env: dict[str, Path]) -> None:
    result = CliRunner().invoke(main, ["fleet", "pull", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert "locally edited file(s) kept" in result.output
    rules = fleet_env["both"] / ".kiro" / "steering" / "rules.md"
    assert rules.read_text() == LOCAL
    rows = status(fleet_env)
    assert rows["behind"]["state"] == "in-sync"
    assert rows["both"]["state"] == "diverged"


def test_overwrite_is_a_plan_step_with_a_backup_and_restore(fleet_env: dict[str, Path]) -> None:
    rules = fleet_env["both"] / ".kiro" / "steering" / "rules.md"
    planned = CliRunner().invoke(
        main, ["fleet", "pull", "--repo", str(fleet_env["both"]), "--plan", "--json"]
    )
    steps = {s["id"].split(":")[0]: s for s in json.loads(planned.stdout)["steps"]}
    assert steps["overwrite"]["risk"] == "review"
    assert steps["overwrite"]["default"] is False
    kept = CliRunner().invoke(main, ["fleet", "pull", "--repo", str(fleet_env["both"]), "--yes"])
    assert kept.exit_code == 0, kept.output
    assert rules.read_text() == LOCAL
    result = CliRunner().invoke(
        main, ["fleet", "pull", "--repo", str(fleet_env["both"]), "--overwrite-local", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert rules.read_text() == STEERING.format(n=3)
    assert "backup:" in result.output
    listed = CliRunner().invoke(main, ["restore", "--list"]).output
    stamp = listed.split()[0]
    restored = CliRunner().invoke(main, ["restore", stamp, "--yes"])
    assert restored.exit_code == 0, restored.output
    assert rules.read_text() == LOCAL


def test_offline_remote_refuses_push_and_pull(
    fleet_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise GitError("git fetch failed (128): ssh: Could not resolve hostname central")

    monkeypatch.setattr(gitops, "ensure_cache_clone", unreachable)
    result = CliRunner().invoke(main, ["fleet", "push", "--state", "local-ahead", "--yes"])
    assert "offline" in result.output
    assert "skip " in result.output
    assert "Nothing to do" in result.output
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
    applied: list[list[str]] = []

    def build(action: fleet_plan.Action, rows: Sequence[RepoStatus]) -> tuple[Plan, list[Any]]:
        sink: list[Any] = []
        plan = fleet_plan.build_fleet_plan(action, rows, os.environ, sink=sink)
        recorded = Plan(
            plan.command,
            tuple(
                s.__class__(**{**s.__dict__, "apply": _recorder(s.id, applied)}) for s in plan.steps
            ),
        )
        return recorded, sink

    script = Scripted(
        [
            DETAILS,
            path["both"],
            PUSH,
            [f"push:{path['ahead']}"],
            PULL,
            [f"pull:{path['behind']}", f"overwrite:{path['both']}"],
            PULL,
            [],
            QUIT,
        ]
    )

    def show(row: RepoStatus) -> None:
        shown.append(row.path)

    outcomes = overview(script.prompts(), lambda: report, lambda _: None, show, build)
    assert outcomes == []
    assert shown == [path["both"]]
    assert [sorted(group) for group in applied] == [
        [f"push:{path['ahead']}"],
        sorted([f"pull:{path['behind']}", f"overwrite:{path['both']}"]),
    ]
    # One checkbox per action, and no other question.
    assert script.asked.count("Select the changes to apply (space toggles, enter applies):") == 3


def _recorder(step_id: str, applied: list[list[str]]) -> Any:
    from steersync.plan import StepResult

    def apply() -> StepResult:
        if not applied or applied[-1][-1].split(":")[0] == "push" or step_id.startswith("push"):
            applied.append([])
        applied[-1].append(step_id)
        return StepResult(step_id, True, "recorded")

    return apply
