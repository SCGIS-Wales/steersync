"""MCP stdio server exposing steersync operations as tools.

Requires the ``mcp`` extra: ``uvx --from "steersync[mcp]" steersync mcp`` (or the
``steersync-mcp`` alias). Built on the official SDK v2 ``MCPServer`` (MCP specification
2026-07-28). The logic lives in :mod:`steersync.mcp_tools`; this module adapts it:

* every tool returns a typed pydantic report, published as its output schema and
  returned as structured content;
* failures come back as tool results with ``isError`` set (``ToolError``), carrying the
  message the agent should show the user;
* pull and push report progress while they run;
* a forced pull, which overwrites local edits, asks the user through elicitation when
  the client supports it (multi round trip requests on 2026-07-28), and otherwise
  requires ``confirm: true``;
* tools carry titles and read only, destructive and idempotent hints, and logs go to
  stderr because protocol logging is deprecated in 2026-07-28.

The server description and each tool description are written for the agent that
reads them, so a session in any repository can act on a plain sentence.
"""

import functools
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Annotated, Any, Literal, NamedTuple

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server import MCPServer
from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, JsonValue, create_model

from steersync import __version__, mcp_plans, mcp_tools
from steersync.errors import SteersyncError
from steersync.logging_utils import configure_logging
from steersync.plan import default_selection, pending

log = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """\
steersync keeps the Kiro configuration of this workspace (.kiro/: steering, specs, \
skills, agents, hooks, settings, sandbox policy, workflow recipes, prompts, AGENTS.md \
files and .kiroignore) and of this machine (~/.kiro/) in sync with a central git \
repository where every change is reviewed as a merge request. Per user files such as \
workspace permissions, memories, sessions and powers are never synced.

Use these tools when the user talks about Kiro configuration, steering, specs, skills, \
agents, hooks, prompts or settings being pulled, pushed, synced, proposed, validated or \
reviewed:
- pull_kiro_config: "pull/sync/refresh/update the kiro config (steering, skills, ...)" \
from the central repository into this workspace and ~/.kiro. Safe to run at any time; \
local edits are kept and reported.
- push_kiro_config: "push/propose/sync up/share my kiro config / steering / skills" to the \
central repository. Lints first, then pushes one proposal branch for this project and \
opens or confirms its merge request.
- raise_merge_request: "raise/open/create an MR (merge request) for the kiro config". Use \
after push_kiro_config, or when the user asks for the merge request link.
- diff_kiro_config: "what changed / status / is my kiro config in sync / any open \
proposals". Read only.
- validate_kiro_config: "validate/lint/check the kiro config, steering, agents, hooks, \
skills" against Kiro's rules and best practices. Errors block a push; warnings are \
advice from the Kiro documentation.
- list_kiro_collections: which kinds of content are synced and where they live.
- doctor_kiro_config: "is steersync set up / why does sync fail": git, configuration, \
remote, token, directories and the Kiro CLI harness. Read only.
- fleet_status: "which of my repositories have unpushed or unpulled Kiro changes". Read \
only; covers every repository set up for steersync on this machine.
- kiro_check: "is my Kiro supported / do my agents need upgrading": kiro-cli version, \
agent engine and agent files against the V3 baseline. Read only; never starts a chat, so it \
costs no Kiro credits.
Results are structured JSON. A failed call is returned as an error result whose text the \
user should see verbatim.
"""

Cwd = Annotated[
    str | None,
    Field(description="Project directory (default: the directory the server runs in)"),
]
Collections = Annotated[
    list[str] | None,
    Field(
        description="Limit to these collections (steering, specs, skills, agents, hooks, "
        "settings, sandbox, workflows, prompts, agents-md, kiroignore); default: all enabled"
    ),
]


class ForceConfirmation(BaseModel):
    """What the user is asked before a forced pull overwrites local edits."""

    overwrite: bool = Field(
        description="Overwrite the locally modified Kiro files with the central versions"
    )


