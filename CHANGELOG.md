# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## [Unreleased]

## [0.5.2] - 2026-11-22

### Fixed

- The MCP server entry written by `steersync init` did not start in Kiro on machines whose
  default uv Python is older than 3.14: uvx resolved `steersync[mcp]` with that interpreter,
  failed, and Kiro reported "Connection closed". The entry now passes `--python ">=3.14"`.
  Found by an end to end test with Kiro CLI 3.5.
- `steersync init` wrote `/.kiro/settings/` to `.git/info/exclude` twice.

## [0.5.1] - 2026-11-22

### Added

- V3 detection reads `acp.agentEngine`, the harness `kiro-cli acp` starts for editor and IDE
  integrations, and reminds when it is still `v1` or `v2`.
- The MCP server sends cache hints: the tool list and server description may be cached for
  an hour and shared (`ttlMs`, `cacheScope: public`).

### Changed

- Rule enumerations checked against the Kiro documentation and the harness bundled with Kiro
  CLI 3.5: the `evaluate` permission effect and the `workflow` and `sessions` capabilities
  are accepted; every hook trigger alias the harness maps is recognised (`fileEdited`,
  `promptSubmit`, `agentStop`, `userTriggered`, `AfterFileEdit`, ...) and `migrate-hooks`
  uses the same map; the documented workflow limits (50 `step` nodes, 8 levels) are errors
  and count only `step` nodes; `acp.agentEngine`, `auth.portalUrl`, `chat.uiMode`,
  `chat.screenReader` and `chat.terminalTitle` are rejected in a workspace `cli.json`.
- Development dependencies are split into the PEP 735 groups `test`, `lint` and `security`
  (`dev` includes all three); each CI job installs only its group.

### Fixed

- `permissions.rules[].effect: evaluate` was reported as an error.

## [0.5.0] - 2026-11-22

### Added

- MCP tools return typed pydantic reports, published as each tool's output schema and
  returned as structured content. Failures come back as tool results with `isError` set
  rather than `"ok": false` payloads. Pull and push send progress notifications. A forced
  pull asks the user through elicitation (multi round trip requests on 2026-07-28) when the
  client supports it, and otherwise needs `confirm: true`. Arguments carry descriptions in
  the input schema. New `doctor_kiro_config` tool.
- The tool logic moved to `steersync.mcp_tools`, which does not import `mcp`; `steersync
  mcp` prints the `uvx --from "steersync[mcp]"` command when the extra is missing.
- Windows: local remotes given as drive or UNC paths (`C:\repos\central.git`) are
  recognised as local paths instead of an SCP style host named `C`, and CI runs the test
  suite on Windows.
- Property based tests (hypothesis) for path containment, change plans and every lint
  profile on arbitrary input, and golden file tests for every default collection.

### Changed

- mypy runs with the pydantic plugin.

### Fixed

- Lint rules crashed with `TypeError` on JSON values that are lists or objects where a
  string enumeration was expected (a workflow node `type`, a permission `effect`, a hook
  `trigger` in the best practice checks); they now report a finding.

## [0.4.0] - 2026-11-22

### Added

- Collections for the rest of the committable Kiro configuration (Kiro IDE 1.5, Kiro CLI 3.5):
  `workflows` (`.kiro/workflows/*.workflow.{json,yaml,yml}` and `~/.kiro/workflows`),
  `sandbox` (`.kiro/settings/sandbox.json`), `kiroignore` (`.kiroignore` at any depth and
  `~/.kiro/settings/kiroignore`) and the opt in `cli-settings` (`.kiro/settings/cli.json`).
  `agents-md` now syncs nested `AGENTS.md` files, and `specs` syncs every file in a spec
  directory (`bugfix.md`, metadata), not only markdown.
