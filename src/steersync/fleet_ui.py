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


HEADINGS = ("#", "Repository", "Project", "State", "To push", "To pull", "Note")
GAP = 3  # SIMPLE_HEAD puts three characters between columns
NOTE_MIN = 12


def _cells(repo: RepoStatus, index: int, home: str) -> tuple[str, ...]:
    label = "g" if repo.is_global() else str(index)
    path = f"{repo.path} (global)" if repo.is_global() else short_path(repo.path, home)
    state = repo.state + (" (offline)" if repo.offline else "")
    if repo.state in {"error", "missing", "unconfigured"}:
        return (label, path, repo.project or "none", state, "", "", row_note(repo))
    pull = repo.pull.render()
    if repo.conflicts:
        pull = f"{pull}, {len(repo.conflicts)} both"
    return (label, path, repo.project or "none", state, repo.push.render(), pull, row_note(repo))


def column_widths(rows: Sequence[tuple[str, ...]], width: int) -> list[int] | None:
    """Exact widths that fill ``width``, or None when the terminal is too narrow.

    States, counts and the row number never wrap; the repository and project keep whole
    names up to 40 and 30 characters; the note takes what is left and folds.
    """
    natural = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADINGS)]
    natural[1] = min(natural[1], 40)
    natural[2] = min(natural[2], 30)
    widths = [*natural[:6], 0]
    deficit = NOTE_MIN - (width - sum(widths) - GAP * (len(HEADINGS) - 1))
    for position, floor in ((1, 16), (2, 10)):  # names give way only as far as needed
        if deficit > 0:
            give = min(deficit, max(widths[position] - floor, 0))
            widths[position] -= give
            deficit -= give
    if deficit > 0:
        return None
    widths[6] = min(natural[6], width - sum(widths) - GAP * (len(HEADINGS) - 1))
    return widths


def table(report: FleetReport, *, home: str = "", width: int | None = None) -> Table:
    rows = [_cells(repo, index, home) for index, repo in enumerate(report.repositories, 1)]
    widths = column_widths(rows, width) if width and rows else None
    grid = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold")
    for position, heading in enumerate(HEADINGS):
        wraps = position in {1, 2, 5, 6}
        grid.add_column(
            heading,
            no_wrap=not wraps,
            overflow="fold" if wraps else "ellipsis",
            width=widths[position] if widths else None,
        )
    for repo, cells in zip(report.repositories, rows, strict=True):
        grid.add_row(*cells[:3], Text(cells[3], STATE_STYLE[repo.state]), *cells[4:])
    return grid


def render(report: FleetReport, console: Console, *, home: str = "") -> None:
    for line in header(report):
        console.print(line, highlight=False)
    console.print()
    if not report.repositories:
        console.print("no repositories registered yet; run `steersync fleet scan ~/code`")
        return
    console.print(table(report, home=home, width=console.width))


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
