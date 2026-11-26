# Plan: Kiro V3 only, single approval UX, front matter schema, knowledge collection and safe lint fixing

Audience: Claude Code, working in this repository.
Baseline: `main` at `31857ea` (Release 0.9.0). Target release: 0.10.0.
Author: Dejan Gregor.

Read this whole document before writing code. Work through section 13 in order, one pull
request per phase. Breaking changes are acceptable: nobody uses steersync yet.

## 1. Goals

1. **Kiro V3 only.** steersync supports Kiro CLI V3 (and IDE 1.x, which shares the V3 schema)
   and nothing older. Every V1 and V2 code path, rule, reminder and migration is removed.
2. **Fewer questions.** `setup`, `init` and `fleet` compute a plan, show it once and ask for
   one approval. A separate typed confirmation remains only for destructive steps that have
   no backup.
3. **A documented front matter schema** that separates Kiro native keys from organisational
   metadata, exported as pydantic models and JSON Schema, and enforced by lint.
4. **A `knowledge` collection** for team knowledge notes that are retrieved through Kiro
   knowledge bases, never loaded as steering.
5. **`steersync lint --fix`**: recommend first, apply after approval, verify every fix, never
   break a file.

Non goals:

- Reflowing Markdown (line length), rewriting prose or guessing code fence languages.
- Converting legacy V2 agents. Kiro's own `/upgrade-agent` does that.
- Syncing or rebuilding knowledge base indexes. They are local to each machine.
- A new TUI framework. questionary and rich are enough.

## 2. Engineering rules (apply to every change)

- Latest stable Python (the project already requires 3.14), PEP 8, PEP 257, full type hints.
- Functional style: pure functions and small modules. No classes other than frozen
  dataclasses, `NamedTuple`s, pydantic models and existing exception types.
- All behaviour that a team may tune lives in YAML (`resources/default_config.yaml`, then the
  user and project config). No thresholds, rule lists, safety classes or messages hard coded
  in logic.
- click for every CLI surface. rich for tables. questionary for interactive selection.
- Every package on its latest stable release: `uv lock --upgrade`, then raise the floors in
  `pyproject.toml` to the versions the lowest dependency CI job passes with.
- Writes are atomic (temporary file in the same directory, then rename) and backed up through
  `backups.py`. Never write outside the workspace or `~/.kiro`.
- Tests first for every new function (`superpowers:test-driven-development`). Coverage gates in
  section 11.
- Commits follow Conventional Commits. Merge a PR only when CI is green.

## 3. Workstream A: Kiro V3 only

### 3.1 Supported baseline

Add to `default_config.yaml`:

```yaml
kiro:
  min_cli_version: "3.1.0"   # documented file:// resource limits start here
  min_ide_version: "1.4.86"
```

`kiro_version.py` reads these values instead of the `V3_MIN_VERSION` constant.

### 3.2 Remove

| Item | Location | Action |
|---|---|---|
| V1 (Classic) and V2 harness detection and reminders | `kiro_version.py` (`engine in {"v1", "v2"}`, `acp_engine` checks) | Delete. Any engine other than V3 is reported as unsupported. |
| `steersync v3-status` command and the `kiro_v3_status` MCP tool | `cli.py`, `mcp_server.py`, `mcp_tools.py` | Delete. Replace with `steersync doctor --kiro` and the MCP tool `kiro_check`. |
| `steersync migrate-hooks` and `migrate.py` | `cli.py`, `migrate.py`, `tests/test_migrate.py` | Delete. Legacy hook files become lint errors that point to the V3 hook format. |
| `find_v2_agents` and the V2 rule set | `kiro_version.py`, `kiro_rules.py` | Delete. Legacy agent keys become lint errors (3.3). |
| `HOOK_TRIGGER_ALIASES` and the `hook-trigger-alias` rule | `kiro_rules.py` | Delete. Non V3 trigger names are errors. |
| README sections on V2, the Classic harness, migration and exit code 10 wording | `README.md`, `docs/*.md` | Rewrite for V3 only. |
| Golden fixtures for legacy formats used as valid input | `tests/golden/agents/legacy.json*`, `tests/golden/hooks/*.kiro.hook` if present | Keep only as negative cases for the new errors. |

Do not confuse the hook file field `"version": "v1"` with the V1 harness. It is the version of
the V3 hook file schema and stays required.

### 3.3 Replace with V3 errors

| Rule id | Severity | Trigger | Message must say |
|---|---|---|---|
| `kiro-unsupported-version` | error | kiro-cli below `kiro.min_cli_version`, or `chat.agentEngine` not V3 | the installed and minimum versions, and how to upgrade |
| `agent-legacy-key` | error | `allowedTools`, `toolsSettings`, `toolAliases`, `keyboardShortcut` in an agent | the V3 replacement (`permissions`, `tools`, `excludedTools`), and to run `/upgrade-agent` in Kiro, then commit the result |
| `hook-legacy-file` | error | `*.kiro.hook`, `when`/`then` keys, or hooks inline in an agent file | the V3 path `.kiro/hooks/<id>.json` and the required fields |
| `steering-v3-only-mode` | none | removed | (V3 supports all four inclusion modes, so no warning for `fileMatch`, `manual` or `auto`) |

**Implementation note (phase 1):** Kiro CLI 3.5's `/upgrade-agent` keeps `allowedTools` and
`toolsSettings` next to the new `permissions` and adds a `kiroUpgrade` block, so those keys
are `agent-legacy-key` errors only in an agent without `permissions`
(`lint.upgraded_agent_keys`); otherwise every correctly upgraded agent would fail.

