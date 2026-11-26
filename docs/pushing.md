# Git hook and merge requests

How a push becomes a proposal branch and a GitLab merge request.

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
    rev: v0.10.0
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
