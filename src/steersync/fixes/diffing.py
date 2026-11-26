"""Unified diffs with a/ and b/ prefixes and git's no newline marker, as git prints them."""

import difflib

NO_NEWLINE = "\\ No newline at end of file\n"


def unified(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(line if line.endswith("\n") else line + "\n" + NO_NEWLINE for line in lines)
