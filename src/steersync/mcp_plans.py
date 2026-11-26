"""Plans and fixes for the MCP server: propose, then apply after one approval.

``plan_setup``, ``plan_fleet`` and ``propose_fixes`` compute everything and write nothing;
each returns a ``plan_id``. ``apply_plan`` and ``apply_fixes`` recompute from the same
inputs and refuse a stale ``plan_id`` (``stale_plan``), so the agent never applies
something the user did not see. The registry lives in this server process only.
"""

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field

from steersync import backups, cli_fix, fleet, fleet_plan, registry, setup_plan
from steersync import setup as setup_steps
from steersync.errors import ConfigError, SteersyncError
from steersync.fixes import engine
from steersync.interaction import Interaction
from steersync.mcp_tools import _Report, load
from steersync.paths import backup_root, registry_path
from steersync.plan import Plan, default_selection, pending, plan_hash, run_steps


class StalePlanError(SteersyncError):
    """The plan changed since it was shown; propose it again."""


class StepInfo(_Report):
    id: str
    title: str
    detail: str
    target: str
    risk: Literal["safe", "review", "destructive"]
    default: bool
    done: bool


class PlanReport(_Report):
    plan_id: str = Field(description="Pass to apply_plan; it is refused if the plan changed")
    command: str
    steps: list[StepInfo]
    notes: list[str]
    nothing_to_do: bool


class StepResultInfo(_Report):
    step_id: str
    ok: bool
    detail: str


class ApplyReport(_Report):
    plan_id: str
    applied: list[str]
    results: list[StepResultInfo]
    backup_id: str | None
    undo: str | None = Field(description="The command that undoes this run")


class FixFileInfo(_Report):
    path: str
    rules: list[str]
    safety: Literal["safe", "unsafe", "never"]
    diff: str


class FixOfferInfo(_Report):
    path: str
    rule: str
    summary: str
    choice: str = Field(description="Pass this in unsafe_rule_ids to apply the fix")


class FixProposal(_Report):
    plan_id: str
    fixes: list[FixFileInfo] = Field(description="Safe fixes, applied by apply_fixes")
    offers: list[FixOfferInfo] = Field(description="Unsafe fixes, applied only when chosen")
    failed: list[str]
    skipped: list[str]


class FixApplyReport(_Report):
    plan_id: str
    applied: list[str]
    backup_id: str | None
    undo: str | None
    errors: int
    warnings: int


# --- plan registry: plan_id -> how to rebuild it -------------------------------------------

_PLANS: dict[str, tuple[str, dict[str, object]]] = {}
_NO_INPUT = Interaction(False, False, False, False, "json")


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "absent"


def _identify(plan: Plan) -> str:
    inputs = {str(p): _digest(p) for step in plan.steps for p in step.paths}
    return plan_hash(plan, inputs)


def _setup(cwd: str | None) -> tuple[Plan, list[str]]:
    config, root = load(cwd)
    if not config.remote.url:
        raise ConfigError("no central repository is configured; run `steersync setup` first")
    check = setup_steps.probe_remote(config, config.remote.url, os.environ)
    if not check.reachable:
        raise SteersyncError(f"the central repository is not reachable: {check.error}")
    branch = (
        config.remote.default_branch
        if config.remote.default_branch in check.branches
        else setup_steps.suggested_branch(check)
    )
    candidates = setup_steps.candidate_urls(config.remote.url)
    local = config.remote.allow_local or bool(candidates and candidates[0].local)
    state = setup_plan.detect_setup_state(
        os.environ, current=config, root=root, remote=check, local=local, branch=branch
    )
    scope: setup_plan.Scope = "all" if root is not None else "machine"
    return setup_plan.build_setup_plan(state, scope), setup_plan.notes(state)