Exit code 10 changes meaning: `doctor --kiro --strict` exits 10 when the machine or the
configuration is below the V3 baseline.

### 3.4 Accepted V3 agent fields

Validate agents against this set (IDE 1.0 and CLI V3 schema): `name`, `description`, `prompt`,
`model`, `tools`, `excludedTools`, `permissions`, `resources` (`file://`, `skill://` and
`knowledgeBase` objects), `mcpServers`, `hooks`, `includeMcpJson`, `includePowers`,
`welcomeMessage`. Any other top level key is an error in JSON agents (Kiro validates them) and a
warning in Markdown agent front matter.

Resource budget rule `agent-resource-budget` (warning): resolved `file://` resources over 32
files or 100,000 characters in total, or any single file over 50,000 characters (truncated) or
1 MiB (skipped). Values come from config:

```yaml
lint:
  kiro_limits:
    agent_file_resources_max: 32
    agent_file_resources_chars: 100000
    file_chars_truncate: 50000
    file_bytes_skip: 1048576
```

## 4. Workstream B: plan and single approval UX

### 4.1 Principles

1. **Detect, then ask once.** Gather all state first (config, git remote, branches, `.kiro`
   folders, MCP registration, hooks). Build a `Plan` of `Step`s. Show it. Ask once.
2. **Risk decides the prompt.**

   | Risk | Meaning | Interaction |
   |---|---|---|
   | `safe` | Creates files or changes local config; restorable from a backup | In the plan, no own prompt |
   | `review` | Modifies existing user files or restorable remote state, with a backup | In the plan, highlighted; covered by the single approval |
   | `destructive` | Irreversible and not covered by any backup | Off by default; needs `--force` or a typed repository name |

3. **Undo instead of asking.** Every run that writes ends with
   `Undo with: steersync restore <backup id>`. Promote `fleet restore` to a top level
   `steersync restore` that works for every command.
4. **Idempotent.** Steps whose desired state already holds show as `already done`. Rerunning
   `setup` on a configured repository prints `Nothing to do` and exits 0 without a prompt.
5. **Remember answers.** Save the last accepted selection under `setup.last` in the user config.
   `--profile NAME` loads a saved selection. Never store secrets.
6. **Never require a prompt.** Every question has a flag. Without a TTY, in CI, with
   `--no-input`, `STEERSYNC_NO_INPUT=1` or `--json`, steersync never prompts: it prints the plan
   and exits 2 with `Re-run with --yes to apply N steps, or --plan to inspect.`

### 4.2 New modules

`src/steersync/interaction.py`

```python
"""Decide once per command whether steersync may prompt."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Output = Literal["text", "json"]


@dataclass(frozen=True)
class Interaction:
    interactive: bool
    assume_yes: bool
    force: bool
    dry_run: bool
    output: Output


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() not in {"", "0", "false", "no"}


def resolve_interaction(
    *,
    yes: bool,
    no_input: bool,
    force: bool,
    dry_run: bool,
    output: Output,
    env: Mapping[str, str] | None = None,
    stdin_tty: bool | None = None,
) -> Interaction:
    environ = dict(os.environ) if env is None else env
    tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
    interactive = (
        tty
        and not no_input
        and output == "text"
        and not _env_flag(environ, "STEERSYNC_NO_INPUT")
        and not _env_flag(environ, "CI")
    )
    return Interaction(interactive, yes, force, dry_run, output)
```

`src/steersync/plan.py`: frozen `Step` (`id`, `title`, `detail`, `risk`, `default`, `done`,
`apply: Callable[[], StepResult]`), frozen `Plan` (`command`, `steps`), and pure functions
`pending(plan)`, `default_selection(plan, interaction)`, `plan_to_json(plan)`,
`plan_hash(plan, input_digests)` (SHA256 of canonical JSON plus input file hashes).

`src/steersync/plan_render.py`: `render_plan(plan, console)` with one rich table (Step, Change,
Target, Risk), and `render_results(results, backup_id, console)`.

`src/steersync/plan_select.py`:

```python
import questionary

from steersync.plan import Plan


def select_steps(plan: Plan, remembered: frozenset[str] | None) -> list[str] | None:
    """One checkbox for the whole plan; None when the user cancels."""
    choices = [
        questionary.Choice(
            title=f"{step.title}  ({step.detail})",
            value=step.id,
            # Preselect with checked=True; questionary's `default` only moves the cursor.
            checked=(step.id in remembered) if remembered is not None else step.default,
            disabled="needs --force" if step.risk == "destructive" else None,
        )
        for step in plan.steps
        if not step.done
    ]
    return questionary.checkbox(
        "Select the changes to apply (space toggles, enter applies):", choices=choices
    ).ask()
```

Enter on the checkbox is the approval. Do not add a second "Apply?" question unless
`ux.confirm_after_select: true` is set (default `false`).

`src/steersync/cli_options.py`: one decorator shared by every command that changes state.

