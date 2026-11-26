"""Show a plan as one table, and the results with the undo hint."""

from collections.abc import Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from steersync.plan import Plan, StepResult

RISK_STYLE = {"safe": "green", "review": "yellow", "destructive": "bold red"}


def render_plan(plan: Plan, console: Console) -> None:
    """One table: Step, Change, Target, Risk. Steps already in place say so."""
    grid = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold")
    grid.add_column("Step", no_wrap=True)
    grid.add_column("Change", overflow="fold")
    grid.add_column("Target", overflow="fold")
    grid.add_column("Risk", no_wrap=True)
    for step in plan.steps:
        change = f"{step.title} ({step.detail})" if step.detail else step.title
        risk = Text("already done", "dim") if step.done else Text(step.risk, RISK_STYLE[step.risk])
        grid.add_row(step.id, change, step.target, risk)
    console.print(grid)


def render_results(
    results: Sequence[StepResult], backup_id: str | None, console: Console, *, undo_hint: bool
) -> None:
    for result in results:
        mark = "done  " if result.ok else "FAILED"
        console.print(f"{mark} {result.step_id}: {result.detail}", highlight=False)
    if backup_id and undo_hint:
        console.print(f"Undo with: steersync restore {backup_id}", highlight=False)
