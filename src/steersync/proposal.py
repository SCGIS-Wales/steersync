"""The title and description of a proposal: its commit message and merge request.

GitLab uses the commit message of a single commit merge request as its description, and
the same text is sent through the API when a token is available, so a reviewer reads one
short Markdown summary whichever way the merge request was opened: what changed per
collection, lint errors that were proposed anyway, and, folded away, every file.
"""

from collections.abc import Mapping, Sequence
from typing import NamedTuple

from steersync import __version__
from steersync.lint import LintFinding
from steersync.steering import ChangePlan

_NOTE_CHARS = 110


class ProposalText(NamedTuple):
    title: str
    body: str

    def commit_message(self) -> str:
        return f"{self.title}\n\n{self.body}"


class _Group(NamedTuple):
    collection: str
    added: tuple[str, ...]
    updated: tuple[str, ...]
    deleted: tuple[str, ...]


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def title(project: str, plan: ChangePlan, user: str) -> str:
    """``Kiro config: acme/payments-api (31 files, jdoe)``."""
    return f"Kiro config: {project} ({_files(plan.change_count)}, {user})"


def _collection_of(path: str, prefixes: Mapping[str, str]) -> tuple[str, str]:
    """The collection a ``<central_dir>/<path>`` key belongs to, and the path inside it."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix and path.startswith(prefix + "/"):
            return prefixes[prefix], path[len(prefix) + 1 :]
    return prefixes.get("", "other"), path


def _groups(plan: ChangePlan, prefixes: Mapping[str, str]) -> list[_Group]:
    order = list(dict.fromkeys(prefixes.values()))
    found: dict[str, dict[str, list[str]]] = {}
    for kind, paths in (("added", plan.add), ("updated", plan.update), ("deleted", plan.delete)):
        for path in paths:
            name, inner = _collection_of(path, prefixes)
            found.setdefault(name, {"added": [], "updated": [], "deleted": []})[kind].append(inner)
    names = [n for n in order if n in found] + sorted(set(found) - set(order))
    return [
        _Group(
            name,
            tuple(sorted(found[name]["added"])),
            tuple(sorted(found[name]["updated"])),
            tuple(sorted(found[name]["deleted"])),
        )
        for name in names
    ]


def _short(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _NOTE_CHARS else line[: _NOTE_CHARS - 3] + "..."


def describe(
    *,
    project: str,
    user: str,
    plan: ChangePlan,
    prefixes: Mapping[str, str],
    lint_errors: Sequence[LintFinding] = (),
    source: str | None = None,
    base: str | None = None,
    default_branch: str = "main",
    reason: str | None = None,
) -> ProposalText:
    """Title and Markdown description for proposing ``plan``.

    ``prefixes`` maps each collection's central directory to its name, so files are
    grouped by collection; ``reason`` is the user's ``push -m`` text.
    """
    lines: list[str] = []
    if reason and reason.strip():
        lines += [reason.strip(), ""]
    if project == "global":
        lines.append(f"Global Kiro configuration (`~/.kiro`), proposed by **{user}**.")
    else:
        lines.append(f"Kiro configuration for **{project}**, proposed by **{user}**.")
    groups = _groups(plan, prefixes)
    lines += ["", "| Collection | Added | Updated | Deleted |", "|---|---:|---:|---:|"]
    lines += [
        f"| {g.collection} | {len(g.added)} | {len(g.updated)} | {len(g.deleted)} |" for g in groups
    ]
    if lint_errors:
        files = _files(len({f.path for f in lint_errors}))
        note = f"**Note:** {files} with lint errors proposed anyway (`steersync lint` shows them):"
        lines += ["", note]
        lines += [f"- `{f.path}`: {_short(f.message)}" for f in lint_errors]
    lines += ["", f"<details><summary>All {_files(plan.change_count)}</summary>", ""]
    for group in groups:
        lines.append(f"**{group.collection}**")
        lines.append("")
        lines += [f"- added `{p}`" for p in group.added]
        lines += [f"- updated `{p}`" for p in group.updated]
        lines += [f"- deleted `{p}`" for p in group.deleted]
        lines.append("")
    lines += ["</details>", ""]
    footer = [f"Source: {source}"] if source else []
    if base:
        footer.append(f"base `{default_branch}` @ {base[:8]}")
    footer.append(f"steersync {__version__}")
    lines.append(" · ".join(footer))
    return ProposalText(title(project, plan, user), "\n".join(lines) + "\n")


def fallback(project: str, branch: str) -> ProposalText:
    """For ``steersync mr --branch`` when the branch content is not known locally."""
    return ProposalText(
        f"Kiro config: {project}",
        f"Kiro configuration for **{project}** on branch `{branch}`.\n\nsteersync {__version__}\n",
    )
