# Validation reference

`steersync lint`, `steersync push` and the `validate_kiro_config` MCP tool check every
collection with the profile configured for it. `steersync lint` exits with code 4 on any
**error**. A push is stopped only by a possible **secret** (a `forbidden-*` pattern or a
literal value in `mcp.json`), because a secret in the central repository reaches every
machine and its history; every other error is printed ("pushing anyway") and the files are
synced as they are, so a formatting problem never holds up the rest of a sync.
**Warnings** never block; they are best practices taken from the Kiro documentation, so a
reviewer sees the trade off before the change reaches the central repository. Set
`lint.practices: false` to switch the warnings off, or tune the thresholds under
`lint.practice_limits`.

Each profile applies the common checks (secret patterns, UTF-8 for text files)
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
the collection. Files under subdirectories such as `prompts/` are linted as markdown. The
universal format written by `/upgrade-agent` (both `toolsSettings` and `permissions`, plus a
`kiroUpgrade` block) passes; `*.json.bak` backups are not synced.

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax`, `agent-schema` | error | Valid JSON object. | Configuration reference |
| `agent-name` | warning | A `name` is expected. | Configuration reference |
| `agent-v2-format` | error | `toolsSettings` or `allowedTools` without `permissions` is the CLI 2.x format. Kiro rewrites it in place on load (automatic agent upgrade), so a synced copy would turn into a local edit everywhere. Run `/upgrade-agent` or `kiro-cli agent upgrade <dir>` and commit the universal format, which keeps both. | Agent config changes (V3) |
| `agent-permissions` | error | `permissions.rules[]` objects with a `capability`, an `effect` of `allow`, `ask`, `deny` or `evaluate` (the weakest, written by the approve-for-me preset), and `match`/`exclude` as strings or lists of strings. | Permissions |
| `agent-permission-capability` | warning | Unknown capability (known: `fs_read`, `fs_write`, `shell`, `web_fetch`, `web_search`, `mcp`, `subagent`, `skill`, `power`, `context`, `diagnostics`, `workflow`, `sessions`, `sandbox_network`, and the meta capabilities `all`, `builtin`, `filesystem`). | Permissions, Kiro CLI 3.5 |
| `agent-legacy-key` | warning | `toolAliases` and `keyboardShortcut` are not supported by V3. | Agent config changes (V3) |
| `agent-v2-tool-name` | warning | `fs_read`, `fs_write`, `execute_bash`, `execute_cmd`, `use_subagent` in `tools`; V3 uses `read`, `write`, `shell`, `subagent`. | Agent config changes (V3) |
| `agent-use-aws` | warning | The built in `use_aws` tool is gone in V3; use the AWS MCP server. | Agent config changes (V3) |
| `agent-resource-scheme`, `agent-knowledge-base` | warning, error | Resources are `file://` or `skill://` strings or `knowledgeBase` objects with a `source` (`indexType` `best` or `fast`). | Configuration reference |
| `agent-machine-path` | warning | A resource under `/Users/...` or `/home/...` does not resolve for the rest of the team. | Configuration reference |
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
| `hook-schema` | error | `"version": "v1"` and `hooks[]` with `name`, `trigger` and `action`; `enabled` is a boolean. | Hooks |
| `hook-trigger` | warning | Unknown trigger. V3 triggers: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest` (CLI 3.4), `PreCompact`, `PostCompact` (CLI 3.1), `PreTaskExec`, `PostTaskExec`, `PostFileCreate`, `PostFileSave`, `PostFileDelete`, `Stop`, `Manual`. | Hook triggers |
| `hook-trigger-alias` | warning | Spellings Kiro still accepts but no longer writes: CLI 2.x camelCase (`agentSpawn`, `preToolUse`, `stop`), IDE 0.x names (`fileEdited`, `promptSubmit`, `agentStop`, `userTriggered`) and `AfterFileEdit`; the message names the canonical trigger. | Hook triggers, Kiro CLI 3.5 |
| `hook-action` | error | `action.type` is `command` (with `action.command`) or `agent` (with `action.prompt`). | Hooks |
| `hook-timeout-value` | error | `timeout` is a whole number of seconds, 0 or more (0 disables it). | Hooks |
| `hook-confirm` | error | `confirm` needs a `question`, options with `id` and `label`, and only works on `Stop` command hooks. | Hooks |
| `hook-duplicate-name`, `hook-use-aws` | warning | Duplicate hook names in one file; matchers on the removed `use_aws` tool. | Hooks |
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

Scripts and assets are scanned for secrets; there is no per file size limit (the
`sync.max_total_bytes` safety limit on a whole sync still applies).

## settings (`.kiro/settings`: `mcp.json`, `lsp.json`, `sandbox.json`, `cli.json`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax` | error | Valid JSON. | MCP configuration |
| `mcp-literal-secret` | error | A secret looking `env` or `headers` entry (token, secret, password, API key, `Authorization`) or `oauth.clientSecret` holds a literal value instead of `${VAR}`. | MCP configuration |
| `mcp-oauth` | error, warning | `oauth.mode` is `browser`, `enterprise` or `auto`; `clientMetadataUrl` is `https://`; Kiro ignores it when `clientSecret` is set. | MCP configuration |
| `mcp-timeout`, `mcp-shape` (errors) | error | `timeout`/`timeoutMs` are positive integers, `autoApprove`/`disabledTools` lists of names, `disabled` a boolean. | MCP configuration |
| `mcp-shape`, `mcp-server-command` | warning | `mcpServers` mapping; each server has a `command` or a `url`. | MCP configuration |
| `mcp-machine-path` | warning | A home directory path will not work on other machines. | MCP configuration |
| `lsp-shape` | warning | `lsp.json` has a `languages` mapping. | Code intelligence |

