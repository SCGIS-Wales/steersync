"""Machine readable lint output: steersync's JSON document and SARIF 2.1.0.

JSON (``--format json``)::

    {"version": 1, "findings": [...], "fixes": [{"path", "rules", "safety", "diff",
     "applied"}], "summary": {...}}

SARIF (``--format sarif``) has one run whose ``tool.driver.rules`` lists every rule that
was reported and every fix in the catalogue; each result carries its fix as
``fixes[].artifactChanges`` with ``properties.safety``.
"""

from collections.abc import Sequence
from typing import Any

from steersync import __version__
from steersync.fixes.model import FileFix
from steersync.lint import LintFinding, LintReport

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVEL = {"error": "error", "warning": "warning", "info": "note"}


def _summary(report: LintReport, fixes: Sequence[FileFix], applied: bool) -> dict[str, int]:
    return {
        "checked": report.checked,
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "infos": len(report.infos),
        "fixes": len(fixes),
        "applied": len(fixes) if applied else 0,
    }


def to_json(
    report: LintReport, fixes: Sequence[FileFix] = (), *, applied: bool = False
) -> dict[str, Any]:
    summary = _summary(report, fixes, applied)
    return {
        "version": 1,
        # The counts stay at the top level too, as `lint --json` has always printed them.
        "checked": summary["checked"],
        "errors": summary["errors"],
        "warnings": summary["warnings"],
        "infos": summary["infos"],
        "findings": [f._asdict() for f in report.findings],
        "fixes": [
            {
                "path": fix.path,
                "rules": list(fix.rules),
                "safety": fix.safety,
                "diff": fix.diff,
                "applied": applied,
            }
            for fix in fixes
        ],
        "summary": summary,
    }


def _location(finding: LintFinding) -> dict[str, Any]:
    physical: dict[str, Any] = {"artifactLocation": {"uri": finding.path}}
    if finding.line:
        physical["region"] = {"startLine": finding.line}
    return {"physicalLocation": physical}


def _whole_file_change(fix: FileFix) -> dict[str, Any]:
    lines = max(1, fix.original.count("\n") + (0 if fix.original.endswith("\n") else 1))
    return {
        "description": {"text": f"{', '.join(fix.rules)} ({fix.safety})"},
        "artifactChanges": [
            {
                "artifactLocation": {"uri": fix.path},
                "replacements": [
                    {
                        "deletedRegion": {"startLine": 1, "endLine": lines},
                        "insertedContent": {"text": fix.fixed},
                    }
                ],
            }
        ],
        "properties": {"safety": fix.safety, "rules": list(fix.rules)},
    }


def to_sarif(
    report: LintReport,
    fixes: Sequence[FileFix] = (),
    catalogue: Sequence[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    """``catalogue``: (fix id, safety, summary) for every fix steersync knows."""
    by_path = {fix.path: fix for fix in fixes}
    rule_ids = sorted({f.rule for f in report.findings} | {fix_id for fix_id, _, _ in catalogue})
    summaries = {fix_id: summary for fix_id, _, summary in catalogue}
    safeties = {fix_id: safety for fix_id, safety, _ in catalogue}
    rules = [
        {
            "id": rule_id,
            "shortDescription": {"text": summaries.get(rule_id, rule_id)},
            "properties": {"fixSafety": safeties.get(rule_id, "never")},
        }
        for rule_id in rule_ids
    ]
    results: list[dict[str, Any]] = []
    for finding in report.findings:
        result: dict[str, Any] = {
            "ruleId": finding.rule,
            "level": _LEVEL[finding.severity],
            "message": {"text": finding.message},
            "locations": [_location(finding)],
        }
        fix = by_path.get(finding.path)
        if fix is not None:
            result["fixes"] = [_whole_file_change(fix)]
        results.append(result)
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "steersync",
                        "version": __version__,
                        "informationUri": "https://github.com/SCGIS-Wales/steersync",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
