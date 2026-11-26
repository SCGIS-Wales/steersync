"""Plans, risk, the single approval and when steersync may prompt."""

import io
import json
from collections.abc import Callable
from typing import NoReturn

import click
import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st
from rich.console import Console

from steersync import plan_render, plan_select
from steersync.cli_options import plan_options
from steersync.config import Config
from steersync.interaction import Interaction, resolve_interaction
from steersync.plan import (
    Plan,
    Step,
    StepResult,
    choose,
    default_selection,
    non_interactive_message,
    pending,
    plan_hash,
    plan_to_json,
    restrict,
    run_steps,
)


def ok(step_id: str) -> Callable[[], StepResult]:
    return lambda: StepResult(step_id, True, "done")


def step(step_id: str, risk: str = "safe", *, default: bool = True, done: bool = False) -> Step:
    return Step(
        id=step_id,
        title=f"Do {step_id}",
        detail=f"{step_id} detail",
        target=f"~/{step_id}",
        risk=risk,  # type: ignore[arg-type]
        default=default,
        done=done,
        apply=ok(step_id),
    )


PLAN = Plan(
    "setup",
    (
        step("config.save"),
        step("folder:notes", default=False),
        step("mcp.register", "review"),
        step("hook.session", done=True),
        step("repo.reset", "destructive"),
    ),
)


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


# --- interaction -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "tty", "no_input", "output", "expected"),
    [
        ({}, True, False, "text", True),
        ({}, False, False, "text", False),
        ({"CI": "true"}, True, False, "text", False),
        ({"CI": "0"}, True, False, "text", True),
        ({"STEERSYNC_NO_INPUT": "1"}, True, False, "text", False),
        ({}, True, True, "text", False),
        ({}, True, False, "json", False),
    ],
)
def test_resolve_interaction(
    env: dict[str, str], tty: bool, no_input: bool, output: str, expected: bool
) -> None:
    result = resolve_interaction(
        yes=True,
        no_input=no_input,
        force=False,
        dry_run=False,
        output=output,  # type: ignore[arg-type]
        env=env,
        stdin_tty=tty,
    )
    assert result.interactive is expected
    assert result.assume_yes is True


# --- plan ------------------------------------------------------------------------


def test_pending_and_default_selection() -> None:
    assert [s.id for s in pending(PLAN)] == [
        "config.save",
        "folder:notes",
        "mcp.register",
        "repo.reset",
    ]
    assert default_selection(PLAN, interaction()) == ["config.save", "mcp.register"]
    # --force adds destructive steps that are on by default; never ones that are off.
    assert default_selection(PLAN, interaction(force=True)) == [
        "config.save",
        "mcp.register",
        "repo.reset",
    ]


def test_restrict_with_only_and_skip() -> None:
    assert [s.id for s in restrict(PLAN, only=("mcp.register",), skip=()).steps] == ["mcp.register"]
    assert "folder:notes" not in [
        s.id for s in restrict(PLAN, only=(), skip=("folder:notes",)).steps
    ]
    # Prefixes select every step of a kind.
    assert [s.id for s in restrict(PLAN, only=("folder",), skip=()).steps] == ["folder:notes"]
    with pytest.raises(click.UsageError, match="unknown step"):
        restrict(PLAN, only=("nope",), skip=())


def test_plan_json_and_hash() -> None:
    document = plan_to_json(PLAN)
    assert document["command"] == "setup"
    assert document["steps"][2] == {
        "id": "mcp.register",
        "title": "Do mcp.register",
        "detail": "mcp.register detail",
        "target": "~/mcp.register",
        "risk": "review",
        "default": True,
        "done": False,
    }
    json.dumps(document)
    first = plan_hash(PLAN, {"a.yaml": "1"})
    assert first == plan_hash(PLAN, {"a.yaml": "1"})
    assert first != plan_hash(PLAN, {"a.yaml": "2"})
    changed = Plan("setup", (*PLAN.steps[:-1], step("repo.reset", "review")))
    assert first != plan_hash(changed, {"a.yaml": "1"})


