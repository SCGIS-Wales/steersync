"""Rendering and prompting for ``steersync fleet`` (rich and questionary).

The only module that imports the interface libraries; the CLI imports it lazily so
other commands keep their start up time. Prompt functions are parameters, so every flow
is testable with scripted answers. Colour never carries meaning on its own: every state
is also a word, and ``NO_COLOR`` or a plain terminal turns colour off.
"""

from collections.abc import Callable, Sequence
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from steersync.fleet import FleetReport, RepoStatus

STATE_STYLE = {
    "error": "bold red",
    "diverged": "red",
    "local-ahead": "yellow",
    "central-ahead": "cyan",
    "proposed": "blue",
    "in-sync": "green",
    "unconfigured": "dim",
    "missing": "dim",
}


def short_path(path: str, home: str) -> str:
    return "~" + path[len(home) :] if home and path.startswith(home + "/") else path


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown age"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172_800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86_400:.0f} days ago"


def row_note(repo: RepoStatus) -> str:
    if repo.error and repo.state in {"error", "missing", "unconfigured"}:
        return repo.error
    notes = []
    if repo.proposal:
        notes.append(repo.merge_request or repo.proposal)
    if repo.offline:
        notes.append("offline: cached copy, push disabled")
    return "; ".join(notes) or "none"


def header(report: FleetReport) -> list[str]:
    lines = []
    for remote in report.remotes:
        commit = (remote.commit or "?")[:8]
        state = "offline, cached copy" if remote.offline else "refreshed"
        problem = f"  ERROR {remote.error}" if remote.error else ""
        lines.append(
            f"central: {remote.url} @ {commit} ({state} {age_text(remote.cache_age_seconds)})"
            + problem
        )
    count = len([r for r in report.repositories if not r.is_global()])
    lines.append(f"{count} repositories, {report.needing_attention()} need attention")
    return lines


def table(report: FleetReport, *, home: str = "") -> Table:
    grid = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold")
    # States and counts are read at a glance and never wrap; paths and notes fold.
    grid.add_column("#", no_wrap=True)
    grid.add_column("Repository", overflow="fold", min_width=16)
    grid.add_column("Project", overflow="fold", min_width=10)
    grid.add_column("State", no_wrap=True)
    grid.add_column("To push", no_wrap=True)
    grid.add_column("To pull", overflow="fold", min_width=8)
    grid.add_column("Note", overflow="fold", min_width=12)
    for index, repo in enumerate(report.repositories, start=1):
        label = "g" if repo.is_global() else str(index)
        path = f"{repo.path} (global)" if repo.is_global() else short_path(repo.path, home)
        state = repo.state + (" (offline)" if repo.offline else "")
        pull = repo.pull.render()
        if repo.conflicts:
            pull = f"{pull}, {len(repo.conflicts)} both"
        if repo.state in {"error", "missing", "unconfigured"}:
            grid.add_row(
                label,
                path,
                repo.project or "none",
                Text(state, STATE_STYLE[repo.state]),
                "",
                "",
                row_note(repo),
            )
            continue
        grid.add_row(
            label,
            path,
            repo.project or "none",
            Text(state, STATE_STYLE[repo.state]),
            repo.push.render(),
            pull,
            row_note(repo),
        )
    return grid


def render(report: FleetReport, console: Console, *, home: str = "") -> None:
    for line in header(report):
        console.print(line, highlight=False)
    console.print()
    if not report.repositories:
        console.print("no repositories registered yet; run `steersync fleet scan ~/code`")
        return
    console.print(table(report, home=home))


def details(repo: RepoStatus, console: Console) -> None:
    """Per collection file list of one repository."""
    console.print(f"{repo.path}  [{repo.state}]", highlight=False)
    if repo.error:
        console.print(f"  {repo.error}", highlight=False)
    by_collection: dict[str, list[str]] = {}
    for change in repo.files:
        mark = {"push": "push", "pull": "pull", "conflict": "both"}[change.direction]
        by_collection.setdefault(change.collection, []).append(
            f"    {mark:<5} {change.kind:<7} {change.path}"
        )
    for name in sorted(by_collection):
        console.print(f"  {name}", highlight=False)
        for line in by_collection[name]:
            console.print(line, highlight=False)
    if not repo.files:
        console.print("  nothing differs", highlight=False)


Ask = Callable[..., Any]


def choose_repositories(
    rows: Sequence[RepoStatus],
    eligible: Callable[[RepoStatus], str | None],
    preselect: Callable[[RepoStatus], bool],
    *,
    message: str,
    checkbox: Ask,
) -> list[RepoStatus]:
    """Checkbox list; ``eligible`` returns a reason when a row cannot be chosen."""
    import questionary  # noqa: PLC0415 - interface library, loaded on demand

    choices = []
    for repo in rows:
        reason = eligible(repo)
        title = f"{repo.path}  [{repo.state}]  push {repo.push.render()}  pull {repo.pull.render()}"
        choices.append(
            questionary.Choice(
                title,
                value=repo.path,
                checked=reason is None and preselect(repo),
                disabled=reason,
            )
        )
    picked = checkbox(message, choices=choices) or []
    chosen = set(picked)
    return [repo for repo in rows if repo.path in chosen]
