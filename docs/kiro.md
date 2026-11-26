# Kiro integration

How steersync starts with Kiro, the supported versions, hooks and the MCP tools.

## Starting Kiro in any repository

The goal is that anyone who clones a project and starts Kiro gets the whole configuration
without asking for it. `steersync setup` makes that true once per machine, in `~/.kiro`, so
it covers every repository; teams that prefer to commit the pieces into each project use
`steersync init --kiro` instead:

| Piece | What it does | Written by |
|---|---|---|
| `hooks/steersync.json` | A V3 `SessionStart` hook running `steersync pull --quiet --best-effort` (through `uvx`, pinned to the installed version), so the session starts from the latest merged configuration. Bounded git time; never fails the session. | `steersync setup` in `~/.kiro`, or `steersync init --kiro` (or `steersync kiro-hook --write`) in the repository |
| `settings/mcp.json` | The steersync MCP server, so "pull the kiro config", "push my steering", "raise an MR for the kiro config" or "validate the agents" work in chat. | `steersync setup` in `~/.kiro`, or `steersync init --kiro` in the repository (merged into an existing file, other servers kept) |
| `.git/hooks/pre-push` | Proposes `.kiro/` on every push of the project. | `steersync setup`, or `steersync init --hook` |

Kiro then loads what it finds: always on steering in every session, `fileMatch`, `manual`
and `auto` steering on CLI V3, skills on demand, prompts as slash commands, hooks and
settings. `steersync lint` warns (`autoload-hook-missing`, `autoload-mcp-missing`) when a
repository lacks either piece.

## Kiro integration

**Session hook (V3).** `steersync setup` creates `~/.kiro/hooks/steersync.json`
(`steersync init --kiro` or `steersync kiro-hook --write` create `.kiro/hooks/steersync.json`
in a repository) with a `SessionStart` trigger that runs
`steersync pull --quiet --best-effort` through
`uvx --quiet --python ">=3.14" --from "steersync==<version>"`, so it never depends on which
`steersync` comes first on Kiro's `PATH`. Run `steersync setup --yes` again after upgrading
to move the pin. Field reports say Kiro hooks
fire only in headless runs or with a custom agent active, so treat them as a convenience and
keep the git hook as the dependable trigger.

### Supported Kiro versions

steersync supports Kiro V3 only: Kiro CLI `kiro.min_cli_version` (3.1.0) or later running the
V3 agent harness, and Kiro IDE `kiro.min_ide_version` (1.4.86) or later, which reads the same
schema. Both minimums are configuration, not code. `steersync doctor --kiro` (add `--json`
for machines, `--strict` to exit 10), the `kiro_check` MCP tool and the session hook (at most
once a day) check `kiro-cli --version`, `chat.agentEngine` and your agent files:

- kiro-cli below the minimum, or any engine other than V3: upgrade kiro-cli and run
  `kiro-cli settings chat.agentEngine v3`.
- Agent files with keys V3 does not read (`allowedTools` or `toolsSettings` without
  `permissions`, `toolAliases`, `keyboardShortcut`): open the agent in Kiro, run
  `/upgrade-agent` and commit the result. The keys `/upgrade-agent` keeps next to
  `permissions` (and its `kiroUpgrade` block) are accepted.

The probes never start a chat, so they use no Kiro credits, and the check never fails a
sync. Set `STEERSYNC_NO_KIRO_CHECK=1` to silence the session hook notice.

**MCP server.** `steersync setup` registers the server in `~/.kiro/settings/mcp.json`
(`steersync init --kiro` in a repository's `.kiro/settings/mcp.json`)
(`steersync mcp-config` prints the fragment). It runs through
`uvx --python ">=3.14" --from "steersync[mcp]==<version>" steersync mcp`, so nothing needs
installing (the Python constraint matters: uvx otherwise uses its default interpreter, and the
server exits before Kiro connects when that is older than 3.14), and it
auto approves only the read only tools. The server is built on the MCP Python SDK v2
(`MCPServer`, specification 2026-07-28): every tool returns a typed report with an output
schema, failures are error results the agent can read, pull and push report progress, and
tools carry titles and read only, destructive and idempotent hints. A forced pull overwrites
local edits, so the server asks the user first (elicitation) when the client supports it and
otherwise needs `confirm: true`. The server tells the agent which tool answers
which request, so plain sentences work:

| Say | Tool |
|---|---|
| "pull / sync / refresh the kiro config", "update the steering and skills" | `pull_kiro_config` |
| "push / propose / share my kiro config", "sync my steering up" | `push_kiro_config` |
| "raise / open an MR for the kiro config", "merge request link" | `raise_merge_request` |
| "what changed", "status", "any open proposals for this repo" | `diff_kiro_config` |
| "validate / lint / check the kiro config, agents, hooks, skills" | `validate_kiro_config` |
| "what is synced and where" | `list_kiro_collections` |
| "is steersync set up", "why does the sync fail" | `doctor_kiro_config` |
| "is my Kiro supported", "do my agents need upgrading" | `kiro_check` |
| "what would setup change", "set this machine up" | `plan_setup`, then `apply_plan` |
| "push / pull all my repositories" | `plan_fleet`, then `apply_plan` |
| "fix the lint findings", "tidy the steering" | `propose_fixes`, then `apply_fixes` |

The `plan_*` and `propose_fixes` tools write nothing and return a `plan_id`. `apply_plan` and
`apply_fixes` rebuild the plan, refuse a `plan_id` that no longer matches (`stale_plan`), and
ask the user once: one form with the steps (defaults ticked), `approve`, and
`allow_destructive` only when a step has no backup; for fixes, `apply_safe` (on) and the
unsafe fixes (none ticked). A client that cannot show a form gets an error asking it to show
the plan to the user and call again with the same `plan_id` and `confirm=true`. Forms never
ask for secrets.
| "which repos have unpushed kiro changes", "fleet status" | `fleet_status` (read only) |

Every tool accepts a `collections` list. Kiro does not pass the shell's `PATH`,
`SSH_AUTH_SOCK`, proxy variables or `GITLAB_TOKEN` to MCP servers, so the rendered entry
forwards them explicitly through `env`; use `steersync init --mcp-command /abs/path/uvx`
if `uvx` is not on Kiro's path.

**Custom agents.** On Kiro CLI V3 custom agents inherit steering, skills and `AGENTS.md` by
default, so list only the always on steering files in `resources` and reference skills with
`skill://.kiro/skills/*/SKILL.md`. A `file://.kiro/steering/**/*.md` glob loads every document
at startup and defeats the `fileMatch`, `manual` and `auto` inclusion modes.
