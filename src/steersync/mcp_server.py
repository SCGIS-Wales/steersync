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
from typing import Annotated, Any, NamedTuple

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
from pydantic import BaseModel, Field

from steersync import __version__, mcp_tools
from steersync.errors import SteersyncError
from steersync.logging_utils import configure_logging

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
- kiro_v3_status: "am I on Kiro CLI V3 / do my agents need upgrading". Read only; never \
starts a chat, so it costs no Kiro credits.
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


async def kiro_v3_status(cwd: Cwd = None) -> mcp_tools.HarnessReport:
    """Report whether kiro-cli runs the V3 agent harness and which agents are not V3 ready.

    Use for "am I on Kiro V3", "do my agents need upgrading", "why does Kiro rewrite my
    agent". Runs only `kiro-cli --version` and `kiro-cli settings`; never starts a chat.
    "message" holds the reminder to show the user when "needs_reminder" is true.
    """
    return await _run(functools.partial(mcp_tools.v3_status, cwd))


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
    "kiro_v3_status": ToolSpec(
        kiro_v3_status, "Kiro CLI V3 status", read_only=True, open_world=False
    ),
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