def test_non_interactive_message() -> None:
    assert non_interactive_message(PLAN) == (
        "Re-run with --yes to apply 4 steps, or --plan to inspect."
    )


def test_run_steps_keeps_going_and_reports_failures() -> None:
    def boom() -> StepResult:
        raise OSError("disk full")

    plan = Plan("x", (step("a"), Step("b", "B", "", "", "safe", True, False, boom), step("c")))
    assert [r.step_id for r in run_steps(plan, ["c"])] == ["c"]
    results = run_steps(plan, ["a", "b", "c"])
    assert [(r.step_id, r.ok) for r in results] == [("a", True), ("b", False), ("c", True)]
    assert "disk full" in results[1].detail


# --- choose: the one approval ----------------------------------------------------


def never(*_: object) -> NoReturn:
    raise AssertionError("must not ask")


def test_nothing_to_do_never_asks() -> None:
    done = Plan("setup", (step("a", done=True),))
    assert choose(done, interaction(), ask=never, typed=never).status == "nothing"


def test_plan_only_and_yes() -> None:
    assert choose(PLAN, interaction(dry_run=True), ask=never, typed=never).status == "plan-only"
    yes = choose(PLAN, interaction(assume_yes=True), ask=never, typed=never)
    assert (yes.status, yes.step_ids) == ("apply", ("config.save", "mcp.register"))


def test_without_a_terminal_it_needs_input() -> None:
    result = choose(PLAN, interaction(interactive=False), ask=never, typed=never)
    assert result.status == "needs-input"


def test_one_checkbox_is_the_approval() -> None:
    asked: list[frozenset[str] | None] = []

    def ask(plan: Plan, remembered: frozenset[str] | None) -> list[str] | None:
        asked.append(remembered)
        return ["config.save", "folder:notes"]

    result = choose(PLAN, interaction(), ask=ask, typed=never, remembered=None)
    assert (result.status, result.step_ids) == ("apply", ("config.save", "folder:notes"))
    assert asked == [None]
    cancelled = choose(PLAN, interaction(), ask=lambda *_: None, typed=never)
    assert cancelled.status == "cancelled"
    empty = choose(PLAN, interaction(), ask=lambda *_: [], typed=never)
    assert empty.status == "cancelled"


def test_destructive_needs_force_or_the_typed_word() -> None:
    picked = lambda *_: ["config.save", "repo.reset"]  # noqa: E731
    words: list[str] = []

    def refuse(word: str) -> bool:
        words.append(word)
        return False

    kept = choose(PLAN, interaction(), ask=picked, typed=refuse)
    assert kept.step_ids == ("config.save",)
    assert words == ["setup"]
    confirmed = choose(PLAN, interaction(), ask=picked, typed=lambda _: True)
    assert confirmed.step_ids == ("config.save", "repo.reset")
    forced = choose(PLAN, interaction(force=True), ask=picked, typed=never)
    assert forced.step_ids == ("config.save", "repo.reset")


@given(st.lists(st.sampled_from(["safe", "review", "destructive"]), max_size=6), st.booleans())
def test_yes_never_selects_destructive_without_force(risks: list[str], force: bool) -> None:
    plan = Plan("x", tuple(step(f"s{i}", risk) for i, risk in enumerate(risks)))
    result = choose(plan, interaction(assume_yes=True, force=force), ask=never, typed=never)
    chosen = {s.risk for s in plan.steps if s.id in result.step_ids}
    assert force or "destructive" not in chosen


# --- rendering and the checkbox -------------------------------------------------------


def test_render_plan_and_results() -> None:
    console = Console(file=io.StringIO(), width=120, color_system=None, force_terminal=False)
    plan_render.render_plan(PLAN, console)
    text = console.file.getvalue()  # type: ignore[attr-defined]
    for word in ("Do config.save", "~/mcp.register", "review", "destructive", "already done"):
        assert word in text
    console = Console(file=io.StringIO(), width=120, color_system=None, force_terminal=False)
    results = [StepResult("a", True, "saved"), StepResult("b", False, "denied")]
    plan_render.render_results(results, "20261126T000000Z", console, undo_hint=True)
    text = console.file.getvalue()  # type: ignore[attr-defined]
    assert "saved" in text
    assert "FAILED" in text
    assert "Undo with: steersync restore 20261126T000000Z" in text


