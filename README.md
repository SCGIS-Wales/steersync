# steersync

Sync the whole [Kiro](https://kiro.dev) `.kiro/` configuration, steering, specs, skills,
agents, hooks and settings, between developer machines and a central git repository. A plain
Python CLI runs from a git pre-push hook and shells out to the `git` binary, so your existing
SSH agent or credential helper does the authentication. An MCP server and Kiro session hooks
are optional layers on top of the same core.

Kiro CLI has no built in command to import or sync its configuration from a remote
repository; its guidance is to copy files from a central repository into `~/.kiro/` and
`.kiro/`. steersync automates that in both directions for every *collection* it is
configured with:

| Direction | Command | What happens |
|---|---|---|
| Central to local | `steersync pull` | Shallow clones or refreshes the central repository, writes `global/<collection>/` to `~/.kiro/<dir>/` and `projects/<slug>/<collection>/` to `<repo>/.kiro/<dir>/`, prunes files removed centrally, keeps files you edited locally. |
| Local to central | `steersync push` | Lints every collection with its own profile, pushes them together to one branch named after the project, user and content hash, and sends GitLab push options so a merge request is opened for review. |

## Collections

A collection is one kind of Kiro content. The packaged defaults cover the full `.kiro/`
layout; each one is configurable and new ones are added with a single YAML block.

| Collection | Workspace | Global (`~/.kiro`) | Central directory | Files | Lint profile |
|---|---|---|---|---|---|
| `steering` | `.kiro/steering` | `steering` | `steering/` | `**/*.md` | `steering`: Kiro front matter, references, secrets, markdown |
| `specs` | `.kiro/specs` | none | `specs/` | `**/*.md` | `markdown`: secrets and markdown style |
| `skills` | `.kiro/skills` | `skills` | `skills/` | everything (`SKILL.md`, `scripts/`, `references/`, `assets/`) | `skill`: `SKILL.md` needs `name` and `description`, secrets in scripts |
| `agents` | `.kiro/agents` | `agents` | `agents/` | `**/*.json`, `**/*.md` | `agent`: valid JSON with a `name`, flags deprecated `toolsSettings` |
| `hooks` | `.kiro/hooks` | `hooks` | `hooks/` | `**/*.json` | `hooks`: `version`, `hooks[]` with `name`, `trigger`, `action` |
| `settings` | `.kiro/settings` | none | `settings/` | `lsp.json`, `mcp.json` | `json`: valid JSON and secrets |

Execute bits on skill scripts survive the round trip, hidden files are never synced, and
files a collection's patterns do not select are never touched on either side. Limit any
command to some collections with `--collection NAME` (repeatable), list them with
`steersync config collections`, and see [docs/central-layout.md](docs/central-layout.md) for
a worked example of a full `.kiro/` tree in the central repository.
[RFC 0001](docs/rfc/0001-central-kiro-configuration-sync.md) describes the end to end flow
between a project repository, the workstations and the central repository, with an
architecture diagram in [SVG](docs/diagrams/kiro-config-sync.svg),
[PNG](docs/diagrams/kiro-config-sync.png) and [draw.io](docs/diagrams/kiro-config-sync.drawio).

## Install

Requires Python 3.14 or newer and git 2.18 or newer. Every
[GitHub release](https://github.com/SCGIS-Wales/steersync/releases) carries the wheel, the
sdist and a `SHA256SUMS` file, with signed build provenance.

```bash
VERSION=0.1.0
uv tool install "https://github.com/SCGIS-Wales/steersync/releases/download/v${VERSION}/steersync-${VERSION}-py3-none-any.whl"
# with the MCP server
uv tool install "steersync[mcp] @ https://github.com/SCGIS-Wales/steersync/releases/download/v${VERSION}/steersync-${VERSION}-py3-none-any.whl"
# verify where the wheel came from
gh attestation verify "steersync-${VERSION}-py3-none-any.whl" --repo SCGIS-Wales/steersync
```

Every release is also published to [PyPI](https://pypi.org/project/steersync/) through
trusted publishing, so `uv tool install steersync` (or `steersync[mcp]`) installs the same
files that the GitHub release carries.

## Quick start

1. Point steersync at the central repository, once per machine:

   ```bash
   mkdir -p ~/.config/steersync
   cat > ~/.config/steersync/config.yaml <<'YAML'
   remote:
     url: git@gitlab.example.com:platform/kiro-steering.git
     allowed_hosts: [gitlab.example.com]
   YAML
   steersync config validate
   ```

2. Prepare a project. This adds every synced directory (`.kiro/steering/`, `.kiro/specs/`,
   `.kiro/skills/`, ...) and the lock file to `.git/info/exclude` (so centrally managed
   content is never committed to the project by accident; set `git_exclude: false` on a
   collection to keep committing it) and installs a chaining pre-push hook:

   ```bash
   cd ~/code/my-service
   steersync init --hook
   steersync pull
   ```

3. Edit anything under `.kiro/`. On your next `git push`, or on demand with
   `steersync push`, every collection is proposed to the central repository as one merge
   request.

Run `steersync doctor` at any time to see what steersync has resolved.

## Central repository layout

```text
kiro-config/
├── global/                        # applied to every machine (~/.kiro)
│   ├── steering/engineering-rules.md
│   ├── skills/release-checklist/SKILL.md
│   └── hooks/steersync.json
└── projects/
    └── acme/platform/api/         # derived from the project's origin URL
        ├── steering/product.md
        ├── specs/tls-cert-staged-rotation/tasks.md
        ├── skills/venafi-tpp-websdk/SKILL.md
        ├── agents/entra-app-registration.json
        ├── hooks/tests-on-stop.json
        └── settings/lsp.json
```

The project slug is the remote path with the host and `.git` removed and unsafe characters
replaced, so `ssh://git@gitlab.example.com/acme/platform/api.git` becomes
`acme/platform/api`. The `global` and `projects` directory names and every collection
directory are configurable. Repositories created with steersync 0.1, which kept steering
directly under `global/` and `projects/<slug>/`, need their files moved into a `steering/`
subdirectory; `pull` and `status` print a warning pointing at them until that is done.

## Commands

| Command | Purpose |
|---|---|
| `steersync pull [--force] [--dry-run] [--no-global] [--no-workspace] [--strict] [--best-effort] [-c NAME] [--json]` | Apply central collections locally. `--best-effort` warns and exits 0 on failure. |
| `steersync push [--dry-run] [--scope global] [--no-lint] [-m MSG] [-c NAME] [--json]` | Propose local collections centrally in one branch. `--from-hook` is used by git hooks and never blocks the push unless strict. |
| `steersync status [-c NAME] [--json]` | Show adds, updates, deletes and local edits per collection in both directions. |
| `steersync lint [PATHS] [-c NAME] [--profile P] [--json]` | Validate every collection with its profile, or explicit paths. Exit code 4 on errors. |
| `steersync init [--write-config] [--hook] [--no-exclude]` | Prepare a repository. |
| `steersync install-hook [--force]` | Install or update the chaining pre-push hook. |
| `steersync kiro-hook [--engine v2\|v3] [--write]` | Render a Kiro hook that runs `steersync pull --quiet` at session start. |
| `steersync mcp-config [--remote URL]` | Render an `mcp.json` fragment. |
| `steersync mcp` | Run the MCP stdio server. |
| `steersync config show\|validate\|default\|collections` | Inspect configuration and the resolved collection directories. |
| `steersync doctor` | Report git version, resolved paths, remote and project slug. |

Global options: `--config PATH`, `--log-level`, `--log-format text|json`, `--quiet`.

## Configuration

Configuration is layered: packaged defaults, then one YAML file, then environment variables.
The file is found in this order: `--config`, `$STEERSYNC_CONFIG`, `<repo>/.kiro/steersync.yaml`,
`$XDG_CONFIG_HOME/steersync/config.yaml` (default `~/.config/steersync/config.yaml`). See
[`steersync.example.yaml`](steersync.example.yaml) for every key and `steersync config default`
for the packaged defaults.

| Variable | Overrides |
|---|---|
| `STEERSYNC_REMOTE` | `remote.url` |
| `STEERSYNC_DEFAULT_BRANCH` | `remote.default_branch` |
| `STEERSYNC_CACHE_DIR` | `paths.cache_dir` |
| `STEERSYNC_GIT_BINARY` | `git.binary` |
| `STEERSYNC_LOG_LEVEL`, `STEERSYNC_LOG_FORMAT` | `logging.*` |
| `STEERSYNC_STRICT` | `hook.strict` |
| `KIRO_HOME` | Location of `~/.kiro`, as honoured by kiro-cli |

Unknown keys, invalid patterns, unsafe paths and malformed push options are rejected at load
time with a precise message. The pre 0.2 keys `paths.workspace_steering` and
`paths.global_steering` still load, with a deprecation warning, as the steering collection's
`workspace_path` and `global_path`.

### Adding or changing a collection

Collections are merged over the packaged defaults, so a project only lists what differs:

```yaml
collections:
  specs:
    enabled: false                # do not sync plans for this project
  settings:
    include: [lsp.json]           # keep mcp.json local
  runbooks:                       # a new collection, central directory runbooks/
    description: Operational runbooks
    workspace_path: docs/runbooks
    include: ["**/*.md"]
    lint: markdown
    git_exclude: false            # the project keeps committing these too
  agents-md:                      # a single file at the repository root
    workspace_path: .
    include: [AGENTS.md]
    max_depth: 1
    lint: markdown
    git_exclude: false
```

Every key is documented in `steersync config default`. A collection needs a `workspace_path`
or a `global_path`; `central_dir` defaults to the collection name; enabled collections may not
share a directory on either side.

## Reliability

| Mechanism | Configuration |
|---|---|
| Clone, fetch, ls-remote and push retry transient network and server errors with exponential backoff and jitter. Authentication and not found errors fail at once. | `git.retries`, `git.retry_backoff_seconds`, `git.retry_max_backoff_seconds` |
| An exclusive lock serialises processes sharing a cache clone, such as a Kiro session hook and a git hook. The operating system releases it if a process dies. | `sync.lock_timeout_seconds` |
| A corrupt or half written cache clone, or a stale `index.lock`, is deleted and cloned again. The cache is never deleted for network errors. | automatic |
| A failed push always resets the cache clone to a clean default branch. | automatic |
| From a pre-push hook each git call is capped and retried at most once, and no failure, including an unexpected bug, blocks the push unless strict mode is on. | `hook.timeout_seconds`, `hook.strict` |
| Central and local content larger than the limits is refused. | `sync.max_files`, `sync.max_total_bytes` |

## How pull decides what to write

steersync records the hash of every file it wrote, per collection, in a lock file
(`.kiro/steersync.lock.json` in the workspace, `~/.kiro/steersync.lock.json` globally).
Version 1 lock files written by steersync 0.1 are read as the steering collection. On the
next pull:

| Local file | Compared with | Result |
|---|---|---|
| Missing | | Added |
| Equals central | | Unchanged |
| Equals lock, differs from central | | Updated |
| Differs from lock and central | | Kept and reported as a conflict (`--force` overwrites) |
| In lock, removed centrally, unmodified | | Deleted (`sync.prune: false` keeps it) |
| Never synced | | Left alone and reported as local only |

Kiro metadata is never written into steering front matter; all tool state lives in the lock
file, so documents stay exactly as authored. Pulling a subset of collections leaves the lock
entries of the others untouched.

## Git hook

`steersync install-hook` writes `.git/hooks/pre-push`. An existing hook is preserved as
`pre-push.local` and run first with the same arguments and stdin. `core.hooksPath` is never
changed. The hook calls `steersync push --from-hook --remote-url "$2"`, exits 0 on any sync
failure unless `hook.strict` or `STEERSYNC_STRICT=1` is set, and skips pushes that only delete
branches.

Teams that already use [pre-commit](https://pre-commit.com) can use the published hook instead:

```yaml
# .pre-commit-config.yaml in the project
default_install_hook_types: [pre-commit, pre-push]
repos:
  - repo: https://github.com/SCGIS-Wales/steersync
    rev: v0.1.0
    hooks:
      - id: steersync-push
      - id: steersync-lint
```

The lint hook receives the changed files under `.kiro/` and lints each with the profile of
the collection it belongs to.

## GitLab merge requests

The default push options create a merge request against the default branch, remove the source
branch on merge and set a title. Branch names follow `steering/{project}/{user}-{hash}`, where
the hash covers every file, so two people proposing identical content share one branch and a
second identical push is reported as `already-proposed`. Servers that do not advertise push
options (GitHub, plain bare repositories) make git fail, so steersync retries once without them
and says so. Push options may use `{default_branch}`, `{project}`, `{user}` and `{hash}`.

## Lint rules

Each collection runs one lint profile. The `steering` profile applies every rule below.
`markdown` (specs, prompts, references) applies the encoding, size, secret and markdown rules
without Kiro front matter checks. `skill` adds `skill-front-matter-required`,
`skill-requires-metadata` and `skill-name-mismatch` for `SKILL.md`. `json` adds `json-syntax`,
`hooks` adds `hook-schema`, `hook-trigger` and `hook-action`, `agent` adds `agent-schema`,
`agent-name` and `agent-deprecated-key`, and `generic` only enforces the size limit and secret
scanning, allowing binary assets. New profiles are one function in `steersync.lint.PROFILES`.

| Rule | Severity | Check |
|---|---|---|
| `front-matter-position` | error | Front matter must start on line 1; Kiro ignores it after a blank line. |
| `front-matter-yaml` | error | Front matter must be valid YAML. |
| `inclusion-value` | error | `inclusion` is `always`, `fileMatch`, `manual` or `auto`. |
| `filematch-pattern-required`, `filematch-pattern-type` | error | `fileMatch` needs a string or list of strings. |
| `auto-requires-metadata` | error | `auto` needs `name` and `description`. |
| `unknown-key` | warning | Keys Kiro does not know are ignored; `x-steersync` is allowed. |
| `encoding-utf8`, `max-size` | error | UTF-8 only; default limit 64 KiB because always included bytes cost context. |
| `require-h1` | warning | A level one heading is expected. |
| `file-reference-missing`, `file-reference-escape` | warning, error | `#[[file:path]]`, `#[[file:path:line]]`, `#[[file:path:start-end]]` and `#[[folder:path]]` targets must exist inside the workspace. |
| `forbidden-*` | error | AWS keys, private keys, GitHub, GitLab and Slack tokens, generic credentials. Patterns are configurable. |
| `markdown-MD*` | configurable | pymarkdown rules with the front matter extension enabled. |

## Kiro integration

**Session hook (V3).** `steersync kiro-hook --write` creates `.kiro/hooks/steersync.json` with a
`SessionStart` trigger that runs `steersync pull --quiet --best-effort`. For CLI 2.x agents paste the output of
`steersync kiro-hook --engine v2` into the agent's `hooks` block. Field reports say Kiro hooks fire
only in headless runs or with a custom agent active, so treat them as a convenience and keep the
git hook as the dependable trigger.

**MCP server.** Add the fragment from `steersync mcp-config` to `.kiro/settings/mcp.json`. The
server exposes `pull_steering`, `push_steering`, `diff_steering` and `lint_steering`. Kiro does
not pass the shell's `PATH`, `SSH_AUTH_SOCK` or proxy variables to MCP servers, so the rendered
fragment forwards them explicitly through `env`; use an absolute `--command` if `uvx` is not on
Kiro's path.

**Custom agents.** On Kiro CLI V3 custom agents inherit steering, skills and `AGENTS.md` by
default, so list only the always on steering files in `resources` and reference skills with
`skill://.kiro/skills/*/SKILL.md`. A `file://.kiro/steering/**/*.md` glob loads every document
at startup and defeats the `fileMatch`, `manual` and `auto` inclusion modes. On CLI 2.x list
the documents you need explicitly.

## Security

See [SECURITY.md](SECURITY.md). In short: no credentials of its own, no shell, validated remote
URLs with an optional host allow list, path containment for every write, central symlinks never
followed, git hooks disabled in the cache, secret scanning before a push, recursion guards in
hooks, redacted logs, SHA pinned actions, locked and audited dependencies, signed provenance on
releases.

## Development

```bash
uv sync --all-extras --group dev
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md).

## Licence

Apache 2.0. Kiro is a trademark of Amazon Web Services; this project is not affiliated with AWS.
