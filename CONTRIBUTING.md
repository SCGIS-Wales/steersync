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

Releases are driven by the version number; nobody needs to push tags.

1. In a pull request, bump `__version__` in `src/steersync/__init__.py` and move the
   Unreleased notes in `CHANGELOG.md` under the new version.
2. Merge it. The `Release` workflow sees a version with no GitHub release, runs the full CI
   gate, builds the wheel and sdist, writes `SHA256SUMS`, attests provenance, creates the
   annotated `vX.Y.Z` tag and publishes the GitHub release with the files attached.
3. To retry a failed release, run the `Release` workflow manually on `main`. A version that is
   already released is skipped, and a version without a changelog section is refused.
   Pushing a matching `vX.Y.Z` tag by hand also works.
4. Optional PyPI publishing: configure a trusted publisher on PyPI for `release.yml` and the
   `pypi` environment, then set the repository variable `PUBLISH_TO_PYPI` to `true`.

## Dependency review

pip-audit checks the locked dependencies on every CI run. Pull requests can also run
GitHub dependency review: enable "Dependency graph" under Settings, Code security, then set
the repository variable `DEPENDENCY_REVIEW` to `true`.

## Repository metadata

The GitHub description, homepage and topics live in `.github/repository.yml`. The
`Repository metadata` workflow applies them when the file changes on `main`. It needs a
repository secret `REPO_ADMIN_TOKEN`: a fine grained token scoped to this repository with
"Administration: read and write". Without the secret the workflow only warns.
