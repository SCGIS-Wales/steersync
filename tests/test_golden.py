"""Golden file tests: lint each collection's fixture tree and compare with expected.json.

Every directory under ``tests/golden`` is a collection as it would sit in a workspace
(the directory name before an optional ``-suffix`` names the collection). The findings
of linting it with that collection's patterns and profile are compared, rule by rule,
with ``tests/golden/<dir>.expected.json``. Style findings from pymarkdown are left out
so a pymarkdown release does not churn the files.

Regenerate after an intended change with ``STEERSYNC_UPDATE_GOLDEN=1 uv run pytest
tests/test_golden.py`` and review the diff.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from steersync.config import Config
from steersync.lint import lint_paths
from steersync.steering import FileSpec

GOLDEN = Path(__file__).parent / "golden"
CASES = sorted(p.name for p in GOLDEN.iterdir() if p.is_dir())


def findings(config: Config, case: str) -> dict[str, Any]:
    collection = config.collections[case.split("-bad", maxsplit=1)[0].split("-bare", maxsplit=1)[0]]
    directory = GOLDEN / case
    report = lint_paths(
        [directory],
        config.lint,
        directory,
        profile=collection.lint,
        spec=FileSpec(collection.include, collection.exclude, collection.max_depth),
    )
    return {
        "checked": report.checked,
        "findings": sorted(
            (
                {"path": f.path, "rule": f.rule, "severity": f.severity, "line": f.line}
                for f in report.findings
                if not f.rule.startswith("markdown-")
            ),
            key=lambda f: (f["path"], f["rule"], f["line"] or 0),
        ),
    }


def test_every_default_collection_has_a_golden_case(config: Config) -> None:
    covered = {case.split("-bad")[0].split("-bare")[0] for case in CASES}
    assert covered == set(config.collections)


@pytest.mark.parametrize("case", CASES)
def test_golden(config: Config, case: str) -> None:
    expected_file = GOLDEN / f"{case}.expected.json"
    actual = findings(config, case)
    if os.environ.get("STEERSYNC_UPDATE_GOLDEN") == "1":
        expected_file.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(expected_file.read_text("utf-8"))
    assert actual == expected