```python
from collections.abc import Callable

import click

_PLAN_OPTIONS: tuple[Callable[[Callable], Callable], ...] = (
    click.option("-y", "--yes", is_flag=True, help="Apply the default selection without asking."),
    click.option("--no-input", is_flag=True, help="Never prompt; fail when input is needed."),
    click.option("-f", "--force", is_flag=True, help="Allow destructive steps."),
    click.option("--plan", "--dry-run", "dry_run", is_flag=True, help="Show the plan and exit."),
    click.option("--only", multiple=True, help="Limit the plan to these step ids."),
    click.option("--skip", multiple=True, help="Leave these step ids out."),
    click.option("--profile", default=None, help="Load a saved selection."),
    click.option("--json", "as_json", is_flag=True, help="Machine readable output."),
)


def plan_options(func: Callable) -> Callable:
    for option in reversed(_PLAN_OPTIONS):
        func = option(func)
    return func
```

`--yes` applies the default selection, which never includes destructive steps. `--force` adds
them. `--plan` prints the plan (text or JSON) and exits 0.

### 4.3 Command changes

**`setup`** (`cli.py` lines 693 to 940 today; split into `setup_plan.py`):

1. `detect_setup_state(env, cwd, config) -> SetupState`: git check, remote from `--remote`, then
   existing config, then `git remote get-url origin`; branch from `--branch`, then config, then
   `setup_steps.suggested_branch`. Prompt only for the remote, and only when nothing was
   detected and the session is interactive. Keep the three attempt reachability loop.
2. `build_setup_plan(state, scope) -> Plan` with steps:
   `config.save` (safe, on), `folder:<name>` for each unsynced `.kiro` folder (safe, off),
   `mcp.register` (review, on), `hook.session` (safe, on), `repo.prepare` (review, on).
3. Delete the inner `ask()` helper and every `click.confirm` in `setup`.

**`init`**: reuse `build_setup_plan(state, scope="project")`.

**`fleet push` and `fleet pull`** (`cli_fleet_actions.py`): `build_fleet_plan(action, repos,
state) -> Plan`, one step per repository showing `+added ~changed -removed`. Overwrites covered
by a backup are `review`; overwrites of diverged remote content without a backup are
`destructive`. Replace the `Proceed?` prompts and `typed_overwrite` with the single approval;
keep a typed confirmation only for destructive steps without `--force`.

**`fleet interactive`**: use `select_steps`. Its `Prompts` indirection stays for tests.

### 4.4 Configuration

```yaml
ux:
  confirm_after_select: false
  remember_selection: true
  show_undo_hint: true
```

## 5. Workstream C: front matter schema

### 5.1 What Kiro V3 reads

| Artefact | Kiro native front matter | Where organisational metadata goes |
|---|---|---|
| Steering `.kiro/steering/*.md` | `inclusion` (`always` default, `fileMatch`, `manual`, `auto`), `fileMatchPattern` (glob string or list), `name` and `description` (both required for `auto`) | One `metadata` map of strings |
| Skill `SKILL.md` | `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools` | The specification's own `metadata` map |
| Agent `.md` / `.json` | V3 fields in 3.4 | Sidecar `.kiro/steersync.meta.yaml` |
| Hooks, settings, prompts, workflows, `AGENTS.md` | As documented | Sidecar |
| Knowledge notes `knowledge/**/*.md` | None (Kiro indexes the text, does not parse front matter) | Rich steersync schema (section 6) |

Key facts to encode as rules:

- Front matter must be the first content in the file: no BOM, blank line or whitespace before
  `---`.
- `inclusion: auto` loads only `name` and `description` until the model judges the file
  relevant, then loads the whole file. It is not vector search.
- File references: `#[[file:path]]`, `#[[file:path:N]]`, `#[[file:path:start-end]]`,
  `#[[folder:path]]`. Paths resolve from the workspace root (workspace steering), from
  `~/.kiro/steering/` (global steering), and from the containing folder (`AGENTS.md`).
- A comma separated `fileMatchPattern` string never matches and Kiro does not warn
  (kirodotdev/Kiro issue 11095). Use a YAML list.
- Files under `.kiro/steering/` without `.md` are not loaded.
- Kiro does not document how it treats unknown keys, and IDE 1.4.86 started flagging steering
  files it "could not fully use". Keep extra keys to the single `metadata` map.

### 5.2 Organisational vocabulary (YAML, configurable)

```yaml
lint:
  frontmatter:
    metadata_placement: embedded    # embedded | sidecar
    metadata_key: metadata
    allowed_extra_keys: []
    metadata_fields:
      owner:       { pattern: "^@?[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$" }
      tags:        { list: true, separator: ",", allowed: [] }   # empty list allows any tag
      references:  { list: true, separator: " ", pattern: "^(https://|#\\[\\[(file|folder):)" }
      review-by:   { type: date, warn_when_past: true }
      verified-on: { type: date }
      status:      { enum: [draft, active, partial, superseded, deprecated] }
      version:     { pattern: "^\\d+\\.\\d+(\\.\\d+)?$" }
```

All values in `metadata` are strings so that one encoding works for steering and skills: lists
are separator joined, dates are ISO 8601.

### 5.3 Models

Create `src/steersync/schema/` with `frontmatter.py`, `agent.py`, `hook.py`, `knowledge.py`.