def test_select_steps_builds_one_checkbox() -> None:
    seen: dict[str, object] = {}

    def checkbox(message: str, choices: list[object]) -> list[str]:
        seen["message"] = message
        seen["choices"] = choices
        return ["config.save"]

    assert plan_select.select_steps(PLAN, None, checkbox=checkbox) == ["config.save"]
    choices = {c.value: c for c in seen["choices"]}  # type: ignore[attr-defined]
    assert set(choices) == {"config.save", "folder:notes", "mcp.register", "repo.reset"}
    assert choices["config.save"].checked is True
    assert choices["folder:notes"].checked is False
    assert choices["repo.reset"].disabled == "needs --force"
    plan_select.select_steps(PLAN, frozenset({"folder:notes"}), checkbox=checkbox)
    remembered = {c.value: c for c in seen["choices"]}  # type: ignore[attr-defined]
    assert remembered["folder:notes"].checked is True
    assert remembered["config.save"].checked is False
    plan_select.select_steps(PLAN, None, checkbox=checkbox, force=True)
    forced = {c.value: c for c in seen["choices"]}  # type: ignore[attr-defined]
    assert forced["repo.reset"].disabled is None


# --- shared options and configuration -------------------------------------------------


def test_plan_options_decorator() -> None:
    seen: dict[str, object] = {}

    @click.command()
    @plan_options
    def cmd(**kwargs: object) -> None:
        seen.update(kwargs)

    result = CliRunner().invoke(cmd, ["-y", "--no-input", "-f", "--plan", "--only", "a", "--json"])
    assert result.exit_code == 0, result.output
    assert seen == {
        "yes": True,
        "no_input": True,
        "force": True,
        "dry_run": True,
        "only": ("a",),
        "skip": (),
        "profile": None,
        "as_json": True,
    }
    assert CliRunner().invoke(cmd, ["--dry-run"]).exit_code == 0


def test_ux_defaults() -> None:
    ux = Config().ux
    assert (ux.confirm_after_select, ux.remember_selection, ux.show_undo_hint) == (
        False,
        True,
        True,
    )


def test_only_destructive_refused_is_a_cancel() -> None:
    result = choose(PLAN, interaction(), ask=lambda *_: ["repo.reset"], typed=lambda _: False)
    assert result.status == "cancelled"


def test_results_without_a_backup_have_no_undo_hint() -> None:
    console = Console(file=io.StringIO(), width=80, color_system=None, force_terminal=False)
    plan_render.render_results([StepResult("a", True, "ok")], None, console, undo_hint=True)
    assert "Undo" not in console.file.getvalue()  # type: ignore[attr-defined]


def test_questionary_checkbox_is_asked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import questionary

    class Fake:
        def ask(self) -> list[str]:
            return ["config.save"]

    calls: list[str] = []

    def fake_checkbox(message: str, choices: list[object]) -> Fake:
        calls.append(message)
        return Fake()

    monkeypatch.setattr(questionary, "checkbox", fake_checkbox)
    assert plan_select.select_steps(PLAN, None) == ["config.save"]
    assert len(calls) == 1


def test_coverage_gate_script(tmp_path: object) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "coverage_gate.py"
    spec = importlib.util.spec_from_file_location("coverage_gate", path)
    assert spec is not None
    assert spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    def file(lines: int, branches: int) -> dict[str, object]:
        return {
            "summary": {
                "covered_lines": lines,
                "num_statements": 100,
                "covered_branches": branches,
                "num_branches": 100,
            }
        }

    report = {
        "files": {
            "src/steersync/plan.py": file(96, 91),
            "src/steersync/plan_render.py": file(90, 95),
            "src/steersync/interaction.py": file(100, 80),
            "src/steersync/sync.py": file(10, 10),
        }
    }
    assert gate.check(report) == [
        "src/steersync/interaction.py: 80.0% of branches, gate 90.0%",
        "src/steersync/plan_render.py: 90.0% of lines, gate 95.0%",
    ]
