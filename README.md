# steersync

Sync the whole [Kiro](https://kiro.dev) `.kiro/` configuration (steering, specs, skills,
agents, hooks, settings, the sandbox policy, workflow recipes, prompts, `AGENTS.md` and
`.kiroignore` files) between developer machines and a central git repository. It targets Kiro
IDE 1.5 and Kiro CLI 3.5 with the V3 agent harness, and reminds you when a machine is still
on V2. A plain
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
| `specs` | `.kiro/specs` | none | `specs/` | every file in each spec (`requirements.md` or `bugfix.md`, `design.md`, `tasks.md`, metadata) | `markdown`: secrets and markdown style |
| `skills` | `.kiro/skills` | `skills` | `skills/` | everything (`SKILL.md`, `scripts/`, `references/`, `assets/`) | `skill`: `SKILL.md` needs `name` and `description`, secrets in scripts |
| `agents` | `.kiro/agents` | `agents` | `agents/` | `**/*.json`, `**/*.md` (not the `*.json.bak` upgrade backups) | `agent`: V3 universal format, `permissions.rules`, tool names, resources, inline hooks |
| `hooks` | `.kiro/hooks` | `hooks` | `hooks/` | `**/*.json`, legacy `**/*.kiro.hook` | `hooks`: `version: v1`, triggers, actions, timeouts, `confirm` |
| `settings` | `.kiro/settings` | none | `settings/` | `lsp.json`, `mcp.json` | `settings`: shapes, literal secrets, OAuth, machine paths |
| `sandbox` | `.kiro/settings` | none | `sandbox/` | `sandbox.json` | `settings`: `enabled`, `networkAccess` |
| `cli-settings` (off by default) | `.kiro/settings` | none | `cli-settings/` | `cli.json` | `settings`: rejects global only keys such as `chat.agentEngine` |
| `workflows` | `.kiro/workflows` | `workflows` | `workflows/` | `**/*.workflow.json`, `.yaml`, `.yml` | `workflow`: the recipe schema of Kiro CLI 3.5 |
| `prompts` | `.kiro/prompts` | `prompts` | `prompts/` | `**/*.md` | `prompt`: markdown plus the 50 character name limit |
| `agents-md` | the repository | none | `agents-md/` | `**/AGENTS.md` (root and nested) | `markdown` (kept in the project repository too) |
| `kiroignore` | the repository | `settings` (`kiroignore`) | `kiroignore/` | `**/.kiroignore` | `kiroignore`: gitignore syntax, advice on `.env*` and `*.pem` |

Workflow recipes are an opt in Kiro feature: they load only after
`kiro-cli settings chat.enableWorkflows true`, which `steersync doctor` reports. Execute bits
on skill scripts survive the round trip. Hidden directories are never synced,
and a hidden file only when a pattern names it (`**/.kiroignore`). Files a collection's
patterns do not select are never touched on either side, which is why `settings`, `sandbox`
and `cli-settings` can share `.kiro/settings`. The repository wide collections skip
`node_modules`, virtual environments, `dist`, `build` and `target` without walking them. Limit any
command to some collections with `--collection NAME` (repeatable), list them with
`steersync config collections`, and see [docs/central-layout.md](docs/central-layout.md) for
a worked example of a full `.kiro/` tree in the central repository.

Never synced, by design: workspace permissions (`~/.kiro/workspace-roots/<hash>/permissions.yaml`,
kept per user so that a cloned repository cannot inject permission rules) and
`~/.kiro/settings/permissions.yaml`, `~/.kiro/powers/` (installed from a registry),
`~/.kiro/memories/`, `~/.kiro/sessions/`, `~/.kiro/crew/`, `~/.kiro/settings/cli.json`,
`managed-settings.json` and knowledge base indexes (the declarations in agent `resources`
are synced with the agent). Every path above comes from
the Kiro configuration scopes documentation; a new kind of content is one YAML block.

## Install

