# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## [Unreleased]

## [0.1.0] - 2026-10-10

### Added

- `steersync pull` applies global and per project steering from a central git repository,
  keeps locally modified files unless `--force` is given, prunes files removed centrally and
  records what it wrote in a lock file so later pulls can tell local edits from stale copies.
- `steersync push` proposes workspace or global steering as a content hashed branch with
  GitLab merge request push options, dedupes identical proposals and falls back to a plain
  push when the server does not accept push options.
- `steersync status`, `lint`, `doctor`, `config show|validate|default`.
- `steersync init` writes `.git/info/exclude` entries and an optional configuration file,
  and `steersync install-hook` installs a chaining pre-push hook without touching
  `core.hooksPath`.
- `.pre-commit-hooks.yaml` exposing `steersync-push` (pre-push) and `steersync-lint`.
- `steersync kiro-hook` renders Kiro CLI 2.x `agentSpawn` and V3 `SessionStart` hooks and
  `steersync mcp-config` renders an `mcp.json` fragment.
- Optional MCP stdio server (`steersync[mcp]`) exposing `pull_steering`, `push_steering`,
  `diff_steering` and `lint_steering`.
- Lint rules for Kiro front matter, file size, UTF-8, level one headings, `#[[file:]]` and
  `#[[folder:]]` references, secret patterns and pymarkdown style checks.
- Layered YAML configuration with environment overrides and strict pydantic validation.
- CI pipeline with ruff, mypy strict, pytest with coverage gate, bandit, pip-audit, CodeQL,
  wheel smoke test, Dependabot and a tag driven release workflow with build provenance and
  PyPI trusted publishing.
- GitHub releases carry the wheel, the sdist and a `SHA256SUMS` file with build provenance
  attestations. Releases run the full CI gate first and no longer depend on PyPI being set
  up; publishing to PyPI is enabled with the `PUBLISH_TO_PYPI` repository variable.
- Releases are driven by `__version__`: merging a version bump to `main`, or running the
  `Release` workflow manually, creates the annotated tag and the release, so nobody needs
  permission to push tags.
- CI lints every workflow with actionlint and shellcheck.
- `pull --best-effort` warns and exits 0 on failure; the generated Kiro hooks use it.
- Repository description, homepage and topics are kept in `.github/repository.yml` and
  applied by a workflow when a `REPO_ADMIN_TOKEN` secret is configured.

### Security

- Symlinks in central repository content are never followed, so a link such as
  `global/x.md -> ~/.ssh/id_rsa` cannot copy local secrets into steering or back upstream.
- Git hooks never run inside the cache clone, for any operation.
- The cache directory is created with mode 0700.
- Lock files never store credentials from the remote URL, and a remote URL with an embedded
  password triggers a warning.
- git subprocesses never inherit standard input, which protects the MCP stdio protocol stream.
- Every GitHub Action and pre-commit hook is pinned to a commit SHA, and CodeQL also analyses
  the workflows. Pull requests can run dependency review once the Dependency graph is enabled
  and the `DEPENDENCY_REVIEW` repository variable is set.

### Reliability

- An exclusive lock serialises processes that share a cache clone (`sync.lock_timeout_seconds`).
- Network operations retry transient failures with exponential backoff and jitter
  (`git.retries`, `git.retry_backoff_seconds`, `git.retry_max_backoff_seconds`).
- A corrupt or half written cache clone, or a stale `index.lock`, is repaired automatically.
- A failed push always returns the cache clone to a clean default branch.
- Pre-push hook runs use a bounded git timeout (`hook.timeout_seconds`) and never block the
  push on any unexpected error unless strict mode is enabled.
- Central and local content is checked against `sync.max_files` and `sync.max_total_bytes`.
- Synced files are written with mode 0644 instead of 0600.
- The console entry point turns unexpected errors into a one line message and exit code.
- Tests run on Linux and macOS.
