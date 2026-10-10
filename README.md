# steersync

Sync [Kiro](https://kiro.dev) steering documents between developer machines and a central
git repository. A plain Python CLI runs from a git pre-push hook and shells out to the `git`
binary, so your existing SSH agent or credential helper does the authentication. An MCP server
and Kiro session hooks are optional layers on top of the same core.

Kiro CLI has no built in command to import or sync steering from a remote repository; its
guidance is to copy files from a central repository into `~/.kiro/steering`. steersync automates
that in both directions:

| Direction | Command | What happens |
|---|---|---|
| Central to local | `steersync pull` | Shallow clones or refreshes the central repository, writes `global/*.md` to `~/.kiro/steering/` and `projects/<slug>/**/*.md` to `<repo>/.kiro/steering/`, prunes files removed centrally, keeps files you edited locally. |
| Local to central | `steersync push` | Lints `.kiro/steering`, pushes it to a branch named after the project, user and content hash, and sends GitLab push options so a merge request is opened for review. |

## Install

Requires Python 3.14 or newer and git 2.18 or newer.

```bash
uv tool install steersync            # or: pipx install steersync
uv tool install "steersync[mcp]"     # adds the MCP server
```

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

2. Prepare a project. This adds `.kiro/steering/` and the lock file to `.git/info/exclude`
   (so steering is never committed to the project by accident) and installs a chaining
   pre-push hook:

   ```bash
   cd ~/code/my-service
   steersync init --hook
   steersync pull
   ```

3. Edit `.kiro/steering/*.md`. On your next `git push`, or on demand with `steersync push`,
   the steering is proposed to the central repository as a merge request.

Run `steersync doctor` at any time to see what steersync has resolved.

## Central repository layout

```text
kiro-steering/
├── global/                       # applied to every workspace (~/.kiro/steering)
│   └── engineering-rules.md
└── projects/
    └── acme/platform/api/        # derived from the project's origin URL
        └── python.md
```

The project slug is the remote path with the host and `.git` removed and unsafe characters
replaced, so `ssh://git@gitlab.example.com/acme/platform/api.git` becomes
`acme/platform/api`. Both directory names are configurable.

## Commands

| Command | Purpose |
|---|---|
| `steersync pull [--force] [--dry-run] [--no-global] [--no-workspace] [--strict] [--json]` | Apply central steering locally. |
| `steersync push [--dry-run] [--scope global] [--no-lint] [-m MSG] [--json]` | Propose local steering centrally. `--from-hook` is used by git hooks and never blocks the push unless strict. |
| `steersync status [--json]` | Show adds, updates, deletes and local edits in both directions. |
| `steersync lint [PATHS] [--json]` | Validate steering documents. Exit code 4 on errors. |
| `steersync init [--write-config] [--hook] [--no-exclude]` | Prepare a repository. |
| `steersync install-hook [--force]` | Install or update the chaining pre-push hook. |
| `steersync kiro-hook [--engine v2\|v3] [--write]` | Render a Kiro hook that runs `steersync pull --quiet` at session start. |
| `steersync mcp-config [--remote URL]` | Render an `mcp.json` fragment. |
| `steersync mcp` | Run the MCP stdio server. |
| `steersync config show\|validate\|default` | Inspect configuration. |
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
time with a precise message.

## How pull decides what to write

steersync records the hash of every file it wrote in a lock file (`.kiro/steersync.lock.json`
in the workspace, `~/.kiro/steersync.lock.json` globally). On the next pull:

| Local file | Compared with | Result |
|---|---|---|
| Missing | | Added |
| Equals central | | Unchanged |
| Equals lock, differs from central | | Updated |
| Differs from lock and central | | Kept and reported as a conflict (`--force` overwrites) |
| In lock, removed centrally, unmodified | | Deleted (`sync.prune: false` keeps it) |
| Never synced | | Left alone and reported as local only |

Kiro metadata is never written into steering front matter; all tool state lives in the lock
file, so documents stay exactly as authored.

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

## GitLab merge requests

The default push options create a merge request against the default branch, remove the source
branch on merge and set a title. Branch names follow `steering/{project}/{user}-{hash}`, where
the hash covers every file, so two people proposing identical content share one branch and a
second identical push is reported as `already-proposed`. Servers that do not advertise push
options (GitHub, plain bare repositories) make git fail, so steersync retries once without them
and says so. Push options may use `{default_branch}`, `{project}`, `{user}` and `{hash}`.

## Lint rules

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
`SessionStart` trigger that runs `steersync pull --quiet`. For CLI 2.x agents paste the output of
`steersync kiro-hook --engine v2` into the agent's `hooks` block. Field reports say Kiro hooks fire
only in headless runs or with a custom agent active, so treat them as a convenience and keep the
git hook as the dependable trigger.

**MCP server.** Add the fragment from `steersync mcp-config` to `.kiro/settings/mcp.json`. The
server exposes `pull_steering`, `push_steering`, `diff_steering` and `lint_steering`. Kiro does
not pass the shell's `PATH`, `SSH_AUTH_SOCK` or proxy variables to MCP servers, so the rendered
fragment forwards them explicitly through `env`; use an absolute `--command` if `uvx` is not on
Kiro's path.

**Custom agents** must list steering in `resources` (`file://.kiro/steering/**/*.md`), otherwise
Kiro does not auto include it.

## Security

See [SECURITY.md](SECURITY.md). In short: no credentials of its own, no shell, validated remote
URLs with an optional host allow list, path containment for every write, secret scanning before
a push, recursion guards in hooks, redacted logs, locked and audited dependencies, signed
provenance on releases.

## Development

```bash
uv sync --all-extras --group dev
uv run pytest --cov
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md).

## Licence

Apache 2.0. Kiro is a trademark of Amazon Web Services; this project is not affiliated with AWS.
