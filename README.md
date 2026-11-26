# steersync

[![PyPI](https://img.shields.io/pypi/v/steersync)](https://pypi.org/project/steersync/)
[![Python](https://img.shields.io/pypi/pyversions/steersync)](https://pypi.org/project/steersync/)
[![CI](https://github.com/SCGIS-Wales/steersync/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SCGIS-Wales/steersync/actions/workflows/ci.yml)
[![Licence](https://img.shields.io/pypi/l/steersync)](https://github.com/SCGIS-Wales/steersync/blob/main/LICENSE)

Keep the [Kiro](https://kiro.dev) configuration of every repository (steering, specs, skills,
agents, hooks, settings, prompts, workflow recipes, `AGENTS.md`, `.kiroignore`) in one central
GitLab repository. `steersync pull` brings the merged configuration to your machine;
`steersync push` proposes your changes as a merge request; `steersync fleet` shows and syncs
every repository on the machine at once. It uses your existing git authentication and never
overwrites a file you edited without asking.

## Install

Requires Python 3.14 or newer and git 2.18 or newer. [uv](https://docs.astral.sh/uv/)
installs Python 3.14 for you when it is missing.

```bash
uv tool install --python 3.14 "steersync[mcp]"
```

Without uv: `pipx install "steersync[mcp]"` or `python3.14 -m pip install "steersync[mcp]"`.
The `mcp` extra adds the MCP server for Kiro chat; leave it out for the command line only.
To try it without installing: `uvx --python ">=3.14" steersync --help`.

## Quick start

### One repository

```bash
cd ~/code/my-service            # any git repository you work in
steersync setup
```

`steersync setup` detects everything first, shows one plan and asks once. It asks one
question only when no central repository is configured yet: paste the GitLab page address
(`https://gitlab.example.com/platform/kiro-config`) or a clone URL. setup tries SSH, then
HTTPS, with `git ls-remote`, and explains what to fix when neither answers (an unknown host
key, a refused SSH key, a blocked port 22, no access). The branch is the repository's default
(`master` when it cannot tell), checked with git. Then one checkbox (space toggles, enter
applies) covers the plan:

| Step | What it does | Risk |
|---|---|---|
| `config.save` | Saves `~/.config/steersync/config.yaml` for every repository on the machine. | safe |
| `folder:<name>` | Syncs an extra folder such as `.kiro/notes` (off by default). | safe |
| `mcp.register` | Adds `steersync` to `~/.kiro/settings/mcp.json` only when no server there runs steersync yet, under any name. Every other server stays exactly as it is, and the result is read back before setup reports success. A file that is not valid JSON is never touched. | review |
| `hook.session` | Adds `~/.kiro/hooks/steersync.json`, so every Kiro session pulls the latest merged configuration. | safe |
| `repo.exclude`, `repo.hook` | Keeps the synced folders out of `git status` and installs the pre-push hook that proposes your Kiro changes on `git push`. | review |

Every file a run changes is backed up first, and the run ends with
`Undo with: steersync restore <id>`. Steps already in place show as `already done`;
running setup again on a configured machine prints `Nothing to do`. Your last selection is
preselected next time (`ux.remember_selection`).

For scripts and CI: `steersync setup --yes --remote <url> [--branch <name>]` applies the
default selection without asking; `--plan` (or `--dry-run`, with `--json`) shows the plan and
exits; `--only` and `--skip` take step ids or kinds (`--skip repo`); `--no-mcp`,
`--no-session-hook` and `--no-project` leave a step out; `--profile NAME` loads, or saves, a
named selection. Without a terminal, in CI (`CI=true`), with `--no-input` or
`STEERSYNC_NO_INPUT=1`, steersync never prompts: it prints the plan and exits 2 with
`Re-run with --yes to apply N steps, or --plan to inspect.` A destructive step (none in setup
today) needs `--force` or typing the command name.

Then edit anything under `.kiro/`. On your next `git push`, or with `steersync push`, every
collection is proposed to the central repository as one merge request. After it is merged,
everyone gets it on their next Kiro session or `steersync pull`.

### Every repository on the machine

Every repository you run steersync in is remembered. To pick up the ones you set up before,
scan your code folder once, then work with all of them together:

```bash
steersync fleet scan ~/code     # find and remember repositories set up for steersync
steersync fleet                 # overview; on a terminal it asks what to push or pull
```

```text
central: git@gitlab.example.com:platform/kiro-config.git @ 48686f7c (refreshed 1s ago)
7 repositories, 4 need attention

#   Repository             Project         State           To push    To pull        Note
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
1   ~/code/acme-both       acme/both       diverged        none       none, 1 both   none
2   ~/code/acme-ahead      acme/ahead      local-ahead     +1 ~0 -0   none           none
3   ~/code/acme-behind     acme/behind     central-ahead   none       +0 ~1 -0       none
4   ~/code/acme-proposed   acme/proposed   proposed        +1 ~0 -0   none           kiro-config/acme/proposed
                                                                                     /tester-70fa698b
5   ~/code/acme-same       acme/same       in-sync         none       none           none
6   ~/code/plain           none            unconfigured                              no origin remote to
                                                                                     derive a project
7   ~/code/acme-gone       none            missing                                   path no longer exists or
                                                                                     is not a git work tree
g   ~/.kiro (global)       global          central-ahead   none       +1 ~0 -0       none
```

```bash
steersync fleet pull --all      # bring every repository up to date; local edits are kept
steersync fleet push --state local-ahead   # one merge request per repository
```

See [Working across many repositories](https://github.com/SCGIS-Wales/steersync#working-across-many-repositories)
for what each state means and how overwrites are protected.

### Everyday commands

| You want to | Run |
|---|---|
| Get the latest merged configuration | Nothing: each Kiro session pulls. Or `steersync pull`, or ask Kiro to "pull the kiro config". |
| Share a change | Edit under `.kiro/`, then `git push` the project, or `steersync push`, or ask Kiro to "push my kiro config". |
| See what differs | `steersync status` here, `steersync fleet status` everywhere. |
| Find the merge request | `steersync mr` (needs a GitLab token in `GITLAB_TOKEN`). |
| Check the setup | `steersync doctor`, and `steersync fleet doctor` for every repository. |
| Validate the configuration | `steersync lint`. |
| Replace your local edits with the central copy | `steersync pull --force`, or `steersync fleet pull --overwrite-local` (backed up first; undo with `steersync fleet restore`). |

## Contents

- [How it works](https://github.com/SCGIS-Wales/steersync#how-it-works)
- [Collections](https://github.com/SCGIS-Wales/steersync#collections)
- [Getting started with existing GitLab repositories](https://github.com/SCGIS-Wales/steersync#getting-started-with-existing-gitlab-repositories)
- [Working across many repositories](https://github.com/SCGIS-Wales/steersync#working-across-many-repositories)
- [Starting Kiro in any repository](https://github.com/SCGIS-Wales/steersync#starting-kiro-in-any-repository)
- [Central repository layout](https://github.com/SCGIS-Wales/steersync#central-repository-layout)
- [Commands](https://github.com/SCGIS-Wales/steersync#commands) and [exit codes](https://github.com/SCGIS-Wales/steersync#exit-codes)
- [Configuration](https://github.com/SCGIS-Wales/steersync#configuration)
- [Reliability](https://github.com/SCGIS-Wales/steersync#reliability)
- [How pull decides what to write](https://github.com/SCGIS-Wales/steersync#how-pull-decides-what-to-write)
- [Git hook](https://github.com/SCGIS-Wales/steersync#git-hook)
- [Proposal branches and GitLab merge requests](https://github.com/SCGIS-Wales/steersync#proposal-branches-and-gitlab-merge-requests)
- [Validation](https://github.com/SCGIS-Wales/steersync#validation)
- [Fixing lint findings](https://github.com/SCGIS-Wales/steersync#fixing-lint-findings)
- [Front matter reference](https://github.com/SCGIS-Wales/steersync#front-matter-reference), [knowledge notes](https://github.com/SCGIS-Wales/steersync#knowledge-notes-and-knowledge-bases) and [writing good steering](https://github.com/SCGIS-Wales/steersync#writing-good-steering)
- [Kiro integration](https://github.com/SCGIS-Wales/steersync#kiro-integration)
- [Verifying a release](https://github.com/SCGIS-Wales/steersync#verifying-a-release)
- [Security](https://github.com/SCGIS-Wales/steersync#security)
- [Development](https://github.com/SCGIS-Wales/steersync#development)

## How it works

Kiro has no built in command to import or sync its configuration from a remote repository;
its guidance is to copy files from a central repository into `~/.kiro/` and `.kiro/`.
steersync automates that in both directions for every *collection* it is configured with.
It supports Kiro V3 only: Kiro CLI 3.1.0 or later with the V3 agent harness, and Kiro IDE
1.4.86 or later (see [Supported Kiro versions](https://github.com/SCGIS-Wales/steersync#supported-kiro-versions)). It is a plain Python CLI that shells out to the `git` binary, so your
SSH agent or credential helper does the authentication; the MCP server and Kiro hooks are
optional layers on the same core.

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
`steersync config collections`, and see [docs/central-layout.md](https://github.com/SCGIS-Wales/steersync/blob/main/docs/central-layout.md) for
a worked example of a full `.kiro/` tree in the central repository.

Never synced, by design: workspace permissions (`~/.kiro/workspace-roots/<hash>/permissions.yaml`,
kept per user so that a cloned repository cannot inject permission rules) and
`~/.kiro/settings/permissions.yaml`, `~/.kiro/powers/` (installed from a registry),
`~/.kiro/memories/`, `~/.kiro/sessions/`, `~/.kiro/crew/`, `~/.kiro/settings/cli.json`,
`managed-settings.json` and knowledge base indexes (the declarations in agent `resources`
are synced with the agent). Every path above comes from
the Kiro configuration scopes documentation; a new kind of content is one YAML block.

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
  [Central repository layout](https://github.com/SCGIS-Wales/steersync#central-repository-layout)). If you adopt a repository that
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
run it in (see [Quick start](https://github.com/SCGIS-Wales/steersync#quick-start) for each step). It replaces steps 3 and 5 below.

### 3. Optional: share the configuration through the project

To spare teammates even the setup question, commit `.kiro/steersync.yaml` with the same
`remote` section setup saved in `~/.config/steersync/config.yaml`; a project file wins over
the user file inside that repository. For merge request links and `steersync mr`, export a
GitLab token with the `api` scope as `GITLAB_TOKEN`; without one, the merge request is still
opened through git push options.

### 4. Check what you already have

```bash
steersync doctor     # look for "project slug: acme/svc", taken from the origin URL
steersync lint       # errors need fixing; only a possible secret blocks a push
steersync status     # what would be proposed: one "+" line per file
```

The project slug decides where the project lives centrally (`projects/acme/svc/`). It comes
from the `origin` URL you configured, including subgroups, and is not affected by
`insteadOf` rewrites. Fix lint errors when you can (only secrets stop a push). An agent with
keys V3 does not read (`agent-legacy-key`) is fixed by opening it in Kiro and running
`/upgrade-agent`, then committing the result; a legacy `*.kiro.hook` file
(`hook-legacy-file`) is rewritten by hand as `.kiro/hooks/<id>.json` (the error shows the
fields).

### 5. Set up each further project

setup prepared the repository you ran it in. In every other repository run
`steersync setup --yes` (it reuses the saved answers) or just `steersync init --hook`. This
adds the synced directories to `.git/info/exclude` (new files there are no longer offered to
the project repository; files you already committed stay tracked) and installs the
pre-push hook. The session hook and MCP server come from `~/.kiro`, so nothing is written
into the repository's own `.kiro` unless you add `--kiro`.

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
| See every repository at once | `steersync fleet` (see [Working across many repositories](https://github.com/SCGIS-Wales/steersync#working-across-many-repositories)). |

If something looks wrong, `steersync doctor` shows the configuration, the remote, the
project slug, every directory and the Kiro CLI harness.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `the central repository ... has no branch 'main'` | The GitLab project has no commit yet. Create it with a README, or set `remote.default_branch`. |
| git operations time out, `ssh: connect to host gitlab.com port 22` | The network blocks SSH on port 22. GitLab.com also serves SSH on port 443: set `url: ssh://git@altssh.gitlab.com:443/<group>/<project>.git` and add `altssh.gitlab.com` to `allowed_hosts` (accept its host key once with `ssh -T -p 443 git@altssh.gitlab.com`; it is the same key as `gitlab.com`). HTTPS with a credential helper works too. |
| `remote host ... is not in remote.allowed_hosts` | Add the host of `remote.url` to `allowed_hosts`. |
| The first pull reports a file as locally modified | Your copy differs from the merged one, for example a file still tracked by the project in an older version. Compare, then `steersync pull --force` to take the central copy or `steersync push` to propose yours. |
| `push` stops with "possible secret(s)" | A file matches a secret pattern or `mcp.json` holds a literal value. Remove it (use `${VAR}` in `mcp.json`), or pass `--no-lint` if it is not a secret. Other lint errors are reported and never stop a push. |

## Working across many repositories

`steersync fleet` answers "which of my repositories have Kiro changes that are not pushed,
and which are behind the central repository?" for every repository on the machine, and
pushes or pulls a chosen set in one go.

**Which repositories.** Every repository where you run `pull`, `push`, `status`, `init` or
`setup`, or where Kiro starts the MCP server, is remembered in
`$XDG_STATE_HOME/steersync/workspaces.json` (default `~/.local/state/steersync/`). Add the
ones you set up earlier with `steersync fleet scan ~/code` (searches four folder levels by
default; `--no-add` only lists them) or `steersync fleet add PATH`. `fleet list` shows the
registry, `fleet remove PATH` forgets a repository without touching its files, and
`fleet prune` forgets the ones that no longer exist.

**States.** Each repository is compared three ways: your files, the central default
branch, and the record of the last sync. Files that only line endings separate (a Windows
checkout with `core.autocrlf`) count as equal.

| State | Meaning | What to do |
|---|---|---|
| `diverged` | A file changed both locally and centrally since the last sync. | `fleet pull` keeps your edit; compare, then push yours or overwrite with the central copy. |
| `local-ahead` | You have changes the central repository does not. | `fleet push`. |
| `central-ahead` | The central repository has merged changes you do not have. | `fleet pull`. |
| `proposed` | Your changes are already on a proposal branch or merge request. | Get it reviewed and merged. |
| `in-sync` | Nothing differs. | Nothing. |
| `unconfigured` | steersync cannot work out the project, for example no `origin` remote. | `steersync doctor` in that repository. |
| `missing` | The folder is gone or is no longer a git work tree. | `fleet prune`. |
| `error` | Comparing this repository failed; the note says why. | Fix it; every other repository is still shown. |

The `~/.kiro` row (`g`, your global configuration) is left out by default: add `--global`
to `fleet`, `fleet status`, `push` or `pull` for one run, set `fleet.include_global: true`
to always show it, or name it with `--repo ~/.kiro`. Each central repository is refreshed
once per run, not once per repository; `--no-refresh` uses the cached copy, and by default
a copy younger than `fleet.refresh_max_age_seconds` (300) is reused. When the central
repository cannot be reached the table says `offline` and shows the cached copy; pushes and
pulls for those rows are refused until it answers again.

**Pushing and pulling.** On a terminal, bare `steersync fleet` shows the table and asks what
to do: details of one repository, push, pull, refresh or quit. Without a terminal it prints
the table. The same actions take flags for scripts:

```bash
steersync fleet push --repo ~/code/a --repo ~/code/b      # or --state local-ahead, or --all
steersync fleet pull --all --yes
steersync fleet status --json                            # the stable machine readable view
```

Push and pull show one plan, a step per repository, and ask once (one checkbox; `--yes`
applies the default selection; without a terminal they exit 2 naming `--yes`). Each
repository gets its own proposal branch and merge request, exactly as `steersync push` in
that repository would. One failure never stops the others: the summary lists what was
done, skipped and failed, each failure with the command to rerun it, and the exit code is 5
when anything failed.

**Local edits are never lost.** `fleet pull` keeps every file you edited locally and says
so. For a repository whose local edits also changed centrally, the plan has a separate
`overwrite:` step (risk `review`, off by default; `--overwrite-local` preselects it) that
replaces them with the central versions, only after a verified backup:

1. every file about to be replaced or deleted is copied to
   `~/.local/state/steersync/backups/<UTC time>/<project>/` with a `manifest.json` of
   hashes, modes and sizes, and the copy is checked against it;
2. just before writing, the originals are hashed again, and a file that changed since the
   backup stops the overwrite;
3. any failure, such as a full disk, stops the overwrite and leaves your files untouched.

`steersync fleet restore --list` shows the backups and `steersync fleet restore STAMP`
puts the files back (the backup is verified first). Backups older than
`fleet.backup_keep_days` (14) or beyond `fleet.backup_max_bytes` (512 MiB) are pruned by
`steersync fleet doctor --repair`.

**Health check.** `steersync fleet doctor` checks the registry, every work tree, every sync
record, the cache clones (`git fsck`), the cache locks and the backups. `--repair` applies
the safe fixes: a damaged registry or sync record is set aside (renamed, never deleted) and
rebuilt, a broken cache clone is cloned again, old backups are pruned. It never changes a
work tree. Details are in [docs/fleet.md](https://github.com/SCGIS-Wales/steersync/blob/main/docs/fleet.md).

## Starting Kiro in any repository

The goal is that anyone who clones a project and starts Kiro gets the whole configuration
without asking for it. `steersync setup` makes that true once per machine, in `~/.kiro`, so
it covers every repository; teams that prefer to commit the pieces into each project use
`steersync init --kiro` instead:

| Piece | What it does | Written by |
|---|---|---|
| `hooks/steersync.json` | A V3 `SessionStart` hook running `steersync pull --quiet --best-effort` (through `uvx`, pinned to the installed version), so the session starts from the latest merged configuration. Bounded git time; never fails the session. | `steersync setup` in `~/.kiro`, or `steersync init --kiro` (or `steersync kiro-hook --write`) in the repository |
| `settings/mcp.json` | The steersync MCP server, so "pull the kiro config", "push my steering", "raise an MR for the kiro config" or "validate the agents" work in chat. | `steersync setup` in `~/.kiro`, or `steersync init --kiro` in the repository (merged into an existing file, other servers kept) |
| `.git/hooks/pre-push` | Proposes `.kiro/` on every push of the project. | `steersync setup`, or `steersync init --hook` |

Kiro then loads what it finds: always on steering in every session, `fileMatch`, `manual`
and `auto` steering on CLI V3, skills on demand, prompts as slash commands, hooks and
settings. `steersync lint` warns (`autoload-hook-missing`, `autoload-mcp-missing`) when a
repository lacks either piece.

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
| `steersync setup [--remote URL] [--branch NAME] [--no-mcp] [--no-session-hook] [--no-project]` plus the plan options | One plan for the machine and the current repository, one approval; safe to repeat. |
| `steersync pull [--force] [--dry-run] [--no-global] [--no-workspace] [--strict] [--best-effort] [-c NAME] [--json]` | Apply central collections locally. `--best-effort` warns and exits 0 on failure. |
| `steersync push [--dry-run] [--scope global] [--no-lint] [-m WHY] [-c NAME] [--json]` | Propose local collections centrally in one branch; `-m` adds your reason to the merge request. `--from-hook` is used by git hooks and never blocks the push unless strict. |
| `steersync status [-c NAME] [--json]` | Show adds, updates, deletes and local edits per collection in both directions. |
| `steersync lint [PATHS] [-c NAME] [--profile P] [--format text\|json\|sarif] [--fix] [--unsafe-fixes] [--diff] [--show-fixes] [--select ID] [--ignore ID] [--allow-dirty]` | Validate every collection with its profile, or explicit paths; `--fix` repairs what is safe (see [Fixing lint findings](https://github.com/SCGIS-Wales/steersync#fixing-lint-findings)). |
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
| `steersync fleet status [--json] [--refresh\|--no-refresh] [--state S]` | The table, or the stable JSON document. |
| `steersync fleet push [--repo PATH]... [--state S]... [--all]` plus the plan options | Propose several repositories, one merge request each; one plan, one approval. |
| `steersync fleet pull [--repo PATH]... [--state S]... [--all] [--overwrite-local]` plus the plan options | Pull several repositories; local edits are kept unless an `overwrite:` step is selected (`--overwrite-local` preselects it; verified backup first). |
| `steersync fleet restore` | The same as `steersync restore`. |
| `steersync fleet scan ROOTS... [--max-depth N] [--no-add]`, `fleet add\|remove\|list\|prune` | Manage which repositories are registered. |
| `steersync fleet doctor [--repair] [--json]` | Check the registry, work trees, sync records, cache clones, locks and backups. |

Global options: `--config PATH`, `--log-level`, `--log-format text|json`, `--quiet`.

### Exit codes

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
    rev: v0.9.0
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
kiro-config/acme/platform/payments-api/jdoe-3f9a1c2e
```

`{project}` is the slug of the project's origin URL, `{user}` comes from the sources listed
in `branch.user_from` (the operating system user name first, without a domain, so a
domain joined login such as `jdoe@corp.example.com` or `CORP\jdoe` gives `jdoe`, then the
local part of the git `user.email`, then the git `user.name`; reduced to letters, digits and
dashes) and `{hash}`
covers every synced file, so two people proposing identical content share one branch and a
second identical push is reported as `already-proposed`. The template is configurable; it
must keep `{project}` and `{hash}`.

Before pushing, steersync lists the other proposal branches of the same project still on
the remote and warns about them, so unmerged work is reviewed before more is added
(`branch.warn_existing`). `steersync status` prints the same list.

Every proposal is one commit whose message is a short Markdown summary, and GitLab shows
it as the merge request description:

```text
Kiro config: acme/payments-api (7 files, jdoe)

Kiro configuration for **acme/payments-api**, proposed by **jdoe**.

| Collection | Added | Updated | Deleted |
|---|---:|---:|---:|
| steering | 4 | 0 | 0 |
| specs | 1 | 0 | 0 |
| settings | 0 | 1 | 0 |

**Note:** 1 file with lint errors proposed anyway (`steersync lint` shows them):
- `steering/product.md`: the value of `status` contains ': ', ...

<details><summary>All 7 files</summary> ...every file, by collection... </details>

Source: gitlab.example.com/acme/payments-api · base `main` @ d52d9192 · steersync 0.9.0
```

`steersync push -m "why"` puts your reason above the summary. The same title and
description are used when the merge request is created through the API and by
`steersync mr`.

The merge request is opened in two ways:

1. **Push options.** The default `remote.push_options` ask GitLab to create a merge request
   against the default branch, remove the source branch on merge and set the title
(`merge_request.title={title}`; `{project}`, `{user}`, `{hash}` and `{default_branch}`
also work). Servers
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
`networkAccess`, an agent key V3 does not read, a legacy hook file) or secrets, including literal tokens in `mcp.json`
`env`, `headers` and `oauth.clientSecret`. Only secrets stop a push; every other error is
printed and the files are synced as they are. **Warnings** are best practices from
the Kiro documentation, reported objectively and never blocking: a steering file with a
numeric prefix, an `auto` description that is not written as a trigger, a `globs` key where
Kiro reads `fileMatchPattern`, an agent with `tools: ["*"]` or a `file://.kiro/steering/**`
resource that loads everything at startup, inline agent hooks on CLI 3.0, a hook with a
15 minute timeout, a skill name that is not kebab case, an `mcp.json` with a home directory
path, more than 48 KiB of always on steering, missing `product.md`, `tech.md` and
`structure.md`. Every rule, its severity and the documentation page it comes from are listed
in [docs/validation.md](https://github.com/SCGIS-Wales/steersync/blob/main/docs/validation.md); thresholds live under `lint.practice_limits`
and `lint.practices: false` turns the warnings off.

## Fixing lint findings

`steersync lint --fix` repairs what can be repaired without changing what a file means. It
recommends first and applies after approval, the way ruff does:

```bash
steersync lint --diff                 # show every fix as a unified diff; writes nothing (exit 4 if any)
steersync lint --fix                  # apply the safe fixes, backed up; list the unsafe ones
steersync lint --fix --unsafe-fixes   # also apply the unsafe ones
steersync lint --fix --show-fixes --select fm- --ignore fm-move-to-metadata
```

| Class | Meaning | Applied |
|---|---|---|
| safe | Keeps the meaning: a byte order mark, CRLF, blank lines before `---`, `filematchpattern` spelt `fileMatchPattern`, `Always` written `always`, JSON indentation, Markdown whitespace (final newline, trailing spaces, spaces after `#`, list markers and `>`). Bullet, rule and fence styles, list indentation, tabs and blank lines around headings and fences are safe only while the rendered Markdown is unchanged; otherwise they are offered as unsafe | With `--fix`, without a question |
| unsafe | Changes how Kiro loads or renders the file: a misspelt inclusion mode, a comma separated pattern split into a list, a quoted glob, a derived `auto` name, keys moved into `metadata`, a lowercased skill name, blank lines around lists, heading levels, list renumbering, code block style | With `--unsafe-fixes`, or chosen in one checkbox (all unchecked) |
| never | Line length, bare URLs, fence languages, tables, anything inside `#[[...]]` or fenced code, agent and hook semantics, legacy V3 keys, any file with a possible secret | Reported only |

Markdown fixes run PyMarkdown's fix mode on the body only (front matter is put back byte for
byte), and only for rules the installed PyMarkdown reports it can fix; blank lines around
headings, fences and lists are steersync's own. `lint.fix.engine: builtin` leaves PyMarkdown
out. Every fix is verified before anything is written: the front matter data (other than the keys
the fix changes), the JSON data, the rendered Markdown, every `#[[...]]` reference and every
fenced code block must stay the same, the fix must be idempotent and relinting must find
nothing new. A file that fails is left untouched and lint exits 11. Files with uncommitted
changes are left alone unless `--allow-dirty` (`lint.fix.require_clean_git`). Each file's hash
is checked again just before an atomic write, and one backup covers the run:
`Undo with: steersync restore <id>`. The classes are configuration (`lint.fix.safety`,
`extend_safe_fixes`, `extend_unsafe_fixes`, `unfixable`).

`--format json` (or `--json`) prints one document: `version`, `findings`, `fixes` (path,
rules, safety, unified diff, whether it was applied) and `summary`, with the counts also at
the top level. `--format sarif` prints SARIF 2.1.0 for code scanning: the rule catalogue with
each rule's fix safety, and each fixable result with its fix as `artifactChanges`. With either
format every message goes to stderr, so stdout stays parseable:

```bash
steersync lint --diff --format sarif > steersync.sarif   # upload with github/codeql-action/upload-sarif
```

## Front matter reference

Kiro V3 reads only a few front matter keys. Everything a team adds for itself goes in one
`metadata` map of strings, which `steersync lint` checks against
`lint.frontmatter.metadata_fields` (owner, tags, references, review-by, verified-on, status,
version; add your own fields there). `steersync schema export --out schemas/` writes the JSON
Schemas of steering, skill, agent and hook files for editors.

| File | Kiro native front matter | Organisational metadata |
|---|---|---|
| Steering `.kiro/steering/*.md` | `inclusion` (`always` default, `fileMatch`, `manual`, `auto`), `fileMatchPattern` (a glob or a YAML list), `name` and `description` (both required for `auto`) | `metadata` map |
| Skill `SKILL.md` | `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools` | the specification's `metadata` map |
| Agents, hooks, settings, prompts, workflows | as documented by Kiro | `.kiro/steersync.meta.yaml` (sidecar) |

Facts the rules encode: front matter must be the first content of the file (no byte order
mark, blank line or space before `---`); `inclusion: auto` loads only `name` and
`description` until the model judges the file relevant, then the whole file (it is not vector
search); a comma separated `fileMatchPattern` string never matches and Kiro does not warn, so
use a YAML list; files under `.kiro/steering/` without `.md` are not loaded. File references
are `#[[file:path]]`, `#[[file:path:N]]`, `#[[file:path:start-end]]` and `#[[folder:path]]`,
resolved from the workspace root (workspace steering), `~/.kiro/steering/` (global steering)
or the folder that holds the file (`AGENTS.md`).

```markdown
---
inclusion: fileMatch
fileMatchPattern: ["src/api/**/*.py", "tests/api/**/*.py"]
metadata:
  owner: "platform/api-guild"
  tags: "api,security"
  references: "#[[file:docs/adr/0007-errors.md]] https://example.org/api-style"
  review-by: "2027-03-31"
  status: "active"
---

# API conventions

Use the error envelope in #[[file:docs/api/errors.md:12-40]] for every non 2xx response.
```

```markdown
---
inclusion: auto
name: db-migrations
description: Alembic migration rules. Use when creating, editing or reviewing database migrations.
metadata:
  owner: "data/platform"
---
```

A sidecar keeps metadata for files that have no front matter of their own:

```yaml
# .kiro/steersync.meta.yaml
".kiro/agents/backend-dev.json": { owner: "platform", tags: "agents", review-by: "2027-02-28" }
".kiro/hooks/lint-on-save.json": { owner: "dx", tags: "quality" }
```

Set `lint.frontmatter.metadata_placement: sidecar` to keep all metadata there.

## Knowledge notes and knowledge bases

Steering always loads whole files. For team knowledge (runbooks, incident notes, CVE
exposure records) Kiro knowledge bases are the better fit: the `knowledge` tool retrieves
chunks on demand (the Best index uses local all-MiniLM-L6-v2 embeddings, the Fast index
BM25), and nothing enters the context until the model asks. Kiro still labels knowledge
experimental and it is CLI only; indexes are local to each machine and agent, and never
synced.

The `knowledge` collection is **off by default**. To use it, enable it and choose the folder
(default `.kiro/knowledge`, so nothing new appears at the repository root):

```yaml
collections:
  knowledge:
    enabled: true
    workspace_path: .kiro/knowledge
```

`steersync init --only knowledge` then adds `knowledge-routing.md` (always included steering
that tells Kiro when to search) and `platform-knowledge.json` (a JSON agent with a
`knowledgeBase` resource on that folder; Markdown agents have dropped `knowledgeBase` objects
in some Kiro builds). After a pull that changed notes, steersync prints
`knowledge/ changed. In Kiro run /knowledge update and wait for it to finish.` Notes are
secret scanned like everything else.

A note's front matter is for steersync and reviewers (Kiro indexes the text):

```markdown
---
title: "Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)"
domain: apigee
type: exposure-record
status: partial
verified_on: 2026-11-20
review_by: 2026-12-05
aliases: [CVE-2026-114938, Java4Shell]
---

# Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)

Summary: Hybrid 1.17.0 to 1.17.3 bundle an affected JDK; 1.17.4 fixes it.
```

`title`, `domain`, `type`, `status` and `verified_on` are required; `inclusion` and
`fileMatchPattern` are forbidden. The `kn-*` rules keep notes retrieval friendly (summary
early, compact front matter, short sections, identifiers in the body, nothing stale); the
numbers are under `lint.knowledge`.

## Writing good steering

1. One domain per file. `always` only for rules that apply to every task; `fileMatch` for path
   specific rules; `auto` for heavier reference material.
2. Structure: a level one heading, one sentence of purpose, `## Rules` as short imperative
   bullets each with a one clause reason, `## Examples` with paired Do and Avoid fenced blocks
   (language set), `## References`.
3. Reference, do not paste: `#[[file:path:start-end]]` keeps excerpts current.
4. Budget tokens: every `always` file costs context on every turn (`steering-size`).
5. No secrets: steering is committed and synced centrally.
6. State each rule once (`steering-dup-rule`). Organisation wide rules go in global steering;
   repository overrides go in workspace steering.
7. Dated facts (status records, incident notes, CVE exposure) are knowledge, not steering.

[docs/examples/steering-template.md](https://github.com/SCGIS-Wales/steersync/blob/main/docs/examples/steering-template.md)
is a complete template.

## Kiro integration

**Session hook (V3).** `steersync setup` creates `~/.kiro/hooks/steersync.json`
(`steersync init --kiro` or `steersync kiro-hook --write` create `.kiro/hooks/steersync.json`
in a repository) with a `SessionStart` trigger that runs
`steersync pull --quiet --best-effort` through
`uvx --quiet --python ">=3.14" --from "steersync==<version>"`, so it never depends on which
`steersync` comes first on Kiro's `PATH`. Run `steersync setup --yes` again after upgrading
to move the pin. Field reports say Kiro hooks
fire only in headless runs or with a custom agent active, so treat them as a convenience and
keep the git hook as the dependable trigger.

### Supported Kiro versions

steersync supports Kiro V3 only: Kiro CLI `kiro.min_cli_version` (3.1.0) or later running the
V3 agent harness, and Kiro IDE `kiro.min_ide_version` (1.4.86) or later, which reads the same
schema. Both minimums are configuration, not code. `steersync doctor --kiro` (add `--json`
for machines, `--strict` to exit 10), the `kiro_check` MCP tool and the session hook (at most
once a day) check `kiro-cli --version`, `chat.agentEngine` and your agent files:

- kiro-cli below the minimum, or any engine other than V3: upgrade kiro-cli and run
  `kiro-cli settings chat.agentEngine v3`.
- Agent files with keys V3 does not read (`allowedTools` or `toolsSettings` without
  `permissions`, `toolAliases`, `keyboardShortcut`): open the agent in Kiro, run
  `/upgrade-agent` and commit the result. The keys `/upgrade-agent` keeps next to
  `permissions` (and its `kiroUpgrade` block) are accepted.

The probes never start a chat, so they use no Kiro credits, and the check never fails a
sync. Set `STEERSYNC_NO_KIRO_CHECK=1` to silence the session hook notice.

**MCP server.** `steersync setup` registers the server in `~/.kiro/settings/mcp.json`
(`steersync init --kiro` in a repository's `.kiro/settings/mcp.json`)
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
| "is my Kiro supported", "do my agents need upgrading" | `kiro_check` |
| "what would setup change", "set this machine up" | `plan_setup`, then `apply_plan` |
| "push / pull all my repositories" | `plan_fleet`, then `apply_plan` |
| "fix the lint findings", "tidy the steering" | `propose_fixes`, then `apply_fixes` |

The `plan_*` and `propose_fixes` tools write nothing and return a `plan_id`. `apply_plan` and
`apply_fixes` rebuild the plan, refuse a `plan_id` that no longer matches (`stale_plan`), and
ask the user once: one form with the steps (defaults ticked), `approve`, and
`allow_destructive` only when a step has no backup; for fixes, `apply_safe` (on) and the
unsafe fixes (none ticked). A client that cannot show a form gets an error asking it to show
the plan to the user and call again with the same `plan_id` and `confirm=true`. Forms never
ask for secrets.
| "which repos have unpushed kiro changes", "fleet status" | `fleet_status` (read only) |

Every tool accepts a `collections` list. Kiro does not pass the shell's `PATH`,
`SSH_AUTH_SOCK`, proxy variables or `GITLAB_TOKEN` to MCP servers, so the rendered entry
forwards them explicitly through `env`; use `steersync init --mcp-command /abs/path/uvx`
if `uvx` is not on Kiro's path.

**Custom agents.** On Kiro CLI V3 custom agents inherit steering, skills and `AGENTS.md` by
default, so list only the always on steering files in `resources` and reference skills with
`skill://.kiro/skills/*/SKILL.md`. A `file://.kiro/steering/**/*.md` glob loads every document
at startup and defeats the `fileMatch`, `manual` and `auto` inclusion modes.

## Verifying a release

Every [GitHub release](https://github.com/SCGIS-Wales/steersync/releases) carries the wheel,
the sdist and a `SHA256SUMS` file, with signed build provenance, and the same files are
published to [PyPI](https://pypi.org/project/steersync/) through trusted publishing.

```bash
VERSION=0.9.0
uv tool install "steersync[mcp] @ https://github.com/SCGIS-Wales/steersync/releases/download/v${VERSION}/steersync-${VERSION}-py3-none-any.whl"
gh attestation verify "steersync-${VERSION}-py3-none-any.whl" --repo SCGIS-Wales/steersync
```

## Security

See [SECURITY.md](https://github.com/SCGIS-Wales/steersync/blob/main/SECURITY.md). In short: no credentials of its own, no shell, validated remote
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

See [CONTRIBUTING.md](https://github.com/SCGIS-Wales/steersync/blob/main/CONTRIBUTING.md) and [docs/architecture.md](https://github.com/SCGIS-Wales/steersync/blob/main/docs/architecture.md).

## Licence

Apache 2.0. Kiro is a trademark of Amazon Web Services; this project is not affiliated with AWS.