- Schema rules (`steersync.kiro_rules`): hooks need `version: "v1"` and a matching
  `action.command` or `action.prompt`; integer timeouts; `confirm` only on `Stop` command
  hooks; the `SessionEnd`, `PermissionRequest`, `PreCompact` and `PostCompact` triggers, with
  CLI 2.x camelCase names flagged. Agents: V2 format (`toolsSettings` or `allowedTools`
  without `permissions`) is an error because Kiro rewrites such files on load, plus checks
  for `permissions.rules`, 2.x tool names, `use_aws`, `toolAliases`, `keyboardShortcut`,
  resource schemes, home directory paths and inline V3 hooks, for JSON and Markdown agents.
  `mcp.json`: literal secrets in `env`, `headers` and `oauth.clientSecret`, `oauth.mode`,
  `clientMetadataUrl`, timeouts. `sandbox.json`, workspace `cli.json`, workflow recipes
  (node types, unique ids, `repeat` limits, join policies, depth and size) and
  `.kiroignore` syntax.
- Kiro CLI V3 harness detection: `steersync v3-status`, a harness section in `doctor`, the
  `kiro_v3_status` MCP tool and a once a day reminder from the session hook. Uses only
  `kiro-cli --version` and `kiro-cli settings`, never a chat session.
- `steersync migrate-hooks` converts `*.kiro.hook` files and CLI 2.x inline agent hooks into
  standalone `.kiro/hooks/*.json` files (dry run by default).
- The `steersync-mcp` entry point, MCP tool titles and read only, destructive and idempotent
  annotations, server version and title, `confirm` for a forced pull, and logs on stderr.

### Changed

- The rendered `mcp.json` entry runs `uvx --from "steersync[mcp]==<version>" steersync mcp`
  (the previous `uvx steersync mcp` lacked the `mcp` extra) and auto approves the read only
  tools.
- Collections may share a directory when each selects its files by literal name and the
  names do not overlap. Hidden files are synced when a pattern names them literally, and
  excluded trees such as `node_modules` are no longer walked.
- Dependency floors: click 8.5.1, pydantic 2.14.4, PyYAML 6.0.4, pymarkdownlnt 0.9.42,
  hatchling 1.33, pytest 9.2, ruff 0.17.6, bandit 1.9, pip-audit 2.10. pytest uses its
  native TOML configuration, coverage must stay at 90 per cent, CI also tests Python 3.15
  and runs ty as an advisory check, and `from __future__ import annotations` is gone
  (Python 3.14 defers annotations).

### Fixed

- The Kiro session hook command `steersync pull --quiet --best-effort` failed with "No such
  option: --quiet"; `pull` now accepts `-q/--quiet`.
- `steersync lint FILE` crashed for a file outside the repository, and
  `steersync lint --profile P DIR` used the steering file patterns for every profile.

## [0.3.0] - 2026-10-13

### Added

- Two more collections from the Kiro configuration scopes: `prompts` (`.kiro/prompts` and
  `~/.kiro/prompts`, the saved prompts that become slash commands) and `agents-md` (the
  `AGENTS.md` at the repository root). Legacy `.kiro.hook` files are picked up by the hooks
  collection and flagged.
- Best practice validation. Every collection profile now reports warnings taken from the Kiro
  documentation (steering file naming and `auto` descriptions, the `globs` key, always on
  context budget, missing foundation files, agent tool wildcards and startup globs, inline
  agent hooks on CLI 3.0, hook timeouts and legacy schemas, skill name and description
  limits, machine specific paths in `mcp.json`, prompt name length) plus the `settings` and
  `prompt` profiles. Warnings never block; thresholds live under `lint.practice_limits` and
  `lint.practices: false` turns them off. The rules are listed in `docs/validation.md`.
- `steersync init` writes the Kiro `SessionStart` hook and registers the steersync MCP server
  in `.kiro/settings/mcp.json` (other servers kept, an existing entry left alone), so a Kiro
  session in any repository refreshes and loads the configuration without being asked.
  `steersync lint` warns when either piece is missing.
- GitLab merge requests through the REST API: `steersync mr` and the `raise_merge_request`
  MCP tool open or find the merge request for the proposal branch, `push` confirms or creates
  it and prints the URL when the token is present. The token comes from `GITLAB_TOKEN` (name
  configurable with `gitlab.token_env`); `gitlab.api_url`, `gitlab.project` and
  `gitlab.target_branch` are derived from `remote.url` and `remote.default_branch` unless set.
