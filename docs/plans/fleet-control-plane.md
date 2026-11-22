# Plan: `steersync fleet`, a control plane across every initialised repository

Audience: Claude Code, working in this repository.
Baseline: `main` at `1e9cfca` (Release 0.6.0). Target release: 0.7.0.
Author: Dejan Gregor.

## 1. Goal

One command that shows, for every repository on this machine where steersync has been
initialised, what Kiro configuration has been pushed to the central repository and what
has not, and lets the user select one or many repositories and either push or pull them.

Success means:

- `steersync fleet` opens an interactive overview in under two seconds for 30 repositories
  once the central cache is warm, with one network refresh per central remote, not per
  repository.
- The user selects repositories with a checkbox list and chooses push or pull.
- A pull that would overwrite local `.kiro` edits never happens without an explicit, typed
  confirmation, and a backup is taken first.
- GitLab and git transport faults (HTTP 502, 503, 504, 429, a transient 404, timeouts,
  connection resets) are retried with backoff. The user sees each retry and a clear final
  message when retries are exhausted. One failing repository never stops the others.
- No new file is created inside any repository. Net result of this work is fewer steersync
  files per repository than today.

Non goals: syncing repositories that are not cloned on this machine, a web UI, changing the
proposal branch and merge request model, support for hosts other than GitLab in the API
layer.

Nobody uses steersync yet. Make breaking changes where they simplify the design. Do not
write migration shims or deprecation paths; remove what is replaced.

## 2. What exists today (verified on main)

| Area | Fact | Consequence |
|---|---|---|
| Discovery | Nothing records where `init` or `setup` ran. | A registry is needed. |
| Per repo footprint | `init` writes `.git/info/exclude` entries, optionally `.git/hooks/pre-push`, `.kiro/hooks/steersync.json`, an entry in `.kiro/settings/mcp.json`, and sync writes `.kiro/steersync.lock.json`. `setup` can provide the hook and MCP entry globally under `~/.kiro`, and `init` already skips the workspace copies when it does. | The lock file is the remaining per repo clutter. Move it. |
| Status | `sync.status()` refreshes the cache clone on every call and runs one `git ls-remote` per project for proposal branches. It computes the workspace plan in the push direction only. | Looping it over N repositories costs 2N network calls and cannot show "to pull" or conflicts. Split it. |
| Git resilience | `gitops.run_git(retries=...)` retries when `is_transient()` matches `_TRANSIENT_RE` (covers `HTTP 5xx`, timeouts, resets), with jittered exponential backoff. Retries are reported through `log.warning` only. | Good base. Add a retry observer so the UI can show retries. |
| GitLab API resilience | `gitlab._request()` uses `urllib` and raises `GitLabError` on the first `HTTPError`. No retry at all. | Add a retry policy here. |
| Pull safety | `pull(force=False)` keeps locally modified files and reports them in `PullResult.conflicts`. `force=True` overwrites with no backup. | Add a backup step before any forced apply. |
| Interaction | `setup` uses `click.prompt` and `click.confirm`. No table or multi select UI. | Add a UI layer. |
| Cache | One cache clone per central remote, guarded by `locking.exclusive_lock` through `_with_cache_lock`. | Status for many repositories can share one refreshed clone. Pushes to one remote must stay serial. |

## 3. Decisions

These are settled. Do not reopen them; flag a problem in the pull request if one proves
unworkable.

1. **Command name.** A click group `fleet`. Running `steersync fleet` with no subcommand on
   a terminal opens the interactive overview. Subcommands cover scripted use.
2. **Registry outside repositories.** `$XDG_STATE_HOME/steersync/workspaces.json`, default
   `~/.local/state/steersync/workspaces.json`. On Windows use `%LOCALAPPDATA%\steersync`.
   Resolve through one function in `paths.py`.
3. **Self registering.** Every command that resolves a workspace root (`init`, `setup`,
   `pull`, `push`, `status`, and the MCP tools) upserts that root in the registry. A user
   never has to register by hand; `fleet scan` exists for repositories initialised before
   this release or cloned by other means.
4. **Lock file leaves the work tree (breaking).** Move the workspace lock from
   `.kiro/steersync.lock.json` to `<git dir>/steersync/lock.json`, using `gitops.git_dir()`
   so linked worktrees each get their own. Remove `paths.workspace_lock` from the
   configuration model, the default YAML, the example YAML, the exclude pattern in `init`
   and the documentation. No fallback read of the old location.
