"""Validation of synced content: front matter, size, references, secrets, markdown, JSON.

Each collection selects a lint profile. ``steering`` applies every Kiro front matter
rule, ``skill`` validates ``SKILL.md`` metadata, ``hooks`` and ``agent`` check the JSON
shape Kiro expects, ``json`` and ``markdown`` check syntax and style, and ``generic``
only enforces size limits and secret scanning. Profiles are looked up in
:data:`PROFILES`, so a new kind of content is one function and one table entry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from steersync.config import LintConfig, LintProfile
from steersync.steering import (
    FILE_REFERENCE_RE,
    INCLUSION_MODES,
    KNOWN_KEYS,
    FileSpec,
    SteeringDocument,
    discover_files,
    document_from_bytes,
    file_references,
)

Severity = Literal["error", "warning"]
_H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)
SKILL_FILE = "SKILL.md"
HOOK_TRIGGERS: frozenset[str] = frozenset(
    {
        "SessionStart",
        "Stop",
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "PreTaskExec",
        "PostTaskExec",
        "PostFileCreate",
        "PostFileSave",
        "PostFileDelete",
        "Manual",
    }
)


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


def _secret_findings(doc: SteeringDocument, config: LintConfig) -> list[LintFinding]:
    """Scan decodable text for the configured forbidden patterns."""
    findings: list[LintFinding] = []
    if doc.text is None:
        return findings
    for item in config.forbidden_patterns:
        for match in re.finditer(item.pattern, doc.text):
            line = doc.text.count("\n", 0, match.start()) + 1
            findings.append(
                LintFinding(doc.relative_path, f"forbidden-{item.id}", "error", item.message, line)
            )
    return findings


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
    findings.extend(_secret_findings(doc, config))
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


def _size_findings(doc: SteeringDocument, max_file_bytes: int) -> list[LintFinding]:
    if len(doc.raw) <= max_file_bytes:
        return []
    return [
        LintFinding(
            doc.relative_path,
            "max-size",
            "error",
            f"file is {len(doc.raw)} bytes; limit is {max_file_bytes}",
        )
    ]


def _text_findings(doc: SteeringDocument) -> list[LintFinding]:
    """Findings for content that must be UTF-8 text without NUL bytes."""
    if doc.text is None:
        return [LintFinding(doc.relative_path, "encoding-utf8", "error", "file is not valid UTF-8")]
    if "\0" in doc.text:
        return [LintFinding(doc.relative_path, "encoding-utf8", "error", "file contains NUL bytes")]
    return []


def _is_text(doc: SteeringDocument) -> bool:
    return doc.text is not None and "\0" not in doc.text


def _parse_json(doc: SteeringDocument) -> tuple[object, list[LintFinding]]:
    """Parse the document as JSON; a syntax error is reported as a finding."""
    if doc.text is None:
        return None, []
    try:
        return json.loads(doc.text), []
    except json.JSONDecodeError as exc:
        finding = LintFinding(
            doc.relative_path, "json-syntax", "error", f"invalid JSON: {exc.msg}", exc.lineno
        )
        return None, [finding]


def _profile_steering(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    findings = _text_findings(doc)
    if findings:
        return findings
    findings.extend(_front_matter_findings(doc, config))
    findings.extend(_body_findings(doc, config, workspace_root))
    findings.extend(_markdown_findings(doc, config))
    return findings


def _profile_markdown(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    """Plain markdown such as specs: text, secrets and style, no Kiro front matter rules."""
    if PurePosixPath(doc.relative_path).suffix.lower() != ".md":
        return _profile_generic(doc, config, workspace_root)
    findings = _text_findings(doc)
    if findings:
        return findings
    findings.extend(_secret_findings(doc, config))
    findings.extend(_markdown_findings(doc, config))
    return findings


def _skill_metadata_findings(doc: SteeringDocument) -> list[LintFinding]:
    fm = doc.front_matter
    if fm is None or fm.leading_blank or not fm.present:
        return [
            LintFinding(
                doc.relative_path,
                "skill-front-matter-required",
                "error",
                "SKILL.md must start with front matter containing name and description",
                1,
            )
        ]
    if fm.error:
        return [LintFinding(doc.relative_path, "front-matter-yaml", "error", fm.error, 1)]
    findings: list[LintFinding] = []
    for key in ("name", "description"):
        value = fm.metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            findings.append(
                LintFinding(
                    doc.relative_path,
                    "skill-requires-metadata",
                    "error",
                    f"SKILL.md requires a non empty {key}",
                    2,
                )
            )
    name = fm.metadata.get("name")
    folder = PurePosixPath(doc.relative_path).parent.name
    if isinstance(name, str) and folder and name.strip() != folder:
        findings.append(
            LintFinding(
                doc.relative_path,
                "skill-name-mismatch",
                "warning",
                f"skill name {name!r} does not match its directory {folder!r}",
                2,
            )
        )
    return findings


def _profile_skill(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    """Agent Skills: SKILL.md metadata, markdown style, secrets in scripts and references."""
    if PurePosixPath(doc.relative_path).name != SKILL_FILE:
        return _profile_markdown(doc, config, workspace_root)
    findings = _text_findings(doc)
    if findings:
        return findings
    findings.extend(_skill_metadata_findings(doc))
    findings.extend(_secret_findings(doc, config))
    findings.extend(_markdown_findings(doc, config))
    return findings


def _profile_json(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    if PurePosixPath(doc.relative_path).suffix.lower() != ".json":
        return _profile_generic(doc, config, workspace_root)
    findings = _text_findings(doc)
    if findings:
        return findings
    _, syntax = _parse_json(doc)
    findings.extend(syntax)
    findings.extend(_secret_findings(doc, config))
    return findings


def _hook_shape_findings(doc: SteeringDocument, data: object) -> list[LintFinding]:
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), list):
        return [
            LintFinding(
                doc.relative_path,
                "hook-schema",
                "error",
                'a standalone hook file is an object with "version" and a "hooks" list',
            )
        ]
    findings: list[LintFinding] = []
    if data.get("version") != "v1":
        findings.append(
            LintFinding(doc.relative_path, "hook-schema", "warning", 'expected "version": "v1"')
        )
    for index, hook in enumerate(data["hooks"]):
        label = f"hooks[{index}]"
        if not isinstance(hook, dict):
            findings.append(
                LintFinding(doc.relative_path, "hook-schema", "error", f"{label} must be an object")
            )
            continue
        findings.extend(
            LintFinding(doc.relative_path, "hook-schema", "error", f"{label} lacks {key!r}")
            for key in ("name", "trigger", "action")
            if key not in hook
        )
        trigger = hook.get("trigger")
        if isinstance(trigger, str) and trigger not in HOOK_TRIGGERS:
            findings.append(
                LintFinding(
                    doc.relative_path,
                    "hook-trigger",
                    "warning",
                    f"{label}: unknown trigger {trigger!r}; Kiro V3 triggers are PascalCase",
                )
            )
        action = hook.get("action")
        if isinstance(action, dict) and action.get("type") not in {"command", "agent"}:
            findings.append(
                LintFinding(
                    doc.relative_path,
                    "hook-action",
                    "error",
                    f'{label}: action.type must be "command" or "agent"',
                )
            )
    return findings


def _profile_hooks(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    if PurePosixPath(doc.relative_path).suffix.lower() != ".json":
        return _profile_generic(doc, config, workspace_root)
    findings = _text_findings(doc)
    if findings:
        return findings
    data, syntax = _parse_json(doc)
    findings.extend(syntax)
    if not syntax:
        findings.extend(_hook_shape_findings(doc, data))
    findings.extend(_secret_findings(doc, config))
    return findings


def _profile_agent(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    """Custom agents: JSON definitions need a name; prompt files are plain markdown."""
    suffix = PurePosixPath(doc.relative_path).suffix.lower()
    if suffix == ".md":
        return _profile_markdown(doc, config, workspace_root)
    if suffix != ".json":
        return _profile_generic(doc, config, workspace_root)
    findings = _text_findings(doc)
    if findings:
        return findings
    data, syntax = _parse_json(doc)
    findings.extend(syntax)
    if not syntax:
        if not isinstance(data, dict):
            findings.append(
                LintFinding(
                    doc.relative_path, "agent-schema", "error", "an agent file is a JSON object"
                )
            )
        else:
            name = data.get("name")
            if not isinstance(name, str) or not name.strip():
                findings.append(
                    LintFinding(
                        doc.relative_path, "agent-name", "warning", 'agent definition has no "name"'
                    )
                )
            if "toolsSettings" in data:
                findings.append(
                    LintFinding(
                        doc.relative_path,
                        "agent-deprecated-key",
                        "warning",
                        "toolsSettings is deprecated; use permissions.rules",
                    )
                )
    findings.extend(_secret_findings(doc, config))
    return findings


def _profile_generic(
    doc: SteeringDocument, config: LintConfig, workspace_root: Path | None
) -> list[LintFinding]:
    """Any file: binary content is allowed, decodable text is scanned for secrets."""
    del workspace_root
    return _secret_findings(doc, config) if _is_text(doc) else []


Profile = Callable[[SteeringDocument, LintConfig, Path | None], list[LintFinding]]

PROFILES: dict[str, Profile] = {
    "steering": _profile_steering,
    "markdown": _profile_markdown,
    "skill": _profile_skill,
    "agent": _profile_agent,
    "hooks": _profile_hooks,
    "json": _profile_json,
    "generic": _profile_generic,
}


def lint_document(
    doc: SteeringDocument,
    config: LintConfig,
    workspace_root: Path | None = None,
    *,
    profile: LintProfile = "steering",
    max_file_bytes: int | None = None,
) -> list[LintFinding]:
    """Lint one document with the given profile and return its findings."""
    findings = _size_findings(doc, max_file_bytes or config.max_file_bytes)
    findings.extend(PROFILES[profile](doc, config, workspace_root))
    return list(dict.fromkeys(findings))


def lint_snapshot(
    snapshot: Mapping[str, bytes],
    config: LintConfig,
    workspace_root: Path | None = None,
    *,
    profile: LintProfile = "steering",
    max_file_bytes: int | None = None,
) -> LintReport:
    """Lint an in memory snapshot."""
    findings: list[LintFinding] = []
    for relative in sorted(snapshot):
        findings.extend(
            lint_document(
                document_from_bytes(relative, snapshot[relative]),
                config,
                workspace_root,
                profile=profile,
                max_file_bytes=max_file_bytes,
            )
        )
    return LintReport(tuple(findings), len(snapshot))


def lint_paths(
    paths: Iterable[Path],
    config: LintConfig,
    workspace_root: Path | None = None,
    *,
    profile: LintProfile = "steering",
    spec: FileSpec | None = None,
    max_file_bytes: int | None = None,
) -> LintReport:
    """Lint files or directories on disk. Directories are expanded with ``spec``."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(discover_files(path, spec) if spec else discover_files(path))
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
            lint_document(
                document_from_bytes(label, file.read_bytes()),
                config,
                workspace_root,
                profile=profile,
                max_file_bytes=max_file_bytes,
            )
        )
    return LintReport(tuple(findings), len(files))


def prefix_report(report: LintReport, prefix: str) -> LintReport:
    """Return ``report`` with every finding path prefixed, for combined reports."""
    if not prefix:
        return report
    return LintReport(
        tuple(f._replace(path=f"{prefix.rstrip('/')}/{f.path}") for f in report.findings),
        report.checked,
    )


def merge_reports(reports: Iterable[LintReport]) -> LintReport:
    """Combine several reports into one."""
    findings: list[LintFinding] = []
    checked = 0
    for report in reports:
        findings.extend(report.findings)
        checked += report.checked
    return LintReport(tuple(findings), checked)
