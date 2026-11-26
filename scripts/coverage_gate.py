"""Per module coverage gates on top of the global fail_under (docs/plans section 11).

Usage: uv run coverage json -o coverage.json && uv run python scripts/coverage_gate.py

Reads coverage.json and fails when a gated module covers fewer lines or branches than its
gate. Modules that do not exist yet are skipped, so gates can be declared ahead of time.
"""

import fnmatch
import json
import sys
from pathlib import Path

# (glob over the source path, minimum line percent, minimum branch percent)
GATES: tuple[tuple[str, float, float], ...] = (
    ("src/steersync/interaction.py", 95.0, 90.0),
    ("src/steersync/plan*.py", 95.0, 90.0),
    ("src/steersync/cli_options.py", 95.0, 90.0),
    ("src/steersync/fixes/*.py", 95.0, 90.0),
    ("src/steersync/schema/*.py", 95.0, 90.0),
)


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def check(report: dict[str, object]) -> list[str]:
    files = report.get("files", {})
    if not isinstance(files, dict):
        return ["coverage.json has no files section"]
    failures: list[str] = []
    for name, data in sorted(files.items()):
        path = Path(name).as_posix()
        for pattern, min_lines, min_branches in GATES:
            if not fnmatch.fnmatch(path, pattern) and not fnmatch.fnmatch(path, "*/" + pattern):
                continue
            summary = data["summary"]
            lines = _percent(summary["covered_lines"], summary["num_statements"])
            branches = _percent(summary.get("covered_branches", 0), summary.get("num_branches", 0))
            if lines < min_lines:
                failures.append(f"{path}: {lines:.1f}% of lines, gate {min_lines}%")
            if branches < min_branches:
                failures.append(f"{path}: {branches:.1f}% of branches, gate {min_branches}%")
    return failures


def main() -> int:
    report = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json").read_text())
    failures = check(report)
    for failure in failures:
        print(f"coverage gate: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
