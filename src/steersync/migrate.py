"""Convert legacy Kiro hooks into standalone V3 hook files (``.kiro/hooks/*.json``).

Two legacy shapes are converted:

* IDE 0.x ``*.kiro.hook`` files (``when``/``then``), which IDE 1.0 no longer runs.
* CLI 2.x inline agent hooks in object form (``"hooks": {"agentSpawn": [...]}``).

Conversion is pure: :func:`plan_migrations` returns what would be written and
:func:`apply_migrations` writes it, never overwriting an existing file and never
deleting the source. Standalone hooks apply to every agent, unlike inline agent
hooks, so the plan says which ones came from an agent.
"""

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from steersync.kiro_rules import HOOK_TRIGGER_ALIASES

# IDE 0.x ``when.type`` and the V3 trigger it became.
LEGACY_WHEN: Mapping[str, str] = {
    "fileEdited": "PostFileSave",
    "fileSaved": "PostFileSave",
    "fileCreated": "PostFileCreate",
    "fileDeleted": "PostFileDelete",
    "promptSubmit": "UserPromptSubmit",
    "userPromptSubmit": "UserPromptSubmit",
    "agentStop": "Stop",
    "userTriggered": "Manual",
    "manual": "Manual",
}
LEGACY_THEN_AGENT = frozenset({"askAgent", "agent"})
LEGACY_THEN_COMMAND = frozenset({"runCommand", "shellCommand", "command"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class Migration(NamedTuple):
    source: Path
    target: Path
    hooks: tuple[dict[str, Any], ...]
    from_agent: bool
    skipped: tuple[str, ...]

    def render(self) -> str:
        return json.dumps({"version": "v1", "hooks": list(self.hooks)}, indent=2) + "\n"


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "hook"


def convert_legacy_hook(
    data: Mapping[str, Any], fallback_name: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert one IDE 0.x ``.kiro.hook`` definition; returns hooks and skip reasons."""
    when = _mapping(data.get("when"))
    then = _mapping(data.get("then"))
    name = str(data.get("name") or fallback_name)
    trigger = LEGACY_WHEN.get(str(when.get("type")))
    if trigger is None:
        return [], [f"{name}: unknown when.type {when.get('type')!r}"]
    then_type = str(then.get("type"))
    if then_type in LEGACY_THEN_AGENT and then.get("prompt"):
        action: dict[str, Any] = {"type": "agent", "prompt": then["prompt"]}
    elif then_type in LEGACY_THEN_COMMAND and then.get("command"):
        action = {"type": "command", "command": then["command"]}
    else:
        return [], [f"{name}: unsupported then.type {then.get('type')!r}"]
    base: dict[str, Any] = {"name": _slug(name), "trigger": trigger, "action": action}
    if isinstance(data.get("description"), str):
        base["description"] = data["description"]
    if isinstance(data.get("enabled"), bool):
        base["enabled"] = data["enabled"]
    patterns = [p for p in when.get("patterns") or [] if isinstance(p, str)]
    if len(patterns) <= 1:
        return [{**base, **({"matcher": patterns[0]} if patterns else {})}], []
    return [
        {**base, "name": f"{base['name']}-{index + 1}", "matcher": pattern}
        for index, pattern in enumerate(patterns)
    ], []


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def convert_agent_hooks(
    hooks: Mapping[str, Any], agent: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert CLI 2.x object form agent hooks to standalone V3 hooks."""
    converted: list[dict[str, Any]] = []
    skipped: list[str] = []
    for legacy_trigger, entries in hooks.items():
        trigger = HOOK_TRIGGER_ALIASES.get(legacy_trigger, legacy_trigger)
        items = entries if isinstance(entries, list) else [entries]
        for index, entry in enumerate(items):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("command"), str):
                skipped.append(f"{agent}: hooks.{legacy_trigger}[{index}] has no command")
                continue
            hook: dict[str, Any] = {
                "name": f"{_slug(agent)}-{_slug(trigger)}-{index + 1}",
                "trigger": trigger,
                "action": {"type": "command", "command": entry["command"]},
            }
            if isinstance(entry.get("matcher"), str):
                hook["matcher"] = entry["matcher"]
            timeout_ms = entry.get("timeout_ms")
            if isinstance(timeout_ms, int) and not isinstance(timeout_ms, bool):
                hook["timeout"] = math.ceil(timeout_ms / 1000)
            converted.append(hook)
    return converted, skipped


def plan_migrations(root: Path, *, hooks_dir: str, agents_dir: str) -> list[Migration]:
    """Find legacy hooks under ``root`` and plan their standalone replacements."""
    target_dir = root / hooks_dir
    plans: list[Migration] = []
    for source in sorted(root.glob("**/*.kiro.hook")):
        if any(part in {".git", "node_modules"} for part in source.parts):
            continue
        data = _load(source)
        if data is None:
            plans.append(Migration(source, source, (), False, (f"{source.name}: not valid JSON",)))
            continue
        stem = source.name.removesuffix(".kiro.hook")
        hooks, skipped = convert_legacy_hook(data, stem)
        plans.append(
            Migration(
                source, target_dir / f"{_slug(stem)}.json", tuple(hooks), False, tuple(skipped)
            )
        )
    agents = root / agents_dir
    for source in sorted(agents.glob("*.json")) if agents.is_dir() else []:
        data = _load(source)
        hooks_value = data.get("hooks") if data is not None else None
        if not isinstance(hooks_value, Mapping):
            continue
        agent = str(data.get("name") or source.stem) if data is not None else source.stem
        hooks, skipped = convert_agent_hooks(hooks_value, agent)
        plans.append(
            Migration(
                source,
                target_dir / f"agent-{_slug(agent)}.json",
                tuple(hooks),
                True,
                tuple(skipped),
            )
        )
    return plans


def _load(path: Path) -> Mapping[str, Any] | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, Mapping) else None


def apply_migrations(plans: list[Migration]) -> list[tuple[Migration, str]]:
    """Write each planned hook file that does not exist yet; returns (plan, outcome)."""
    outcomes: list[tuple[Migration, str]] = []
    for plan in plans:
        if not plan.hooks:
            outcomes.append((plan, "skipped"))
        elif plan.target.exists():
            outcomes.append((plan, "exists"))
        else:
            plan.target.parent.mkdir(parents=True, exist_ok=True)
            plan.target.write_text(plan.render(), encoding="utf-8")
            outcomes.append((plan, "written"))
    return outcomes