def _fleet(
    action: fleet_plan.Action, repos: Sequence[str], states: Sequence[str]
) -> tuple[Plan, list[str]]:
    report = fleet.collect(registry.load(registry_path(os.environ)).paths(), os.environ)
    wanted = {str(Path(r).expanduser().resolve()) for r in repos}
    rows = [
        r
        for r in report.repositories
        if (not wanted and not states) or r.path in wanted or r.state in states
    ]
    home = str(Path.home())
    sink: list[fleet.Outcome] = []
    plan = fleet_plan.build_fleet_plan(action, rows, os.environ, sink=sink, home=home)
    return plan, fleet_plan.skipped(action, rows, home)


def _cwd(params: dict[str, object]) -> str | None:
    value = params.get("cwd")
    return value if isinstance(value, str) else None


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def _rebuild(kind: str, params: dict[str, object]) -> tuple[Plan, list[str]]:
    if kind == "setup":
        return _setup(_cwd(params))
    action: fleet_plan.Action = "push" if params.get("action") == "push" else "pull"
    return _fleet(action, _strings(params.get("repos")), _strings(params.get("states")))


def _report(plan: Plan, notes: list[str], kind: str, params: dict[str, object]) -> PlanReport:
    plan_id = _identify(plan)
    _PLANS[plan_id] = (kind, params)
    return PlanReport(
        plan_id=plan_id,
        command=plan.command,
        steps=[
            StepInfo(
                id=s.id,
                title=s.title,
                detail=s.detail,
                target=s.target,
                risk=s.risk,
                default=s.default,
                done=s.done,
            )
            for s in plan.steps
        ],
        notes=notes,
        nothing_to_do=not pending(plan),
    )


def plan_setup(cwd: str | None = None) -> PlanReport:
    params: dict[str, object] = {"cwd": cwd}
    plan, notes = _rebuild("setup", params)
    return _report(plan, notes, "setup", params)


def plan_fleet(
    action: fleet_plan.Action, repos: Sequence[str] = (), states: Sequence[str] = ()
) -> PlanReport:
    params: dict[str, object] = {"action": action, "repos": list(repos), "states": list(states)}
    plan, notes = _rebuild("fleet", params)
    return _report(plan, notes, "fleet", params)


def current_plan(plan_id: str) -> Plan:
    """The plan behind ``plan_id`` rebuilt now; StalePlanError when anything changed."""
    if plan_id not in _PLANS:
        raise StalePlanError("stale_plan: unknown plan_id; call plan_setup or plan_fleet again")
    kind, params = _PLANS[plan_id]
    plan, _ = _rebuild(kind, params)
    if _identify(plan) != plan_id:
        raise StalePlanError("stale_plan: the plan changed since it was shown; plan it again")
    return plan


def apply_plan(
    plan_id: str, step_ids: Sequence[str] | None, *, allow_destructive: bool = False
) -> ApplyReport:
    plan = current_plan(plan_id)
    risk = {s.id: s.risk for s in pending(plan)}
    if step_ids is None:
        chosen = default_selection(plan, _NO_INPUT)
    else:
        unknown = sorted(set(step_ids) - set(risk))
        if unknown:
            raise ConfigError(f"unknown or finished steps: {', '.join(unknown)}")
        chosen = list(step_ids)
    if not allow_destructive:
        chosen = [i for i in chosen if risk[i] != "destructive"]
    paths = list(dict.fromkeys(p for s in plan.steps if s.id in chosen for p in s.paths))
    stamp = (
        backups.take_files(backup_root(os.environ), paths, label=plan.command).stamp
        if paths
        else None
    )
    results = run_steps(plan, chosen)
    return ApplyReport(
        plan_id=plan_id,
        applied=chosen,
        results=[StepResultInfo(step_id=r.step_id, ok=r.ok, detail=r.detail) for r in results],
        backup_id=stamp,
        undo=f"steersync restore {stamp}" if stamp else None,
    )


# --- fixes ------------------------------------------------------------------------------------


