"""Blank line fixers steersync owns, where PyMarkdown has no fix (MD022, MD032).

They work line by line outside fenced code and never inside front matter. Blank lines
around headings and fences are safe while the rendered Markdown stays the same (verified,
otherwise demoted to unsafe); around lists they can end a lazy continuation, so unsafe.
"""

import re

from steersync.fixes.model import FileContext, Fixer
from steersync.fixes.pymarkdown_bridge import MARKDOWN_PROFILES

_FM_RE = re.compile(r"\A(---[ \t]*\n.*?\n---[ \t]*\n)", re.DOTALL)
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(\s|$)")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_LIST_RE = re.compile(r"^ {0,3}([-*+]|\d+[.)])\s")


def _split(text: str) -> tuple[str, list[str]]:
    match = _FM_RE.match(text)
    head = match.group(1) if match else ""
    return head, text[len(head) :].split("\n")


def _blank(line: str) -> bool:
    return not line.strip()


def _around(text: str, ctx: FileContext, *, lists: bool) -> str:
    if not ctx.path.endswith(".md"):
        return text
    head, lines = _split(text)
    out: list[str] = []
    fence: str | None = None
    in_list = False
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if fence is not None:  # inside a code fence: copy, and leave room after it
            out.append(line)
            if line.strip().startswith(fence):
                fence = None
                if following.strip():
                    out.append("")
            continue
        if opening := _FENCE_RE.match(line):
            if out and not _blank(out[-1]):
                out.append("")
            fence = opening.group(1)
            out.append(line)
            continue
        if lists:
            item = _LIST_RE.match(line) is not None
            continuation = in_list and line.startswith((" ", "\t")) and line.strip()
            if item and not in_list and out and not _blank(out[-1]):
                out.append("")
            elif in_list and line.strip() and not item and not continuation:
                out.append("")  # the list ended without a blank line
            in_list = bool(item or continuation)
            out.append(line)
            continue
        if _HEADING_RE.match(line):
            if out and not _blank(out[-1]):
                out.append("")
            out.append(line)
            if following.strip():
                out.append("")
            continue
        out.append(line)
    fixed = head + "\n".join(out)
    return fixed if fixed != text else text


def blanks_around(text: str, ctx: FileContext) -> str:
    return _around(text, ctx, lists=False)


def blanks_around_lists(text: str, ctx: FileContext) -> str:
    return _around(text, ctx, lists=True)


FIXERS: tuple[Fixer, ...] = (
    Fixer(
        "md-blanks-around",
        MARKDOWN_PROFILES,
        blanks_around,
        "a blank line around headings and code fences",
        demotable=True,
    ),
    Fixer(
        "md-blanks-around-lists",
        MARKDOWN_PROFILES,
        blanks_around_lists,
        "a blank line around lists (can end a lazy continuation)",
        changes_markdown=True,
    ),
)