Requires Python 3.14 or newer and git 2.18 or newer. Every
[GitHub release](https://github.com/SCGIS-Wales/steersync/releases) carries the wheel, the
sdist and a `SHA256SUMS` file, with signed build provenance.

```bash
VERSION=0.6.1
uv tool install "https://github.com/SCGIS-Wales/steersync/releases/download/v${VERSION}/steersync-${VERSION}-py3-none-any.whl"
# or run it without installing
uvx --python ">=3.14" steersync --help
uvx --python ">=3.14" --from "steersync[mcp]" steersync-mcp
# with the MCP server
uv tool install "steersync[mcp] @ https://github.com/SCGIS-Wales/steersync/releases/download/v${VERSION}/steersync-${VERSION}-py3-none-any.whl"
# verify where the wheel came from
gh attestation verify "steersync-${VERSION}-py3-none-any.whl" --repo SCGIS-Wales/steersync
```

Every release is also published to [PyPI](https://pypi.org/project/steersync/) through
trusted publishing, so `uv tool install steersync` (or `steersync[mcp]`) installs the same
files that the GitHub release carries.

## Quick start

```bash
uv tool install --python 3.14 "steersync[mcp]"
cd ~/code/my-service            # any git repository you work in
steersync setup
```

`steersync setup` asks a few questions and does the rest. Nothing needs editing by hand,
and it is safe to run again: it only adds what is missing.

1. **git**: checks that git 2.18 or later is installed.
2. **Central repository**: paste the GitLab page address
   (`https://gitlab.example.com/platform/kiro-config`) or a clone URL. setup tries the SSH
   URL, then HTTPS, with `git ls-remote`, and explains what to fix when neither answers (an
   unknown host key, a refused SSH key, a blocked port 22, no access).
3. **Branch**: offers the repository's default branch (`master` when it cannot tell) and
   confirms it exists with git. An empty project stops here with how to fix it.
4. **Extra folders**: offers to sync folders under `.kiro` that steersync does not sync by
   default, such as `.kiro/notes` or `.kiro/plan`.
5. **Configuration**: saves `~/.config/steersync/config.yaml` (the previous file is kept as
   `config.yaml.bak`). It applies to every repository on the machine.
6. **Kiro MCP server**: adds `steersync` to `~/.kiro/settings/mcp.json` only when no server
   there runs steersync yet, under any name. Every other server is left exactly as it is; the
   original is kept as `mcp.json.bak-steersync`, and the result is read back and compared
   before setup reports success. A file that is not valid JSON is never touched.
7. **Kiro session hook**: adds `~/.kiro/hooks/steersync.json`, so every Kiro session, in any
   repository, starts by pulling the latest merged configuration.
8. **This repository**: adds the synced folders to `.git/info/exclude`, installs the
   pre-push hook that proposes your Kiro changes on `git push`, and shows what the first
   `steersync push` would propose.

For scripts: `steersync setup --yes --remote <url> [--branch <name>]` accepts every default;
`--no-mcp`, `--no-session-hook` and `--no-project` skip a step.

Then edit anything under `.kiro/`. On your next `git push`, or with `steersync push`, every
collection is proposed to the central repository as one merge request. Run
`steersync doctor` at any time to see what steersync has resolved, and `steersync lint` to
validate the configuration against Kiro's rules and best practices.

## Getting started with existing GitLab repositories

Use this when your projects already live on GitLab and already have Kiro files: a committed
`.kiro/` directory, an `AGENTS.md`, or files that exist only on your machine. Nothing is
deleted or overwritten along the way: steersync proposes what you have, you review it as a
merge request, and only merged content flows back to everyone.

The examples use a project at `gitlab.example.com/acme/svc` and a central repository at
`gitlab.example.com/platform/kiro-config`; replace both with yours.

### 1. Create the central repository (once per organisation)

In GitLab, create a project for the shared configuration, for example `platform/kiro-config`.

- Tick **Initialize repository with a README**. steersync works from the default branch
  (`main`), and a new GitLab project has no branch until its first commit; without one every
  command stops with "the central repository ... has no branch 'main'".
- Protect `main` and require merge request approval. Developers need the Developer role so
  they can push proposal branches (`kiro-config/<group>/<project>/<user>-<hash>`); nobody
  pushes to `main` directly.
- Leave it otherwise empty: the first push creates the layout (see
  [Central repository layout](#central-repository-layout)). If you adopt a repository that
  already has Kiro files, they must follow that layout: `global/<collection>/` and
  `projects/<group>/<project>/<collection>/`.

### 2. Install and run setup (every developer, once per machine)

```bash
uv tool install --python 3.14 "steersync[mcp]"
cd ~/code/svc
steersync setup
```

uv installs Python 3.14 when it is missing. Authentication uses your existing git setup (SSH
key or credential helper); steersync never asks for or stores a password. setup asks for the
central repository (paste its GitLab address), confirms the branch with git, saves the
configuration for every repository on the machine, registers the MCP server and the session
hook in `~/.kiro` without disturbing anything already there, and prepares the repository you
run it in (see [Quick start](#quick-start) for each step). It replaces steps 3 and 5 below.

### 3. Optional: share the configuration through the project

To spare teammates even the setup question, commit `.kiro/steersync.yaml` with the same
`remote` section setup saved in `~/.config/steersync/config.yaml`; a project file wins over
the user file inside that repository. For merge request links and `steersync mr`, export a
GitLab token with the `api` scope as `GITLAB_TOKEN`; without one, the merge request is still
opened through git push options.

### 4. Check what you already have

```bash
steersync doctor     # look for "project slug: acme/svc", taken from the origin URL
steersync lint       # errors block a push; warnings are advice
steersync status     # what would be proposed: one "+" line per file
```

The project slug decides where the project lives centrally (`projects/acme/svc/`). It comes
from the `origin` URL you configured, including subgroups, and is not affected by
`insteadOf` rewrites. Fix lint errors before the first push: `steersync migrate-hooks
--write` converts old `*.kiro.hook` files, and `kiro-cli agent upgrade .kiro/agents`
rewrites CLI 2.x agents in place into the universal format that Kiro CLI 2 and 3 both load
(it keeps each original as `<name>.json.bak`, which steersync never syncs).

### 5. Set up each further project

setup prepared the repository you ran it in. In every other repository run
`steersync setup --yes` (it reuses the saved answers) or just `steersync init --hook`. This
adds the synced directories to `.git/info/exclude` (new files there are no longer offered to
the project repository; files you already committed stay tracked) and installs the
pre-push hook. When `~/.kiro` already has the steersync session hook and MCP server, nothing
is added to the repository's own `.kiro`.

### 6. Seed the central repository

```bash
steersync push
```

Every collection is proposed as one branch, and GitLab opens a merge request for it (the
URL is printed when `GITLAB_TOKEN` is set). Review it like code and merge it. Then:

```bash
steersync pull       # "applied 0 change(s)": you and the central repository now agree
```

Pulling before the merge is safe too. A first pull never overwrites a file you already
have: one that differs from the central copy is reported as locally modified and kept, and
one the central repository lacks is reported as local only. Repeat steps 3 to 6 in every
project; each lands in its own `projects/<group>/<project>/` directory.

### 7. Decide where the files live in the project repository

The Kiro files you had committed are still tracked by the project. Pick one model:

- **The central repository is the source of truth** (recommended when several projects
  share configuration). Stop tracking the synced directories in the project. The files stay
  on disk; Kiro keeps loading them, and every session refreshes them:

  ```bash
  git rm -r --cached --ignore-unmatch .kiro/steering .kiro/specs .kiro/skills \
    .kiro/agents .kiro/hooks .kiro/settings .kiro/workflows .kiro/prompts
  git add .kiro/steersync.yaml
  git commit -m "Kiro configuration comes from the central repository (steersync)"
  git ls-files .kiro              # only .kiro/steersync.yaml is left
  ```

  `steersync init` prints this command, limited to the directories that hold tracked files.
  Keep `--ignore-unmatch`: without it `git rm` refuses the whole command when one directory
  has nothing tracked, and nothing is untracked. Untrack them only after the merge request
  from step 6 is merged, or teammates who pull in between lose the files. `AGENTS.md` and
  `.kiroignore` are synced and also stay committed, because other tools read them.
- **Keep committing the files in the project too.** Run `steersync init --hook
  --no-exclude` instead of step 5, and commit what a pull changes like any other change.

### 8. Teammates

```bash
git clone git@gitlab.example.com:acme/svc.git && cd svc
uv tool install --python 3.14 "steersync[mcp]"    # once per machine
steersync setup                                   # once per machine; --yes afterwards
steersync pull
```

From then on every Kiro session starts with a pull, and every `git push` of the project
proposes their Kiro changes.

### 9. Files in your home directory (`~/.kiro`)

`steersync pull` also syncs `global/` with `~/.kiro` (steering, skills, agents, hooks,
prompts and workflows that apply to every project). The first pull keeps your existing
personal files. To share some of them with everyone, propose them with
`steersync push --scope global`; to leave your home directory out entirely, use
`steersync pull --no-global`. Per user files such as workspace permissions, memories and
sessions are never synced.

### 10. Day to day

| You want to | Do |
|---|---|
| Get the latest merged configuration | Nothing: each Kiro session pulls. Or `steersync pull`, or ask Kiro to "pull the kiro config". |
| Share a change | Edit under `.kiro/`, then `git push` the project, or `steersync push`, or ask Kiro to "push my kiro config". |
| Find the merge request | `steersync mr` (needs `GITLAB_TOKEN`). |
| See what differs | `steersync status`. |
| Overwrite your local edits with the central copy | `steersync pull --force`. |

If something looks wrong, `steersync doctor` shows the configuration, the remote, the
project slug, every directory and the Kiro CLI harness.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `the central repository ... has no branch 'main'` | The GitLab project has no commit yet. Create it with a README, or set `remote.default_branch`. |
| git operations time out, `ssh: connect to host gitlab.com port 22` | The network blocks SSH on port 22. GitLab.com also serves SSH on port 443: set `url: ssh://git@altssh.gitlab.com:443/<group>/<project>.git` and add `altssh.gitlab.com` to `allowed_hosts` (accept its host key once with `ssh -T -p 443 git@altssh.gitlab.com`; it is the same key as `gitlab.com`). HTTPS with a credential helper works too. |
| `remote host ... is not in remote.allowed_hosts` | Add the host of `remote.url` to `allowed_hosts`. |
| The first pull reports a file as locally modified | Your copy differs from the merged one, for example a file still tracked by the project in an older version. Compare, then `steersync pull --force` to take the central copy or `steersync push` to propose yours. |
| `push` stops with lint errors | The errors name each file and rule; fix them (`steersync migrate-hooks --write`, `kiro-cli agent upgrade .kiro/agents`) or see [Validation](#validation). |

## Starting Kiro in any repository

The goal is that anyone who clones a project and starts Kiro gets the whole configuration
without asking for it. `steersync init` makes that true for a repository:

| Piece | What it does | Written by |
|---|---|---|
| `.kiro/hooks/steersync.json` | A V3 `SessionStart` hook running `steersync pull --quiet --best-effort` (through `uvx`, pinned to the installed version), so the session starts from the latest merged configuration. Bounded git time; never fails the session. | `steersync init` (or `steersync kiro-hook --write`) |
| `.kiro/settings/mcp.json` | The steersync MCP server, so "pull the kiro config", "push my steering", "raise an MR for the kiro config" or "validate the agents" work in chat. | `steersync init` (merged into an existing file, other servers kept) |
| `.git/hooks/pre-push` | Proposes `.kiro/` on every push of the project. | `steersync init --hook` |

Kiro then loads what it finds: always on steering in every session, `fileMatch`, `manual`
and `auto` steering on CLI V3, skills on demand, prompts as slash commands, hooks and
settings. `steersync lint` warns (`autoload-hook-missing`, `autoload-mcp-missing`) when a
repository lacks either piece. On CLI 2.x, where standalone hooks do not exist, paste
`steersync kiro-hook --engine v2` into the custom agent's `hooks` block.

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
| `steersync setup [--remote URL] [--branch NAME] [--yes] [--no-mcp] [--no-session-hook] [--no-project]` | Guided setup of the machine and the current repository; safe to repeat. |
| `steersync pull [--force] [--dry-run] [--no-global] [--no-workspace] [--strict] [--best-effort] [-c NAME] [--json]` | Apply central collections locally. `--best-effort` warns and exits 0 on failure. |
| `steersync push [--dry-run] [--scope global] [--no-lint] [-m MSG] [-c NAME] [--json]` | Propose local collections centrally in one branch. `--from-hook` is used by git hooks and never blocks the push unless strict. |
| `steersync status [-c NAME] [--json]` | Show adds, updates, deletes and local edits per collection in both directions. |
| `steersync lint [PATHS] [-c NAME] [--profile P] [--json]` | Validate every collection with its profile, or explicit paths. Exit code 4 on errors. |
| `steersync mr [--branch NAME] [--title T] [-c NAME] [--json]` | Open, or find, the GitLab merge request for the pushed proposal branch. Needs the token in `GITLAB_TOKEN` (configurable). |
| `steersync init [--write-config] [--hook] [--no-exclude] [--no-kiro] [--mcp-command PATH]` | Prepare a repository: excludes, git hook, Kiro session hook and MCP server entry. |
| `steersync install-hook [--force]` | Install or update the chaining pre-push hook. |
| `steersync kiro-hook [--engine v2\|v3] [--write]` | Render a Kiro hook that runs `steersync pull --quiet` at session start. |
| `steersync migrate-hooks [--write]` | Convert `*.kiro.hook` files and CLI 2.x inline agent hooks into `.kiro/hooks/*.json`. Dry run by default; never overwrites or deletes. |
| `steersync v3-status [--json] [--strict]` | Report the kiro-cli version, `chat.agentEngine` and agents still in the V2 format. `--strict` exits 10 when a reminder applies. |
| `steersync mcp-config [--command uvx] [--remote URL]` | Render an `mcp.json` fragment. |
| `steersync mcp` (or `steersync-mcp`) | Run the MCP stdio server. |
| `steersync config show\|validate\|default\|collections` | Inspect configuration and the resolved collection directories. |
| `steersync doctor` | Report git version, resolved paths, remote, project slug and the Kiro CLI harness. |

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
| `STEERSYNC_GITLAB_API_URL`, `STEERSYNC_GITLAB_PROJECT` | `gitlab.api_url`, `gitlab.project` |
| `GITLAB_TOKEN` (name set by `gitlab.token_env`) | Token with the `api` scope used only for merge requests |
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
| When the central repository cannot be reached and a cache clone exists, `pull` and `status` apply the cached copy of the default branch and say so (`offline` in JSON output). Permanent errors such as authentication failures still fail, and `push` never uses the cache. | `sync.offline_fallback` |
| `pull --best-effort` (the Kiro session hook) runs with the hook time budget, so a slow server never stalls a session start. | `hook.timeout_seconds` |

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
    rev: v0.6.1
    hooks:
      - id: steersync-push
      - id: steersync-lint
```

The lint hook receives the changed files under `.kiro/` and lints each with the profile of
the collection it belongs to.

## Proposal branches and GitLab merge requests

Pulls always come from the central repository's default branch (`remote.default_branch`,
`main` unless configured). Pushes go to a proposal branch named after the project repository
you are working in:

```text
kiro-config/{project}/{user}-{hash}
kiro-config/acme/platform/entra-app-registration/dgregor-3f9a1c2e
```

`{project}` is the slug of the project's origin URL, `{user}` comes from the sources listed
in `branch.user_from` (the operating system user name first, reduced to letters, digits and
dashes, then the local part of the git `user.email`, then the git `user.name`) and `{hash}`
covers every synced file, so two people proposing identical content share one branch and a
second identical push is reported as `already-proposed`. The template is configurable; it
must keep `{project}` and `{hash}`.

Before pushing, steersync lists the other proposal branches of the same project still on
the remote and warns about them, so unmerged work is reviewed before more is added
(`branch.warn_existing`). `steersync status` prints the same list.

The merge request is opened in two ways:

1. **Push options.** The default `remote.push_options` ask GitLab to create a merge request
   against the default branch, remove the source branch on merge and set a title. Servers
   that do not advertise push options (GitHub, plain bare repositories) make git fail, so
   steersync retries once without them and says so.
2. **GitLab API.** When the token variable is set (`GITLAB_TOKEN`, name configurable with
   `gitlab.token_env`), `push` confirms or creates the merge request through the REST API and
   prints its URL, and `steersync mr` (or the `raise_merge_request` MCP tool) does the same on
   demand. The API base URL and project path are derived from `remote.url` and can be set
   with `gitlab.api_url` and `gitlab.project`; `gitlab.target_branch` defaults to
   `remote.default_branch`. The token is read from the environment only, never from a file,
   and never logged.

## Validation

Each collection runs one lint profile: `steering`, `agent`, `hooks`, `skill`, `settings`,
`prompt`, `workflow`, `kiroignore`, `markdown`, `json` or `generic`. **Errors** are things
Kiro cannot read or would rewrite (front matter after a blank line, an unknown inclusion
mode, invalid JSON, a `SKILL.md` without `name` and `description`, a hook without
`version: "v1"`, a workflow `repeat` without `maxIterations`, an unknown sandbox
`networkAccess`, a V2 format agent) or secrets, including literal tokens in `mcp.json`
`env`, `headers` and `oauth.clientSecret`; they stop a push. **Warnings** are best practices from
the Kiro documentation, reported objectively and never blocking: a steering file with a
numeric prefix, an `auto` description that is not written as a trigger, a `globs` key where
Kiro reads `fileMatchPattern`, an agent with `tools: ["*"]` or a `file://.kiro/steering/**`
resource that loads everything at startup, inline agent hooks on CLI 3.0, a hook with a
15 minute timeout, a skill name that is not kebab case, an `mcp.json` with a home directory
path, more than 48 KiB of always on steering, missing `product.md`, `tech.md` and
`structure.md`. Every rule, its severity and the documentation page it comes from are listed
in [docs/validation.md](docs/validation.md); thresholds live under `lint.practice_limits`
and `lint.practices: false` turns the warnings off.

## Kiro integration

**Session hook (V3).** `steersync init` (or `steersync kiro-hook --write`) creates
`.kiro/hooks/steersync.json` with a `SessionStart` trigger that runs
`steersync pull --quiet --best-effort` through
`uvx --quiet --python ">=3.14" --from "steersync==<version>"`, so it never depends on which
`steersync` comes first on Kiro's `PATH`. Run `steersync init` again after upgrading to move
the pin. For CLI 2.x agents paste the output of
`steersync kiro-hook --engine v2` into the agent's `hooks` block. Field reports say Kiro hooks
fire only in headless runs or with a custom agent active, so treat them as a convenience and
keep the git hook as the dependable trigger.

**Kiro CLI V3.** Kiro CLI 3.0.0 made V3 the default agent harness; V2 still runs but gets no
new features. `steersync doctor`, `steersync v3-status`, the `kiro_v3_status` MCP tool and
the session hook (at most once a day) check `kiro-cli --version`, `chat.agentEngine`,
`acp.agentEngine` (the harness `kiro-cli acp` starts for editor integrations) and your agent
files. When a reminder applies, run `kiro-cli settings chat.agentEngine v3`, start
`kiro-cli` and run `/upgrade-agent`; for a shared agent directory run
`kiro-cli agent upgrade <dir>`. See <https://kiro.dev/docs/cli/v3/>. The probes never start a
chat, so they use no Kiro credits, and a reminder never fails a sync. Set
`STEERSYNC_NO_V3_REMINDER=1` to silence the session hook reminder.

**MCP server.** `steersync init` registers the server in `.kiro/settings/mcp.json`
(`steersync mcp-config` prints the fragment). It runs through
`uvx --python ">=3.14" --from "steersync[mcp]==<version>" steersync mcp`, so nothing needs
installing (the Python constraint matters: uvx otherwise uses its default interpreter, and the
server exits before Kiro connects when that is older than 3.14), and it
auto approves only the read only tools. The server is built on the MCP Python SDK v2
(`MCPServer`, specification 2026-07-28): every tool returns a typed report with an output
schema, failures are error results the agent can read, pull and push report progress, and
tools carry titles and read only, destructive and idempotent hints. A forced pull overwrites
local edits, so the server asks the user first (elicitation) when the client supports it and
otherwise needs `confirm: true`. The server tells the agent which tool answers
which request, so plain sentences work:

| Say | Tool |
|---|---|
| "pull / sync / refresh the kiro config", "update the steering and skills" | `pull_kiro_config` |
| "push / propose / share my kiro config", "sync my steering up" | `push_kiro_config` |
| "raise / open an MR for the kiro config", "merge request link" | `raise_merge_request` |
| "what changed", "status", "any open proposals for this repo" | `diff_kiro_config` |
| "validate / lint / check the kiro config, agents, hooks, skills" | `validate_kiro_config` |
| "what is synced and where" | `list_kiro_collections` |
| "is steersync set up", "why does the sync fail" | `doctor_kiro_config` |
| "am I on Kiro V3", "do my agents need upgrading" | `kiro_v3_status` |

Every tool accepts a `collections` list. Kiro does not pass the shell's `PATH`,
`SSH_AUTH_SOCK`, proxy variables or `GITLAB_TOKEN` to MCP servers, so the rendered entry
forwards them explicitly through `env`; use `steersync init --mcp-command /abs/path/uvx`
if `uvx` is not on Kiro's path.

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
