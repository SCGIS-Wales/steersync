"""The fix catalogue: every fixer, with its safety from configuration.

``lint.fix.safety`` gives each fix its default class (safe, unsafe, never);
``extend_safe_fixes`` and ``extend_unsafe_fixes`` move fixes between classes and
``unfixable`` turns fixes off.
"""

from collections.abc import Sequence

from steersync.config import FixConfig
from steersync.fixes import frontmatter_fixes, json_fixes, markdown_fixes, pymarkdown_bridge
from steersync.fixes.model import Fixer, Safety

ALL_FIXERS: tuple[Fixer, ...] = (
    *frontmatter_fixes.FIXERS,
    *json_fixes.FIXERS,
    *pymarkdown_bridge.FIXERS,
    *markdown_fixes.FIXERS,
)
BRIDGE_IDS = frozenset(f.rule_id for f in pymarkdown_bridge.FIXERS)


def _matches(rule: str, names: Sequence[str]) -> bool:
    return any(rule == n or rule.startswith(n) for n in names)


def safety_of(rule: str, config: FixConfig) -> Safety:
    if _matches(rule, config.unfixable):
        return "never"
    if _matches(rule, config.extend_unsafe_fixes):
        return "unsafe"
    if _matches(rule, config.extend_safe_fixes):
        return "safe"
    return config.safety.get(rule, "never")


def catalogue(
    config: FixConfig, *, select: Sequence[str] = (), ignore: Sequence[str] = ()
) -> list[tuple[Fixer, Safety]]:
    """Fixers in catalogue order with their safety; --select and --ignore take prefixes."""
    chosen: list[tuple[Fixer, Safety]] = []
    for fixer in ALL_FIXERS:
        if config.engine == "builtin" and fixer.rule_id in BRIDGE_IDS:
            continue
        if select and not _matches(fixer.rule_id, select):
            continue
        if ignore and _matches(fixer.rule_id, ignore):
            continue
        safety = safety_of(fixer.rule_id, config)
        if safety != "never":
            chosen.append((fixer, safety))
    return chosen
