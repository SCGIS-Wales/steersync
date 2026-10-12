# Central repository layout

This page shows how a complete `.kiro/` tree is stored in the central repository and how
each part maps onto a collection. The example is a Unity Deploy resource for Entra ID app
registrations whose steering was reorganised from numeric prefixes into domain prefixes,
plans moved into specs and a runbook turned into a skill.

## The project tree

```text
my-service/
├── AGENTS.md
└── .kiro/
    ├── steering/
    │   ├── product.md                          always
    │   ├── tech.md                             always
    │   ├── structure.md                        always
    │   ├── dev-code-intelligence.md            always
    │   ├── dev-local-environment.md            manual
    │   ├── unity-sdk-capabilities.md           fileMatch **/*.groovy
    │   ├── lang-groovy-conventions.md          fileMatch ["**/*.groovy", "**/*.gradle"]
    │   ├── lang-terraform-conventions.md       fileMatch ["**/*.tf", "**/*.tfvars"]
    │   ├── entra-oauth2-app-registration.md    auto
    │   ├── entra-credential-lifecycle.md       auto
    │   ├── testing-strategy.md                 fileMatch src/test/**
    │   └── testing-verification.md             manual
    ├── specs/
    │   ├── access-token-claims-graph-migration/  requirements.md, design.md, tasks.md
    │   ├── tls-cert-staged-rotation/             design.md, tasks.md
    │   └── mfa-all-time-stamping/                requirements.md, design.md, tasks.md
    ├── skills/
    │   └── venafi-tpp-websdk/
    │       ├── SKILL.md                        name + description, step summary
    │       ├── scripts/renew.sh                execute bit preserved
    │       └── references/websdk-runbook.md    full runbook body
    ├── agents/
    │   ├── entra-app-registration.json
    │   └── prompts/entra-app-registration.md
    ├── hooks/
    │   ├── tests-on-stop.json
    │   └── terraform-fmt-on-save.json
    ├── settings/
    │   ├── lsp.json                            committed: /code init drops custom entries
    │   ├── mcp.json
    │   └── groovy-classpath.txt                generated, not synced
    └── steersync.lock.json                     written by steersync, never committed
```

## The same tree in the central repository

The project slug is derived from the project's origin URL. Each collection has its own
directory, so a reviewer of a merge request sees exactly which kind of content changed.

```text
kiro-config/
├── global/
│   ├── steering/
│   │   └── engineering-rules.md
│   ├── skills/
│   │   └── release-checklist/SKILL.md
│   ├── agents/
│   │   └── reviewer.json
│   └── hooks/
│       └── steersync.json
└── projects/
    └── acme/platform/my-service/
        ├── steering/
        │   ├── product.md
        │   ├── tech.md
        │   └── entra-oauth2-app-registration.md
        ├── specs/
        │   └── tls-cert-staged-rotation/
        │       ├── design.md
        │       └── tasks.md
        ├── skills/
        │   └── venafi-tpp-websdk/
        │       ├── SKILL.md
        │       ├── scripts/renew.sh
        │       └── references/websdk-runbook.md
        ├── agents/
        │   ├── entra-app-registration.json
        │   └── prompts/entra-app-registration.md
        ├── hooks/
        │   ├── tests-on-stop.json
        │   └── terraform-fmt-on-save.json
        └── settings/
            ├── lsp.json
            └── mcp.json
```

| Collection | Local directory | Central directory | Notes |
|---|---|---|---|
| `steering` | `.kiro/steering/` and `~/.kiro/steering/` | `steering/` | Front matter is linted: `fileMatchPattern`, `inclusion: auto` with `name` and `description`, references. |
| `specs` | `.kiro/specs/` | `specs/` | Workspace only. Plans and history, never current rules. |
| `skills` | `.kiro/skills/` and `~/.kiro/skills/` | `skills/` | `SKILL.md` must start with `name` and `description`; scripts keep their execute bit; images and other assets are allowed up to 1 MiB. |
| `agents` | `.kiro/agents/` and `~/.kiro/agents/` | `agents/` | JSON agents need a `name`; `toolsSettings` is flagged as deprecated; prompt files are plain markdown. |
| `hooks` | `.kiro/hooks/` and `~/.kiro/hooks/` | `hooks/` | `version`, `hooks[]` with `name`, `trigger` (PascalCase V3 triggers) and `action`. |
| `settings` | `.kiro/settings/` | `settings/` | Only `lsp.json` and `mcp.json` by default; generated files such as a class path export stay local. |

## Day to day

```bash
# everything, both scopes
steersync pull
# only the skill and the specs you are reviewing
steersync pull -c skills -c specs
# what changed locally, per collection
steersync status
# propose every collection as one merge request
steersync push
# propose only the hooks
steersync push -c hooks -m "hooks: format terraform on save"
# validate before committing; the pre-commit hook does the same per file
steersync lint
```

## Syncing `AGENTS.md`

`AGENTS.md` sits at the repository root rather than under `.kiro/`. Add a collection with a
depth limit so only that file is scanned:

```yaml
collections:
  agents-md:
    description: Root AGENTS.md
    workspace_path: .
    include: [AGENTS.md]
    max_depth: 1
    lint: markdown
    git_exclude: false
```

## Migrating from steersync 0.1

Version 0.1 stored steering directly under `global/` and `projects/<slug>/`. Move those files
into a `steering/` subdirectory in the central repository:

```bash
cd kiro-steering
git mv global/*.md global/steering/
for p in projects/*/*/*/; do mkdir -p "$p/steering" && git mv "$p"/*.md "$p/steering/"; done
git commit -m "steering: move into collection directories"
```

Until that is done, `steersync pull` and `steersync status` print a warning naming the
directory that still holds flat files. Lock files are upgraded automatically on the next
pull, and the deprecated `paths.workspace_steering` and `paths.global_steering` keys keep
working with a warning.
