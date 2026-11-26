"""steersync's own fixers: encoding, line endings and front matter.

Each is a pure function over the whole text. Edits are made line by line on the front
matter so nothing else in the file moves; only ``fm-move-to-metadata`` re-emits the front
matter (with ruamel.yaml round trip), which is why it is unsafe.
"""

import io
import re
from collections.abc import Callable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from steersync.fixes.model import FileContext, Fixer

BOM = "\ufeff"
INCLUSION_MODES = ("always", "fileMatch", "manual", "auto")
NATIVE_KEYS = frozenset({"inclusion", "fileMatchPattern", "name", "description", "metadata"})
_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", re.DOTALL)
_KEY_ALIASES = ("filematchpattern", "FileMatchPattern", "file_match_pattern", "filematch_pattern")
TEXT_PROFILES = frozenset(
    {
        "steering",
        "markdown",
        "spec",
        "knowledge",
        "skill",
        "agent",
        "prompt",
        "hooks",
        "settings",
        "workflow",
        "json",
        "kiroignore",
        "generic",
    }
)
FM_PROFILES = frozenset({"steering", "skill"})


def _split(text: str) -> tuple[str, str, str, str] | None:
    """(opening ---, front matter lines, closing ---, body) or None without front matter."""
    match = _FM_RE.match(text)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3), text[match.end() :]


def _on_front_matter(edit: Callable[[str], str]) -> Callable[[str, FileContext], str]:
    def apply(text: str, ctx: FileContext) -> str:
        del ctx
        parts = _split(text)
        if parts is None:
            return text
        opening, header, closing, body = parts
        return opening + edit(header) + closing + body

    return apply


def strip_bom(text: str, ctx: FileContext) -> str:
    del ctx
    return text.removeprefix(BOM)


def to_lf(text: str, ctx: FileContext) -> str:
    del ctx
    return text.replace("\r\n", "\n")


def leading_blank(text: str, ctx: FileContext) -> str:
    """Remove blank lines before an opening ``---`` so Kiro sees the front matter."""
    del ctx
    stripped = text.lstrip(" \t\r\n")
    return stripped if stripped.startswith("---") and stripped != text else text


def _key_case(header: str) -> str:
    if re.search(r"^fileMatchPattern\s*:", header, re.MULTILINE):
        return header
    alias = "|".join(re.escape(a) for a in _KEY_ALIASES)
    return re.sub(rf"^({alias})(\s*:)", r"fileMatchPattern\2", header, count=1, flags=re.MULTILINE)


def _inclusion_value(header: str, choose: Callable[[str], str | None]) -> str:
    def replace(match: re.Match[str]) -> str:
        quote, value = match.group(2), match.group(3)
        chosen = choose(value)
        return match.group(0) if chosen is None else f"{match.group(1)}{quote}{chosen}{quote}"

    return re.sub(
        r"^(inclusion\s*:\s*)([\"']?)([A-Za-z_-]+)\2[ \t]*$",
        replace,
        header,
        count=1,
        flags=re.MULTILINE,
    )


def _case(value: str) -> str | None:
    if value in INCLUSION_MODES:
        return None
    by_lower = {mode.lower(): mode for mode in INCLUSION_MODES}
    return by_lower.get(value.lower())


def _distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _typo(value: str) -> str | None:
    if value in INCLUSION_MODES or _case(value) is not None:
        return None
    near = [m for m in INCLUSION_MODES if _distance(value.lower(), m.lower()) <= 2]
    return near[0] if len(near) == 1 else None


def _quote_glob(header: str) -> str:
    """``fileMatchPattern: **/*.py`` is a YAML alias error; quote the glob."""
    return re.sub(
        r"^(fileMatchPattern\s*:\s*)(\*[^\n\"']*?)[ \t]*$",
        lambda m: f'{m.group(1)}"{m.group(2)}"',
        header,
        flags=re.MULTILINE,
    )


def _comma_list(header: str) -> str:
    def replace(match: re.Match[str]) -> str:
        items = [p.strip() for p in match.group(3).split(",") if p.strip()]
        return f"{match.group(1)}[" + ", ".join(f'"{i}"' for i in items) + "]"

    return re.sub(
        r"^(fileMatchPattern\s*:\s*)([\"'])([^\"'\n]*,[^\"'\n]*)\2[ \t]*$",
        replace,
        header,
        count=1,
        flags=re.MULTILINE,
    )


