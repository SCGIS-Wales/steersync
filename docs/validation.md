# Validation reference

`steersync lint`, `steersync push` and the `validate_kiro_config` MCP tool check every
collection with the profile configured for it. **Errors** stop a push and exit with code 4.
**Warnings** never block; they are best practices taken from the Kiro documentation, so a
reviewer sees the trade off before the change reaches the central repository. Set
`lint.practices: false` to switch the warnings off, or tune the thresholds under
`lint.practice_limits`.

Each profile applies the common checks (size limit, secret patterns, UTF-8 for text files)
and then its own rules. The source column names the Kiro documentation page the rule comes
from (Steering, Custom agents configuration reference, Hooks, Agent Skills, MCP
configuration, Prompts, Code intelligence).

## steering (`.kiro/steering`, `~/.kiro/steering`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `front-matter-position` | error | Front matter must start on line 1; after a blank line Kiro ignores it. | Steering |
| `front-matter-yaml` | error | Front matter must be valid YAML. | Steering |
| `inclusion-value` | error | `inclusion` is `always`, `fileMatch`, `manual` or `auto`. | Steering |
| `filematch-pattern-required`, `filematch-pattern-type` | error | `fileMatch` needs a string or list of strings. | Steering |
| `filematch-pattern-unused` | warning | `fileMatchPattern` only applies to `fileMatch`. | Steering |
| `auto-requires-metadata` | error | `auto` needs `name` and `description`. | Steering |
| `unknown-key` | warning | Kiro ignores keys it does not know (`x-steersync` is allowed). | Steering |
| `steering-globs-key` | warning | `globs` is Cursor syntax; Kiro reads `fileMatchPattern`, so the file never matches. | Steering |
| `steering-filematch-prefix` | warning | Patterns are relative to the workspace root, no leading `/` or `./`. | Steering |
| `steering-auto-description` | warning | Kiro matches `description` against the request; write it as a trigger ("Use when ...") of at least 20 characters. | Steering |
| `steering-numeric-prefix` | warning | Numeric prefixes collide and become the `#reference` and slash command name; Kiro's examples are domain first kebab case. | Steering |
| `steering-file-name` | warning | Lowercase kebab case file names. | Steering |
| `steering-document-control-key` | warning | `owner`, `status`, `last_updated` and similar keys are not Kiro keys; keep them in a footer line. | Steering |
| `steering-always-size` | warning | An always included file above 16 KiB costs context in every session. | Steering |
| `steering-always-budget` | warning (collection) | More than 48 KiB of always included steering in one collection. | Steering |
| `steering-foundation-missing` | warning (collection) | `product.md`, `tech.md` and `structure.md` are included in every interaction by default. | Steering |
| `require-h1` | warning | A level one heading is expected. | style |
| `file-reference-missing`, `file-reference-escape` | warning, error | `#[[file:...]]` and `#[[folder:...]]` targets must exist inside the workspace. | Steering |
| `markdown-MD*` | configurable | pymarkdown style rules. | style |

## agent (`.kiro/agents`, `~/.kiro/agents`)

JSON agents and Markdown agents (front matter holds the configuration) at the top level of
the collection. Files under subdirectories such as `prompts/` are linted as markdown.

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax`, `agent-schema` | error | Valid JSON object. | Configuration reference |
| `agent-name` | warning | A `name` is expected. | Configuration reference |
| `agent-deprecated-key` | warning | `toolsSettings` is deprecated. | Configuration reference |
| `agent-permissions-missing` | warning | `toolsSettings` without `permissions.rules`; `/upgrade-agent` converts it. | Configuration reference |
| `agent-tools-wildcard` | warning | `tools: ["*"]` grants every tool; list the tags needed and gate the rest with permissions. | Agent config changes (V3) |
| `agent-allowed-wildcard` | warning | `allowedTools: ["*"]` disables every permission prompt. | Configuration reference |
| `agent-steering-glob` | warning | `file://.kiro/steering/**` loads every steering file at startup and bypasses the inclusion modes. | Configuration reference, Steering |
| `agent-source-glob` | warning | A `file://` glob over source code injects the tree at every session start. | Configuration reference |
| `agent-prompt-file-missing` | warning | `prompt: file://...` must exist relative to the agent file. | Configuration reference |
| `agent-hooks-object-form` | warning | Inline hooks must be arrays per trigger on CLI 3.0. | Configuration reference |
| `agent-inline-hooks` | warning | Inline agent hooks are the CLI 2.x mechanism; CLI 3.0 and IDE 1.0 read `.kiro/hooks/*.json`. | Hooks migration |