5. **Global first footprint (breaking).** `init` no longer writes `.kiro/hooks/steersync.json`
   or the workspace `mcp.json` entry by default. `setup` installs both under `~/.kiro` once
   per machine. Keep `init --kiro` as an opt in for teams that want the pieces committed in
   the project. After this change a default `init` touches only `.git/` and the registry.
6. **UI libraries.** `rich` for the table, live progress and panels; `questionary` for the
   checkbox and select prompts. Both become core dependencies so `uvx steersync fleet` works
   with no extra. Verify the latest stable versions on PyPI at implementation time, confirm
   Python 3.14 wheels or pure Python, set floors to the oldest version that passes the
   minimum versions CI job, and relock. If `questionary` fails either check, use
   `InquirerPy`; do not hand roll terminal key handling. Do not adopt Textual: a full screen
   application is more than this needs and slows start up.
7. **Functional style.** Functions and immutable data (`NamedTuple`, frozen pydantic
   models), as in the rest of the code base. All I/O dependencies (prompt functions, clock,
   sleep, HTTP opener) are passed in as parameters so tests need no monkeypatching of
   globals.
8. **Central remote per repository.** A repository may carry its own `.kiro/steersync.yaml`
   with a different `remote.url`. Group repositories by resolved central remote and refresh
   each remote once.

## 4. User experience

### 4.1 Overview

```text
steersync fleet                                   central: gitlab.example.com/platform/kiro-config @ 4f2a91c0
                                                  refreshed 3s ago, 14 repositories, 2 need attention

 #  Repository              Project slug             State            To push      To pull     Proposal
 1  ~/code/payments-api     acme/payments/api        local ahead      +2 ~1 -0     none        none
 2  ~/code/ledger           acme/ledger              in sync          none         none        none
 3  ~/code/onboarding       acme/onboarding          proposed         +1 ~0 -0     none        !42 open
 4  ~/code/kafka-proxy      acme/kafka/proxy         central ahead    none         +0 ~3 -1    none
 5  ~/code/identity         acme/identity            diverged         +1 ~2 -0     ~1 (1 edited locally)  none
 6  ~/code/old-service      acme/old                 missing          path no longer exists
 7  ~/code/sandbox          none                     error            GitLab 503 after 4 attempts
 g  ~/.kiro (global)        global                   central ahead    none         +1 ~0 -0    none

? What next?  (Use arrow keys)
  > Push selected repositories
    Pull selected repositories
    Show details for one repository
    Refresh
    Quit
```

Rules:

- `+ ~ -` are added, updated and deleted file counts summed over the enabled collections.
  The details view breaks them down per collection and lists file names.
- Colour carries no meaning on its own: every state is also a word. Honour `NO_COLOR` and
  a non colour terminal.
- Sort order: error, diverged, local ahead, central ahead, proposed, in sync, missing.
- The checkbox list preselects the rows the chosen action applies to (push: local ahead and
  diverged; pull: central ahead). Rows the action cannot apply to are shown disabled with
  the reason.

### 4.2 States

Classification is one pure function, `classify(push_plan, pull_plan, proposal, error)`.

| State | Condition |
|---|---|
| `in-sync` | Push plan and pull plan are both empty. |
| `local-ahead` | Push plan has changes, pull plan has none, no proposal branch carries the current digest. |
| `proposed` | Push plan has changes and a proposal branch whose `{hash}` equals the current local digest exists on the central remote. This is "pushed, awaiting merge". Show the merge request reference when the GitLab token is available. |
| `central-ahead` | Pull plan has adds, updates or deletes, no conflicts, push plan empty. |
| `diverged` | Pull plan has conflicts (files edited locally since the last sync that also changed centrally), or both plans have changes. |
| `unconfigured` | No central remote resolves for this repository, or it has no origin remote to derive a slug from. |
| `missing` | The registered path no longer exists or is no longer a git work tree. |
| `error` | Status could not be computed; carry the message. |

### 4.3 Push flow

1. User picks repositories. Show a summary panel: repository, files, target proposal branch.
2. One `Proceed? [y/N]` confirmation for the batch.
3. Lint then push each repository, serial per central remote, with a live progress table:
   pending, linting, pushing, retrying (attempt n of m, reason), done, failed.
4. A lint error fails that repository only. Final summary lists merge request URLs, then
   failures with the reason and the exact single repository command to rerun.

### 4.4 Pull flow

1. Repositories with no conflicts are pulled after one batch confirmation. Nothing local is
   lost in this path, as today.
