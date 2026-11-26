# Writing steering

The front matter Kiro reads, and how to write steering that loads when it should.

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
