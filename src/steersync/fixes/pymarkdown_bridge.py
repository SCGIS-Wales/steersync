"""PyMarkdown's fix mode on the Markdown body only.

Front matter is cut off, PyMarkdown fixes the body with only the rules of one fixer
enabled, and the front matter is put back byte for byte. A rule is used only when the
installed PyMarkdown says, through its plugin metadata, that it can fix it (its rules page
and changelog disagree on several rules), so a PyMarkdown upgrade can never make steersync
call a fix that does not exist. ``pymarkdownlnt`` is pinned exactly; the golden tests
catch behaviour changes on upgrades.
"""

import functools
import importlib
import inspect
import pkgutil
import re
from collections.abc import Callable

from steersync.fixes.model import FileContext, Fixer

MARKDOWN_PROFILES = frozenset({"steering", "markdown", "spec", "knowledge", "skill", "prompt"})
_FM_RE = re.compile(r"\A(---[ \t]*\n.*?\n---[ \t]*\n)", re.DOTALL)


@functools.cache
def _plugins() -> tuple[tuple[str, bool], ...]:
    """(rule id, supports fix) for every rule plugin of the installed PyMarkdown."""
    import pymarkdown.plugins  # noqa: PLC0415 - slow import, only for --fix
    from pymarkdown.plugin_manager.rule_plugin import RulePlugin  # noqa: PLC0415

    found: list[tuple[str, bool]] = []
    for module_info in pkgutil.iter_modules(pymarkdown.plugins.__path__):
        module = importlib.import_module(f"pymarkdown.plugins.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            own = cls.__module__ == module.__name__
            if own and issubclass(cls, RulePlugin) and cls is not RulePlugin:
                details = cls().get_details()
                fix = bool(getattr(details, "plugin_supports_fix", False))
                found.append((details.plugin_id.upper(), fix))
    return tuple(sorted(found))


def fixable_rules() -> frozenset[str]:
    """Rule ids the installed PyMarkdown reports fix support for (plugin_supports_fix)."""
    return frozenset(rule for rule, fix in _plugins() if fix)


def _all_rules() -> tuple[str, ...]:
    return tuple(rule for rule, _ in _plugins())


def fix_body(body: str, rules: frozenset[str]) -> str:
    """Run PyMarkdown fix mode with only ``rules`` enabled."""
    from pymarkdown.api import PyMarkdownApi, PyMarkdownApiException  # noqa: PLC0415

    usable = rules & fixable_rules()
    if not usable or not body.strip():
        return body
    api = PyMarkdownApi()
    for rule in _all_rules():
        if rule not in usable:
            api = api.disable_rule_by_identifier(rule.lower())
    for rule in usable:
        api = api.enable_rule_by_identifier(rule.lower())
    # Hard tabs inside fenced code are content, never touched.
    api = api.set_boolean_property("plugins.md010.code_blocks", False)
    try:
        result = api.fix_string(body)
    except PyMarkdownApiException:
        return body
    return result.fixed_file if result.was_fixed else body


def _on_body(rules: frozenset[str]) -> Callable[[str, FileContext], str]:
    def apply(text: str, ctx: FileContext) -> str:
        if not ctx.path.endswith(".md"):
            return text
        match = _FM_RE.match(text)
        head = match.group(1) if match else ""
        body = text[len(head) :]
        fixed = fix_body(body, rules)
        return head + fixed if fixed != body else text

    return apply


def fixer(
    rule_id: str,
    md_rules: frozenset[str],
    summary: str,
    *,
    demotable: bool = False,
    changes_markdown: bool = False,
) -> Fixer:
    return Fixer(
        rule_id,
        MARKDOWN_PROFILES,
        _on_body(md_rules),
        summary,
        demotable=demotable,
        changes_markdown=changes_markdown,
    )


FIXERS: tuple[Fixer, ...] = (
    fixer("md-final-newline", frozenset({"MD047"}), "end the file with one newline"),
    fixer("md-trailing-space", frozenset({"MD009"}), "remove trailing spaces (keeps hard breaks)"),
    # A trailing tab becomes two spaces, a hard line break: safe only while the rendering
    # is unchanged.
    fixer(
        "md-hard-tabs",
        frozenset({"MD010"}),
        "spaces instead of tabs outside code",
        demotable=True,
    ),
    fixer("md-ul-style", frozenset({"MD004"}), "one bullet style", demotable=True),
    fixer("md-hr-style", frozenset({"MD035"}), "one horizontal rule style", demotable=True),
    fixer("md-fence-style", frozenset({"MD048"}), "one code fence style", demotable=True),
    fixer(
        "md-heading-space",
        frozenset({"MD019", "MD021", "MD023"}),
        "one space after # and headings at the left margin",
    ),
    fixer("md-list-space", frozenset({"MD030"}), "one space after list markers"),
    fixer("md-quote-space", frozenset({"MD027"}), "one space after > in block quotes"),
    fixer(
        "md-list-indent",
        frozenset({"MD005", "MD007"}),
        "consistent list indentation",
        demotable=True,
    ),
    # Unsafe: they change the outline, the numbering or the block form on purpose.
    fixer(
        "md-heading-increment",
        frozenset({"MD001"}),
        "headings step one level at a time (changes the outline)",
        changes_markdown=True,
    ),
    fixer("md-ol-prefix", frozenset({"MD029"}), "renumber ordered lists", changes_markdown=True),
    fixer(
        "md-code-style",
        frozenset({"MD046"}),
        "one code block style (fenced)",
        changes_markdown=True,
    ),
)