```python
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Inclusion = Literal["always", "fileMatch", "manual", "auto"]
SkillName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)]


class SteeringFrontMatter(BaseModel):
    # extra="allow": unknown keys are reported by the fm-unknown-key lint rule, not by pydantic.
    model_config = ConfigDict(extra="allow", populate_by_name=True, frozen=True)
    inclusion: Inclusion = "always"
    file_match_pattern: str | list[str] | None = Field(None, alias="fileMatchPattern")
    name: str | None = None
    description: str | None = None
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def _mode_rules(self) -> "SteeringFrontMatter":
        if self.inclusion == "fileMatch" and not self.file_match_pattern:
            raise ValueError("fileMatch requires fileMatchPattern")
        if self.inclusion == "auto" and not (self.name and self.description):
            raise ValueError("auto requires name and description")
        return self


class SkillFrontMatter(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)
    name: SkillName
    description: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    license: str | None = None
    compatibility: Annotated[str, StringConstraints(max_length=500)] | None = None
    metadata: dict[str, str] | None = None
    allowed_tools: str | None = Field(None, alias="allowed-tools")
```

Organisational fields are checked by a pure function
`validate_org_metadata(meta: Mapping[str, str], fields: Mapping[str, FieldSpec]) -> list[Issue]`
driven by `lint.frontmatter.metadata_fields`.

New command: `steersync schema export --kind steering|skill|agent|hook|knowledge --out schemas/`
using `model_json_schema(by_alias=True)`. Commit `schemas/*.json`; a golden test fails when the
models and the committed schemas drift.

### 5.4 Examples to ship in README and `docs/examples/`

Steering, conditional:

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

Steering, auto:

```markdown
---
inclusion: auto
name: db-migrations
description: Alembic migration rules. Use when creating, editing or reviewing database migrations.
metadata:
  owner: "data/platform"
  tags: "infra"
---
```

Skill:

```markdown
---
name: release-notes
description: Draft release notes from merged pull requests. Use when preparing a release or changelog.
license: Proprietary
compatibility: Requires git and the glab CLI.
metadata:
  owner: "dejan.gregor"
  tags: "docs"
  review-by: "2027-01-31"
---
```

Sidecar:

```yaml
# .kiro/steersync.meta.yaml
".kiro/agents/backend-dev.json": { owner: "platform", tags: "agents", review-by: "2027-02-28" }
".kiro/hooks/lint-on-save.json": { owner: "dx", tags: "quality" }
```

### 5.5 Lint rules (new or revised)

| Rule id | Profile | Severity | Check |
|---|---|---|---|
| `fm-position` | steering, skill, agent | error | Front matter not on line 1, BOM, or blank line before `---` (replaces `front-matter-position`) |
| `fm-yaml` | all with front matter | error | Invalid YAML or not a mapping (replaces `front-matter-yaml`) |
| `fm-inclusion` | steering | error | Not one of the four modes, exact case |
| `fm-filematch-comma` | steering | error | `fileMatchPattern` string containing a comma |
| `fm-filematch-relative` | steering | warning | Pattern starting with `/` or `./` |
| `fm-auto-fields` | steering | error | `auto` without `name` and `description` |
| `fm-unknown-key` | steering | warning | Top level key not native, not `metadata`, not allowed; message names `metadata` as the destination |
| `fm-metadata` | steering, skill | warning or error | Organisational field validation, including a past `review-by` |
| `fm-knowledge-in-steering` | steering | error | Knowledge schema keys (`title`, `domain`, `verified_on`, `sources`, `relations`) with no `inclusion`: a knowledge note that would load on every turn. Suggest moving it to `knowledge/` |
| `ref-unresolved` | steering, `AGENTS.md` | error | `#[[file:]]` or `#[[folder:]]` target missing, or line range outside the file |
| `ref-syntax` | steering | error | Malformed reference |
| `steering-ext` | steering | error | File under `.kiro/steering/` without `.md` |
| `steering-size` | steering | warning | Over `file_chars_truncate`, or the `always` set over `lint.budgets.always_chars` |
| `steering-dup-rule` | steering | warning | The same normalised bullet in more than one steering file |
| `skill-name` | skill | error | Name pattern, 64 characters, equals the directory name |
| `skill-size` | skill | warning | Body over 500 lines or about 5,000 tokens |
| `hook-schema` | hooks | error | `version: "v1"`, `hooks[]`, PascalCase `trigger`, `action.type` with its matching field |
| `spec-ears` | specs | info | Acceptance criteria not in WHEN/IF/WHILE ... SHALL form |

Put every number in `lint.budgets` or `lint.kiro_limits`.

### 5.6 Steering writing guide (README section and a `docs/examples/steering-template.md`)

1. One domain per file. `always` only for rules that apply to every task; `fileMatch` for path
   specific rules; `auto` for heavier reference material.
2. Structure: H1, one sentence purpose, `## Rules` as short imperative bullets with a one clause
   reason, `## Examples` with paired Do and Avoid fenced blocks (language set), `## References`.
3. Reference, do not paste: `#[[file:path:start-end]]` keeps excerpts current.
4. Budget tokens: every `always` file costs context on every turn.
5. No secrets: steering is committed and synced centrally.
6. State each rule once. Organisation wide rules go in global steering; repository overrides go
   in workspace steering.
7. Dated facts (status records, incident notes, CVE exposure) are knowledge, not steering.

## 6. Workstream D: knowledge collection

### 6.1 Why

Steering is never vector based: all four inclusion modes load whole files. Only Kiro knowledge
bases retrieve chunks, through the `knowledge` tool, on the Best index (local
all-MiniLM-L6-v2 embeddings) or the Fast index (BM25). Nothing from the index enters context
until the model calls the tool. V3 enables knowledge by default, but Kiro still labels it
experimental and it is CLI only. Indexes are local, per agent and per machine, and are not
shared through git.

