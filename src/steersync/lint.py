"""Validation of steering documents: front matter, size, references, secrets, markdown."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from steersync.config import LintConfig
from steersync.steering import (
    FILE_REFERENCE_RE,
    INCLUSION_MODES,
    KNOWN_KEYS,
    SteeringDocument,
    discover_steering_files,
    document_from_bytes,
    file_references,
)

Severity = Literal["error", "warning"]
_H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)


class LintFinding(NamedTuple):
    path: str
    rule: str
    severity: Severity
    message: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.severity}: [{self.rule}] {self.message}"


class LintReport(NamedTuple):
    findings: tuple[LintFinding, ...]
    checked: int

    @property
    def errors(self) -> tuple[LintFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[LintFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> str:
        return json.dumps(
            {
                "checked": self.checked,
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "findings": [f._asdict() for f in self.findings],
            },
            indent=2,
        )


def _front_matter_findings(doc: SteeringDocument, config: LintConfig) -> list[LintFinding]:
    findings: list[LintFinding] = []
    fm = doc.front_matter
    if fm is None:
        return findings
    if fm.leading_blank:
        findings.append(
            LintFinding(
                doc.relative_path,
                "front-matter-position",
                "error",
                "front matter must start on line 1; Kiro ignores it after a blank line",
                1,
            )
        )
        return findings
    if fm.error:
        findings.append(LintFinding(doc.relative_path, "front-matter-yaml", "error", fm.error, 1))
        return findings
    if not fm.present:
        return findings
    meta = fm.metadata
    inclusion = meta.get("inclusion", "always")
    if inclusion not in INCLUSION_MODES:
        findings.append(
            LintFinding(
                doc.relative_path,
                "inclusion-value",
                "error",
                f"inclusion must be one of {', '.join(INCLUSION_MODES)}; got {inclusion!r}",
                2,
            )
        )
    pattern = meta.get("fileMatchPattern")
    if inclusion == "fileMatch":
        if pattern is None:
            findings.append(
                LintFinding(
                    doc.relative_path,
                    "filematch-pattern-required",
                    "error",
                    "fileMatch requires fileMatchPattern",
                    2,
                )
            )
        elif not _is_pattern(pattern):
            findings.append(
                LintFinding(
                    doc.relative_path,
                    "filematch-pattern-type",
                    "error",
                    "fileMatchPattern must be a string or a list of strings",
                    2,
                )
            )
    elif pattern is not None:
        findings.append(
            LintFinding(
                doc.relative_path,
                "filematch-pattern-unused",
                "warning",
                "fileMatchPattern is ignored unless inclusion is fileMatch",
                2,
            )
        )
    if inclusion == "auto":
        for key in ("name", "description"):
            value = meta.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    LintFinding(
                        doc.relative_path,
                        "auto-requires-metadata",
                        "error",
                        f"inclusion: auto requires a non empty {key}",
                        2,
                    )
                )
    allowed = KNOWN_KEYS | set(config.allowed_extra_keys)
    findings.extend(
        LintFinding(
            doc.relative_path,
            "unknown-key",
            "warning",
            f"unknown front matter key {key!r} is ignored by Kiro",
            2,
        )
        for key in meta
        if key not in allowed
    )
    return findings


def _is_pattern(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, str) and v.strip() for v in value)
    )


def _body_findings(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    if doc.text is None:
        return findings
    body = doc.front_matter.body if doc.front_matter else doc.text
    if config.require_h1 and not _H1_RE.search(body):
        findings.append(
            LintFinding(
                doc.relative_path, "require-h1", "warning", "document has no level one heading"
            )
        )
    for item in config.forbidden_patterns:
        for match in re.finditer(item.pattern, doc.text):
            line = doc.text.count("\n", 0, match.start()) + 1
            findings.append(
                LintFinding(doc.relative_path, f"forbidden-{item.id}", "error", item.message, line)
            )
    if config.file_reference_check and workspace_root is not None:
        for kind, target in file_references(doc.text):
            candidate = (workspace_root / target).resolve()
            if (
                workspace_root.resolve() not in candidate.parents
                and candidate != workspace_root.resolve()
            ):
                findings.append(
                    LintFinding(
                        doc.relative_path,
                        "file-reference-escape",
                        "error",
                        f"#[[{kind}:{target}]] points outside the workspace",
                    )
                )
            elif not candidate.exists():
                findings.append(
                    LintFinding(
                        doc.relative_path,
                        "file-reference-missing",
                        "warning",
                        f"#[[{kind}:{target}]] does not exist in this workspace",
                    )
                )
    return findings


def _markdown_findings(doc: SteeringDocument, config: LintConfig) -> list[LintFinding]:
    if not config.pymarkdown.enabled or doc.text is None:
        return []
    from pymarkdown.api import PyMarkdownApi, PyMarkdownApiException  # noqa: PLC0415  (slow import)

    api = PyMarkdownApi().enable_extension_by_identifier("front-matter")
    for rule in config.pymarkdown.disabled_rules:
        api = api.disable_rule_by_identifier(rule)
    masked = FILE_REFERENCE_RE.sub(lambda m: "x" * len(m.group(0)), doc.text)
    try:
        result = api.scan_string(masked)
    except PyMarkdownApiException as exc:
        return [
            LintFinding(
                doc.relative_path, "markdown-scan", "warning", f"markdown scan failed: {exc}"
            )
        ]
    return [
        LintFinding(
            doc.relative_path,
            f"markdown-{failure.rule_id}",
            config.pymarkdown.severity,
            failure.rule_description,
            failure.line_number,
        )
        for failure in result.scan_failures
    ]


def lint_document(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None = None
) -> list[LintFinding]:
    """Lint one document and return its findings."""
    findings: list[LintFinding] = []
    if len(doc.raw) > config.max_file_bytes:
        findings.append(
            LintFinding(
                doc.relative_path,
                "max-size",
                "error",
                f"file is {len(doc.raw)} bytes; limit is {config.max_file_bytes}",
            )
        )
    if doc.text is None:
        findings.append(
            LintFinding(doc.relative_path, "encoding-utf8", "error", "file is not valid UTF-8")
        )
        return findings
    if "\0" in doc.text:
        findings.append(
            LintFinding(doc.relative_path, "encoding-utf8", "error", "file contains NUL bytes")
        )
        return findings
    findings.extend(_front_matter_findings(doc, config))
    findings.extend(_body_findings(doc, config, workspace_root))
    findings.extend(_markdown_findings(doc, config))
    return list(dict.fromkeys(findings))


def lint_snapshot(
    snapshot: Mapping[str, bytes], config: LintConfig, workspace_root: Path | None = None
) -> LintReport:
    """Lint an in memory snapshot."""
    findings: list[LintFinding] = []
    for relative in sorted(snapshot):
        findings.extend(
            lint_document(document_from_bytes(relative, snapshot[relative]), config, workspace_root)
        )
    return LintReport(tuple(findings), len(snapshot))


def lint_paths(
    paths: Iterable[Path], config: LintConfig, workspace_root: Path | None = None
) -> LintReport:
    """Lint files or directories on disk."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(discover_steering_files(path))
        elif path.is_file():
            files.append(path)
    findings: list[LintFinding] = []
    for file in files:
        label = (
            file.relative_to(workspace_root).as_posix()
            if workspace_root and file.is_relative_to(workspace_root)
            else str(file)
        )
        findings.extend(
            lint_document(document_from_bytes(label, file.read_bytes()), config, workspace_root)
        )
    return LintReport(tuple(findings), len(files))
