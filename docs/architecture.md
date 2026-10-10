# Architecture

steersync is a set of small, functional modules. Data flows in one direction: configuration
is loaded once, git state is read into in memory snapshots, a change plan is computed from
pure data, and only then are files or branches written.

```text
cli.py          click commands; maps results and errors to output and exit codes
mcp_server.py   optional MCP tools calling the same functions as the CLI
sync.py         pull / push / status orchestration
  ├── gitops.py     hardened subprocess wrapper around git; URL and ref sanitising
  ├── steering.py   discovery, front matter, snapshots, change plans, atomic writes, lock file
  ├── lint.py       validation rules producing LintFinding records
  ├── paths.py      Kiro directories, lock paths, cache location, path containment
  └── config.py     layered YAML + environment configuration validated by pydantic
hooks.py        pre-push hook installer, Kiro hook and mcp.json renderers
logging_utils.py text or JSON logging with secret redaction
errors.py       exception types carrying exit codes
```

## Pull

1. Validate the remote URL and refresh the cache clone (`~/.cache/steersync/repos/<sha>`),
   a shallow single branch clone reset to the remote tip on every run.
2. Snapshot the central `global/` and `projects/<slug>/` directories and lint them.
3. Snapshot the local target directories and read the lock files.
4. `plan_changes(source, target, lock)` classifies every path as add, update, delete,
   conflict, unchanged or local only.
5. `apply_plan` writes atomically (temp file + rename) inside the target directory only, then
   the lock file is rewritten. Conflicts keep their previous lock entry so later pulls still
   detect the local edit.

## Push

1. Snapshot and lint the local steering; refuse on lint errors unless `--no-lint`.
2. Refresh the cache clone and compare with the central project directory. Identical content
   ends with `up-to-date`.
3. Build the branch name from the template, user and a digest of every file; validate with
   `git check-ref-format`. If the branch already exists on the remote, end with
   `already-proposed`.
4. In the cache clone: branch from `origin/<default>`, replace the project directory, commit
   with the user's git identity, and push with push options. Hooks are disabled for the push
   through `GIT_CONFIG_*` so repository hooks never execute during a sync. If the server rejects
   push options the push is retried once without them.
5. Reset the cache clone to the default branch.

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

## Design decisions

- **git binary over GitPython.** GitPython shells out anyway and has had command injection
  CVEs; the binary gives push options, ls-remote and the user's credential helpers for free.
- **Real files, not symlinks.** The Kiro IDE does not follow directory symlinks under
  `~/.kiro`.
- **Metadata in a lock file, not front matter.** Kiro does not document how unknown front matter
  keys are handled, so steering documents are stored byte for byte as authored.
- **Never block the user's push.** Steering sync is best effort from a hook; strict mode is an
  explicit opt in.
