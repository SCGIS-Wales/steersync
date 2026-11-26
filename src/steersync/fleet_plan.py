"""fleet push and fleet pull as one plan: a step per repository, approved once.

Push steps propose a repository (a remote branch and merge request: restorable remote
state, so ``review``). Pull steps apply the central configuration and keep local edits
(``safe``). For a repository whose local edits also changed centrally, a separate
``overwrite:`` step replaces them after a verified backup (``review``, off unless
``--overwrite-local``); it runs after the repository's pull step. Repositories that cannot
take part are not steps; :func:`skipped` explains them.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from steersync import fleet
from steersync.fleet import Outcome, RepoStatus
from steersync.plan import Plan, Step, StepResult

Action = Literal["push", "pull"]


def _short(path: str, home: str) -> str:
    return "~" + path[len(home) :] if home and path.startswith(home + "/") else path


def _result(step_id: str, outcome: Outcome, sink: list[Outcome]) -> StepResult:
    sink.append(outcome)
    detail = outcome.detail + (f"; rerun: {outcome.rerun}" if outcome.rerun else "")
    return StepResult(step_id, outcome.status in {"done", "dry-run"}, detail)


def _push_step(row: RepoStatus, env: Mapping[str, str], sink: list[Outcome], home: str) -> Step:
    step_id = f"push:{_short(row.path, home)}"
    return Step(
        id=step_id,
        title=f"Propose {_short(row.path, home)}",
        detail=f"push {row.push.render()}, one merge request",
        target=row.project or row.path,
        risk="review",
        default=row.state in {"local-ahead", "diverged"},
        done=False,
        apply=lambda: _result(step_id, fleet.push_one(row, env, dry_run=False), sink),
    )


def _pull_steps(
    row: RepoStatus, env: Mapping[str, str], sink: list[Outcome], home: str, overwrite: bool
) -> list[Step]:
    pull_id = f"pull:{_short(row.path, home)}"
    kept = f", {len(row.conflicts)} local edit(s) kept" if row.conflicts else ""
    steps = [
        Step(
            id=pull_id,
            title=f"Pull {_short(row.path, home)}",
            detail=f"pull {row.pull.render()}{kept}",
            target=row.project or row.path,
            risk="safe",
            default=True,
            done=False,
            apply=lambda: _result(
                pull_id, fleet.pull_one(row, env, overwrite=False, dry_run=False), sink
            ),
        )
    ]
    if row.conflicts:
        over_id = f"overwrite:{_short(row.path, home)}"
        steps.append(
            Step(
                id=over_id,
                title=f"Overwrite local edits in {_short(row.path, home)}",
                detail=f"{', '.join(row.conflicts)}; backed up first",
                target=row.project or row.path,
                risk="review",
                default=overwrite,
                done=False,
                apply=lambda: _result(
                    over_id, fleet.pull_one(row, env, overwrite=True, dry_run=False), sink
                ),
            )
        )
    return steps


def build_fleet_plan(
    action: Action,
    rows: Sequence[RepoStatus],
    env: Mapping[str, str],
    *,
    sink: list[Outcome],
    overwrite: bool = False,
    home: str = "",
) -> Plan:
    """One step per eligible repository (and an overwrite step for local edits)."""
    reason = fleet.push_reason if action == "push" else fleet.pull_reason
    steps: list[Step] = []
    for row in rows:
        if reason(row) is not None:
            continue
        if action == "push":
            steps.append(_push_step(row, env, sink, home))
        else:
            steps.extend(_pull_steps(row, env, sink, home, overwrite))
    return Plan(f"fleet {action}", tuple(steps))


def skipped(action: Action, rows: Sequence[RepoStatus], home: str = "") -> list[str]:
    """Why each selected repository is not part of the plan."""
    reason = fleet.push_reason if action == "push" else fleet.pull_reason
    return [
        f"skip {_short(row.path, home)}: {why}" for row in rows if (why := reason(row)) is not None
    ]