- `push` and `status` list other proposal branches of the same project still on the central
  remote and warn about them (`branch.warn_existing`).
- Offline fallback: when the central repository is unreachable and a cache clone exists,
  `pull` and `status` apply the cached default branch and say so (`sync.offline_fallback`).
  `pull --best-effort` runs with the hook time budget.
- Automatic releases: merging a pull request to `main` computes the version from a
  `release:*` label, a Conventional Commits title prefix or the Unreleased changelog section,
  commits the changelog and version bump, publishes to PyPI and creates the tag and GitHub
  release in the same run (`scripts/prepare_release.py`).

### Changed

- Proposal branches are named `kiro-config/{project}/{user}-{hash}`; `{user}` now prefers the
  operating system user name (letters, digits and dashes), then the git email, then the git
  name (`branch.user_from`). The template must keep `{project}` and `{hash}`.
- MCP tools are named by intent and described for the agent: `pull_kiro_config`,
  `push_kiro_config`, `raise_merge_request`, `diff_kiro_config`, `validate_kiro_config` and
  `list_kiro_collections` replace `pull_steering`, `push_steering`, `diff_steering` and
  `lint_steering`. The rendered `mcp.json` entry forwards the GitLab token variable.
- `pull` logs the central content's lint errors and a one line count of warnings instead of
  every warning.

## [0.2.0] - 2026-10-12

### Added

- Collections: the whole `.kiro/` layout is synced, not only steering. The packaged defaults
  define `steering`, `specs`, `skills` (with execute bits preserved on scripts), `agents`,
  `hooks` and `settings` (`lsp.json`, `mcp.json`); each maps a workspace directory, an
  optional global directory and a central directory, with include and exclude globs, an
  optional depth limit, a lint profile and a per collection size limit. Collections are
  configured in YAML, can be disabled, and new ones need no code.
- `--collection NAME` (repeatable) on `pull`, `push`, `status` and `lint`, and
  `steersync config collections` to list the resolved directories. The MCP tools accept a
  `collections` list.
- Lint profiles: `markdown`, `skill` (`SKILL.md` must carry `name` and `description`, name
  should match its directory), `agent`, `hooks` (schema and trigger names), `json` and
  `generic` (binary assets allowed, secrets still scanned), next to the existing `steering`
  rules. `steersync lint` validates every collection with its profile; `--profile` applies one
  to explicit paths.
- `docs/central-layout.md` describes a full `.kiro/` tree in the central repository.

### Changed

- **Central repository layout.** Each collection lives in its own directory:
  `global/steering/`, `projects/<slug>/steering/`, `projects/<slug>/specs/` and so on.
  Repositories created with 0.1 must move their files into `steering/`; `pull` and `status`
  warn while files are still found directly under `global/` or `projects/<slug>/`.
- Lock files are version 2 and record hashes per collection. Version 1 files are read as the
  steering collection and upgraded on the next pull. Pulling a subset of collections keeps the
  lock entries of the others.
- `paths.workspace_steering` and `paths.global_steering` are deprecated in favour of
  `collections.steering.workspace_path` and `collections.steering.global_path`; the old keys
  still load with a warning.
- `steersync init` excludes every synced workspace directory from the project repository
  unless a collection sets `git_exclude: false`.
- Push plans, JSON output and the MCP results are keyed by collection; push plan paths carry
  the central directory prefix (`steering/api.md`). One push proposes every selected
  collection in a single branch and commit.
- File discovery returns paths sorted by relative path, independent of directory walk order.

- Releases publish to PyPI through trusted publishing with PEP 740 attestations. The
  release jobs now live in `ci.yml`, the workflow the PyPI publisher is registered for, and
  run after the normal CI jobs on `main` when the package version has no release yet; the
  separate `Release` workflow is gone. PyPI rejects attestations produced from a reusable
  workflow, which is why the earlier split between `release.yml` and `ci.yml` could not
  publish. The GitHub release attests and attaches the very files that went to PyPI
  instead of building a second time. The `PUBLISH_TO_PYPI` variable is gone.

