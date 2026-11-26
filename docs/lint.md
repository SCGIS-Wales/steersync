# Lint and fixes

What `steersync lint` checks and how `lint --fix` repairs it. Every rule id is in the [validation reference](https://github.com/SCGIS-Wales/steersync/blob/main/docs/validation.md).

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
