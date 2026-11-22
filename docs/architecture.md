# Architecture

steersync is a set of small, functional modules. Data flows in one direction: configuration
is loaded once, git state is read into in memory snapshots, a change plan is computed from
pure data, and only then are files or branches written.

Everything is expressed in terms of *collections*. A collection (`CollectionConfig`) names a
workspace directory, an optional global directory under the Kiro home, a directory in the
central repository, the glob patterns that select files inside it and the lint profile that
validates them. The packaged defaults define steering, specs, skills, agents, hooks,
settings, sandbox, cli-settings, workflows, prompts, agents-md and kiroignore; nothing in the sync, lint or CLI code knows those names, so a new collection is
configuration only.

```text
cli.py          click commands; maps results and errors to output and exit codes
cli_common.py   helpers shared by the command modules (workspace root, output, failures)
cli_fleet.py    `steersync fleet` group: registry commands, status, doctor
cli_fleet_actions.py  fleet push, pull, restore and the interactive overview
fleet.py        three way classification, fleet collection, per repository push and pull
fleet_ui.py     rich table and questionary prompts (the only module importing them; lazy)
fleet_doctor.py registry, work tree, sync record, cache clone, lock and backup checks
registry.py     the remembered repositories ($XDG_STATE_HOME/steersync/workspaces.json)
discovery.py    finding repositories set up for steersync below a folder
backups.py      verified backups before a forced overwrite, restore and pruning
retry.py        one retry policy and loop for git and GitLab, with visible retry events
locking.py      cross process exclusive lock around each cache clone
mcp_tools.py    the operations behind the MCP tools, returning typed pydantic reports
mcp_server.py   MCP adapter (needs the mcp extra): structured output, error results,
                progress, elicitation for a forced pull, tool annotations
sync.py         pull / push / status / merge_request orchestration over every collection
  ├── gitops.py     hardened subprocess wrapper around git; URL and ref sanitising
  ├── gitlab.py     merge requests through the GitLab REST API (standard library only)
  ├── steering.py   FileSpec discovery, front matter, snapshots, change plans, atomic writes,
  │                 executable bits, versioned sync record, line ending normalised hashes
  ├── lint.py       lint profiles (steering, markdown, skill, agent, hooks, settings, prompt,
  │                 workflow, kiroignore, json, generic) and the workspace auto load checks
  ├── kiro_rules.py pure schema rules for hooks, agents, mcp/sandbox/cli settings, workflow
  │                 recipes and .kiroignore
  ├── practices.py  best practice warnings cited from the Kiro documentation
  ├── paths.py      Kiro home, per collection directories, lock paths, cache, containment
  └── config.py     layered YAML + environment configuration validated by pydantic
hooks.py        pre-push hook installer, Kiro hook and mcp.json renderers
kiro_version.py Kiro CLI V3 harness detection and the rate limited reminder
migrate.py      legacy *.kiro.hook and inline agent hooks to .kiro/hooks/*.json
logging_utils.py text or JSON logging with secret redaction
errors.py       exception types carrying exit codes
```

## Pull

1. Validate the remote URL and refresh the cache clone (`~/.cache/steersync/repos/<sha>`),
   a shallow single branch clone reset to the remote tip on every run.
2. For each scope (global, workspace) and each selected collection with a directory in that
   scope, snapshot `global/<central_dir>/` or `projects/<slug>/<central_dir>/` using the
   collection's `FileSpec` (include and exclude globs, depth) and lint it with the
   collection's profile.
3. Snapshot the local target directory with the same spec and read that collection's entry
   from the scope's lock file.
4. `plan_changes(source, target, lock)` classifies every path as add, update, delete,
   conflict, unchanged or local only.
5. `apply_plan` writes atomically (temp file + rename) inside the target directory only,
   restoring execute bits recorded in the central clone, then the scope's lock file is
   rewritten once with every collection's hashes. Conflicts keep their previous lock entry so
   later pulls still detect the local edit; collections that were not selected keep theirs.

## Push

1. Snapshot every selected collection of the scope and lint each with its profile; refuse on
   lint errors unless `--no-lint`. The snapshots are combined into one, keyed by
   `<central_dir>/<path>`.
2. Refresh the cache clone and compare with the matching central directories. Identical
   content ends with `up-to-date`.
3. Build the branch name from the template, user and a digest of every file in every
   collection; validate with `git check-ref-format`. If the branch already exists on the
   remote, end with `already-proposed`.
4. In the cache clone: branch from `origin/<default>`, replace each collection directory
   (only the files its patterns select), commit once with the user's git identity, and push
   with push options. Hooks are disabled for the push through `GIT_CONFIG_*` so repository
   hooks never execute during a sync. If the server rejects push options the push is retried
   once without them, and that rejection is never mistaken for a network fault.
5. Reset the cache clone to the default branch.

## Resilience

- `_refresh_cache` falls back to the cached default branch for `pull` and `status` when the
  central repository is unreachable for a transient reason and `sync.offline_fallback` is
  on; `push` always needs a fresh base and never uses the fallback.
