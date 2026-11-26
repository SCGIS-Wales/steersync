"""Front matter, knowledge, skill name, duplicate rule and EARS checks as pure functions.

Each returns :class:`Hit` records; :mod:`steersync.lint` adds the file path. Every
vocabulary and number comes from configuration (``lint.frontmatter``, ``lint.budgets``,
``lint.spec``), never from here.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal, NamedTuple

from steersync.config import BudgetsConfig, FrontMatterConfig, SpecConfig
from steersync.schema.frontmatter import validate_org_metadata

Severity = Literal["error", "warning", "info"]
INCLUSION_MODES: tuple[str, ...] = ("always", "fileMatch", "manual", "auto")
NATIVE_STEERING_KEYS: frozenset[str] = frozenset(
    {"inclusion", "fileMatchPattern", "name", "description"}
)
BOM = "\ufeff"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")


class Hit(NamedTuple):
    rule: str
    severity: Severity
    message: str
    line: int | None = None


def position_hits(text: str) -> list[Hit]:
    """fm-position: front matter must be the very first content (no BOM, blank or space)."""
    if text.startswith(BOM) and text[1:].lstrip(" \t\r\n").startswith("---"):
        return [Hit("fm-position", "error", "a byte order mark precedes the front matter", 1)]
    stripped = text.lstrip(" \t\r\n")
    if stripped.startswith("---") and stripped != text:
        return [
            Hit(
                "fm-position",
                "error",
                "front matter must start on line 1; Kiro ignores it after a blank line or space",
                1,
            )
        ]
    return []


def _is_pattern(value: object) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)
    )


def steering_hits(meta: Mapping[str, Any], config: FrontMatterConfig) -> list[Hit]:
    """The Kiro native keys of a steering file, then what does not belong there."""
    hits: list[Hit] = []
    inclusion = meta.get("inclusion", "always")
    if inclusion not in INCLUSION_MODES:
        hits.append(
            Hit(
                "fm-inclusion",
                "error",
                f"inclusion must be one of {', '.join(INCLUSION_MODES)} (exact case); "
                f"got {inclusion!r}",
                2,
            )
        )
    pattern = meta.get("fileMatchPattern")
    if inclusion == "fileMatch" and pattern is None:
        hits.append(Hit("fm-filematch-required", "error", "fileMatch requires fileMatchPattern", 2))
    elif pattern is not None and not _is_pattern(pattern):
        hits.append(
            Hit(
                "fm-filematch-type",
                "error",
                "fileMatchPattern must be a glob string or a list of globs",
                2,
            )
        )
    elif pattern is not None:
        if inclusion != "fileMatch":
            hits.append(
                Hit(
                    "fm-filematch-unused",
                    "warning",
                    "fileMatchPattern is ignored unless inclusion is fileMatch",
                    2,
                )
            )
        globs = [pattern] if isinstance(pattern, str) else list(pattern)
        if isinstance(pattern, str) and "," in pattern:
            hits.append(
                Hit(
                    "fm-filematch-comma",
                    "error",
                    "a comma separated fileMatchPattern never matches and Kiro does not warn; "
                    "use a YAML list: fileMatchPattern: ["
                    + ", ".join(f'"{p.strip()}"' for p in pattern.split(",") if p.strip())
                    + "]",
                    2,
                )
            )
        hits.extend(
            Hit(
                "fm-filematch-relative",
                "warning",
                f"{glob!r}: patterns are relative to the workspace; drop the leading / or ./",
                2,
            )
            for glob in globs
            if glob.startswith(("/", "./"))
        )
    if inclusion == "auto":
        hits.extend(
            Hit(
                "fm-auto-fields",
                "error",
                f"inclusion: auto needs a non empty {key}; Kiro loads only name and "
                "description until the model judges the file relevant",
                2,
            )
            for key in ("name", "description")
            if not isinstance(meta.get(key), str) or not str(meta.get(key)).strip()
        )
    hits.extend(_extra_key_hits(meta, config))
    return hits


def _extra_key_hits(meta: Mapping[str, Any], config: FrontMatterConfig) -> list[Hit]:
    hits: list[Hit] = []
    knowledge = [k for k in config.knowledge_keys if k in meta]
    if knowledge and "inclusion" not in meta:
        hits.append(
            Hit(
                "fm-knowledge-in-steering",
                "error",
                f"{', '.join(knowledge)} are knowledge note keys and there is no inclusion, so "
                "this note would load on every turn; move it to the knowledge collection",
                2,
            )
        )
    allowed = NATIVE_STEERING_KEYS | set(config.allowed_extra_keys) | set(config.knowledge_keys)
    if config.metadata_placement == "embedded":
        allowed |= {config.metadata_key}
        destination = f"under {config.metadata_key}: as a string"
    else:
        destination = f"to {config.sidecar}"
    hits.extend(
        Hit(
            "fm-unknown-key",
            "warning",
            f"{key!r} is not a Kiro steering key; move it {destination}",
            2,
        )
        for key in meta
        if key not in allowed
    )
    return hits


def metadata_hits(
    meta: Mapping[str, Any], config: FrontMatterConfig, *, today: date | None = None
) -> list[Hit]:
    """fm-metadata: the organisational map, embedded in steering and skill front matter."""
    if config.metadata_key not in meta:
        return []
    value = meta[config.metadata_key]
    if config.metadata_placement == "sidecar":
        return [
            Hit(
                "fm-metadata",
                "warning",
                f"{config.metadata_key} belongs in {config.sidecar} (lint.frontmatter."
                "metadata_placement is sidecar)",
                2,
            )
        ]
    if not isinstance(value, Mapping):
        return [Hit("fm-metadata", "error", f"{config.metadata_key} must be a map of strings", 2)]
    return [
        Hit("fm-metadata", issue.severity, issue.message, 2)
        for issue in validate_org_metadata(value, config.metadata_fields, today=today)
    ]


def skill_name_hits(name: object, folder: str, max_chars: int = 64) -> list[Hit]:
    """skill-name: the Agent Skills name rules, and the directory must carry the same name."""
    if not isinstance(name, str) or not name.strip():
        return []
    problems: list[str] = []
    if not _SKILL_NAME_RE.match(name):
        problems.append("lowercase letters, digits and single hyphens only")
    if len(name) > max_chars:
        problems.append(f"at most {max_chars} characters")
    if folder and name != folder:
        problems.append(f"the same as its directory {folder!r}")
    if not problems:
        return []
    return [Hit("skill-name", "error", f"skill name {name!r} must be " + "; ".join(problems), 2)]


def skill_size_hits(body: str, budgets: BudgetsConfig) -> list[Hit]:
    """skill-size: a SKILL.md body over the line or token budget."""
    lines = body.count("\n") + 1
    tokens = len(body) // budgets.chars_per_token
    hits: list[Hit] = []
    if lines > budgets.skill_lines:
        hits.append(
            Hit(
                "skill-size",
                "warning",
                f"SKILL.md body has {lines} lines; keep it under {budgets.skill_lines} and move "
                "detail into references/",
            )
        )
    if tokens > budgets.skill_tokens:
        hits.append(
            Hit(
                "skill-size",
                "warning",
                f"SKILL.md body is about {tokens} tokens; keep it under {budgets.skill_tokens}",
            )
        )
    return hits


def _normalise(bullet: str) -> str:
    return re.sub(r"\s+", " ", bullet).strip().rstrip(".;:").lower()


def dup_rule_hits(texts: Mapping[str, str], budgets: BudgetsConfig) -> list[tuple[str, Hit]]:
    """steering-dup-rule: the same normalised bullet in more than one steering file."""
    seen: dict[str, list[tuple[str, int]]] = {}
    for path, text in sorted(texts.items()):
        for number, line in enumerate(text.splitlines(), start=1):
            match = _BULLET_RE.match(line)
            if match is None:
                continue
            key = _normalise(match.group(1))
            if len(key) >= budgets.dup_rule_min_chars:
                seen.setdefault(key, []).append((path, number))
    hits: list[tuple[str, Hit]] = []
    for places in seen.values():
        files = sorted({path for path, _ in places})
        if len(files) < 2:
            continue
        for path, number in places:
            others = ", ".join(f for f in files if f != path)
            hits.append(
                (
                    path,
                    Hit(
                        "steering-dup-rule",
                        "warning",
                        f"the same rule is also in {others}; state each rule once",
                        number,
                    ),
                )
            )
    return hits


def ears_hits(body: str, spec: SpecConfig) -> list[Hit]:
    """spec-ears (info): acceptance criteria not written as WHEN/IF/WHILE ... SHALL."""
    heading = re.compile(spec.criteria_heading)
    ears = re.compile(spec.ears_pattern)
    hits: list[Hit] = []
    inside = False
    for number, line in enumerate(body.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            inside = bool(heading.search(line.strip()))
            continue
        match = _BULLET_RE.match(line) if inside else None
        if match is not None and not ears.search(match.group(1)):
            hits.append(
                Hit(
                    "spec-ears",
                    "info",
                    "acceptance criterion not in EARS form (WHEN/IF/WHILE ... the system "
                    "SHALL ...)",
                    number,
                )
            )
    return hits


def sidecar_hits(
    data: object, existing: Sequence[str], config: FrontMatterConfig, *, today: date | None = None
) -> list[Hit]:
    """The sidecar maps repository paths to metadata maps checked like embedded metadata."""
    if not isinstance(data, Mapping):
        return [Hit("fm-metadata", "error", "the sidecar must map file paths to metadata maps")]
    hits: list[Hit] = []
    for path, meta in data.items():
        if str(path) not in existing:
            hits.append(Hit("fm-metadata", "warning", f"{path!r} does not exist"))
        if not isinstance(meta, Mapping):
            hits.append(Hit("fm-metadata", "error", f"{path!r} must map to a metadata map"))
            continue
        hits.extend(
            Hit("fm-metadata", issue.severity, f"{path}: {issue.message}")
            for issue in validate_org_metadata(meta, config.metadata_fields, today=today)
        )
    return hits
