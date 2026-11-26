# Getting started

From an existing set of GitLab repositories to a shared Kiro configuration, step by step.

Use this when your projects already live on GitLab and already have Kiro files: a committed
`.kiro/` directory, an `AGENTS.md`, or files that exist only on your machine. Nothing is
deleted or overwritten along the way: steersync proposes what you have, you review it as a
merge request, and only merged content flows back to everyone.

The examples use a project at `gitlab.example.com/acme/svc` and a central repository at
`gitlab.example.com/platform/kiro-config`; replace both with yours.

## 1. Create the central repository (once per organisation)

In GitLab, create a project for the shared configuration, for example `platform/kiro-config`.

- Tick **Initialize repository with a README**. steersync works from the default branch
  (`main`), and a new GitLab project has no branch until its first commit; without one every
  command stops with "the central repository ... has no branch 'main'".
- Protect `main` and require merge request approval. Developers need the Developer role so
  they can push proposal branches (`kiro-config/<group>/<project>/<user>-<hash>`); nobody
  pushes to `main` directly.
- Leave it otherwise empty: the first push creates the layout (see
  [Central repository layout](https://github.com/SCGIS-Wales/steersync/blob/main/docs/central-layout.md#central-repository-layout)). If you adopt a repository that
  already has Kiro files, they must follow that layout: `global/<collection>/` and
  `projects/<group>/<project>/<collection>/`.

## 2. Install and run setup (every developer, once per machine)

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

## 3. Optional: share the configuration through the project

To spare teammates even the setup question, commit `.kiro/steersync.yaml` with the same
`remote` section setup saved in `~/.config/steersync/config.yaml`; a project file wins over
the user file inside that repository. For merge request links and `steersync mr`, export a
GitLab token with the `api` scope as `GITLAB_TOKEN`; without one, the merge request is still
opened through git push options.

## 4. Check what you already have

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

## 5. Set up each further project

setup prepared the repository you ran it in. In every other repository run
`steersync setup --yes` (it reuses the saved answers) or just `steersync init --hook`. This
adds the synced directories to `.git/info/exclude` (new files there are no longer offered to
the project repository; files you already committed stay tracked) and installs the
pre-push hook. The session hook and MCP server come from `~/.kiro`, so nothing is written
into the repository's own `.kiro` unless you add `--kiro`.

## 6. Seed the central repository

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

## 7. Decide where the files live in the project repository

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

## 8. Teammates

```bash
git clone git@gitlab.example.com:acme/svc.git && cd svc
uv tool install --python 3.14 "steersync[mcp]"    # once per machine
steersync setup                                   # once per machine; --yes afterwards
steersync pull
```

From then on every Kiro session starts with a pull, and every `git push` of the project
proposes their Kiro changes.

## 9. Files in your home directory (`~/.kiro`)

`steersync pull` also syncs `global/` with `~/.kiro` (steering, skills, agents, hooks,
prompts and workflows that apply to every project). The first pull keeps your existing
personal files. To share some of them with everyone, propose them with
`steersync push --scope global`; to leave your home directory out entirely, use
`steersync pull --no-global`. Per user files such as workspace permissions, memories and
sessions are never synced.

## 10. Day to day

| You want to | Do |
|---|---|
| Get the latest merged configuration | Nothing: each Kiro session pulls. Or `steersync pull`, or ask Kiro to "pull the kiro config". |
| Share a change | Edit under `.kiro/`, then `git push` the project, or `steersync push`, or ask Kiro to "push my kiro config". |
| Find the merge request | `steersync mr` (needs `GITLAB_TOKEN`). |
| See what differs | `steersync status`. |
| Overwrite your local edits with the central copy | `steersync pull --force`. |
| See every repository at once | `steersync fleet` (see [Working across many repositories](https://github.com/SCGIS-Wales/steersync/blob/main/docs/fleet.md#working-across-many-repositories)). |

If something looks wrong, `steersync doctor` shows the configuration, the remote, the
project slug, every directory and the Kiro CLI harness.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `the central repository ... has no branch 'main'` | The GitLab project has no commit yet. Create it with a README, or set `remote.default_branch`. |
| git operations time out, `ssh: connect to host gitlab.com port 22` | The network blocks SSH on port 22. GitLab.com also serves SSH on port 443: set `url: ssh://git@altssh.gitlab.com:443/<group>/<project>.git` and add `altssh.gitlab.com` to `allowed_hosts` (accept its host key once with `ssh -T -p 443 git@altssh.gitlab.com`; it is the same key as `gitlab.com`). HTTPS with a credential helper works too. |
| `remote host ... is not in remote.allowed_hosts` | Add the host of `remote.url` to `allowed_hosts`. |
| The first pull reports a file as locally modified | Your copy differs from the merged one, for example a file still tracked by the project in an older version. Compare, then `steersync pull --force` to take the central copy or `steersync push` to propose yours. |
| `push` stops with "possible secret(s)" | A file matches a secret pattern or `mcp.json` holds a literal value. Remove it (use `${VAR}` in `mcp.json`), or pass `--no-lint` if it is not a secret. Other lint errors are reported and never stop a push. |