- Proposal branches of the project are listed with `git ls-remote` before a push so unmerged
  work is reported; a failure to list them is a warning, never an error.
- The GitLab API is optional: a missing token, a 4xx or a network failure turns into a note
  on the push result, never a failed push, because push options already asked GitLab to
  open the merge request.

## Sync record (lock file)

The workspace record lives at `<git dir>/steersync/lock.json`, inside the git directory and
so outside the work tree: it never appears in `git status` and needs no exclude. The global
record is `~/.kiro/steersync.lock.json`. Schema version 2 records hashes per collection:

```json
{"schema_version": 2, "remote": "...", "commit": "...", "synced_at": "...",
 "collections": {"steering": {"files": {"python.md": "<sha256>"}}, "skills": {"files": {}}}}
```

Hashes are taken over `steering.comparable(bytes)`: text files (no NUL byte) have CRLF
normalised to LF first, so a checkout with `core.autocrlf` is never a local edit; a lone CR
and binary files are hashed as they are. A version 1 file (`"files": {...}`) is read as the
steering collection, and the older `"version": 2` key is still accepted. A record that is
not valid JSON or has the wrong shape is renamed to `lock.json.corrupt-<UTC time>` with a
warning and treated as absent: with no record, `plan_changes` keeps every local file that
differs from the central copy as a conflict, so a damaged record can never cause an
overwrite.

## Fleet

`fleet.collect(paths, env)` builds a `FleetReport` for many repositories:

1. Each registered path is prepared: work tree check, configuration, project slug, central
   URL. Failures become `missing`, `unconfigured` or `error` rows; nothing raises out of one
   repository.
2. Repositories are grouped by central URL. Each remote is refreshed once, under the same
   cache lock as `pull` (`sync.refresh_central`), or reused when younger than
   `fleet.refresh_max_age_seconds`. An unreachable remote with a cache is marked offline.
3. Per repository, in a thread pool (`fleet.max_workers`, default `min(8, cpu)`),
   `sync.compare_workspace` returns a `CollectionView` per collection with
   the local, central and last sync snapshots; `fleet.three_way` turns them into push and
   pull counts and conflicts, and `classify` into a state. Open proposal branches come from
   one `ls-remote` per remote.

Push and pull reuse `sync.push` and `sync.pull` per repository through `fleet.run_batch`,
which records an `Outcome` for each and stops cleanly on Ctrl-C. A forced pull calls
`backups.take` (copy, manifest, verify) and `backups.verify_unchanged` before
`apply_plan`; any failure raises `BackupError` and nothing is written.

The registry is updated atomically under an exclusive lock; a damaged file is set aside and
rebuilt from the next commands that run. `fleet_ui` is imported only by fleet commands, so
rich and questionary never slow down `pull` or the hooks.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (also any `--from-hook` failure in non strict mode) |
| 1 | Generic failure |
| 2 | Configuration error |
| 3 | git failure |
| 4 | Lint errors |
| 5 | Sync conflict, a backup that failed, or a fleet command where a repository failed |
| 6 | Unsafe path |
| 7 | Central or local content exceeds `sync.max_files` or `sync.max_total_bytes` |
| 8 | Timed out waiting for another steersync process using the same cache |
| 9 | GitLab API error or missing token |
| 10 | `v3-status --strict`: a Kiro V3 reminder applies |
| 130 | Interrupted |

## Resiliency

- `locking.py` holds an exclusive `flock` (or `msvcrt` lock on Windows) on
  `<cache>/repos/<sha>.lock` around every pull, push and status.
- `gitops.run_git` retries only idempotent network operations, and only for errors that match
  known transient signatures (DNS, connection reset, early EOF, HTTP 5xx, timeouts).
- `gitops.ensure_cache_clone` deletes and re-clones a cache that cannot be refreshed for a non
  transient reason, and removes a stale `index.lock` before checkout.
- `sync.push` resets the cache clone in a `finally` block so a rejected push cannot leave it on
  a proposal branch.

## Design decisions

- **git binary over GitPython.** GitPython shells out anyway and has had command injection
  CVEs; the binary gives push options, ls-remote and the user's credential helpers for free.
- **Real files, not symlinks.** The Kiro IDE does not follow directory symlinks under
  `~/.kiro`.
- **Metadata in a lock file, not front matter.** Kiro does not document how unknown front matter
  keys are handled, so steering documents are stored byte for byte as authored.
- **Collections over hard coded directories.** Kiro's `.kiro/` layout keeps growing (specs,
  skills, agents, hooks, settings, knowledge bases). Describing each kind as data keeps the
  sync, lint and CLI code free of names and lets a team sync something new without a release.
- **One proposal branch per push.** All collections of a scope travel in one commit, so a
  reviewer sees a steering change together with the skill or spec it belongs to.
- **Never block the user's push.** Steering sync is best effort from a hook; strict mode is an
  explicit opt in.