### 6.2 Layout steersync syncs

```text
repo/
├── knowledge/                     # knowledge notes; never under .kiro/steering
│   ├── apigee/
│   ├── entra-id/
│   └── runbooks/
└── .kiro/
    ├── steering/knowledge-routing.md   # small, always included
    └── agents/platform-knowledge.json  # declares the knowledgeBase resource
```

**Decision (2026-11-26):** the collection ships **disabled**, and its folder is configurable
(`collections.knowledge.workspace_path`, default `.kiro/knowledge`), so nothing new appears at
the repository root unless a team opts in. The layout above shows the opt in case with a
custom folder. Add a `knowledge` collection to `default_config.yaml` (`enabled: false`,
include `**/*.md`, lint profile `knowledge`); the agent template's `source` follows the
configured folder. Add `knowledge-routing.md` and `platform-knowledge.json` as templates under
`docs/examples/` and offer them as plan steps in `init` (`knowledge.routing`,
`knowledge.agent`, both safe and off by default).

Routing steering template:

```markdown
---
inclusion: always
---

# Team knowledge base

This repository has a knowledge base named `team-knowledge`, indexed from `knowledge/`.

- Before answering questions about Apigee, Entra ID, platform runbooks, CVEs or past incidents,
  search it with the `knowledge` tool.
- For exact identifiers (CVE numbers, ticket keys, resource ids), also grep `knowledge/`.
- Cite the note path and its verified date. Flag notes older than 90 days or marked superseded.
- Do not read whole notes unless asked; prefer retrieved chunks.
```

Agent template (JSON, not Markdown: Markdown agents have silently dropped `knowledgeBase`
objects in some builds, issue 9790):

```json
{
  "name": "platform-knowledge",
  "description": "Platform engineering agent with semantic access to the team knowledge corpus",
  "tools": ["read", "write", "shell", "web", "knowledge"],
  "resources": [
    "file://.kiro/steering/knowledge-routing.md",
    "skill://.kiro/skills/**/SKILL.md",
    {
      "type": "knowledgeBase",
      "source": "file://./knowledge",
      "name": "team-knowledge",
      "description": "Team knowledge notes: Apigee, Entra ID, runbooks, CVE exposure records",
      "indexType": "best",
      "autoUpdate": true
    }
  ],
  "includeMcpJson": true
}
```

### 6.3 Knowledge note schema (`schema/knowledge.py`)

Kiro ignores this front matter; it exists for steersync, reviewers and a future shared index.
Keep it compact so the summary stays inside the first embedded chunk.

```markdown
---
title: "Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)"
domain: apigee
type: exposure-record
status: partial
verified_on: 2026-11-20
review_by: 2026-12-05
aliases: [CVE-2026-114938, Java4Shell, GCP-2026-082]
tags: [apigee-hybrid, cve, jdk]
related: [udeploy-version-pin-override.md]
---

# Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)

Summary: Hybrid 1.17.0 to 1.17.3 bundle an affected JDK; 1.17.4 fixes it; 1.16 is not affected.
Verified on 2026-11-20. Action: upgrade non production to 1.17.4 or set the JVM flags.

## Questions this note answers
...
```

Model rules: `title`, `domain`, `type`, `status`, `verified_on` required; `aliases`, `tags`,
`related`, `sources`, `relations` optional; `inclusion` and `fileMatchPattern` forbidden.

### 6.4 Knowledge lint rules (profile `knowledge`)

| Rule id | Severity | Check |
|---|---|---|
| `kn-schema` | error | Model validation |
| `kn-inclusion` | error | Steering keys inside `knowledge/` |
| `kn-summary` | warning | No H1 plus summary paragraph within `lint.knowledge.summary_within_lines` |
| `kn-frontmatter-size` | warning | Front matter over `lint.knowledge.frontmatter_chars` (default 600) |
| `kn-section-size` | warning | A section over `lint.knowledge.section_words` (default 250) |
| `kn-identifiers-in-body` | warning | An alias or CVE id from front matter absent from the body |
| `kn-stale` | warning | `review_by` past, or `verified_on` older than `lint.knowledge.max_age_days` |
| `kn-duplicate` | warning | Duplicate titles or aliases across notes |
| `kn-related-unresolved` | warning | `related` target missing |

Extend `kiro_rules.agent_issues` for `knowledgeBase` resources: `type == "knowledgeBase"`;
`source` starts with `file://` and resolves to an existing directory after sync; `name` present
and unique per agent; `indexType` in `{best, fast}`; `autoUpdate` boolean; `knowledge` in
`tools` (or `*`, or `@builtin`) and not in `excludedTools`; warning when declared in a
Markdown agent.

### 6.5 After sync

Never sync index files. After a `pull` that changed `knowledge/`, print:

```text
knowledge/ changed. In Kiro run /knowledge update and wait for it to finish.
```

Run the existing secret scanner over `knowledge/` with no exceptions; `.kiroignore` is not
documented to protect indexed content.

## 7. Workstream E: `steersync lint --fix`

### 7.1 CLI

```text
steersync lint [PATHS]...
  --fix              apply safe fixes
  --unsafe-fixes     with --fix, also apply unsafe fixes; without it, list them
  --diff             print unified diffs, write nothing
  --show-fixes       list applied fixes per file
  --select RULE      repeatable, prefixes allowed
  --ignore RULE      repeatable, prefixes allowed
  --format text|json|sarif
  --allow-dirty      allow fixing files with uncommitted git changes
  plus the plan options from 4.2
```