2. For each repository with conflicts, list the conflicting files and ask per repository:
   `Keep my local edits (safe default)`, `Overwrite local .kiro with central`, `Skip`.
3. Overwrite requires typing the word `overwrite`. For more than one repository in
   overwrite mode, show the full list once and require typing `overwrite N` where N is the
   count. Anything else cancels the overwrite set and continues with the safe ones.
4. Before a forced apply, copy every file that will be replaced or deleted to
   `$XDG_STATE_HOME/steersync/backups/<UTC timestamp>/<project slug>/`, preserving relative
   paths and modes. Print the backup path. Keep backups for `fleet.backup_keep_days`
   (default 14) and at most `fleet.backup_max_bytes` in total; prune on each run.
5. `steersync fleet restore [--list] [TIMESTAMP] [--repo PATH]` copies a backup back.

### 4.5 Scripted use

```text
steersync fleet status [--json] [--refresh/--no-refresh] [--state STATE ...]
steersync fleet push   (--repo PATH ... | --state local-ahead | --all) [--dry-run] [--yes] [--json]
steersync fleet pull   (--repo PATH ... | --state central-ahead | --all) [--dry-run] [--yes]
                       [--overwrite-local] [--json]
steersync fleet list [--json]
steersync fleet scan ROOT ... [--max-depth 4] [--add/--no-add]
steersync fleet add [PATH]          # default: current repository
steersync fleet remove PATH ...
steersync fleet prune               # drop entries whose path is missing
steersync fleet restore ...
```

- Without a terminal (`not sys.stdin.isatty()`), bare `steersync fleet` prints the status
  table and exits; it never blocks on a prompt.
- `--overwrite-local` without `--yes` still asks for the typed confirmation; with `--yes`
  it proceeds and still takes the backup.
- Exit codes: 0 all good, 1 usage or configuration error, 5 at least one repository failed
  (partial success), keep 4 for lint errors as today. Document them.
- `--json` emits one stable document: `{"remotes": [...], "repositories": [{"path",
  "project", "state", "push", "pull", "conflicts", "proposal", "merge_request", "error"}]}`.

## 5. Architecture

New modules, each under roughly 300 lines, no classes beyond data models:

| Module | Responsibility |
|---|---|
| `registry.py` | Load, upsert, remove, prune the workspace registry. Pydantic model with `schema_version: 1`, entries keyed by `Path.resolve()` string, fields `path`, `added_at`, `last_seen_at`, `source` (`init`, `setup`, `command`, `scan`, `manual`). Atomic write (temp file plus `os.replace`), guarded by `locking.exclusive_lock`. A corrupt file is renamed to `workspaces.json.corrupt-<timestamp>` and a fresh one started, with a warning. Never stores URLs with credentials or tokens. Registration failure never fails the calling command. |
| `discovery.py` | `scan(roots, max_depth)`: walk directories, stop descending at a `.git`, skip `node_modules`, `.venv`, hidden directories. A repository counts as initialised when any marker is present: the lock under the git directory, the steersync marker in `hooks/pre-push`, steersync patterns in `info/exclude`, or `.kiro/steersync.yaml`. Read only. |
| `retry.py` | One policy used by both transports. `RetryPolicy(attempts, base_delay, max_delay, jitter)`, `RetryEvent(operation, attempt, of, delay, reason)`, `call_with_retry(func, policy, is_retryable, on_retry, sleep)`. Move `backoff_delay` here and have `gitops` use it. |
| `fleet.py` | Orchestration, no printing. `collect(entries, env, on_event) -> FleetReport`, `push_many(...)`, `pull_many(...)`, `classify(...)`, backup and restore helpers. Emits typed progress events through a callback. |
| `fleet_ui.py` | Rendering with rich and prompting with questionary. Takes a `FleetReport` and prompt callables. The only module that imports the UI libraries, imported lazily from the CLI so other commands keep their start up time. |
| `cli_fleet.py` | The click group and subcommands, registered on `main`. `cli.py` is already 1,334 lines; do not add to it. |

Changes to existing modules:

- `sync.py`: split `status()` into `refresh_central(config, env, url) -> CentralSnapshot`
  (cache directory, commit, offline flag, proposal branches from one
  `list_remote_branches` call using the branch template prefix up to `{project}`) and
  `status_from_snapshot(config, snapshot, workspace_root, ...)`, which does local I/O only
  and returns both the push plan and the pull plan per collection. Keep `status()` as a thin
  composition of the two so the existing command and MCP tool are unchanged in behaviour.
  Add the workspace pull direction plan using the same `plan_changes(central, local, lock)`
  call that `_apply_collection` uses, so the overview and the actual pull can never disagree.