def _can_elicit(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def confirm_force(
    force: bool, dry_run: bool, confirm: bool, ctx: Context
) -> Elicit[ForceConfirmation] | bool:
    """Resolver: ask before a forced pull when the client can be asked."""
    if not force or dry_run or confirm:
        return True
    if _can_elicit(ctx):
        return Elicit(
            "A forced pull overwrites Kiro files you edited locally with the central "
            "versions. Overwrite them?",
            ForceConfirmation,
        )
    return False


def _approved(outcome: ElicitationResult[ForceConfirmation]) -> bool:
    if not isinstance(outcome, AcceptedElicitation):
        return False
    data: object = outcome.data
    return data is True or (isinstance(data, ForceConfirmation) and data.overwrite)


def _progress_bridge(ctx: Context) -> mcp_tools.Progress:
    """Forward progress from the worker thread to the client (a no op without a token)."""

    def report(done: int, total: int, message: str) -> None:
        try:
            anyio.from_thread.run(ctx.report_progress, done, total or None, message)
        except Exception:
            log.debug("progress notification failed", exc_info=True)

    return report


async def _run[T](work: Callable[[], T]) -> T:
    """Run blocking steersync work off the event loop; failures become tool errors."""
    try:
        return await anyio.to_thread.run_sync(work)
    except (SteersyncError, OSError) as exc:
        raise ToolError(str(exc)) from exc


async def pull_kiro_config(
    ctx: Context,
    approval: Annotated[ElicitationResult[ForceConfirmation], Resolve(confirm_force)],
    cwd: Cwd = None,
    force: Annotated[
        bool, Field(description="Overwrite locally modified files (asks the user first)")
    ] = False,
    dry_run: Annotated[bool, Field(description="Show the plan without writing")] = False,
    collections: Collections = None,
    confirm: Annotated[
        bool,
        Field(
            description="The user already agreed to overwrite local edits; needed for force "
            "when the client cannot show a confirmation prompt"
        ),
    ] = False,
) -> mcp_tools.PullReport:
    """Pull the Kiro configuration from the central repository into this workspace and ~/.kiro.

    Use for "pull / sync / refresh / update the kiro config (steering, specs, skills,
    agents, hooks, settings, prompts)". Files edited locally are kept and listed under
    "conflict" unless force is true; a forced pull asks the user first. dry_run shows
    the plan without writing. If the central repository is unreachable the last cached
    copy is applied and "offline" is true.
    """
    del confirm  # read by the confirm_force resolver
    if not _approved(approval):
        raise ToolError(
            "a forced pull overwrites locally modified files and the user did not agree; "
            "ask the user, then call again with confirm=true, or use dry_run=true to preview"
        )
    work = functools.partial(
        mcp_tools.pull,
        cwd,
        force=force,
        dry_run=dry_run,
        collections=collections,
        progress=_progress_bridge(ctx),
    )
    return await _run(work)


async def push_kiro_config(
    ctx: Context,
    cwd: Cwd = None,
    dry_run: Annotated[bool, Field(description="Compute the proposal without pushing")] = False,
    message: Annotated[str | None, Field(description="Commit message for the proposal")] = None,
    collections: Collections = None,
) -> mcp_tools.PushReport:
    """Propose this workspace's Kiro configuration to the central repository.

    Use for "push / propose / share / sync up my kiro config, steering, skills, agents".
    Lints every collection first (errors stop the push), then pushes one branch named
    after the project and the user and opens or confirms its merge request. The result
    carries "branch", "merge_request" (URL when confirmed through the GitLab API) and
    "existing_branches": other unmerged proposals for the same project that the user
    should review before adding more.
    """
    work = functools.partial(
        mcp_tools.push,
        cwd,
        dry_run=dry_run,
        message=message,
        collections=collections,
        progress=_progress_bridge(ctx),
    )
    return await _run(work)


async def raise_merge_request(
    cwd: Cwd = None,
    branch: Annotated[
        str | None, Field(description="Proposal branch (default: derived from local content)")
    ] = None,
    title: Annotated[str | None, Field(description="Merge request title")] = None,
    collections: Collections = None,
) -> mcp_tools.MergeRequestReport:
    """Open, or return, the GitLab merge request for the pushed Kiro configuration branch.

    Use for "raise / open / create an MR for the kiro config" or "give me the merge
    request link". Needs the GitLab token in the configured environment variable
    (GITLAB_TOKEN by default). Without branch the proposal branch is derived from the
    local content; it must already be on the central remote (push_kiro_config first).
    "others" lists other open merge requests for this project.
    """
    work = functools.partial(
        mcp_tools.merge_request, cwd, branch=branch, title=title, collections=collections
    )
    return await _run(work)


async def diff_kiro_config(
    cwd: Cwd = None, collections: Collections = None
) -> mcp_tools.DiffReport:
    """Compare the local Kiro configuration with the central repository, read only.

    Use for "status / what changed / is my kiro config in sync / any open proposals".
    "workspace" and "global" hold per collection plans; "proposals" lists this
    project's proposal branches still on the central remote.
    """
    return await _run(functools.partial(mcp_tools.diff, cwd, collections=collections))


async def validate_kiro_config(
    cwd: Cwd = None, collections: Collections = None
) -> mcp_tools.ValidateReport:
    """Validate the workspace Kiro configuration against Kiro's rules and best practices.

    Use for "validate / lint / check the kiro config, steering, agents, hooks, skills,
    prompts, settings, workflows". Each collection is checked with its own profile plus
    best practice warnings taken from the Kiro documentation. "ok" is false when an
    error would block a push; warnings are advice. With no collections the auto load
    checks (SessionStart hook, MCP entry) are included.
    """
    return await _run(functools.partial(mcp_tools.validate, cwd, collections=collections))


async def list_kiro_collections(cwd: Cwd = None) -> mcp_tools.CollectionsReport:
    """List the kinds of Kiro content steersync syncs and the directories involved."""
    return await _run(functools.partial(mcp_tools.list_collections, cwd))


async def doctor_kiro_config(cwd: Cwd = None) -> mcp_tools.DoctorReport:
    """Check git, configuration, the central remote, the GitLab token and directories.

    Use for "is steersync set up", "why does the kiro config sync fail", "where does
    steersync write". Includes the Kiro CLI harness status. Read only.
    """
    return await _run(functools.partial(mcp_tools.doctor, cwd))


async def fleet_status(
    refresh: Annotated[
        bool | None,
        Field(description="Refresh the central repositories first (default: when stale)"),
    ] = None,
) -> mcp_tools.FleetStatusReport:
    """Show, for every repository set up for steersync on this machine, what is pushed.

    Use for "which of my repositories have unpushed Kiro changes", "is everything in sync",
    "which projects are behind the central configuration". States: local-ahead (to push),
    proposed (pushed, awaiting merge), central-ahead (to pull), diverged (changed on both
    sides), in-sync, missing, unconfigured, error. Read only; pushing and pulling many
    repositories is done in the terminal with `steersync fleet`.
    """
    return await _run(functools.partial(mcp_tools.fleet_status, refresh))


async def kiro_check(cwd: Cwd = None) -> mcp_tools.KiroReport:
    """Check the Kiro installation against the supported V3 baseline.

    Use for "is my Kiro supported", "do my agents need upgrading", "why does Kiro rewrite
    my agent". Runs only `kiro-cli --version` and `kiro-cli settings`; never starts a chat.
    When "supported" is false, show "message" to the user.
    """
    return await _run(functools.partial(mcp_tools.kiro_check, cwd))


# --- one approval per call: plans and fixes -------------------------------------------------


def ask_once(ctx: Context, form: type[BaseModel], message: str) -> Elicit[Any] | bool:
    """The single question of a call: a form when the client can ask, else False.

    The SDK sends it as a multi round trip request on 2026-07-28 connections and as an
    elicitation on earlier ones. When the client cannot ask, the tool answers with how to
    confirm (show the plan to the user, then call again with confirm=true and the same
    plan_id). Never request secrets in a form.
    """
    return Elicit(message, form) if _can_elicit(ctx) else False


def _choices(values: list[str], default: list[str], description: str) -> tuple[object, object]:
    """A multi select field: an array of strings whose items are an inline enum.

    A Literal of one value renders as const and an Enum as a $ref, neither of which an
    elicitation form accepts; the values are checked again when the plan is applied.
    """
    enum: list[JsonValue] = list(values)
    extra: dict[str, JsonValue] | None = (
        {"items": {"type": "string", "enum": enum}} if values else None
    )
    return (list[str], Field(default=default, description=description, json_schema_extra=extra))


def _plan_form(step_ids: list[str], defaults: list[str], destructive: bool) -> type[BaseModel]:
    fields: dict[str, Any] = {
        "approve": (bool, Field(description="Apply the selected steps")),
        "steps": _choices(step_ids, defaults, "Steps to apply"),
    }
    if destructive:
        fields["allow_destructive"] = (
            bool,
            Field(default=False, description="Also run steps no backup covers"),
        )
    return create_model("PlanApproval", **fields)


def ask_plan(plan_id: str, confirm: bool, ctx: Context) -> Elicit[Any] | bool:
    """Resolver: rebuild the plan and ask once which steps to apply."""
    if confirm:
        return True
    try:
        plan = mcp_plans.current_plan(plan_id)
    except SteersyncError:
        return True  # the tool reports the stale plan
    steps = pending(plan)
    defaults = default_selection(plan, mcp_plans._NO_INPUT)
    form = _plan_form([s.id for s in steps], defaults, any(s.risk == "destructive" for s in steps))
    listed = "; ".join(f"{s.id} ({s.risk}): {s.title}" for s in steps)
    return ask_once(ctx, form, f"Apply these {plan.command} steps? {listed}")


def ask_fixes(plan_id: str, confirm: bool, ctx: Context) -> Elicit[Any] | bool:
    """Resolver: ask once whether to apply the safe fixes and which unsafe ones."""
    if confirm:
        return True
    offers = mcp_plans.fix_offers(plan_id)
    fields: dict[str, Any] = {
        "apply_safe": (bool, Field(default=True, description="Apply the safe fixes")),
        "unsafe": _choices(offers, [], "Unsafe fixes to apply too"),
    }
    return ask_once(ctx, create_model("FixApproval", **fields), "Apply the proposed fixes?")


def _answer(approval: object) -> dict[str, Any] | None:
    """The form data, {} for confirm=true, None when the user did not approve."""
    if not isinstance(approval, AcceptedElicitation):
        return None
    data: object = approval.data
    if data is True:
        return {}
    return data.model_dump(mode="json") if isinstance(data, BaseModel) else None


def _not_approved(ctx: Context) -> ToolError:
    if _can_elicit(ctx):
        return ToolError("the user did not approve; nothing was changed")
    return ToolError(
        "this client cannot ask the user: show the plan to the user, then call again with "
        "the same plan_id and confirm=true"
    )


async def plan_setup(cwd: Cwd = None) -> mcp_plans.PlanReport:
    """Show what `steersync setup` would change on this machine and repository; writes nothing.

    Returns the steps (safe, review, destructive), which are already done, and a plan_id
    for apply_plan.
    """
    return await _run(functools.partial(mcp_plans.plan_setup, cwd))


async def plan_fleet(
    action: Annotated[Literal["push", "pull"], Field(description="push or pull")],
    repos: Annotated[list[str] | None, Field(description="Repository paths")] = None,
    states: Annotated[list[str] | None, Field(description="States such as local-ahead")] = None,
) -> mcp_plans.PlanReport:
    """Plan a push or pull across repositories (one step each); writes nothing."""
    return await _run(functools.partial(mcp_plans.plan_fleet, action, repos or [], states or []))


async def apply_plan(
    ctx: Context,
    approval: Annotated[ElicitationResult[Any], Resolve(ask_plan)],
    plan_id: Annotated[str, Field(description="From plan_setup or plan_fleet")],
    step_ids: Annotated[
        list[str] | None, Field(description="Steps to apply (default: the default selection)")
    ] = None,
    confirm: Annotated[
        bool,
        Field(description="The user approved the shown plan; needed when the client cannot ask"),
    ] = False,
) -> mcp_plans.ApplyReport:
    """Apply a plan after one approval; refused with stale_plan when the plan changed."""
    del confirm  # read by the ask_plan resolver
    answer = _answer(approval)
    if answer is None or (answer and not answer.get("approve", False)):
        raise _not_approved(ctx)
    chosen = answer.get("steps", step_ids) if answer else step_ids
    return await _run(
        functools.partial(
            mcp_plans.apply_plan,
            plan_id,
            chosen,
            allow_destructive=bool(answer.get("allow_destructive", False)),
        )
    )


async def propose_fixes(
    cwd: Cwd = None,
    paths: Annotated[list[str] | None, Field(description="Files or folders (default: all)")] = None,
    select: Annotated[list[str] | None, Field(description="Only these fix ids")] = None,
    include_unsafe: Annotated[bool, Field(description="Compute unsafe fixes too")] = False,
) -> mcp_plans.FixProposal:
    """Run the lint --fix pipeline in memory: diffs, rules, safety and a plan_id; writes nothing."""
    return await _run(
        functools.partial(
            mcp_plans.propose_fixes, cwd, paths or [], select, include_unsafe=include_unsafe
        )
    )


async def apply_fixes(
    ctx: Context,
    approval: Annotated[ElicitationResult[Any], Resolve(ask_fixes)],
    plan_id: Annotated[str, Field(description="From propose_fixes")],
    unsafe_rule_ids: Annotated[
        list[str] | None, Field(description="Unsafe choices (path::rule) to apply too")
    ] = None,
    confirm: Annotated[
        bool,
        Field(description="The user approved the shown fixes; needed when the client cannot ask"),
    ] = False,
) -> mcp_plans.FixApplyReport:
    """Apply proposed fixes after one approval (backed up); stale_plan if files changed."""
    del confirm  # read by the ask_fixes resolver
    answer = _answer(approval)
    if answer is None:
        raise _not_approved(ctx)
    unsafe = answer.get("unsafe", unsafe_rule_ids or []) if answer else unsafe_rule_ids or []
    return await _run(
        functools.partial(
            mcp_plans.apply_fixes,
            plan_id,
            unsafe,
            apply_safe=bool(answer.get("apply_safe", True)) if answer else True,
        )
    )


class ToolSpec(NamedTuple):
    func: Callable[..., Awaitable[Any]]
    title: str
    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True


TOOL_SPECS: dict[str, ToolSpec] = {
    # Pull never deletes local edits unless forced, and a forced pull asks first.
    "pull_kiro_config": ToolSpec(
        pull_kiro_config, "Pull Kiro configuration", read_only=False, idempotent=True
    ),
    "push_kiro_config": ToolSpec(push_kiro_config, "Propose Kiro configuration", read_only=False),
    "raise_merge_request": ToolSpec(
        raise_merge_request, "Raise merge request", read_only=False, idempotent=True
    ),
    "diff_kiro_config": ToolSpec(diff_kiro_config, "Compare Kiro configuration", read_only=True),
    "validate_kiro_config": ToolSpec(
        validate_kiro_config, "Validate Kiro configuration", read_only=True, open_world=False
    ),
    "list_kiro_collections": ToolSpec(
        list_kiro_collections, "List synced collections", read_only=True, open_world=False
    ),
    "doctor_kiro_config": ToolSpec(doctor_kiro_config, "Check steersync setup", read_only=True),
    "fleet_status": ToolSpec(fleet_status, "Fleet status", read_only=True),
    "kiro_check": ToolSpec(kiro_check, "Kiro check", read_only=True, open_world=False),
    "plan_setup": ToolSpec(plan_setup, "Plan setup", read_only=True),
    "plan_fleet": ToolSpec(plan_fleet, "Plan fleet push or pull", read_only=True),
    "apply_plan": ToolSpec(apply_plan, "Apply plan", read_only=False),
    "propose_fixes": ToolSpec(
        propose_fixes, "Propose lint fixes", read_only=True, open_world=False
    ),
    "apply_fixes": ToolSpec(apply_fixes, "Apply lint fixes", read_only=False, open_world=False),
}
READ_ONLY_TOOLS: tuple[str, ...] = tuple(n for n, s in TOOL_SPECS.items() if s.read_only)
# The tool list and server description never change while the server runs and do not
# depend on who asks, so clients may cache them for an hour and share the copy.
CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
}


