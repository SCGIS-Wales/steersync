"""A plan of steps, shown once and approved once.

Commands that change state detect everything first, build a :class:`Plan` of
:class:`Step` objects, show it and ask for one approval. The risk of a step decides how it
is approved:

- ``safe``: creates files or changes local configuration; restorable from a backup.
- ``review``: modifies existing user files or restorable remote state, with a backup;
  highlighted in the plan and covered by the single approval.
- ``destructive``: irreversible and not covered by any backup; off unless ``--force`` or a
  typed confirmation.

Everything here is pure except :func:`run_steps`, which calls each step's ``apply``.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, NamedTuple

import click

from steersync.interaction import Interaction

Risk = Literal["safe", "review", "destructive"]
RISKS: tuple[Risk, ...] = ("safe", "review", "destructive")


@dataclass(frozen=True)
class StepResult:
    step_id: str
    ok: bool
    detail: str
    backup_id: str | None = None


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    detail: str
    target: str
    risk: Risk
    default: bool
    done: bool
    apply: Callable[[], StepResult]
    # Files the step may create or change; they are backed up once before the run.
    paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Plan:
    command: str
    steps: tuple[Step, ...]


ChoiceStatus = Literal["apply", "nothing", "cancelled", "needs-input", "plan-only"]


class Choice(NamedTuple):
    status: ChoiceStatus
    step_ids: tuple[str, ...] = ()


Ask = Callable[[Plan, frozenset[str] | None], list[str] | None]
Typed = Callable[[str], bool]


def pending(plan: Plan) -> tuple[Step, ...]:
    """Steps whose desired state does not hold yet."""
    return tuple(s for s in plan.steps if not s.done)


def default_selection(plan: Plan, interaction: Interaction) -> list[str]:
    """What ``--yes`` applies: pending defaults, destructive ones only with ``--force``."""
    return [
        s.id for s in pending(plan) if s.default and (s.risk != "destructive" or interaction.force)
    ]


def _matches(step_id: str, names: Sequence[str]) -> bool:
    return any(step_id == n or step_id.startswith((n + ":", n + ".")) for n in names)


def restrict(plan: Plan, *, only: Sequence[str], skip: Sequence[str]) -> Plan:
    """The plan limited by ``--only`` and ``--skip`` (exact ids or ``kind`` prefixes)."""
    for name in (*only, *skip):
        if not any(_matches(s.id, [name]) for s in plan.steps):
            known = ", ".join(s.id for s in plan.steps)
            raise click.UsageError(f"unknown step {name!r}; steps: {known}")
    kept = tuple(
        s for s in plan.steps if (not only or _matches(s.id, only)) and not _matches(s.id, skip)
    )
    return Plan(plan.command, kept)


def with_defaults(plan: Plan, selected: Sequence[str]) -> Plan:
    """The plan with ``selected`` as its default selection (a saved profile)."""
    chosen = set(selected)
    return Plan(plan.command, tuple(replace(s, default=s.id in chosen) for s in plan.steps))


def _step_json(step: Step) -> dict[str, Any]:
    return {
        "id": step.id,
        "title": step.title,
        "detail": step.detail,
        "target": step.target,
        "risk": step.risk,
        "default": step.default,
        "done": step.done,
    }


def plan_to_json(plan: Plan) -> dict[str, Any]:
    return {"command": plan.command, "steps": [_step_json(s) for s in plan.steps]}


def plan_hash(plan: Plan, input_digests: Mapping[str, str]) -> str:
    """SHA-256 of the canonical plan and the hashes of the files it was computed from.

    A later apply recomputes the plan and refuses when the hash differs (stale plan).
    """
    canonical = json.dumps(
        {"plan": plan_to_json(plan), "inputs": dict(sorted(input_digests.items()))},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def non_interactive_message(plan: Plan) -> str:
    """What to run instead of answering; counts what --yes would apply."""
    count = sum(1 for s in pending(plan) if s.default and s.risk != "destructive")
    return f"Re-run with --yes to apply {count} steps, or --plan to inspect."


def choose(
    plan: Plan,
    interaction: Interaction,
    *,
    ask: Ask,
    typed: Typed,
    remembered: frozenset[str] | None = None,
) -> Choice:
    """The one decision for a plan: what to apply, or why nothing is applied.

    ``ask`` shows the single checkbox (enter is the approval); ``typed`` asks for the
    command name before a destructive step runs without ``--force``.
    """
    steps = pending(plan)
    if not steps:
        return Choice("nothing")
    if interaction.dry_run:
        return Choice("plan-only")
    if interaction.assume_yes:
        selected = tuple(default_selection(plan, interaction))
        return Choice("apply", selected) if selected else Choice("nothing")
    if not interaction.interactive:
        return Choice("needs-input")
    picked = ask(plan, remembered)
    if not picked:
        return Choice("cancelled")
    risk = {s.id: s.risk for s in steps}
    chosen = [i for i in picked if i in risk]
    destructive = [i for i in chosen if risk[i] == "destructive"]
    if destructive and not interaction.force and not typed(plan.command):
        chosen = [i for i in chosen if i not in destructive]
    return Choice("apply", tuple(chosen)) if chosen else Choice("cancelled")


def run_steps(plan: Plan, step_ids: Sequence[str]) -> list[StepResult]:
    """Apply the chosen steps in plan order; one failure never stops the others."""
    wanted = set(step_ids)
    results: list[StepResult] = []
    for step in plan.steps:
        if step.id not in wanted:
            continue
        try:
            results.append(step.apply())
        except Exception as exc:
            results.append(StepResult(step.id, False, f"{type(exc).__name__}: {exc}"))
    return results
