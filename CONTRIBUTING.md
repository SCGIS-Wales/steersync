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

1. Bump `__version__` in `src/steersync/__init__.py` and move the Unreleased notes in
   `CHANGELOG.md` under the new version.
2. Merge to `main`, then tag: `git tag v0.2.0 && git push origin v0.2.0`.
3. The release workflow builds, attests provenance, publishes to PyPI through the `pypi`
   environment and creates the GitHub release.