def build_server() -> MCPServer:
    """Create the MCP server with every tool registered."""
    server = MCPServer(
        "steersync",
        title="steersync",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        cache_hints=CACHE_HINTS,
    )
    for name, spec in TOOL_SPECS.items():
        annotations = ToolAnnotations(
            title=spec.title,
            read_only_hint=spec.read_only,
            destructive_hint=spec.destructive if not spec.read_only else False,
            idempotent_hint=spec.idempotent or spec.read_only,
            open_world_hint=spec.open_world,
        )
        server.tool(name=name, title=spec.title, annotations=annotations, structured_output=True)(
            spec.func
        )
    return server


_PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def drop_unexpanded_placeholders(environ: MutableMapping[str, str]) -> list[str]:
    """Remove variables whose value is a literal ``${NAME}`` the client did not expand.

    The rendered mcp.json forwards STEERSYNC_REMOTE, GITLAB_TOKEN, SSH_AUTH_SOCK and
    HTTPS_PROXY as ``${VAR}``. Kiro passes the text through unchanged when the variable is
    unset or not approved, which would override the configured remote, point git at a
    proxy called ``${HTTPS_PROXY}`` and present ``${GITLAB_TOKEN}`` as a token.
    """
    dropped = [k for k, v in environ.items() if _PLACEHOLDER.fullmatch(v.strip())]
    for key in dropped:
        del environ[key]
    return dropped


def serve() -> None:
    """Run the stdio MCP server until the client disconnects. Logs go to stderr."""
    configure_logging(os.environ.get("STEERSYNC_LOG_LEVEL", "WARNING").upper(), "text")
    for key in drop_unexpanded_placeholders(os.environ):
        log.info("ignoring %s: the MCP client passed it as an unexpanded placeholder", key)
    build_server().run(transport="stdio")


def main() -> None:
    """Console entry point ``steersync-mcp``."""
    try:
        serve()
    except SteersyncError as exc:
        print(f"steersync: error: {exc}", file=sys.stderr)
        sys.exit(exc.exit_code)