Interactive `--fix`: safe fixes apply without a question (backed up and reversible). If unsafe
fixes exist and `--unsafe-fixes` is absent, one checkbox lists them, all unchecked, labelled
`rule id · file · summary`. Non interactive runs never apply unsafe fixes without the flag.

Exit codes (extend the README table, keep existing codes):

| Code | Meaning |
|---|---|
| 0 | No findings at or above `lint.fail_level` remain |
| 4 | Findings remain, or `--diff` found pending changes |
| 2 | Usage or configuration error, or input needed in a non interactive run |
| 11 | A fix failed verification; the file was left untouched |

### 7.2 Package `src/steersync/fixes/`

| Module | Content |
|---|---|
| `model.py` | frozen `Fixer(rule_id, safety, profiles, apply: Callable[[str, FileContext], str], changes_keys: frozenset[str])`, frozen `FileFix(path, original_sha256, original, fixed, rules, safety, diff)` |
| `registry.py` | Catalogue built from `lint.fix` YAML plus `extend_safe_fixes`, `extend_unsafe_fixes`, `unfixable` |
| `frontmatter_fixes.py` | Front matter fixers (ruamel.yaml round trip) |
| `markdown_fixes.py` | Blank line fixers steersync owns |
| `json_fixes.py` | JSON formatting |
| `pymarkdown_bridge.py` | PyMarkdown fix mode on the body only |
| `verify.py` | Semantic checks |
| `engine.py` | `collect`, `eligible`, `fix_text`, `plan_fixes`, `apply_fixes` |
| `diffing.py` | `difflib.unified_diff` with `a/` and `b/` prefixes |

Pipeline:

1. `collect(paths, config)` returns text, profile and findings per file.
2. `eligible(...)` drops files with any secret finding, dirty files unless `--allow-dirty`
   (`lint.fix.require_clean_git: true` by default), and `never` rules.
3. `fix_text(text, fixers)` applies fixers in catalogue order until a fixpoint, at most
   `lint.fix.max_passes` (100). Reaching the limit raises `NonConvergentFix`.
4. `verify(original, fixed, profile, applied)` must pass all of:
   - front matter data equal before and after (ruamel `typ="safe"` load), except keys the
     applied fixers declare in `changes_keys`;
   - JSON files: `json.loads` equal;
   - Markdown body rendered with markdown-it-py produces the same normalised HTML; otherwise
     demote the fix to unsafe, or abort when nothing else remains;
   - the multiset of `#[[file:]]` and `#[[folder:]]` tokens and all fenced code contents
     unchanged;
   - idempotent: `fix_text(fixed) == fixed`;
   - no new finding on relint.
5. `apply_fixes(plan, backups)` rechecks `original_sha256` immediately before each atomic write
   and records one backup id for the run.

YAML writes use ruamel.yaml round trip mode (`preserve_quotes = True`, high width, indentation
detected from the source). Edit the loaded map in place. If the emitted text changes lines no
fixer targeted, abort that file. JSON writes use `json.dumps(obj, indent=2, ensure_ascii=False)`
plus a newline, keeping key order.

### 7.3 Fix catalogue

| Fix id | Source | Safety | Notes |
|---|---|---|---|
| `file-bom` | steersync | safe | Strip a UTF-8 BOM |
| `file-eol` | steersync | safe | CRLF to LF |
| `fm-leading-blank` | steersync | safe | Remove blank lines before the opening `---` |
| `fm-key-case` | steersync | safe | `filematchpattern`, `FileMatchPattern`, `file_match_pattern` to `fileMatchPattern`, only if the target key is absent |
| `fm-inclusion-case` | steersync | safe | `Always` to `always`, `filematch` to `fileMatch` |
| `json-format` | steersync | safe | Reindent and final newline; data must be equal |
| `md-final-newline` (MD047) | PyMarkdown | safe | |
| `md-trailing-space` (MD009) | PyMarkdown | safe | Keeps configured hard breaks |
| `md-hard-tabs` (MD010) | PyMarkdown | safe | Never inside fenced code |
| `md-ul-style` (MD004), `md-hr-style` (MD035), `md-fence-style` (MD048) | PyMarkdown | safe | Verified by HTML |
| `md-heading-space` (MD019, MD021, MD023), `md-list-space` (MD030), `md-quote-space` (MD027) | PyMarkdown | safe | |
| `md-list-indent` (MD005, MD007) | PyMarkdown | safe after verification | Demoted if HTML differs |
| `md-blanks-around` (MD022, MD031) | steersync | safe after verification | Until PyMarkdown fix support is stable |
| `md-blanks-around-lists` (MD032) | steersync | unsafe | Can end a lazy continuation |
| `fm-inclusion-typo` | steersync | unsafe | Edit distance at most 2 with a unique match; changes loading |
| `fm-quote-glob` | steersync | unsafe | Quote a glob that made the YAML invalid |
| `fm-filematch-comma` | steersync | unsafe | Split a comma string into a list; activates the file |
| `fm-auto-name` | steersync | unsafe | Derive a missing `name` from the file name |
| `fm-move-to-metadata` | steersync | unsafe | Move `tags`, `owner`, `version` and similar into `metadata` as strings; may lose comments |
| `md-heading-increment` (MD001) | PyMarkdown | unsafe | Changes the outline |
| `md-ol-prefix` (MD029) | PyMarkdown | unsafe | Renumbering |
| `md-code-style` (MD046) | PyMarkdown | unsafe | Changes block form |
| `skill-name-case` | steersync | unsafe | Lowercases the name; never renames the directory |
| MD013, MD034, MD040, table rules, anything inside `#[[...]]` or fenced code, agent and hook semantics, any file with secrets, any V3 legacy key | none | never | Report only |

