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

See [Working across many repositories](https://github.com/SCGIS-Wales/steersync/blob/main/docs/fleet.md#working-across-many-repositories)
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

## Documentation

| Page | What it covers |
|---|---|
| [Getting started](https://github.com/SCGIS-Wales/steersync/blob/main/docs/getting-started.md) | Existing GitLab repositories to a shared configuration, step by step. |
| [Working across many repositories](https://github.com/SCGIS-Wales/steersync/blob/main/docs/fleet.md) | `steersync fleet`: status, push and pull for every repository at once. |
| [Kiro integration](https://github.com/SCGIS-Wales/steersync/blob/main/docs/kiro.md) | Starting Kiro anywhere, supported versions, hooks and the MCP tools. |
| [Commands](https://github.com/SCGIS-Wales/steersync/blob/main/docs/commands.md) | Every command and option, and the exit codes. |
| [Configuration](https://github.com/SCGIS-Wales/steersync/blob/main/docs/configuration.md) | The configuration file, collections, reliability, how pull decides what to write. |
| [Git hook and merge requests](https://github.com/SCGIS-Wales/steersync/blob/main/docs/pushing.md) | The pre-push hook, proposal branches and GitLab merge requests. |
| [Lint and fixes](https://github.com/SCGIS-Wales/steersync/blob/main/docs/lint.md) | What lint checks, `lint --fix`, JSON and SARIF output. |
| [Validation reference](https://github.com/SCGIS-Wales/steersync/blob/main/docs/validation.md) | Every rule id and fix id. |
| [Writing steering](https://github.com/SCGIS-Wales/steersync/blob/main/docs/steering.md) | Front matter reference and writing good steering. |
| [Knowledge notes](https://github.com/SCGIS-Wales/steersync/blob/main/docs/knowledge.md) | The optional knowledge collection and Kiro knowledge bases. |
| [Central repository layout](https://github.com/SCGIS-Wales/steersync/blob/main/docs/central-layout.md) | How the central repository is organised, with a worked example. |
| [Architecture](https://github.com/SCGIS-Wales/steersync/blob/main/docs/architecture.md) | The modules and how data flows through them. |

## How it works

Kiro has no built in command to import or sync its configuration from a remote repository;
its guidance is to copy files from a central repository into `~/.kiro/` and `.kiro/`.
steersync automates that in both directions for every *collection* it is configured with.
It supports Kiro V3 only: Kiro CLI 3.1.0 or later with the V3 agent harness, and Kiro IDE
1.4.86 or later (see [Supported Kiro versions](https://github.com/SCGIS-Wales/steersync/blob/main/docs/kiro.md#supported-kiro-versions)). It is a plain Python CLI that shells out to the `git` binary, so your
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

## Verifying a release

Every [GitHub release](https://github.com/SCGIS-Wales/steersync/releases) carries the wheel,
the sdist and a `SHA256SUMS` file, with signed build provenance, and the same files are
published to [PyPI](https://pypi.org/project/steersync/) through trusted publishing.

```bash
VERSION=0.10.0
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
