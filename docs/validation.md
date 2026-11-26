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
| `fm-position` | error | Front matter must be the first content: no byte order mark, blank line or space before `---`. | Steering |
| `fm-yaml` | error | Front matter must be valid YAML and a mapping; a value containing `: ` names the key and shows the quoted fix. | Steering |
| `fm-inclusion` | error | `inclusion` is exactly `always`, `fileMatch`, `manual` or `auto`. | Steering |
| `fm-filematch-required`, `fm-filematch-type` | error | `fileMatch` needs a glob string or a list of globs. | Steering |
| `fm-filematch-comma` | error | A comma separated `fileMatchPattern` string never matches and Kiro does not warn (kirodotdev/Kiro#11095); the message shows the YAML list. | Steering |
| `fm-filematch-relative` | warning | Patterns are relative to the workspace root, no leading `/` or `./`. | Steering |
| `fm-filematch-unused` | warning | `fileMatchPattern` only applies to `fileMatch`. | Steering |
| `fm-auto-fields` | error | `auto` needs `name` and `description`; Kiro loads only those until the model judges the file relevant (it is not vector search). | Steering |
| `fm-unknown-key` | warning | A top level key that is not a Kiro key, not `metadata` and not in `lint.frontmatter.allowed_extra_keys`; the message says to move it under `metadata` (or to the sidecar). | Steering |
| `fm-metadata` | warning, error | The `metadata` map: strings only, checked against `lint.frontmatter.metadata_fields` (owner, tags, references, review-by, verified-on, status, version), including a `review-by` date that has passed. | steersync |
| `fm-knowledge-in-steering` | error | Knowledge note keys (`title`, `domain`, `verified_on`, `sources`, `relations`) with no `inclusion`: a note that would load on every turn. | steersync |
| `ref-syntax` | error | A malformed `#[[file:...]]` or `#[[folder:...]]` (empty path, a folder with lines, an empty range). | Steering |
| `ref-unresolved` | error | The target is missing, outside its base, or the line range is past the end of the file. Workspace steering resolves from the workspace root, global steering from `~/.kiro/steering`, `AGENTS.md` from its folder. | Steering |
| `steering-ext` | error | A file under the steering folder without `.md` is never loaded. | Steering |
| `steering-size` | warning | A file over `lint.kiro_limits.file_chars_truncate` characters (truncated), or the `always` set over `lint.budgets.always_chars` (collection). | Steering |
| `steering-dup-rule` | warning (collection) | The same normalised bullet in more than one steering file; state each rule once. | steersync |
| `steering-globs-key` | warning | `globs` is Cursor syntax; Kiro reads `fileMatchPattern`, so the file never matches. | Steering |
| `steering-auto-description` | warning | Kiro matches `description` against the request; write it as a trigger ("Use when ...") of at least 20 characters. | Steering |
| `steering-numeric-prefix` | warning | Numeric prefixes collide and become the `#reference` and slash command name; Kiro's examples are domain first kebab case. | Steering |
| `steering-file-name` | warning | Lowercase kebab case file names. | Steering |
| `steering-always-size` | warning | An always included file above 16 KiB costs context in every session. | Steering |
| `steering-foundation-missing` | warning (collection) | `product.md`, `tech.md` and `structure.md` are included in every interaction by default. | Steering |
| `require-h1` | warning | A level one heading is expected. | style |
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
| `agent-permissions` | error | `permissions.rules[]` objects with a `capability`, an `effect` of `allow`, `ask`, `deny` or `evaluate` (the weakest, written by the approve-for-me preset), and `match`/`exclude` as strings or lists of strings. | Permissions |
| `agent-permission-capability` | warning | Unknown capability (known: `fs_read`, `fs_write`, `shell`, `web_fetch`, `web_search`, `mcp`, `subagent`, `skill`, `power`, `context`, `diagnostics`, `workflow`, `sessions`, `sandbox_network`, and the meta capabilities `all`, `builtin`, `filesystem`). | Permissions, Kiro CLI 3.5 |
| `agent-legacy-key` | error | A key V3 does not read (`lint.legacy_agent_keys`): `allowedTools` or `toolsSettings` without `permissions`, `toolAliases`, `keyboardShortcut`. The message names the V3 replacement (`permissions`, `tools`, `excludedTools`); run `/upgrade-agent` in Kiro and commit the result. | Agent config changes (V3) |
| `agent-unknown-key` | error (JSON), warning (Markdown) | A top level key outside `lint.agent_fields` (`name`, `description`, `prompt`, `model`, `tools`, `excludedTools`, `permissions`, `resources`, `mcpServers`, `hooks`, `includeMcpJson`, `includePowers`, `welcomeMessage`, plus what `/upgrade-agent` writes). | Configuration reference |
| `agent-resource-budget` | warning | `file://` resources over `lint.kiro_limits`: more than 32 files or 100,000 characters in total, a file over 50,000 characters (truncated) or 1 MiB (skipped). Needs the workspace. | Configuration reference |
| `agent-use-aws` | warning | The built in `use_aws` tool is gone in V3; use the AWS MCP server. | Agent config changes (V3) |
| `agent-resource-scheme`, `agent-knowledge-base` | warning, error | Resources are `file://` or `skill://` strings or `knowledgeBase` objects with a `source` (`indexType` `best` or `fast`). | Configuration reference |
| `agent-machine-path` | warning | A resource under `/Users/...` or `/home/...` does not resolve for the rest of the team. | Configuration reference |
| `agent-tools-wildcard` | warning | `tools: ["*"]` grants every tool; list the tags needed and gate the rest with permissions. | Agent config changes (V3) |
| `agent-allowed-wildcard` | warning | `allowedTools: ["*"]` disables every permission prompt. | Configuration reference |
| `agent-steering-glob` | warning | `file://.kiro/steering/**` loads every steering file at startup and bypasses the inclusion modes. | Configuration reference, Steering |
| `agent-source-glob` | warning | A `file://` glob over source code injects the tree at every session start. | Configuration reference |
| `agent-prompt-file-missing` | warning | `prompt: file://...` must exist relative to the agent file. | Configuration reference |

## hooks (`.kiro/hooks`, `~/.kiro/hooks`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `json-syntax` | error | Valid JSON. | Hooks |
| `hook-schema` | error | `"version": "v1"` and `hooks[]` with `name`, `trigger` and `action`; `enabled` is a boolean. | Hooks |
| `hook-trigger` | error | Not a V3 trigger. V3 triggers: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest` (CLI 3.4), `PreCompact`, `PostCompact` (CLI 3.1), `PreTaskExec`, `PostTaskExec`, `PostFileCreate`, `PostFileSave`, `PostFileDelete`, `Stop`, `Manual`. | Hook triggers |
| `hook-legacy-file` | error | A `*.kiro.hook` file, `when`/`then` keys, or agent hooks keyed by trigger: legacy forms Kiro V3 does not run. The message shows the V3 file `.kiro/hooks/<id>.json` and its fields. | Hooks |
| `hook-action` | error | `action.type` is `command` (with `action.command`) or `agent` (with `action.prompt`). | Hooks |
| `hook-timeout-value` | error | `timeout` is a whole number of seconds, 0 or more (0 disables it). | Hooks |
| `hook-confirm` | error | `confirm` needs a `question`, options with `id` and `label`, and only works on `Stop` command hooks. | Hooks |
| `hook-duplicate-name`, `hook-use-aws` | warning | Duplicate hook names in one file; matchers on the removed `use_aws` tool. | Hooks |
| `hook-timeout` | warning | A timeout above 300 s blocks the session. | Hooks |
| `hook-matcher-ignored` | warning | Matchers are ignored for `Stop` and `SessionStart`. | Hooks |

## skill (`.kiro/skills`, `~/.kiro/skills`)

| Rule | Severity | Check | Source |
|---|---|---|---|
| `skill-front-matter-required` | error | `SKILL.md` starts with front matter. | Agent Skills |
| `skill-requires-metadata` | error | `name` and `description` are required. | Agent Skills |
| `skill-name` | error | Lowercase letters, digits and single hyphens, at most 64 characters, and the same as the directory. | Agent Skills specification |
| `skill-description-length` | warning | At most 1024 characters; only name and description load at startup. | Agent Skills standard |
| `skill-size` | warning | A body over `lint.budgets.skill_lines` (500) lines or about `lint.budgets.skill_tokens` (5,000) tokens; move detail under `references/`. | Agent Skills |
| `fm-metadata` | warning, error | The specification's `metadata` map, checked like steering metadata. | Agent Skills specification |
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

## markdown (nested `AGENTS.md`, prompt files, references)

Encoding, secret patterns and pymarkdown style. No Kiro front matter rules. `AGENTS.md`
also gets `ref-syntax` and `ref-unresolved`, resolved from the folder that holds it.

## spec (`.kiro/specs`)

The markdown profile, plus:

| Rule | Severity | Check |
|---|---|---|
| `spec-ears` | info | An acceptance criterion (a list item under a heading matching `lint.spec.criteria_heading`) not in EARS form (`lint.spec.ears_pattern`: WHEN/IF/WHILE/WHERE ... SHALL). Info never fails lint. |

## knowledge (`collections.knowledge`, off by default)

| Rule | Severity | Check |
|---|---|---|
| `kn-schema` | error | The note schema: `title`, `domain`, `type`, `status`, `verified_on` required; `aliases`, `tags`, `related`, `sources`, `relations`, `review_by` optional; nothing else. |
| `kn-inclusion` | error | `inclusion` or `fileMatchPattern` in a knowledge note: notes are retrieved, never loaded as steering. |
| `kn-summary` | warning | No level one heading and summary paragraph within `lint.knowledge.summary_within_lines`. |
| `kn-frontmatter-size` | warning | Front matter over `lint.knowledge.frontmatter_chars` (600), which pushes the summary out of the first chunk. |
| `kn-section-size` | warning | A section over `lint.knowledge.section_words` (250). |
| `kn-identifiers-in-body` | warning | An alias, or a CVE id in the title, that the body never mentions. |
| `kn-stale` | warning | `review_by` has passed, or `verified_on` is older than `lint.knowledge.max_age_days` (90). |
| `kn-duplicate` | warning (collection) | Two notes share a title or an alias. |
| `kn-related-unresolved` | warning (collection) | A `related` note that is not in the collection. |

Agents with a `knowledgeBase` resource (`agent-knowledge-base`): the source is a `file://`
folder that exists in the workspace, a `name` unique within the agent, `indexType` `best` or
`fast`, `autoUpdate` a boolean, `knowledge` in `tools` (or `*`, `@builtin`) and not in
`excludedTools`; a Markdown agent gets a warning (kirodotdev/Kiro#9790).

## Workspace checks (whole workspace lint only)

| Rule | Severity | Check |
|---|---|---|
| `autoload-hook-missing` | warning | No `SessionStart` hook in the repository or `~/.kiro` runs `steersync pull`, so a Kiro session starts with whatever is on disk. `steersync setup` adds it to `~/.kiro`. |
| `fm-metadata` | warning, error | `.kiro/steersync.meta.yaml` (the sidecar, `lint.frontmatter.sidecar`) maps existing paths to metadata maps checked against `lint.frontmatter.metadata_fields`. |
| `autoload-mcp-missing` | warning | No server that runs steersync is registered in the repository's or `~/.kiro`'s `settings/mcp.json`, so "pull kiro config" is not available in chat. `steersync setup` adds it. |

## Secret patterns (every profile)

`forbidden-aws-access-key`, `forbidden-private-key`, `forbidden-github-token`,
`forbidden-gitlab-token`, `forbidden-slack-token` and `forbidden-generic-secret` are errors.
The patterns are configurable under `lint.forbidden_patterns`.
