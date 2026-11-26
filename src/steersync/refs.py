"""Kiro file references: ``#[[file:path]]``, ``#[[file:path:N]]``, ``#[[file:path:A-B]]``,
``#[[folder:path]]``.

Paths resolve from the workspace root for workspace steering, from ``~/.kiro/steering``
for global steering and from the containing folder for ``AGENTS.md``. ``parse`` finds every
reference and reports malformed ones (``ref-syntax``); ``resolve`` reports targets that do
not exist, escape their base or name lines outside the file (``ref-unresolved``).
"""

import re
from pathlib import Path
from typing import Literal, NamedTuple

# Anything that looks like a reference, so a malformed one is still reported.
_CANDIDATE_RE = re.compile(r"#\[\[([^\]\n]*)\]\]")
_VALID_RE = re.compile(
    r"^(?P<kind>file|folder):(?P<path>[^:\]\n]+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$"
)

Kind = Literal["file", "folder"]


class Reference(NamedTuple):
    kind: Kind
    path: str
    start: int | None
    end: int | None
    line: int  # line of the document the reference is on
    token: str  # the reference as written, #[[...]]


class Problem(NamedTuple):
    rule: Literal["ref-syntax", "ref-unresolved"]
    message: str
    line: int


def parse(text: str) -> tuple[list[Reference], list[Problem]]:
    """Every reference in ``text`` and every malformed one."""
    references: list[Reference] = []
    problems: list[Problem] = []
    for match in _CANDIDATE_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        inner = match.group(1).strip()
        token = match.group(0)
        if not inner.startswith(("file:", "folder:")):
            continue  # some other #[[...]] syntax; not a Kiro reference
        valid = _VALID_RE.match(inner)
        if valid is None:
            problems.append(
                Problem(
                    "ref-syntax",
                    f"{token} is not #[[file:path]], #[[file:path:N]], #[[file:path:A-B]] or "
                    "#[[folder:path]]",
                    line,
                )
            )
            continue
        kind: Kind = "file" if valid.group("kind") == "file" else "folder"
        start = int(valid.group("start")) if valid.group("start") else None
        end = int(valid.group("end")) if valid.group("end") else None
        if kind == "folder" and start is not None:
            problems.append(Problem("ref-syntax", f"{token}: a folder has no line range", line))
            continue
        if start is not None and (start < 1 or (end is not None and end < start)):
            problems.append(Problem("ref-syntax", f"{token}: the line range is empty", line))
            continue
        references.append(Reference(kind, valid.group("path").strip(), start, end, line, token))
    return references, problems


def resolve(reference: Reference, base: Path) -> Problem | None:
    """Why ``reference`` does not resolve from ``base``, or None when it does."""
    target = (base / reference.path).resolve()
    root = base.resolve()
    if target != root and root not in target.parents:
        return Problem("ref-unresolved", f"{reference.token} points outside {root}", reference.line)
    if reference.kind == "folder":
        if not target.is_dir():
            return Problem("ref-unresolved", f"{reference.token}: no such folder", reference.line)
        return None
    if not target.is_file():
        return Problem("ref-unresolved", f"{reference.token}: no such file", reference.line)
    if reference.start is not None:
        try:
            lines = len(target.read_text("utf-8", errors="replace").splitlines())
        except OSError as exc:
            return Problem("ref-unresolved", f"{reference.token}: {exc}", reference.line)
        last = reference.end or reference.start
        if last > lines:
            return Problem(
                "ref-unresolved",
                f"{reference.token}: the file has {lines} lines",
                reference.line,
            )
    return None
