# Architecture

steersync is a set of small, functional modules. Data flows in one direction: configuration
is loaded once, git state is read into in memory snapshots, a change plan is computed from
pure data, and only then are files or branches written.

Everything is expressed in terms of *collections*. A collection (`CollectionConfig`) names a
workspace directory, an optional global directory under the Kiro home, a directory in the
central repository, the glob patterns that select files inside it and the lint profile that
validates them. The packaged defaults define steering, specs, skills, agents, hooks and
settings; nothing in the sync, lint or CLI code knows those names, so a new collection is
configuration only.

```text
cli.py          click commands; maps results and errors to output and exit codes
locking.py      cross process exclusive lock around each cache clone
mcp_server.py   optional MCP tools calling the same functions as the CLI
sync.py         pull / push / status orchestration over every selected collection
  ├── gitops.py     hardened subprocess wrapper around git; URL and ref sanitising
  ├── steering.py   FileSpec discovery, front matter, snapshots, change plans, atomic writes,
  │                 executable bits, versioned lock file
  ├── lint.py       lint profiles (steering, markdown, skill, agent, hooks, json, generic)
  ├── paths.py      Kiro home, per collection directories, lock paths, cache, containment
  └── config.py     layered YAML + environment configuration validated by pydantic
hooks.py        pre-push hook installer, Kiro hook and mcp.json renderers
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

## Lock file

Version 2 records hashes per collection:

```json
{"version": 2, "remote": "...", "commit": "...", "synced_at": "...",
 "collections": {"steering": {"files": {"python.md": "<sha256>"}}, "skills": {"files": {}}}}
```

A version 1 file (`"files": {...}`) is read as the steering collection and rewritten as
version 2 on the next pull.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (also any `--from-hook` failure in non strict mode) |
| 1 | Generic failure |
| 2 | Configuration error |
| 3 | git failure |
| 4 | Lint errors |
| 5 | Sync conflict (raised only by callers that opt in) |
| 6 | Unsafe path |
| 7 | Central or local content exceeds `sync.max_files` or `sync.max_total_bytes` |
| 8 | Timed out waiting for another steersync process using the same cache |
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
