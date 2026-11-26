"""Lint rules for knowledge notes (profile ``knowledge``), as pure functions.

Notes are retrieved in chunks by a Kiro knowledge base, so the rules favour a compact
front matter, an early summary, short sections and the identifiers a search will use. Every
number comes from ``lint.knowledge``.
"""

import re
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from steersync.config import KnowledgeLintConfig
from steersync.fm_rules import Hit
from steersync.schema.knowledge import KnowledgeNote

STEERING_KEYS = ("inclusion", "fileMatchPattern")
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+\S")


def _schema_hits(meta: Mapping[str, Any]) -> list[Hit]:
    data = {k: v for k, v in meta.items() if k not in STEERING_KEYS}
    try:
        KnowledgeNote.model_validate(data)
    except ValidationError as exc:
        return [
            Hit(
                "kn-schema",
                "error",
                f"{'.'.join(str(p) for p in error['loc']) or 'front matter'}: {error['msg']}",
                2,
            )
            for error in exc.errors()
        ]
    return []


def note_hits(
    meta: Mapping[str, Any],
    front_matter_text: str,
    body: str,
    config: KnowledgeLintConfig,
    *,
    today: date | None = None,
) -> list[Hit]:
    """Every per note rule: schema, steering keys, summary, sizes, identifiers, staleness."""
    now = today or date.today()  # noqa: DTZ011 - review dates are calendar dates
    hits = _schema_hits(meta)
    hits.extend(
        Hit(
            "kn-inclusion",
            "error",
            f"{key} is a steering key; knowledge notes are retrieved, never loaded as steering",
            2,
        )
        for key in STEERING_KEYS
        if key in meta
    )
    hits.extend(_summary_hits(body, config))
    if len(front_matter_text) > config.frontmatter_chars:
        hits.append(
            Hit(
                "kn-frontmatter-size",
                "warning",
                f"front matter is {len(front_matter_text)} characters; keep it under "
                f"{config.frontmatter_chars} so the summary stays in the first chunk",
                1,
            )
        )
    hits.extend(_section_hits(body, config))
    hits.extend(_identifier_hits(meta, body))
    hits.extend(_stale_hits(meta, config, now))
    return hits


def _summary_hits(body: str, config: KnowledgeLintConfig) -> list[Hit]:
    lines = body.strip("\n").splitlines()[: config.summary_within_lines]
    has_h1 = any(line.startswith("# ") for line in lines)
    paragraph = any(line.strip() and not line.lstrip().startswith("#") for line in lines)
    if has_h1 and paragraph:
        return []
    return [
        Hit(
            "kn-summary",
            "warning",
            f"start with a level one heading and a summary paragraph within the first "
            f"{config.summary_within_lines} lines",
        )
    ]


def _section_hits(body: str, config: KnowledgeLintConfig) -> list[Hit]:
    hits: list[Hit] = []
    words = 0
    start = 1
    title = "(top)"
    for number, line in enumerate([*body.splitlines(), "# end"], start=1):
        if _HEADING_RE.match(line):
            if words > config.section_words:
                hits.append(
                    Hit(
                        "kn-section-size",
                        "warning",
                        f"section {title!r} has {words} words; split it under "
                        f"{config.section_words} so each chunk stays on one topic",
                        start,
                    )
                )
            words, start, title = 0, number, line.lstrip("#").strip()
        else:
            words += len(line.split())
    return hits


def _identifier_hits(meta: Mapping[str, Any], body: str) -> list[Hit]:
    aliases = [a for a in meta.get("aliases") or [] if isinstance(a, str)]
    title = meta.get("title") if isinstance(meta.get("title"), str) else ""
    wanted = dict.fromkeys([*aliases, *_CVE_RE.findall(str(title))])
    lowered = body.lower()
    return [
        Hit(
            "kn-identifiers-in-body",
            "warning",
            f"{name!r} is in the front matter but not the body; a search for it would miss",
            2,
        )
        for name in wanted
        if name.lower() not in lowered
    ]


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _stale_hits(meta: Mapping[str, Any], config: KnowledgeLintConfig, now: date) -> list[Hit]:
    hits: list[Hit] = []
    review = _as_date(meta.get("review_by"))
    if review is not None and review < now:
        hits.append(Hit("kn-stale", "warning", f"review_by {review} has passed", 2))
    verified = _as_date(meta.get("verified_on"))
    if verified is not None and now - verified > timedelta(days=config.max_age_days):
        hits.append(
            Hit(
                "kn-stale",
                "warning",
                f"verified_on {verified} is more than {config.max_age_days} days ago",
                2,
            )
        )
    return hits


def collection_hits(notes: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, Hit]]:
    """kn-duplicate (titles and aliases) and kn-related-unresolved across the collection."""
    hits: list[tuple[str, Hit]] = []
    owners: dict[str, list[str]] = {}
    for path, meta in sorted(notes.items()):
        names = [meta.get("title"), *(meta.get("aliases") or [])]
        for name in {str(n).strip().lower() for n in names if isinstance(n, str) and n.strip()}:
            owners.setdefault(name, []).append(path)
    for name, paths in owners.items():
        if len(paths) > 1:
            hits.extend(
                (
                    path,
                    Hit(
                        "kn-duplicate",
                        "warning",
                        f"{name!r} also names {', '.join(p for p in paths if p != path)}",
                    ),
                )
                for path in paths
            )
    known = set(notes)
    for path, meta in sorted(notes.items()):
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        for target in meta.get("related") or []:
            if not isinstance(target, str):
                continue
            candidates = {target, f"{folder}/{target}" if folder else target}
            if not candidates & known:
                hits.append(
                    (path, Hit("kn-related-unresolved", "warning", f"related {target!r} not found"))
                )
    return hits
