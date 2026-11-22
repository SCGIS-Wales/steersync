# steersync fleet

`steersync fleet` works with every repository on the machine that steersync knows about: it
shows which have Kiro changes that are not pushed and which are behind the central
repository, and pushes or pulls a chosen set. This page is the reference; the README has the
[short version](https://github.com/SCGIS-Wales/steersync#working-across-many-repositories).

## Registry

The registry is `$XDG_STATE_HOME/steersync/workspaces.json` (default
`~/.local/state/steersync/workspaces.json`, `%LOCALAPPDATA%\steersync` on Windows). It lists
each repository's resolved path, how it was added (`command` for `pull`, `push`, `status` and the MCP
server, `init`, `setup`, `scan`, or `manual` for `fleet add`) and when it was last seen. It is machine state, not
configuration: nothing in it needs editing, and losing it loses nothing but the list.

| Command | Effect |
|---|---|
| `steersync fleet scan ROOTS... [--max-depth N] [--no-add]` | Finds git work trees below each root (default depth `fleet.scan_max_depth`, 4) that are set up for steersync: a sync record, the steersync pre-push hook, the steersync block in `.git/info/exclude`, or `.kiro/steersync.yaml`. Hidden folders, `node_modules`, `venv`, `__pycache__`, `dist`, `build` and `target` are skipped. `--no-add` only lists. |
| `steersync fleet add [PATHS]` | Registers repositories (default: the current one). |
| `steersync fleet remove PATHS` | Forgets repositories. Their files are not touched. |
| `steersync fleet prune` | Forgets repositories whose folder is gone or no longer a git work tree. |
| `steersync fleet list [--json]` | Shows the registry. |

Writes are atomic (temporary file and rename) under an exclusive lock, so two commands
registering at the same time never lose an entry. A file that cannot be read is renamed to
`workspaces.json.corrupt-<UTC time>` and an empty registry is used; registering is best
effort and never makes `pull` or `push` fail.

## Status

For each central repository in use, steersync refreshes its cache clone once (under the same
lock as `pull`), unless `--no-refresh` is given or the cache is younger than
`fleet.refresh_max_age_seconds` (300; `--refresh` always refreshes). Repositories are then
compared in parallel (`fleet.max_workers`, 0 meaning `min(8, CPUs)`).

Each file is compared three ways: the local copy, the central copy and the hash recorded at
the last sync. Text files are compared with line endings normalised.

| Local vs last sync | Central vs last sync | Result |
|---|---|---|
| same | same | nothing to do |
| changed | same | to push |
| same | changed | to pull |
| changed | changed (to something else) | conflict (`both`) |
| present, never synced | present, different | conflict (`both`) |

The counts are shown as `+added ~updated -deleted`. The repository's state is the first that
applies: `error`, `diverged` (any conflict), `proposed` (local changes already on an open
proposal branch), `local-ahead`, `central-ahead`, `in-sync`. Rows that cannot be compared
are `missing` (the folder is gone) or `unconfigured` (for example no `origin` remote); the
note says why. The `g` row is `~/.kiro`, compared with `global/` in the central repository.

When the central repository cannot be reached and a cache clone exists, its rows are shown
from the cached copy, marked `offline`, and push and pull are refused for them. Without a
cache they are `error`.

### JSON

`steersync fleet status --json` prints a stable document (new keys may be added; existing
ones keep their meaning):

```json
{
  "remotes": [
    {"url": "git@gitlab.example.com:platform/kiro-config.git", "commit": "48686f7c...",
     "offline": false, "cache_age_seconds": 0.3, "error": null}
  ],
  "repositories": [
    {"path": "/home/me/code/acme-both", "project": "acme/both",
     "remote": "git@gitlab.example.com:platform/kiro-config.git", "state": "diverged",
     "push": {"add": 0, "update": 0, "delete": 0},
     "pull": {"add": 0, "update": 0, "delete": 0},
     "conflicts": ["steering/rules.md"], "proposal": null, "merge_request": null,
     "offline": false, "error": null}
  ]
}
```

