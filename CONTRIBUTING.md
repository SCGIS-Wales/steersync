# Contributing

## Development environment

```bash
uv python install 3.14
uv sync --all-extras --group dev
uv run pre-commit install
```

## Checks

The CI pipeline runs exactly these commands. Run them before pushing.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
uv run bandit -c pyproject.toml -r src -ll
```

## Conventions

- Python 3.14 or newer. Functional style: modules of pure functions with explicit inputs;
  pydantic models and NamedTuples are used only as typed records.
- All behaviour is configurable through `steersync.yaml`; no hostnames, paths or policies are
  hard coded in the source.
- Every git interaction goes through `steersync.gitops.run_git`. Never call a shell.
- New behaviour needs tests. Integration tests use real local git repositories (see
  `tests/conftest.py`), not mocks.
- Update `CHANGELOG.md` under Unreleased.

## Releasing

Releases happen automatically when a pull request merges to `main`; nobody bumps versions
or pushes tags by hand.

1. Write your notes under `## [Unreleased]` in `CHANGELOG.md` as part of the pull request.
2. Say how big the change is, first match wins:
   - a label `release:major`, `release:minor`, `release:patch` or `release:skip`;
   - a Conventional Commits prefix in the pull request title: `feat!:` or `BREAKING CHANGE`
     is major, `feat:` minor, `fix:`, `perf:`, `refactor:`, `deps:`, `revert:` patch,
     `docs:`, `chore:`, `ci:`, `test:`, `style:`, `build:` no release;
   - otherwise the Unreleased section decides: `### Added` or `### Removed` present is minor,
     any other content patch, an empty section no release.
3. Merge. The `CI` workflow on `main` finds the merged pull request, computes the version
   (`scripts/prepare_release.py`), moves the Unreleased notes under `## [X.Y.Z] - date`
   (using the pull request title as the note when the section was empty), bumps
   `__version__`, the README install snippet and the pre-commit example, and commits that
   to `main` as `github-actions[bot]`. The same run then builds from that commit, publishes
   to PyPI, writes `SHA256SUMS`, attests provenance, creates the annotated tag and the
   GitHub release with the changelog section as notes.
4. A version bumped by hand in the pull request is released as it is, with its changelog
   section. To retry a release whose later steps failed, run the `CI` workflow manually on
   `main`: an unreleased version in the file is released, files already on PyPI are skipped.
   Pushing a matching `vX.Y.Z` tag by hand also works.
5. If `main` is protected against pushes by the GitHub Actions bot, add a repository secret
   `RELEASE_TOKEN` (a token allowed to bypass the protection); the workflow uses it for the
   release commit and falls back to the job token otherwise. A rejected push fails the run
   with that message and nothing is published.
6. PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/): the
   publisher on PyPI is registered for the repository `SCGIS-Wales/steersync` and the
   workflow `ci.yml`, with no environment restriction. No API token or secret is stored
   anywhere. The publishing job must stay in the top level workflow named on PyPI: PyPI
   verifies the PEP 740 attestation against the workflow that owns the run, so publishing
   from a reusable workflow called by another file is rejected. If the publisher is ever
   re-registered under another file name, move the release jobs there.

## Dependency review

pip-audit checks the locked dependencies on every CI run. Pull requests can also run
GitHub dependency review: enable "Dependency graph" under Settings, Code security, then set
the repository variable `DEPENDENCY_REVIEW` to `true`.

## Repository metadata

The GitHub description, homepage and topics live in `.github/repository.yml`. The
`Repository metadata` workflow applies them when the file changes on `main`. It needs a
repository secret `REPO_ADMIN_TOKEN`: a fine grained token scoped to this repository with
"Administration: read and write". Without the secret the workflow only warns.
