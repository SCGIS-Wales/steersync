"""Prove that a fix keeps the meaning of the file before anything is written.

A fix is rejected when it changes front matter data other than the keys its fixers
declare, JSON data, the rendered Markdown (unless a fixer declares it may), the
``#[[...]]`` references or the contents of fenced code; when it is not idempotent; or
when relinting finds something new.
"""

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable

import yaml
from markdown_it import MarkdownIt

from steersync.fixes.model import Fixer, VerificationError

_FM_RE = re.compile(r"\A﻿?\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_REF_RE = re.compile(r"#\[\[[^\]\n]*\]\]")
_FENCE_RE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_MD = MarkdownIt("commonmark")
_NATIVE = frozenset({"inclusion", "fileMatchPattern", "name", "description"})


def _front_matter(text: str) -> tuple[object, str]:
    """(front matter data, body); data is None without front matter, ... when invalid."""
    normal = text.replace("\r\n", "\n")
    match = _FM_RE.match(normal)
    if match is None:
        return None, normal.removeprefix("﻿")
    try:
        return yaml.safe_load(match.group(1)) or {}, normal[match.end() :]
    except yaml.YAMLError:
        return ..., normal[match.end() :]


def _strip(data: object, keys: frozenset[str]) -> object:
    if not isinstance(data, dict):
        return data
    if "*" in keys:
        return {
            k: v
            for k, v in data.items()
            if k in {"inclusion", "fileMatchPattern", "name", "description"}
        }
    return {k: v for k, v in data.items() if k not in keys}


def _html(body: str) -> str:
    return re.sub(r"\s+", " ", _MD.render(body)).strip()


def verify(
    original: str,
    fixed: str,
    path: str,
    applied: Iterable[Fixer],
    *,
    refix: Callable[[str], str],
    relint: Callable[[str], Counter[str]],
) -> None:
    """Raise :class:`VerificationError` when ``fixed`` does not mean what ``original`` did."""
    fixers = list(applied)
    keys = frozenset().union(*(f.changes_keys for f in fixers))
    if Counter(_REF_RE.findall(original)) != Counter(_REF_RE.findall(fixed)):
        raise VerificationError("a #[[...]] reference would change")
    if [m.group(2) for m in _FENCE_RE.finditer(original.replace("\r\n", "\n"))] != [
        m.group(2) for m in _FENCE_RE.finditer(fixed)
    ]:
        raise VerificationError("fenced code would change")
    if path.endswith(".json"):
        try:
            same = json.loads(original) == json.loads(fixed)
        except ValueError as exc:
            raise VerificationError(f"JSON no longer parses: {exc}") from exc
        if not same:
            raise VerificationError("the JSON data would change")
    else:
        before, body_before = _front_matter(original)
        after, body_after = _front_matter(fixed)
        if after is ...:
            raise VerificationError("the front matter would no longer be valid YAML")
        if before is not ... and _strip(before, keys) != _strip(after, keys):
            raise VerificationError("front matter data outside the fixed keys would change")
        markdown_kept = any(f.changes_markdown for f in fixers) or not path.endswith(".md")
        if not markdown_kept and _html(body_before) != _html(body_after):
            raise VerificationError("the rendered Markdown would change")
    if refix(fixed) != fixed:
        raise VerificationError("the fix is not idempotent")
    before_counts, after_counts = relint(original), relint(fixed)
    new = sorted(rule for rule, count in after_counts.items() if count > before_counts[rule])
    if new:
        raise VerificationError(f"relinting finds new problems: {', '.join(new)}")
