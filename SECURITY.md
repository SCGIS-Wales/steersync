# Security policy

## Supported versions

Only the latest minor release receives security fixes.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub security advisories at
https://github.com/SCGIS-Wales/steersync/security/advisories/new. Do not open a public
issue. You will receive an acknowledgement within five working days.

## Security design

steersync is designed to run unattended from git hooks, Kiro hooks and MCP servers, so it
is deliberately conservative.

- **No credentials of its own.** All network access goes through the `git` binary, so the
  user's ssh agent or credential helper is used exactly as it is for a normal push. Interactive
  prompts are disabled (`GIT_TERMINAL_PROMPT=0`, `ssh -o BatchMode=yes`, empty `core.askPass`).
- **No shell.** Every git call is an argument list with `--` before positional arguments.
  Remote URLs are validated: only `ssh://`, `https://` and `user@host:path` forms are accepted,
  helper transports (`ext::`, `fd::`), option like values and insecure schemes are rejected, and
  `protocol.ext.allow=never` is forced on every invocation. An optional host allow list
  restricts which remotes may be used.
- **Path containment.** Files from the central repository are written only inside the target
  steering directory. Paths with `..`, absolute paths, `.git` components or backslashes are
  rejected, writes are atomic and directory symlinks are not followed.
- **No symlinks from the central repository.** Symlinked files in central content are ignored
  and a symlinked `global` or project directory is rejected, so a malicious link cannot copy a
  local file such as `~/.ssh/id_rsa` into steering.
- **Isolated cache.** The cache lives in a 0700 directory, git hooks are disabled for every
  operation inside it, and git never inherits standard input.
- **Bounded input.** Central and local content is limited by file count and total size.
- **Content checks before sharing.** Pushes are linted for secret patterns (AWS keys, private
  keys, GitHub, GitLab and Slack tokens, generic credentials), file size and encoding.
- **Hook safety.** The pre-push hook never blocks a project push unless strict mode is enabled,
  guards against recursion with `STEERSYNC_ACTIVE`, and pushes from the cache clone with hooks
  disabled so repository hooks cannot run arbitrary code during a sync.
- **Logs redact** user info in URLs and known token formats, and lock files never store
  credentials from the remote URL.
- **Supply chain.** Dependencies are pinned in `uv.lock`, audited with pip-audit on every
  change, reviewed with dependency review on pull requests, and statically analysed with
  bandit and CodeQL (Python and workflows). Every GitHub Action and pre-commit hook is pinned
  to a commit SHA and updated by Dependabot. Releases run the full CI gate, publish a
  `SHA256SUMS` file and carry build provenance attestations; PyPI publishing uses trusted
  publishing with no long lived tokens.