## hooks (`.kiro/hooks`, `~/.kiro/hooks`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax` | error | Valid JSON. | Hooks |
| `hook-schema` | error | `hooks[]` with `name`, `trigger` and `action`; `version` should be `v1`. | Hooks |
| `hook-trigger` | warning | Unknown trigger; V3 triggers are PascalCase (`SessionStart`, `Stop`, `PreToolUse`, ...). | Hook triggers |
| `hook-action` | error | `action.type` is `command` or `agent`. | Hooks |
| `hook-when-then` | warning | The IDE 0.x `when`/`then` schema. | IDE 0.x reference |
| `hook-legacy-format` | warning | `.kiro.hook` files are deprecated; IDE 1.0 does not execute them until migrated. | What's new in 1.0 |
| `hook-timeout` | warning | A timeout above 300 s blocks the session. | Hooks |
| `hook-matcher-ignored` | warning | Matchers are ignored for `Stop` and `SessionStart`. | Hooks |

## skill (`.kiro/skills`, `~/.kiro/skills`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `skill-front-matter-required` | error | `SKILL.md` starts with front matter. | Agent Skills |
| `skill-requires-metadata` | error | `name` and `description` are required. | Agent Skills |
| `skill-name-mismatch` | warning | `name` should match the directory. | Agent Skills |
| `skill-name-format` | warning | Lowercase letters, digits and dashes, at most 64 characters. | Agent Skills standard |
| `skill-description-length` | warning | At most 1024 characters; only name and description load at startup. | Agent Skills standard |
| `skill-body-length` | warning | More than 500 lines; move the full runbook under `references/`. | Agent Skills |
| `skill-misplaced` | warning (collection) | `SKILL.md` directly in the skills folder is not discovered. | Agent Skills |

Scripts and assets are scanned for secrets; binary assets are allowed up to the collection's
size limit (1 MiB by default).

## settings (`.kiro/settings`: `mcp.json`, `lsp.json`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax` | error | Valid JSON. | MCP configuration |
| `mcp-shape`, `mcp-server-command` | warning | `mcpServers` mapping; each server has a `command` or a `url`. | MCP configuration |
| `mcp-machine-path` | warning | A home directory path will not work on other machines. | MCP configuration |
| `lsp-shape` | warning | `lsp.json` has a `languages` mapping. | Code intelligence |

## prompt (`.kiro/prompts`, `~/.kiro/prompts`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `prompt-name-length` | warning | Prompt names are limited to 50 characters. | Prompts |

Plus the markdown profile.

## markdown (specs, `AGENTS.md`, prompt files, references)

Encoding, size, secret patterns and pymarkdown style. No Kiro front matter rules.

## Workspace checks (whole workspace lint only)

| Rule | Severity | Check |
|---|---|---|
| `autoload-hook-missing` | warning | No `SessionStart` hook runs `steersync pull`, so a Kiro session starts with whatever is on disk. `steersync init` writes it. |
| `autoload-mcp-missing` | warning | The steersync MCP server is not registered in `.kiro/settings/mcp.json`, so "pull kiro config" is not available in chat. `steersync init` writes it. |

## Secret patterns (every profile)

`forbidden-aws-access-key`, `forbidden-private-key`, `forbidden-github-token`,
`forbidden-gitlab-token`, `forbidden-slack-token` and `forbidden-generic-secret` are errors.
The patterns are configurable under `lint.forbidden_patterns`.
