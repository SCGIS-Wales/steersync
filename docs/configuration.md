# Configuration

The configuration file, collections, and how sync stays safe.

## Configuration

Configuration is layered: packaged defaults, then one YAML file, then environment variables.
The file is found in this order: `--config`, `$STEERSYNC_CONFIG`, `<repo>/.kiro/steersync.yaml`,
`$XDG_CONFIG_HOME/steersync/config.yaml` (default `~/.config/steersync/config.yaml`). See
[`steersync.example.yaml`](https://github.com/SCGIS-Wales/steersync/blob/main/steersync.example.yaml) for every key and `steersync config default`
for the packaged defaults.

| Variable | Overrides |
|---|---|
| `STEERSYNC_REMOTE` | `remote.url` |
| `STEERSYNC_DEFAULT_BRANCH` | `remote.default_branch` |
| `STEERSYNC_CACHE_DIR` | `paths.cache_dir` |
| `STEERSYNC_GIT_BINARY` | `git.binary` |
| `STEERSYNC_LOG_LEVEL`, `STEERSYNC_LOG_FORMAT` | `logging.*` |
| `STEERSYNC_STRICT` | `hook.strict` |
| `STEERSYNC_GITLAB_API_URL`, `STEERSYNC_GITLAB_PROJECT` | `gitlab.api_url`, `gitlab.project` |
| `GITLAB_TOKEN` (name set by `gitlab.token_env`) | Token with the `api` scope used only for merge requests |
| `KIRO_HOME` | Location of `~/.kiro`, as honoured by kiro-cli |
| `XDG_STATE_HOME` | Where the fleet registry and backups live (`$XDG_STATE_HOME/steersync`, default `~/.local/state/steersync`) |

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
| When the central repository cannot be reached and a cache clone exists, `pull` and `status` apply the cached copy of the default branch and say so (`offline` in JSON output). Permanent errors such as authentication failures still fail, and `push` never uses the cache. | `sync.offline_fallback` |
| `pull --best-effort` (the Kiro session hook) runs with the hook time budget, so a slow server never stalls a session start. | `hook.timeout_seconds` |
| GitLab API calls retry 429, 502, 503, 504 and a transient 404 with backoff, honouring `Retry-After`, and print each retry. Creating a merge request is idempotent: a retry first looks for the one an earlier attempt may have opened. | `gitlab.retries`, `gitlab.retry_statuses`, `gitlab.retry_backoff_seconds`, `gitlab.retry_max_backoff_seconds`, `gitlab.retry_not_found` |
| A damaged sync record or fleet registry is renamed aside (`*.corrupt-<time>`) and treated as absent, so the next pull keeps every local file and reports it, instead of failing or overwriting. | automatic |
| A forced overwrite takes a verified backup first and aborts if anything changed meanwhile. | `fleet.backup_keep_days`, `fleet.backup_max_bytes` |

## How pull decides what to write

steersync records the hash of every file it wrote, per collection, in a sync record
(`<git dir>/steersync/lock.json` for a workspace, so it never shows up in `git status`, and
`~/.kiro/steersync.lock.json` globally). Text files are compared with line endings
normalised, so a CRLF checkout is not a local edit.
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