Configuration:

```yaml
lint:
  fail_level: error
  fix:
    engine: pymarkdown        # pymarkdown | builtin
    extend_safe_fixes: []
    extend_unsafe_fixes: []
    unfixable: []
    max_passes: 100
    require_clean_git: true
```

PyMarkdown integration: strip front matter, run fix mode on the body only, reattach the front
matter byte for byte. Enable only allow listed rules that report fix support at runtime through
plugin metadata (the rules page and changelog disagree on several rules). Pin `pymarkdownlnt` to
an exact version; golden tests catch drift on upgrades.

### 7.4 Output

- JSON: `{"version": 1, "findings": [...], "fixes": [{"path", "rules", "safety", "diff",
  "applied"}], "summary": {...}}`.
- SARIF 2.1.0: one run, `tool.driver.rules` from the catalogue, `fixes[].artifactChanges` and
  `properties.safety` per result. Validate in CI against the SARIF schema.

## 8. MCP server

Tools (all with structured output models in `mcp_tools.py`):

| Tool | Behaviour |
|---|---|
| `plan_setup`, `plan_fleet` | Return the plan and `plan_id`; write nothing |
| `apply_plan(plan_id, step_ids=None, confirm=False)` | Recompute; return `stale_plan` when the hash differs; otherwise one approval |
| `propose_fixes(paths, select=None, include_unsafe=False)` | Run the fix pipeline in memory; return `plan_id`, diffs, rules, safety |
| `apply_fixes(plan_id, unsafe_rule_ids=None, confirm=False)` | Recompute, one approval, apply, return backup id and relint summary |
| `kiro_check` | Replaces `kiro_v3_status` |

One approval per call: a single form with `steps` (multi select enum, default selection),
`approve` (boolean) and `allow_destructive` (only when destructive steps exist); for fixes,
`apply_safe` (default true) and `unsafe` (multi select, default empty). Implement one adapter
`ask_once(ctx, schema, message)` that uses the SDK v2 `Resolve`/`Elicit` pattern on 2026-07-28
connections (multi round trip requests), the older elicitation on earlier sessions, and falls
back to `confirm=true` with an unchanged `plan_id` when the client cannot ask. Never request
secrets in a form. The existing `confirm_force` resolver becomes a user of `ask_once`.

## 9. Dependencies

1. Add `ruamel.yaml` (round trip writes) and `markdown-it-py` (render verification) explicitly.
2. Add `hypothesis` to the test group and `jsonschema` for SARIF and schema tests.
3. Keep PyYAML for reads where comments do not matter.
4. `uv lock --upgrade`; set floors to the resolved versions the lowest version CI job passes
   with; pin `pymarkdownlnt` exactly.
5. Confirm the installed `mcp` v2 API names (`MCPServer`, `Resolve`, `Elicit`,
   `NoBackChannelError`) before coding section 8.

## 10. Documentation

README: replace V2 content; add "Supported Kiro versions" (V3 only, minimums from config),
"Setup in one step", "Front matter reference" (native versus `metadata`, examples), "Knowledge
notes and knowledge bases", "Fixing lint findings" (safety table, flags, exit codes), "Writing
good steering", "MCP tools". Update `docs/validation.md` with every rule id. CHANGELOG 0.10.0
under "Removed" (V1 and V2 support, `v3-status`, `migrate-hooks`) and "Changed (breaking)"
(single approval, `--yes` semantics, `restore` at top level, renamed rule ids).

## 11. Tests and coverage

1. Unit tests for every checker, fixer and plan builder, positive and negative.
2. `resolve_interaction` across TTY, CI, `--no-input`, `STEERSYNC_NO_INPUT`, `--yes`, `--json`.
3. Plans for fresh, partly configured and fully configured repositories (the last is empty).
4. `verify` rejects fixes that change YAML data, JSON data, rendered HTML, references or code.
5. Hypothesis properties: `fix_text` is idempotent; safe fixes keep rendered HTML.
6. Golden cases under `tests/golden/fix/<case>/` (`input/`, `expected/`, `diff.patch`,
   `findings.json`, `sarif.json`): BOM plus leading blank line, key case, comma pattern,
   unquoted glob, agent JSON reformat, file with a secret left untouched, dirty file refused,
   non convergent fixer, knowledge note moved out of steering flagged.
7. V3 only: legacy agent keys, `.kiro.hook` files and a V2 engine each produce their error;
   no code path still mentions V1 or V2 (`rg -n "v2|V2|Classic" src/` returns only the hook
   schema `"v1"` value and the new error messages).
8. CLI tests with `CliRunner` for exit codes 0, 2, 4, 10, 11 and the non interactive message.
9. questionary is tested by monkeypatching `.ask` with a pure fake.
10. MCP in process tests: new protocol, legacy elicitation, no elicitation capability, stale
    `plan_id`.
11. Gates in CI: 95 per cent lines and 90 per cent branches for `interaction`, `plan*`,
    `fixes/`, `schema/`; the existing global gate stays.

## 12. Acceptance criteria