- `sync.pull()`: accept `backup_dir: Path | None`; when `force` is set and a backup
  directory is given, back up before `apply_plan`.
- `gitops.run_git()`: accept `on_retry: Callable[[RetryEvent], None] | None` and call it in
  `_wait_before_retry` alongside the existing log line.
- `gitlab._request()`: route through `call_with_retry`. See section 6.
- `paths.py`: add `state_root(env)`, `registry_path(env)`, `backup_root(env)`; change
  `workspace_lock_path` to take the git directory.
- `config.py` and `default_config.yaml`: add a `fleet` section (`scan_max_depth`,
  `max_workers`, `backup_keep_days`, `backup_max_bytes`) and `gitlab.retries`,
  `gitlab.retry_statuses`, `gitlab.retry_backoff_seconds`, `gitlab.retry_max_backoff_seconds`,
  `gitlab.retry_not_found`. Remove `paths.workspace_lock`. No hardcoded values in code.
- `mcp_tools.py` and `mcp_server.py`: add one read only tool `fleet_status` returning the
  JSON document from 4.5, annotated `readOnlyHint: true`. Do not expose fleet push or pull
  over MCP in this release: an agent acting on many repositories at once is out of scope.

### Performance design

1. Load the registry and each repository's configuration (local, parallel).
2. Group by central remote. For each remote: one cache refresh and one branch listing, run
   in parallel across remotes, each under its existing cache lock.
3. Compute `status_from_snapshot` for every repository in a `ThreadPoolExecutor`
   (`fleet.max_workers`, default `min(8, cpu count)`); this is file hashing and reads of the
   shared cache clone, with no writes to it. Hold the cache lock for the whole read phase of
   a remote so a concurrent steersync process cannot reset the clone mid read.
4. Skip lint in the overview (`run_lint=False`); lint runs at push time.
5. Render the table as soon as local data is ready when `--no-refresh` is given or the
   cache is younger than `fleet.refresh_max_age_seconds`; otherwise show a spinner per
   remote during refresh.
6. Add a benchmark test (30 synthetic repositories, local file remote) asserting the
   collect phase stays under a generous bound, marked so it can be deselected.

## 6. Resilience

### GitLab REST API

| Response | Behaviour |
|---|---|
| 502, 503, 504 | Retry, exponential backoff with jitter, default 4 attempts. |
| 429 | Retry, honour `Retry-After` (seconds or HTTP date), capped by `retry_max_backoff_seconds`. |
| 500 | Retry for GET only. |
| 404 | Retry up to 2 extra times for GET when `gitlab.retry_not_found` is true (default true). GitLab can answer 404 for a branch or merge request for a moment after a push. If it persists, raise with a message that names the three real causes: project path wrong, token lacks the `api` scope or project access, or the object does not exist. |
| 401, 403 | Never retry. Message names the token environment variable and the scope needed. |
| `URLError`, `TimeoutError`, connection reset | Retry as for 503. |
| POST create merge request | Never blind retry. After a failed or timed out create, call `find_open_merge_request` for the source branch first; create again only when none exists. This keeps the operation idempotent. |

Every retry produces a `RetryEvent`; the UI shows `GitLab 503, retry 2 of 4 in 3.1s` on the
repository's row and the plain CLI logs it at warning level. On exhaustion the error reads,
for example, `GitLab API GET merge_requests failed with HTTP 503 after 4 attempts over 21s`.

A merge request lookup failure never fails a push that already succeeded over git: report
the push as done and the merge request as unknown, with the `steersync mr` command to retry.

### Git transport

- Keep `_TRANSIENT_RE`. Add a test for the exact messages git prints for 502, 503 and 504
  over HTTPS (`The requested URL returned error: 503`) and for a 404 on fetch
  (`repository '...' not found`), which stays permanent for git: a 404 there means wrong URL
  or no access, and retrying only delays the answer.
- Surface retries through the new `on_retry` callback.
- A remote that stays unreachable marks every repository on it with the offline state from
  the existing `offline_fallback`, shows the cached commit and its age, and disables push
  for those rows.

### Batch behaviour

- Each repository runs inside its own error boundary: catch `SteersyncError`, record it,
  continue. Catch unexpected exceptions at the boundary too, record the type and message,
  and log the traceback at debug level.
