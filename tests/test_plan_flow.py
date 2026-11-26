"""plan_flow: render, decide once, back up, apply and the exit codes."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import click
import pytest

from steersync import backups, plan_flow
from steersync.interaction import Interaction
from steersync.plan import Plan, Step, StepResult


def interaction(**overrides: object) -> Interaction:
    values: dict[str, object] = {
        "interactive": True,
        "assume_yes": False,
        "force": False,
        "dry_run": False,
        "output": "text",
    }
    values.update(overrides)
    return Interaction(**values)  # type: ignore[arg-type]


def make_plan(tmp_path: Path, apply: Callable[[], StepResult] | None = None) -> Plan:
    target = tmp_path / "file.txt"

    def write() -> StepResult:
        target.write_text("new")
        return StepResult("write", True, "written")

    return Plan(
        "demo",
        (
            Step(
                "write",
                "Write",
                "a file",
                str(target),
                "safe",
                True,
                False,
                apply or write,
                (target,),
            ),
            Step("reset", "Reset", "", "", "destructive", False, False, write),
        ),
    )


def env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path / "state"), "HOME": str(tmp_path)}


def never(*_: object) -> NoReturn:
    raise AssertionError("must not be called")


def test_apply_backs_up_and_prints_the_undo_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "file.txt").write_text("old")
    result = plan_flow.execute(
        make_plan(tmp_path),
        interaction(),
        env(tmp_path),
        notes=("note: something left alone",),
        ask=lambda *_: ["write"],
        typed=never,
    )
    out, err = capsys.readouterr()
    assert result.exit_code == 0
    assert result.applied == ("write",)
    assert result.backup_id is not None
    assert f"Undo with: steersync restore {result.backup_id}" in out
    assert "note: something left alone" in err
    (backup,) = backups.listing(tmp_path / "state" / "steersync" / "backups")
    backups.restore(backup)
    assert (tmp_path / "file.txt").read_text() == "old"


def test_json_plan_and_json_results(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_flow.execute(
        make_plan(tmp_path),
        interaction(dry_run=True, output="json"),
        env(tmp_path),
        ask=never,
        typed=never,
    )
    assert json.loads(capsys.readouterr().out)["command"] == "demo"
    result = plan_flow.execute(
        make_plan(tmp_path),
        interaction(assume_yes=True, output="json"),
        env(tmp_path),
        ask=never,
        typed=never,
    )
    document = json.loads(capsys.readouterr().out)
    assert document["applied"] == ["write"]
    assert document["backup"] == result.backup_id
    assert document["results"][0]["ok"] is True


def test_cancel_confirm_and_needs_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        plan_flow.execute(
            make_plan(tmp_path), interaction(), env(tmp_path), ask=lambda *_: None, typed=never
        ).exit_code
        == plan_flow.EXIT_CANCELLED
    )
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    declined = plan_flow.execute(
        make_plan(tmp_path),
        interaction(),
        env(tmp_path),
        confirm_after_select=True,
        ask=lambda *_: ["write"],
        typed=never,
    )
    assert declined.exit_code == plan_flow.EXIT_CANCELLED
    assert not (tmp_path / "file.txt").exists()
    assert (
        plan_flow.execute(
            make_plan(tmp_path),
            interaction(interactive=False),
            env(tmp_path),
            ask=never,
            typed=never,
        ).exit_code
        == plan_flow.EXIT_NEEDS_INPUT
    )


def test_failures_interrupts_and_backup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails() -> StepResult:
        raise OSError("disk full")

    def stopped() -> StepResult:
        raise KeyboardInterrupt

    yes = interaction(assume_yes=True)
    assert (
        plan_flow.execute(
            make_plan(tmp_path, fails), yes, env(tmp_path), ask=never, typed=never
        ).exit_code
        == plan_flow.EXIT_FAILED
    )
    interrupted = plan_flow.execute(
        make_plan(tmp_path, stopped), yes, env(tmp_path), ask=never, typed=never
    )
    assert interrupted.exit_code == plan_flow.EXIT_INTERRUPTED
    assert interrupted.results[0].interrupted

    def no_space(*_: object, **__: object) -> NoReturn:
        raise backups.BackupError("backup failed")

    monkeypatch.setattr(backups, "take_files", no_space)
    assert (
        plan_flow.execute(make_plan(tmp_path), yes, env(tmp_path), ask=never, typed=never).exit_code
        == plan_flow.EXIT_FAILED
    )
    assert not (tmp_path / "file.txt").exists()


def test_default_checkbox_and_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import questionary

    class Fake:
        def ask(self) -> list[str]:
            return ["write"]

    monkeypatch.setattr(questionary, "checkbox", lambda message, choices: Fake())
    result = plan_flow.execute(make_plan(tmp_path), interaction(), env(tmp_path))
    assert result.applied == ("write",)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "demo")
    assert plan_flow._typed("demo") is True
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "nope")
    assert plan_flow._typed("demo") is False


def test_nothing_to_do_and_finish(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    done = Plan("demo", ())
    assert plan_flow.execute(done, interaction(), env(tmp_path), ask=never, typed=never) == (
        plan_flow.FlowResult(0)
    )
    assert "Nothing to do" in capsys.readouterr().out
    plan_flow.finish(plan_flow.FlowResult(0))
    with pytest.raises(SystemExit) as raised:
        plan_flow.finish(plan_flow.FlowResult(5))
    assert raised.value.code == 5