1. `steersync setup` on a fresh repository asks at most for the remote (when undetectable) and
   then shows one checkbox. Nothing else prompts unless a destructive step is selected without
   `--force`.
2. Rerunning `setup` prints `Nothing to do` and exits 0.
3. Without a TTY and without `--yes`, any state changing command exits 2 and names the flag.
4. `fleet push` across N repositories asks once.
5. `lint --fix` on the golden corpus matches the expected files; a second run changes nothing;
   relint shows no new findings.
6. No fix ever changes a file with a secret finding, a `#[[...]]` token, fenced code or JSON
   semantics; each case has a test.
7. `--diff` writes nothing and exits 4 when changes are pending.
8. SARIF validates against 2.1.0.
9. MCP `apply_fixes` and `apply_plan` ask exactly once, and work with `confirm=true` where the
   client cannot ask.
10. Committed JSON Schemas match the models.
11. No V1 or V2 support remains; `doctor --kiro --strict` exits 10 below the V3 baseline.
12. A note with knowledge keys under `.kiro/steering/` fails lint with `fm-knowledge-in-steering`.

## 13. Phases (one PR each, merge when CI is green)

1. **V3 only.** Section 3: remove V1 and V2 code, `v3-status`, `migrate-hooks`; add version
   config, V3 errors, `doctor --kiro`, `kiro_check`. Update README and CHANGELOG.
2. **Foundations.** `interaction.py`, `plan.py`, `plan_render.py`, `plan_select.py`,
   `cli_options.py`, `ux` config. No behaviour change.
3. **Setup and init.** `setup_plan.py`, rewire both commands, `setup.last`, profiles,
   `steersync restore`, golden CLI transcripts.
4. **Fleet.** `build_fleet_plan`, risk from backups, rewire push, pull and interactive.
5. **Schema and rules.** `schema/` package, `schema export`, committed schemas, `refs.py`,
   rules from 5.5, metadata validation, sidecar support.
6. **Knowledge.** Collection, schema, rules from 6.4, `knowledgeBase` validation, templates,
   post pull reminder.
7. **Fix engine.** `fixes/` package, steersync native fixers, `--fix`, `--diff`,
   `--show-fixes`, `--unsafe-fixes`, exit codes, golden corpus, hypothesis tests.
8. **Markdown fixers.** `pymarkdown_bridge.py` with the runtime fix support check, blank line
   fixers with HTML verification and demotion.
9. **Outputs and MCP.** JSON and SARIF, the MCP tools in section 8 with `ask_once`.
10. **Release.** README and CHANGELOG final pass, version 0.10.0, tag from `main` once green.

Per phase:

```bash
# 1. Start the phase from an up to date main branch; next, create the branch.
git switch main && git pull --ff-only
# 2. Create the phase branch; next, write tests first and implement.
git switch -c feat/<phase-slug>
# 3. Lint, type check and test locally; next, push only when all pass.
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest --cov
# 4. Push and open the pull request; next, wait for CI to go green, then merge.
git push -u origin HEAD && gh pr create --fill
```

## 14. Verify before coding

1. Recheck kiro.dev steering, agent configuration reference, knowledge management and the CLI
   changelog for anything published after 26 November 2026. Kiro's V3 "New features" page and
   its steering page disagree on whether `auto` is set in front matter; follow the steering page.
2. Whether custom agents inherit steering by default (`chat.disableInheritingDefaultResources`)
   or need `file://.kiro/steering/**/*.md`: the two Kiro pages conflict. Test on 3.1.0 or later
   and record the result in `docs/validation.md`.
3. Open Kiro issues that affect knowledge bases: 7527 (`autoUpdate`), 12311 (embedding
   truncation), 11281 (headless), 6251 (sub agents), 10805 (quitting during update). Reflect
   only confirmed behaviour in warnings.
4. PyMarkdown fix support per rule at runtime, against the pinned version.

## 15. Sources

- Kiro steering: https://kiro.dev/docs/steering/
- Kiro agent configuration reference: https://kiro.dev/docs/custom-agents/configuration-reference/
- Kiro knowledge management: https://kiro.dev/docs/cli/experimental/knowledge-management/
- Kiro CLI V3 new features: https://kiro.dev/docs/cli/v3/new-features/
- Kiro CLI 3.0 changelog: https://kiro.dev/changelog/cli/3-0/
- Kiro CLI 2.27 changelog (line and folder references): https://kiro.dev/changelog/cli/2-27/
- Kiro IDE 1.4.86 changelog: https://kiro.dev/changelog/ide/1-4-86/
- Kiro hooks: https://kiro.dev/docs/hooks/
- Kiro tools catalogue: https://kiro.dev/docs/tools/
- Kiro `.kiroignore`: https://kiro.dev/docs/kiroignore/
- Kiro issues: https://github.com/kirodotdev/Kiro/issues/11095, /9790, /7527, /12311, /12370, /11281, /6251, /10805
- Agent Skills specification: https://agentskills.io/specification
- Command Line Interface Guidelines: https://clig.dev/
- Ruff fix safety: https://docs.astral.sh/ruff/linter/#fix-safety
- PyMarkdown rules: https://pymarkdown.readthedocs.io/en/latest/rules/
- MCP elicitation (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation
- MCP 2026-07-28 release: https://blog.modelcontextprotocol.io/posts/2026-07-28/
- MCP Python SDK v2: https://py.sdk.modelcontextprotocol.io/whats-new/