- Ctrl C during a batch finishes or abandons the current git call cleanly (the process
  group kill already exists in `gitops`), prints the summary of what completed and exits 130.
- All file writes stay atomic. A second `steersync fleet` started in parallel waits on the
  cache lock, with the existing timeout and a clear message.

## 7. Phases

Each phase is one pull request to `main`, green on every CI job before merge, with tests in
the same pull request. Follow test driven development: failing test first.

| Phase | Content | Pull request title |
|---|---|---|
| 1 | `retry.py`; GitLab retry policy and idempotent create; `on_retry` in `run_git`; configuration keys; tests with a scripted fake opener returning 503, 503, 200 and 404, 404, 404. | `feat: retry GitLab 502, 503, 504, 429 and transient 404 with visible progress` |
| 2 | Lock file moves under the git directory; `init` stops writing workspace hook and MCP entry by default; remove `paths.workspace_lock`; update docs and golden files. | `feat: keep steersync state out of the work tree` |
| 3 | `registry.py`, `discovery.py`, self registration from every workspace command, `fleet list`, `add`, `remove`, `prune`, `scan`. | `feat: workspace registry and fleet scan` |
| 4 | Split `status()`; pull direction plan; `fleet.py` collect and classify; `fleet status` with table and `--json`; MCP `fleet_status`; benchmark test. | `feat: fleet status across every initialised repository` |
| 5 | `fleet push` and `fleet pull`, backups, `fleet restore`, interactive overview with rich and questionary, typed overwrite confirmation. | `feat: interactive fleet push and pull` |
| 6 | README section with a terminal recording or captured output, `docs/fleet.md`, `docs/architecture.md`, CHANGELOG, example YAML. Release 0.7.0. | `docs: fleet control plane guide` |

Release handling: `scripts/prepare_release.py` maps a `feat!:` title to a major bump. To
stay on 0.x, title pull requests `feat:` and describe breaking changes in the CHANGELOG
under `### Changed`, or apply the `release:minor` label. Apply `release:skip` to phases 1 to
5 if a single 0.7.0 release at phase 6 is preferred; otherwise each phase releases a minor
version. Default: single release at phase 6.

## 8. Testing

- Unit: `classify` as a table driven test over every state; hypothesis property that
  classification is total and that `in-sync` implies both plans empty. Registry round trip,
  concurrent upsert from two processes, corrupt file recovery. Retry policy with injected
  sleep and clock; `Retry-After` parsing; no retry on 401 and 403; POST idempotency.
- Integration: build N repositories and a bare central repository under `tmp_path` with
  `remote.allow_local: true` (the existing fixtures in `tests/conftest.py` do this for one
  repository; extend them). Cover every state in 4.2, partial failure, missing path,
  two central remotes, offline fallback.
- CLI: `click.testing.CliRunner` for every subcommand, `--json` schema checked against a
  pydantic model, non terminal behaviour of bare `fleet`, exit codes.
- UI: prompt callables injected, so flows are tested with scripted answers. Golden files for
  the rendered table at a fixed width with colour off (`tests/golden` already exists).
- Safety: a forced pull creates a backup whose restore reproduces the original bytes and
  modes; typing anything other than `overwrite` changes nothing; `git status --porcelain`
  in each test repository is empty after `init`, `fleet status`, `pull` and `push` when the
  synced directories are excluded, proving no stray files.
- Keep coverage at or above the configured 90 per cent, mypy strict, ruff, bandit and
  pip audit clean, and the minimum dependency versions job passing with the two new
  dependencies.

## 9. Acceptance criteria

1. In a fresh clone, `steersync init` followed by `git status --porcelain --ignored`
   shows no steersync file in the work tree.
2. `steersync fleet` lists that repository without any registration step.
3. With three repositories in the states local ahead, central ahead and diverged, the
   overview shows exactly those states, and `fleet status --json` agrees.
4. Selecting two repositories for push produces two proposal branches and prints two merge
   request URLs when the token is set.
5. A pull on the diverged repository keeps local edits by default; with the typed
   confirmation it overwrites and a backup exists that `fleet restore` puts back.
6. With the GitLab API fake answering 503 twice then 200, the operation succeeds and two
   retry lines are shown. With 503 on every attempt, that repository is reported failed
   with the attempt count, the others complete, and the exit code is 5.
7. `uvx steersync fleet status` works from a clean machine with only uv installed.
8. README documents the command with real captured output.
9. A forced pull with a failing backup changes no local file and reports the failure.
10. A truncated last sync record produces a warning, not an error, and no local edit is
    overwritten.