def auto_name(text: str, ctx: FileContext) -> str:
    """inclusion: auto needs a name; derive it from the file name."""
    parts = _split(text)
    if parts is None:
        return text
    opening, header, closing, body = parts
    if not re.search(r"^inclusion\s*:\s*[\"']?auto[\"']?\s*$", header, re.MULTILINE):
        return text
    if re.search(r"^name\s*:", header, re.MULTILINE):
        return text
    stem = ctx.path.rsplit("/", 1)[-1].removesuffix(".md")
    return opening + header + f"name: {stem}\n" + closing + body


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def move_to_metadata(text: str, ctx: FileContext) -> str:
    """Move organisational keys (owner, tags, version, ...) into ``metadata`` as strings."""
    del ctx
    parts = _split(text)
    if parts is None:
        return text
    opening, header, closing, body = parts
    yaml = _yaml()
    try:
        data = yaml.load(header)
    except Exception:
        return text
    if not isinstance(data, CommentedMap):
        return text
    extra = [key for key in data if key not in NATIVE_KEYS]
    if not extra:
        return text
    metadata = data.get("metadata")
    if metadata is None:
        metadata = CommentedMap()
        data["metadata"] = metadata
    if not isinstance(metadata, dict):
        return text
    for key in extra:
        value = data.pop(key)
        metadata[key] = ",".join(str(v) for v in value) if isinstance(value, list) else str(value)
    out = io.StringIO()
    yaml.dump(data, out)
    return opening + out.getvalue() + closing + body


def skill_name_case(text: str, ctx: FileContext) -> str:
    """Lowercase a SKILL.md name with single hyphens; never renames the directory."""
    del ctx

    def edit(header: str) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(3)
            fixed = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
            return f"{match.group(1)}{match.group(2)}{fixed}{match.group(2)}"

        return re.sub(
            r"^(name\s*:\s*)([\"']?)([^\"'\n]+?)\2[ \t]*$",
            replace,
            header,
            count=1,
            flags=re.MULTILINE,
        )

    return _on_front_matter(edit)(text, FileContext("", "skill"))


FIXERS: tuple[Fixer, ...] = (
    Fixer("file-bom", TEXT_PROFILES, strip_bom, "strip the UTF-8 byte order mark"),
    Fixer("file-eol", TEXT_PROFILES, to_lf, "CRLF line endings to LF"),
    Fixer(
        "fm-leading-blank",
        frozenset({"steering", "skill", "agent", "knowledge"}),
        leading_blank,
        "remove blank lines before the opening ---",
    ),
    Fixer(
        "fm-key-case",
        frozenset({"steering"}),
        _on_front_matter(_key_case),
        "spell fileMatchPattern the way Kiro reads it",
        frozenset({"fileMatchPattern", *_KEY_ALIASES}),
    ),
    Fixer(
        "fm-inclusion-case",
        frozenset({"steering"}),
        _on_front_matter(lambda h: _inclusion_value(h, _case)),
        "inclusion in its exact case",
        frozenset({"inclusion"}),
    ),
    Fixer(
        "fm-inclusion-typo",
        frozenset({"steering"}),
        _on_front_matter(lambda h: _inclusion_value(h, _typo)),
        "correct a misspelt inclusion mode (changes how the file loads)",
        frozenset({"inclusion"}),
    ),
    Fixer(
        "fm-quote-glob",
        frozenset({"steering"}),
        _on_front_matter(_quote_glob),
        "quote a glob that made the front matter invalid YAML",
        frozenset({"fileMatchPattern"}),
    ),
    Fixer(
        "fm-filematch-comma",
        frozenset({"steering"}),
        _on_front_matter(_comma_list),
        "split a comma separated fileMatchPattern into a list (activates the file)",
        frozenset({"fileMatchPattern"}),
    ),
    Fixer(
        "fm-auto-name",
        frozenset({"steering"}),
        auto_name,
        "derive a missing name for inclusion: auto from the file name",
        frozenset({"name"}),
    ),
    Fixer(
        "fm-move-to-metadata",
        frozenset({"steering"}),
        move_to_metadata,
        "move organisational keys into metadata as strings (may lose comments)",
        frozenset({"*"}),
    ),
    Fixer(
        "skill-name-case",
        frozenset({"skill"}),
        skill_name_case,
        "lowercase the skill name (the directory is never renamed)",
        frozenset({"name"}),
    ),
)