### `sandbox.json` (the `sandbox` collection)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `sandbox-shape` | error | A JSON object; `enabled` and `mcpSandboxing` are booleans. | Sandbox |
| `sandbox-network-access` | error | `networkAccess` is `DEFAULT_ALLOWED`, `COMMON_DEPENDENCIES` or `DEFAULT_BLOCKED`. | Sandbox, Kiro CLI 3.5 |
| `sandbox-cannot-loosen` | warning | A project file can only tighten an active global policy, so `enabled: false` or `DEFAULT_ALLOWED` may have no effect. | Sandbox |
| `sandbox-unknown-key` | warning | Keys other than `enabled`, `networkAccess`, `fsMode`, `mcpSandboxing`. | Kiro CLI 3.5 |

### Workspace `cli.json` (the opt in `cli-settings` collection)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `cli-settings-global-only` | error | `chat.agentEngine`, `acp.agentEngine`, `chat.enableAutoAgentUpgrade`, `app.warnOnElevatedPrivileges`, `auth.portalUrl`, `chat.uiMode`, `chat.screenReader` and `chat.terminalTitle` are read only from `~/.kiro/settings/cli.json`. Flat (`"chat.agentEngine"`) and nested layouts are both checked. | Settings reference |

## workflow (`.kiro/workflows`, `~/.kiro/workflows`)

Recipes are `<name>.workflow.json`, `.yaml` or `.yml`. The rules follow the workflow
authoring documentation and the recipe schema shipped with Kiro CLI 3.5.

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax`, `yaml-syntax` | error | The file parses. | |
| `workflow-schema` | error | A `name` and a non empty `steps` list; `inputs` maps names to descriptions; each node has an `id` and a `type` of `step` (`agent`, `prompt`), `sequence` (`steps`), `repeat` (`steps`), `parallel` (`branches`, `joinPolicy` `all`, `allSettled` or `any`) or `watch` (`handler`). | Kiro CLI 3.5 |
| `workflow-duplicate-id` | error | Node ids are unique in the recipe. | Kiro CLI 3.5 |
| `workflow-repeat` | error | `maxIterations` from 1 to 1000, `onMaxIterations` `abort`, `continue` or `pause`, and at most one of `stopCondition` or `stopWhen`. | Kiro CLI 3.5 |
| `workflow-file-name` | warning | Kiro saves a recipe as the kebab case of its `name`. | Kiro CLI 3.5 |
| `workflow-unknown-key` | warning | Top level keys Kiro ignores. | Kiro CLI 3.5 |
| `workflow-depth`, `workflow-size` | error | More than 8 nesting levels or more than 50 `step` nodes (watch nodes and repeat iterations do not count). | Workflow authoring |

## kiroignore (`.kiroignore` anywhere, `~/.kiro/settings/kiroignore`)

| Rule | Severity | Check |
|---|---|---|
| `kiroignore-syntax` | error, warning | A trailing backslash escapes nothing; `**` glued to other characters acts like `*`. |
| `kiroignore-secrets` | warning | The root file does not keep `.env*` or `*.pem` files from the agent. |

## prompt (`.kiro/prompts`, `~/.kiro/prompts`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `prompt-name-length` | warning | Prompt names are limited to 50 characters. | Prompts |

Plus the markdown profile.

## markdown (specs, nested `AGENTS.md`, prompt files, references)

Encoding, secret patterns and pymarkdown style. No Kiro front matter rules.

## Workspace checks (whole workspace lint only)

| Rule | Severity | Check |
|---|---|---|
| `autoload-hook-missing` | warning | No `SessionStart` hook runs `steersync pull`, so a Kiro session starts with whatever is on disk. `steersync init` writes it. |
| `autoload-mcp-missing` | warning | The steersync MCP server is not registered in `.kiro/settings/mcp.json`, so "pull kiro config" is not available in chat. `steersync init` writes it. |

## Secret patterns (every profile)

`forbidden-aws-access-key`, `forbidden-private-key`, `forbidden-github-token`,
`forbidden-gitlab-token`, `forbidden-slack-token` and `forbidden-generic-secret` are errors.
The patterns are configurable under `lint.forbidden_patterns`.
