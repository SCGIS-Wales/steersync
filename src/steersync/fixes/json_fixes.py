"""JSON formatting: two space indent, key order kept, one final newline; data unchanged."""

import json

from steersync.fixes.model import FileContext, Fixer

JSON_PROFILES = frozenset({"agent", "hooks", "settings", "json", "workflow"})


def json_format(text: str, ctx: FileContext) -> str:
    if not ctx.path.endswith(".json"):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text  # broken JSON is reported, never guessed at
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


FIXERS: tuple[Fixer, ...] = (
    Fixer("json-format", JSON_PROFILES, json_format, "reindent JSON with a final newline"),
)