### Fixed

- A server that rejects push options also closes the connection, which the retry logic
  treated as a network fault; each proposal to such a server ran two needless retries with
  backoff before the push without options. The rejection is now recognised as permanent.

## [0.1.0] - 2026-10-10

### Added

- `steersync pull` applies global and per project steering from a central git repository,
  keeps locally modified files unless `--force` is given, prunes files removed centrally and
  records what it wrote in a lock file so later pulls can tell local edits from stale copies.
- `steersync push` proposes workspace or global steering as a content hashed branch with
  GitLab merge request push options, dedupes identical proposals and falls back to a plain
  push when the server does not accept push options.
- `steersync status`, `lint`, `doctor`, `config show|validate|default`.
- `steersync init` writes `.git/info/exclude` entries and an optional configuration file,
  and `steersync install-hook` installs a chaining pre-push hook without touching
  `core.hooksPath`.
- `.pre-commit-hooks.yaml` exposing `steersync-push` (pre-push) and `steersync-lint`.
- `steersync kiro-hook` renders Kiro CLI 2.x `agentSpawn` and V3 `SessionStart` hooks and
  `steersync mcp-config` renders an `mcp.json` fragment.
- Optional MCP stdio server (`steersync[mcp]`) exposing `pull_steering`, `push_steering`,
  `diff_steering` and `lint_steering`.
- Lint rules for Kiro front matter, file size, UTF-8, level one headings, `#[[file:]]` and
  `#[[folder:]]` references, secret patterns and pymarkdown style checks.
- Layered YAML configuration with environment overrides and strict pydantic validation.
- CI pipeline with ruff, mypy strict, pytest with coverage gate, bandit, pip-audit, CodeQL,
  wheel smoke test, Dependabot and a tag driven release workflow with build provenance and
  PyPI trusted publishing.
- GitHub releases carry the wheel, the sdist and a `SHA256SUMS` file with build provenance
  attestations. Releases run the full CI gate first and no longer depend on PyPI being set
  up; publishing to PyPI is enabled with the `PUBLISH_TO_PYPI` repository variable.
- Releases are driven by `__version__`: merging a version bump to `main`, or running the
  `Release` workflow manually, creates the annotated tag and the release, so nobody needs
  permission to push tags.
- CI lints every workflow with actionlint and shellcheck.
- `pull --best-effort` warns and exits 0 on failure; the generated Kiro hooks use it.
- Repository description, homepage and topics are kept in `.github/repository.yml` and
  applied by a workflow when a `REPO_ADMIN_TOKEN` secret is configured.

### Security

- Symlinks in central repository content are never followed, so a link such as
  `global/x.md -> ~/.ssh/id_rsa` cannot copy local secrets into steering or back upstream.
- Git hooks never run inside the cache clone, for any operation.
- The cache directory is created with mode 0700.
- Lock files never store credentials from the remote URL, and a remote URL with an embedded
  password triggers a warning.
- git subprocesses never inherit standard input, which protects the MCP stdio protocol stream.
- Every GitHub Action and pre-commit hook is pinned to a commit SHA, and CodeQL also analyses
  the workflows. Pull requests can run dependency review once the Dependency graph is enabled
  and the `DEPENDENCY_REVIEW` repository variable is set.

### Reliability

- An exclusive lock serialises processes that share a cache clone (`sync.lock_timeout_seconds`).
- Network operations retry transient failures with exponential backoff and jitter
  (`git.retries`, `git.retry_backoff_seconds`, `git.retry_max_backoff_seconds`).
- A corrupt or half written cache clone, or a stale `index.lock`, is repaired automatically.
- A failed push always returns the cache clone to a clean default branch.
- Pre-push hook runs use a bounded git timeout (`hook.timeout_seconds`) and never block the
  push on any unexpected error unless strict mode is enabled.
- Central and local content is checked against `sync.max_files` and `sync.max_total_bytes`.
- Synced files are written with mode 0644 instead of 0600.
- The console entry point turns unexpected errors into a one line message and exit code.
- Tests run on Linux and macOS.
