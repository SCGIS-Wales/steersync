# Commands

Every command, its options and the exit codes.

| Command | Purpose |
|---|---|
| `steersync setup [--remote URL] [--branch NAME] [--no-mcp] [--no-session-hook] [--no-project]` plus the plan options | One plan for the machine and the current repository, one approval; safe to repeat. |
| `steersync pull [--force] [--dry-run] [--no-global] [--no-workspace] [--strict] [--best-effort] [-c NAME] [--json]` | Apply central collections locally. `--best-effort` warns and exits 0 on failure. |
| `steersync push [--dry-run] [--scope global] [--no-lint] [-m WHY] [-c NAME] [--json]` | Propose local collections centrally in one branch; `-m` adds your reason to the merge request. `--from-hook` is used by git hooks and never blocks the push unless strict. |
| `steersync status [-c NAME] [--json]` | Show adds, updates, deletes and local edits per collection in both directions. |
| `steersync lint [PATHS] [-c NAME] [--profile P] [--format text\|json\|sarif] [--fix] [--unsafe-fixes] [--diff] [--show-fixes] [--select ID] [--ignore ID] [--allow-dirty]` | Validate every collection with its profile, or explicit paths; `--fix` repairs what is safe (see [Fixing lint findings](https://github.com/SCGIS-Wales/steersync/blob/main/docs/lint.md#fixing-lint-findings)). |
| `steersync mr [--branch NAME] [--title T] [-c NAME] [--json]` | Open, or find, the GitLab merge request for the pushed proposal branch. Needs the token in `GITLAB_TOKEN` (configurable). |
| `steersync init [--write-config] [--hook] [--no-exclude] [--kiro] [--mcp-command PATH]` plus the plan options | Prepare a repository: excludes and git hook; `--kiro` also writes the Kiro session hook and MCP server entry into its `.kiro`. The step flags are answers, so with any of them nothing is asked. |
| `steersync restore [STAMP] [--list] [--repo PATH] [--yes]` | Undo a run: put back what setup, init or a forced pull changed, and remove what it created. |
| Plan options | `-y/--yes`, `--no-input`, `-f/--force`, `--plan`/`--dry-run`, `--only ID`, `--skip ID`, `--profile NAME`, `--json`. |
| `steersync install-hook [--force]` | Install or update the chaining pre-push hook. |
| `steersync kiro-hook [--write]` | Render the Kiro V3 hook that runs `steersync pull --quiet` at session start. |
| `steersync mcp-config [--command uvx] [--remote URL]` | Render an `mcp.json` fragment. |
| `steersync mcp` (or `steersync-mcp`) | Run the MCP stdio server. |
| `steersync schema export [--kind K]... [--out DIR]` | Write the JSON Schemas of steering, skill, agent and hook files. |
| `steersync config show\|validate\|default\|collections` | Inspect configuration and the resolved collection directories. |
| `steersync doctor` | Report git version, resolved paths, remote, project slug and the Kiro CLI harness. |
| `steersync fleet` | Every registered repository: a table, then (on a terminal) what to push or pull. |
| `steersync fleet status [--json] [--refresh\|--no-refresh] [--state S] [--global]` | The table, or the stable JSON document. `~/.kiro` is shown only with `--global` (or `fleet.include_global: true`). |
| `steersync fleet push [--repo PATH]... [--state S]... [--all] [--global]` plus the plan options | Propose several repositories, one merge request each; one plan, one approval. |
| `steersync fleet pull [--repo PATH]... [--state S]... [--all] [--overwrite-local] [--global]` plus the plan options | Pull several repositories; local edits are kept unless an `overwrite:` step is selected (`--overwrite-local` preselects it; verified backup first). |
| `steersync fleet restore` | The same as `steersync restore`. |
| `steersync fleet scan ROOTS... [--max-depth N] [--no-add]`, `fleet add\|remove\|list\|prune` | Manage which repositories are registered. |
| `steersync fleet doctor [--repair] [--json]` | Check the registry, work trees, sync records, cache clones, locks and backups. |

Global options: `--config PATH`, `--log-level`, `--log-format text|json`, `--quiet`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (also `--best-effort` failures, which only warn). |
| 1 | Unexpected error, or a plan the user cancelled. |
| 2 | Configuration error, a command line usage error, or input needed in a run that cannot prompt (the message names `--yes`). |
| 3 | git failed. |
| 4 | Lint findings at or above `lint.fail_level` (default `error`) remain, or `lint --diff` found fixes to make. |
| 5 | Conflicting changes, a backup that could not be taken, a plan step that failed, or a `fleet` command where at least one repository failed. |
| 6 | A path escaped the directory it must stay in. |
| 7 | Central content exceeds the safety limits. |
| 8 | Timed out waiting for another steersync process. |
| 9 | GitLab API error or missing token. |
| 10 | `doctor --kiro --strict`: the machine or its agents are below the supported V3 baseline. |
| 11 | `lint --fix`: a fix failed verification; that file was left untouched. |
| 130 | Interrupted (Ctrl-C); a `fleet` summary still lists what completed. |