def _fix_plan(
    params: dict[str, object], unsafe: dict[str, frozenset[str]] | None = None
) -> tuple[engine.FixPlan, list[engine.Target]]:
    config, root = load(_cwd(params))
    paths = [Path(p).expanduser() for p in _strings(params.get("paths"))]
    found = cli_fix.targets(
        config,
        root,
        paths,
        list(config.enabled_collections().values()),
        cli_fix.lookup(config, root, (), None),
        None,
    )
    select = _strings(params.get("select"))
    plan = engine.plan_fixes(
        found,
        config.lint,
        relint=cli_fix._relint(config, root),
        secret=cli_fix._secret(config, root),
        dirty=cli_fix._dirty(config, root),
        unsafe=unsafe,
        all_unsafe=bool(params.get("include_unsafe")),
        select=select,
    )
    return plan, found


def _fix_id(plan: engine.FixPlan, params: dict[str, object]) -> str:
    material = {
        "params": params,
        "fixes": [(f.path, f.original_sha256, engine.sha256(f.fixed)) for f in plan.fixes],
        "offers": [tuple(o) for o in plan.offers],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


_FIXES: dict[str, dict[str, object]] = {}


def propose_fixes(
    cwd: str | None = None,
    paths: Sequence[str] = (),
    select: Sequence[str] | None = None,
    *,
    include_unsafe: bool = False,
) -> FixProposal:
    params: dict[str, object] = {
        "cwd": cwd,
        "paths": list(paths),
        "select": list(select or []),
        "include_unsafe": include_unsafe,
    }
    plan, _ = _fix_plan(params)
    plan_id = _fix_id(plan, params)
    _FIXES[plan_id] = params
    return FixProposal(
        plan_id=plan_id,
        fixes=[
            FixFileInfo(path=f.path, rules=list(f.rules), safety=f.safety, diff=f.diff)
            for f in plan.fixes
        ],
        offers=[
            FixOfferInfo(
                path=o.label, rule=o.rule, summary=o.summary, choice=f"{o.label}::{o.rule}"
            )
            for o in plan.offers
        ],
        failed=list(plan.failed),
        skipped=list(plan.skipped),
    )


def fix_offers(plan_id: str) -> list[str]:
    """The unsafe choices of a proposal (``path::rule``), for the approval form."""
    params = _FIXES.get(plan_id)
    if params is None:
        return []
    plan, _ = _fix_plan(params)
    return [f"{o.label}::{o.rule}" for o in plan.offers]


def apply_fixes(
    plan_id: str, unsafe_rule_ids: Sequence[str] = (), *, apply_safe: bool = True
) -> FixApplyReport:
    params = _FIXES.get(plan_id)
    if params is None:
        raise StalePlanError("stale_plan: unknown plan_id; call propose_fixes again")
    current, _ = _fix_plan(params)
    if _fix_id(current, params) != plan_id:
        raise StalePlanError("stale_plan: the files changed since the fixes were proposed")
    chosen: dict[str, set[str]] = {}
    for choice in unsafe_rule_ids:
        path, _, rule = choice.rpartition("::")
        chosen.setdefault(path, set()).add(rule)
    plan, found = _fix_plan(params, {k: frozenset(v) for k, v in chosen.items()})
    fixes = [f for f in plan.fixes if apply_safe or f.safety == "unsafe" or f.path in chosen]
    stamp, _changed = engine.apply_fixes(
        fixes, {t.label: t.path for t in found}, backup_root(os.environ)
    )
    config, root = load(_cwd(params))
    errors = warnings = 0
    for target in found:
        counts = cli_fix._severities(config, root, target)
        errors += counts["error"]
        warnings += counts["warning"]
    return FixApplyReport(
        plan_id=plan_id,
        applied=[f.path for f in fixes],
        backup_id=stamp,
        undo=f"steersync restore {stamp}" if stamp else None,
        errors=errors,
        warnings=warnings,
    )