Remote URLs are shown with any credentials removed. `--state STATE` (repeatable) filters the
rows. The exit code is 5 when any row is `error`.

The MCP server offers the same report as the read only `fleet_status` tool, so Kiro can
answer "which of my repositories have unpushed Kiro changes?".

## Push and pull

```bash
steersync fleet push [--repo PATH]... [--state STATE]... [--all] [--dry-run] [--yes] [--json]
steersync fleet pull [--repo PATH]... [--state STATE]... [--all] [--overwrite-local] [--dry-run] [--yes] [--json]
```

Both always refresh the central repositories first, list what they will do and ask for
confirmation (`--yes` skips it). `--all` selects every repository that has something to
push, or to pull. A repository that cannot take part is reported as skipped with the
reason: missing, unconfigured, error, offline, or nothing to do.

Each repository is handled exactly as `steersync push` or `steersync pull` would handle it
there, with its own configuration, so each push gets its own proposal branch and merge
request. Repositories run one after another; a failure is recorded and the next one starts.
The summary lists done, skipped and failed rows, each failure with the command to rerun it
in that repository. Ctrl-C stops at once: the repository in progress and the ones after it
are listed as skipped (interrupted), the summary is printed and the exit code is 130. Files
are written one at a time with a rename, so an interrupted pull never leaves a half written
file.

On a terminal, bare `steersync fleet` shows the table and offers: details of one repository
(every file by collection and direction), push, pull, refresh and quit. The push and pull
choices open a checklist with the eligible repositories ticked; rows that cannot take part
are shown greyed out with the reason.

### Overwriting local edits

`fleet pull` never overwrites a locally edited file unless asked. With `--overwrite-local`,
or the "overwrite" choice in the interactive flow, steersync lists every file it will
replace and asks you to type `overwrite` (`overwrite N` for N repositories). With `--yes` in
a script the typed confirmation is skipped, so use it deliberately.

Before anything is written:

1. **Backup.** Every file that will be replaced or deleted is copied to
   `$XDG_STATE_HOME/steersync/backups/<UTC time>/<project>/files/`, keeping its relative
   path and mode, and `manifest.json` records its path, SHA-256, mode and size.
2. **Verify.** The copies are hashed and compared with the manifest; the file list must match
   exactly.
3. **Recheck.** The originals are hashed again just before writing. A file that changed after
   the backup stops the overwrite.

A failure at any step (a full disk, a permission error, a mismatch) stops that repository
with exit code 5 and leaves its files as they were. There is no option to skip the backup.

### Restore

```bash
steersync fleet restore --list            # newest first: stamp, project, workspace, file count
steersync fleet restore STAMP [--repo PATH] [--yes]
```

The backup is verified against its manifest before any file is copied back, with its mode.

## Doctor

`steersync fleet doctor [--json]` checks, and `--repair` fixes where it is safe:

| Check | Problem | Repair |
|---|---|---|
| registry | unreadable or wrong shape | set aside and rebuilt empty |
| work trees | a registered folder is gone | forgotten (`fleet prune`) |
| sync records | a `lock.json` is unreadable | set aside; the next pull keeps every differing local file |
| cache clones | `git fsck` fails, or HEAD is not the default branch | deleted and cloned again |
| cache locks | held by a running process | none: the operating system releases it when that process exits |
| backups | older than `fleet.backup_keep_days` or over `fleet.backup_max_bytes` | the oldest are deleted |

Repairs never touch a work tree. The exit code is 5 while a problem remains.

## Configuration

```yaml
fleet:
  scan_max_depth: 4
  max_workers: 0                # 0 picks min(8, number of CPUs)
  refresh_max_age_seconds: 300
  backup_keep_days: 14
  backup_max_bytes: 536870912   # 512 MiB
```

Each repository's own configuration (remote, collections, branch template) still applies to
its row; the `fleet` section is read from the machine configuration.
