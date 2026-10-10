---
inclusion: fileMatch
fileMatchPattern:
  - "**/*.py"
  - "pyproject.toml"
---
# Python conventions

Target the latest stable Python. Use ruff for linting and formatting, mypy in strict mode,
and pytest with coverage. Keep configuration in YAML files, never in code.