11. `steersync fleet doctor --repair` restores a deliberately damaged cache clone and the
    next `fleet status` succeeds.
12. A repository checked out with CRLF line endings shows as in sync.

## 10. Notes for the implementer

- The local checkout was on `fix/ty-setup-entry`, one commit ahead of `main`
  (`1465b99`, unmerged). Merge or rebase that first so this work starts from a clean `main`.
- `cli.py` and `sync.py` are the two largest modules. Extracting `status` helpers in phase 4
  is the moment to move shared CLI helpers (`_plan_lines`, `_plans_json`, `_fail`) into a
  small `cli_common.py` that both `cli.py` and `cli_fleet.py` import.
- Keep Oxford English in user facing text and documentation, and avoid hyphens and dashes
  in prose, matching the existing README.
- Write no absolute machine paths into any synced file. The registry is the only place
  that stores them and it never leaves the machine.

## 11. Additional tasks: corruption safety

Implemented in the phases named, each with tests. Delivered together with phases 1 to 6 in
one pull request.

### Task A: backup gate before any overwrite (phase 5)

A forced pull must never overwrite local `.kiro` files unless a verified backup exists.

- Before `apply_plan` runs with `force=True`, copy every file that will be replaced or
  deleted to the backup directory, then verify the backup: same file count, same SHA 256
  per file, same file modes.
- Write a `manifest.json` in the backup directory listing relative path, hash, mode and
  size for each file. `fleet restore` verifies against it before copying back.
- If the copy or the verification fails for any reason (disk full, permission denied,
  hash mismatch), abort the overwrite for that repository, leave local files untouched,
  report the repository as failed with the reason, and continue with the others.
- There is no flag that skips the backup.
- Tests: backup directory made read only; simulated disk full part way through; a file
  changed between backup and apply (detect by hash and abort); restore reproduces the
  original bytes and modes.

### Task B: corrupt or missing last sync record (phase 2)

The last sync record is the lock file under the git directory. A damaged record must never
cause a crash or an overwrite.

- Treat a record that is unreadable, not valid JSON, or fails schema validation as absent.
- Log one warning naming the file. Rename the damaged file to
  `lock.json.corrupt-<UTC timestamp>` so it can be inspected, and continue.
- With no record, any local file that differs from central counts as a local edit and is
  kept. Confirm this is what `plan_changes` does today and add a test that pins it.
- Write the record atomically (temporary file plus `os.replace`) and add a `schema_version`
  field.
- Tests: truncated file, empty file, wrong types, unknown schema version; in every case
  `pull` without force changes no locally edited file.

### Task C: `steersync fleet doctor` (phase 4)

A read only health check with an explicit repair switch.

- Checks: registry parses and every entry is a git work tree; each cache clone passes
  `git fsck --connectivity-only` and its `HEAD` matches the configured default branch;
  each last sync record parses; no lock file is held by a process that no longer exists;
  backup directory is within its size and age limits.
- Output: a table with one line per check, plus `--json`.
- `--repair` performs only safe repairs: rebuild a damaged cache clone from the remote (it
  holds no user data), set aside a corrupt registry or sync record, remove a stale lock,
  prune expired backups. It never touches files in a work tree.
- Exit code 0 when healthy, 5 when a problem remains.

### Task D: verify the cache lock survives a crash (phase 1)

- Finding: `locking.exclusive_lock` already takes an operating system lock (`fcntl.flock`
  on POSIX, `msvcrt.locking` on Windows), which the operating system releases when the
  holding process dies. No change was needed.
- Test: kill a child process that holds the lock, then assert a new process acquires it
  within the timeout.

### Task E: line ending stability (phase 4)

- Establish how snapshots are hashed today when git converts line endings
  (`core.autocrlf`, `.gitattributes`). A repository must not show as local ahead only
  because of CRLF conversion.
- If hashes differ, add `* text=auto eol=lf` to the central repository layout
  documentation and normalise line endings for text files before hashing, with binary
  files left untouched.
- Test: the same content with LF and CRLF yields the in sync state.

### Task F: stale central view is always visible (phase 4)

- When a remote could not be refreshed and the cached copy is used, every affected row
  shows `offline`, the cached commit and its age, and push is disabled for those rows.
- `fleet status --json` carries `"offline": true` and `"cache_age_seconds"` per remote.
- `fleet push` and `fleet pull` refuse to act on an offline remote and say why.
