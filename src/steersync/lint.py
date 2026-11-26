"""Validation of synced content: front matter, size, references, secrets, markdown, JSON.

Each collection selects a lint profile. ``steering`` applies every Kiro front matter
rule, ``skill`` validates ``SKILL.md`` metadata, ``hooks``, ``agent``, ``settings`` and
``workflow`` check the shapes Kiro expects (see :mod:`steersync.kiro_rules`),
``kiroignore`` checks gitignore syntax, ``json`` and ``markdown`` check syntax and style,
and ``generic`` only enforces size limits and secret scanning. Profiles are looked up in
:data:`PROFILES`, so a new kind of content is one function and one table entry.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple

import yaml

from steersync import fm_rules, kiro_rules, practices, refs
from steersync.config import LintConfig, LintProfile
from steersync.kiro_rules import HOOK_TRIGGERS
from steersync.steering import (
    FILE_REFERENCE_RE,
    FileSpec,
    SteeringDocument,
    discover_files,
    document_from_bytes,
    parse_front_matter,
)

Severity = Literal["error", "warning", "info"]


class LintContext(NamedTuple):
    """What a profile may look at besides the document itself."""

    workspace_root: Path | None = None
    # On disk directory of the collection, when the snapshot came from one.
    directory: Path | None = None


_H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)
SKILL_FILE = "SKILL.md"
__all__ = ["HOOK_TRIGGERS", "LintFinding", "LintReport", "lint_paths", "lint_snapshot"]


class LintFinding(NamedTuple):
    path: str
    rule: str
    severity: Severity
    message: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.severity}: [{self.rule}] {self.message}"


# Only these stop a push: a secret in the central repository reaches every machine and
# its history. Every other error is reported and the files are synced as they are.
SECRET_RULES = frozenset({"mcp-literal-secret"})


def is_secret_rule(rule: str) -> bool:
    return rule in SECRET_RULES or rule.startswith("forbidden-")


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
    def infos(self) -> tuple[LintFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "info")

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def blocking(self) -> tuple[LintFinding, ...]:
        """Errors that must never reach the central repository: secrets."""
        return tuple(f for f in self.errors if is_secret_rule(f.rule))

    def to_json(self) -> str:
        return json.dumps(
            {
                "checked": self.checked,
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "infos": len(self.infos),
                "findings": [f._asdict() for f in self.findings],
            },
            indent=2,
        )


def _hits(doc: SteeringDocument, hits: Iterable[fm_rules.Hit]) -> list[LintFinding]:
    return [LintFinding(doc.relative_path, h.rule, h.severity, h.message, h.line) for h in hits]


def _front_matter_findings(doc: SteeringDocument, config: LintConfig) -> list[LintFinding]:
    fm = doc.front_matter
    if fm is None or doc.text is None:
        return []
    position = fm_rules.position_hits(doc.text)
    if position:
        return _hits(doc, position)
    if fm.error:
        return [LintFinding(doc.relative_path, "fm-yaml", "error", fm.error, fm.error_line or 1)]
    if not fm.present:
        return []
    meta = fm.metadata
    return _hits(
        doc,
        [
            *fm_rules.steering_hits(meta, config.frontmatter),
            *fm_rules.metadata_hits(meta, config.frontmatter),
        ],
    )


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


def _reference_findings(
    doc: SteeringDocument, config: LintConfig, base: Path | None
) -> list[LintFinding]:
    """ref-syntax always; ref-unresolved when the base the paths resolve from is known."""
    if doc.text is None:
        return []
    references, problems = refs.parse(doc.text)
    if config.file_reference_check and base is not None:
        problems += [p for r in references if (p := refs.resolve(r, base)) is not None]
    return [LintFinding(doc.relative_path, p.rule, "error", p.message, p.line) for p in problems]


def _reference_base(ctx: LintContext) -> Path | None:
    """Workspace steering resolves from the workspace root, global steering from its folder."""
    if ctx.directory is not None and ctx.workspace_root is not None:
        try:
            ctx.directory.resolve().relative_to(ctx.workspace_root.resolve())
        except ValueError:
            return ctx.directory  # ~/.kiro/steering
    return ctx.workspace_root or ctx.directory


def _body_findings(
    doc: SteeringDocument, config: LintConfig, base: Path | None
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
    findings.extend(_reference_findings(doc, config, base))
    return findings


def _markdown_findings(doc: SteeringDocument, config: LintConfig) -> list[LintFinding]:
    if not config.pymarkdown.enabled or doc.text is None:
        return []
    from pymarkdown.api import PyMarkdownApi, PyMarkdownApiException  # noqa: PLC0415  (slow import)

    api = PyMarkdownApi().enable_extension_by_identifier("front-matter")
    for rule in config.pymarkdown.disabled_rules:
        api = api.disable_rule_by_identifier(rule)
    # Windows checkouts carry CRLF line endings; pymarkdown would misread them.
    text = doc.text.replace("\r\n", "\n")
    masked = FILE_REFERENCE_RE.sub(lambda m: "x" * len(m.group(0)), text)
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
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    findings = _text_findings(doc)
    if findings:
        return findings
    findings.extend(_front_matter_findings(doc, config))
    findings.extend(_body_findings(doc, config, _reference_base(ctx)))
    findings.extend(_markdown_findings(doc, config))
    if doc.text is not None and len(doc.text) > config.kiro_limits.file_chars_truncate:
        findings.append(
            LintFinding(
                doc.relative_path,
                "steering-size",
                "warning",
                f"{len(doc.text)} characters; Kiro truncates a file at "
                f"{config.kiro_limits.file_chars_truncate}",
            )
        )
    if config.practices and doc.front_matter is not None and not doc.front_matter.error:
        meta = doc.front_matter.metadata if doc.front_matter.present else {}
        findings.extend(_warnings(practices.steering_document_findings(doc, meta, _limits(config))))
    return findings


def _profile_markdown(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Plain markdown such as specs: text, secrets and style, no Kiro front matter rules."""
    if PurePosixPath(doc.relative_path).suffix.lower() != ".md":
        return _profile_generic(doc, config, ctx)
    findings = _text_findings(doc)
    if findings:
        return findings
    findings.extend(_secret_findings(doc, config))
    findings.extend(_markdown_findings(doc, config))
    if PurePosixPath(doc.relative_path).name == "AGENTS.md":
        # AGENTS.md references resolve from the folder that holds it.
        base = (
            (ctx.workspace_root / PurePosixPath(doc.relative_path).parent)
            if ctx.workspace_root
            else None
        )
        findings.extend(_reference_findings(doc, config, base))
    return findings


def _profile_spec(doc: SteeringDocument, config: LintConfig, ctx: LintContext) -> list[LintFinding]:
    """Specs: markdown, plus acceptance criteria in EARS form (info)."""
    findings = _profile_markdown(doc, config, ctx)
    if doc.text is not None and PurePosixPath(doc.relative_path).suffix.lower() == ".md":
        findings.extend(_hits(doc, fm_rules.ears_hits(doc.text, config.spec)))
    return findings


def _skill_metadata_findings(doc: SteeringDocument, config: LintConfig) -> list[LintFinding]:
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
        return [LintFinding(doc.relative_path, "fm-yaml", "error", fm.error, fm.error_line or 1)]
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
    folder = PurePosixPath(doc.relative_path).parent.name
    limit = config.practice_limits.skill_name_max_chars
    findings.extend(_hits(doc, fm_rules.skill_name_hits(fm.metadata.get("name"), folder, limit)))
    findings.extend(_hits(doc, fm_rules.metadata_hits(fm.metadata, config.frontmatter)))
    findings.extend(_hits(doc, fm_rules.skill_size_hits(fm.body, config.budgets)))
    return findings


def _profile_skill(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Agent Skills: SKILL.md metadata, markdown style, secrets in scripts and references."""
    if PurePosixPath(doc.relative_path).name != SKILL_FILE:
        return _profile_markdown(doc, config, ctx)
    findings = _text_findings(doc)
    if findings:
        return findings
    findings.extend(_skill_metadata_findings(doc, config))
    findings.extend(_secret_findings(doc, config))
    findings.extend(_markdown_findings(doc, config))
    fm = doc.front_matter
    if config.practices and fm is not None and fm.present and not fm.error:
        findings.extend(_warnings(practices.skill_findings(doc, fm.metadata, _limits(config))))
    return findings


def _profile_json(doc: SteeringDocument, config: LintConfig, ctx: LintContext) -> list[LintFinding]:
    if PurePosixPath(doc.relative_path).suffix.lower() != ".json":
        return _profile_generic(doc, config, ctx)
    findings = _text_findings(doc)
    if findings:
        return findings
    _, syntax = _parse_json(doc)
    findings.extend(syntax)
    findings.extend(_secret_findings(doc, config))
    return findings


def _profile_settings(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Kiro settings files: valid JSON plus the mcp.json and lsp.json shapes."""
    if PurePosixPath(doc.relative_path).suffix.lower() != ".json":
        return _profile_generic(doc, config, ctx)
    findings = _text_findings(doc)
    if findings:
        return findings
    data, syntax = _parse_json(doc)
    findings.extend(syntax)
    if not syntax:
        findings.extend(_issues(doc, _settings_issues(PurePosixPath(doc.relative_path).name, data)))
        if config.practices:
            findings.extend(_warnings(practices.settings_findings(doc, data)))
    findings.extend(_secret_findings(doc, config))
    return findings


def _settings_issues(name: str, data: object) -> list[kiro_rules.Issue]:
    match name:
        case "mcp.json":
            return kiro_rules.mcp_issues(data)
        case "sandbox.json":
            return kiro_rules.sandbox_issues(data)
        case "cli.json":
            return kiro_rules.cli_settings_issues(data)
        case _:
            return []


def _issues(doc: SteeringDocument, issues: Iterable[kiro_rules.Issue]) -> list[LintFinding]:
    return [LintFinding(doc.relative_path, i.rule, i.severity, i.message) for i in issues]


def _profile_prompt(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Saved prompts: markdown files whose name becomes the slash command."""
    findings = _profile_markdown(doc, config, ctx)
    if config.practices and PurePosixPath(doc.relative_path).suffix.lower() == ".md":
        findings.extend(_warnings(practices.prompt_findings(doc, _limits(config))))
    return findings


def _profile_hooks(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    name = PurePosixPath(doc.relative_path).name
    if not name.endswith((".json", ".kiro.hook")):
        return _profile_generic(doc, config, ctx)
    findings = _text_findings(doc)
    if findings:
        return findings
    if name.endswith(".kiro.hook"):
        findings.append(
            LintFinding(
                doc.relative_path,
                "hook-legacy-file",
                "error",
                "*.kiro.hook is the legacy hook format and Kiro V3 does not run it; write "
                '.kiro/hooks/<id>.json with "version": "v1" and "hooks": [{"name", "trigger" '
                '(PascalCase), "action": {"type": "command", "command"}}]',
            )
        )
        findings.extend(_secret_findings(doc, config))
        return findings
    data, syntax = _parse_json(doc)
    findings.extend(syntax)
    if not syntax:
        findings.extend(_issues(doc, kiro_rules.hook_file_issues(data)))
    if config.practices:
        findings.extend(_warnings(practices.hook_findings(doc, data, _limits(config))))
    findings.extend(_secret_findings(doc, config))
    return findings


def _profile_agent(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Custom agents: JSON definitions need a name; prompt files are plain markdown."""
    suffix = PurePosixPath(doc.relative_path).suffix.lower()
    if suffix == ".md":
        return _profile_markdown_agent(doc, config, ctx)
    if suffix != ".json":
        return _profile_generic(doc, config, ctx)
    findings = _text_findings(doc)
    if findings:
        return findings
    data, syntax = _parse_json(doc)
    findings.extend(syntax)
    if not syntax:
        if isinstance(data, dict) and config.practices:
            findings.extend(_warnings(practices.agent_findings(doc, data, ctx.directory)))
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
            findings.extend(_issues(doc, _agent_rules(data, config, markdown=False)))
            findings.extend(_resource_budget(doc, data, config, ctx))
    findings.extend(_secret_findings(doc, config))
    return findings


def _agent_rules(data: Mapping[str, Any], config: LintConfig, *, markdown: bool) -> list[Any]:
    return kiro_rules.agent_issues(
        data,
        fields=config.agent_fields,
        legacy_keys=config.legacy_agent_keys,
        upgraded_keys=config.upgraded_agent_keys,
        markdown=markdown,
    )


def _resource_budget(
    doc: SteeringDocument, data: Mapping[str, Any], config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """agent-resource-budget: file:// resources Kiro would truncate or skip at startup."""
    if ctx.workspace_root is None:
        return []
    limits = config.kiro_limits
    files: dict[Path, int] = {}
    for resource in data.get("resources") or []:
        if not isinstance(resource, str) or not resource.startswith("file://"):
            continue
        for match in sorted(ctx.workspace_root.glob(resource.removeprefix("file://"))):
            if match.is_file():
                files[match] = match.stat().st_size
    issues: list[str] = []
    if len(files) > limits.agent_file_resources_max:
        issues.append(
            f"{len(files)} file:// resource files; Kiro loads at most "
            f"{limits.agent_file_resources_max}"
        )
    chars = 0
    for path, size in files.items():
        if size > limits.file_bytes_skip:
            issues.append(
                f"{path.name} is {size} bytes; Kiro skips files over {limits.file_bytes_skip}"
            )
            continue
        length = len(path.read_text("utf-8", errors="replace"))
        chars += length
        if length > limits.file_chars_truncate:
            issues.append(
                f"{path.name} has {length} characters; Kiro truncates at "
                f"{limits.file_chars_truncate}"
            )
    if chars > limits.agent_file_resources_chars:
        issues.append(
            f"file:// resources total {chars} characters; Kiro loads at most "
            f"{limits.agent_file_resources_chars}"
        )
    return [
        LintFinding(doc.relative_path, "agent-resource-budget", "warning", message)
        for message in issues
    ]


def _profile_markdown_agent(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """A Markdown agent carries its configuration in front matter; prompt files do not."""
    findings = _profile_markdown(doc, config, ctx)
    fm = doc.front_matter
    if (
        fm is not None
        and fm.present
        and not fm.error
        and PurePosixPath(doc.relative_path).parent == PurePosixPath()
    ):
        findings.extend(_issues(doc, _agent_rules(fm.metadata, config, markdown=True)))
        if config.practices:
            findings.extend(_warnings(practices.agent_findings(doc, fm.metadata, ctx.directory)))
    return findings


_WORKFLOW_SUFFIXES: tuple[str, ...] = (".workflow.json", ".workflow.yaml", ".workflow.yml")


def _profile_workflow(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Workflow recipes: ``<name>.workflow.json`` (or YAML) checked against the recipe schema."""
    name = PurePosixPath(doc.relative_path).name
    if not name.endswith(_WORKFLOW_SUFFIXES):
        return _profile_generic(doc, config, ctx)
    findings = _text_findings(doc)
    if findings or doc.text is None:
        return findings
    if name.endswith(".json"):
        data, syntax = _parse_json(doc)
        findings.extend(syntax)
    else:
        try:
            data = yaml.safe_load(doc.text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = mark.line + 1 if mark is not None else None
            return [LintFinding(doc.relative_path, "yaml-syntax", "error", str(exc), line)]
    if not findings:
        findings.extend(_issues(doc, kiro_rules.workflow_issues(data, name)))
    findings.extend(_secret_findings(doc, config))
    return findings


def _profile_kiroignore(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """``.kiroignore`` files: gitignore syntax, plus advice on secrets in the root file."""
    del ctx
    findings = _text_findings(doc)
    if findings or doc.text is None:
        return findings
    root_file = doc.relative_path in {".kiroignore", "kiroignore"}
    findings.extend(_issues(doc, kiro_rules.kiroignore_issues(doc.text, root_file=root_file)))
    findings.extend(_secret_findings(doc, config))
    return findings


def _profile_generic(
    doc: SteeringDocument, config: LintConfig, ctx: LintContext
) -> list[LintFinding]:
    """Any file: binary content is allowed, decodable text is scanned for secrets."""
    del ctx
    return _secret_findings(doc, config) if _is_text(doc) else []


def _warnings(found: Iterable[practices.Finding]) -> list[LintFinding]:
    return [LintFinding(f.path, f.rule, "warning", f.message, f.line) for f in found]


def _limits(config: LintConfig) -> practices.PracticeLimits:
    return practices.PracticeLimits(**config.practice_limits.model_dump())


def collection_findings(
    profile: LintProfile, snapshot: Mapping[str, bytes], config: LintConfig
) -> list[LintFinding]:
    """Practices that look at a whole collection rather than one file."""
    if not config.practices:
        return []
    if profile == "steering":
        return [
            *_warnings(practices.steering_collection_findings(snapshot)),
            *_steering_collection(snapshot, config),
        ]
    if profile == "skill":
        return _warnings(practices.skill_collection_findings(snapshot))
    return []


def _steering_collection(snapshot: Mapping[str, bytes], config: LintConfig) -> list[LintFinding]:
    """steering-dup-rule, and steering-size for the always included set."""
    texts: dict[str, str] = {}
    always = 0
    for path, raw in snapshot.items():
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        texts[path] = text
        front = parse_front_matter(text)
        if front.metadata.get("inclusion", "always") == "always":
            always += len(text)
    findings = [
        LintFinding(path, hit.rule, hit.severity, hit.message, hit.line)
        for path, hit in fm_rules.dup_rule_hits(texts, config.budgets)
    ]
    if always > config.budgets.always_chars:
        findings.append(
            LintFinding(
                ".",
                "steering-size",
                "warning",
                f"{always} characters of steering are always included; above "
                f"{config.budgets.always_chars} every turn pays for them, so move occasional "
                "guidance to fileMatch, manual or auto steering",
            )
        )
    return findings


def _steering_extension_findings(directory: Path) -> list[LintFinding]:
    """steering-ext: Kiro loads only .md files from a steering folder."""
    return [
        LintFinding(
            path.relative_to(directory).as_posix(),
            "steering-ext",
            "error",
            "Kiro loads only .md files from the steering folder; rename it to .md or move it",
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        and path.suffix.lower() != ".md"
        and not any(part.startswith(".") for part in path.relative_to(directory).parts)
    ]


def sidecar_report(root: Path, config: LintConfig) -> LintReport:
    """fm-metadata for .kiro/steersync.meta.yaml: existing paths mapped to metadata maps."""
    sidecar = root / config.frontmatter.sidecar
    if not sidecar.is_file():
        return LintReport((), 0)
    label = config.frontmatter.sidecar
    try:
        data = yaml.safe_load(sidecar.read_text("utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return LintReport((LintFinding(label, "fm-yaml", "error", f"invalid YAML: {exc}"),), 1)
    existing = (
        [str(path) for path in data if (root / str(path)).exists()]
        if isinstance(data, Mapping)
        else []
    )
    hits = fm_rules.sidecar_hits(data, existing, config.frontmatter)
    return LintReport(tuple(_hits_at(label, hits)), 1)


def _hits_at(path: str, hits: Iterable[fm_rules.Hit]) -> list[LintFinding]:
    return [LintFinding(path, h.rule, h.severity, h.message, h.line) for h in hits]


def lint_workspace(
    root: Path,
    config: LintConfig,
    *,
    hooks_dir: str,
    settings_dir: str,
    kiro_home: Path | None = None,
) -> LintReport:
    """Report whether a Kiro session in ``root`` loads and refreshes the configuration."""
    sidecar = sidecar_report(root, config)
    if not config.practices:
        return sidecar
    found = practices.workspace_findings(
        root, hooks_dir=hooks_dir, settings_dir=settings_dir, kiro_home=kiro_home
    )
    return LintReport((*_warnings(found), *sidecar.findings), sidecar.checked)


Profile = Callable[[SteeringDocument, LintConfig, LintContext], list[LintFinding]]

PROFILES: dict[str, Profile] = {
    "steering": _profile_steering,
    "markdown": _profile_markdown,
    "spec": _profile_spec,
    "skill": _profile_skill,
    "agent": _profile_agent,
    "hooks": _profile_hooks,
    "settings": _profile_settings,
    "prompt": _profile_prompt,
    "workflow": _profile_workflow,
    "kiroignore": _profile_kiroignore,
    "json": _profile_json,
    "generic": _profile_generic,
}


def lint_document(
    doc: SteeringDocument,
    config: LintConfig,
    workspace_root: Path | None = None,
    *,
    profile: LintProfile = "steering",
    directory: Path | None = None,
) -> list[LintFinding]:
    """Lint one document with the given profile and return its findings."""
    findings = PROFILES[profile](doc, config, LintContext(workspace_root, directory))
    return list(dict.fromkeys(findings))


def lint_snapshot(
    snapshot: Mapping[str, bytes],
    config: LintConfig,
    workspace_root: Path | None = None,
    *,
    profile: LintProfile = "steering",
    directory: Path | None = None,
    collection_level: bool = True,
) -> LintReport:
    """Lint an in memory snapshot, including the practices that span the collection.

    ``collection_level`` is off for ad hoc directories that are not a collection, so a
    folder of examples is not told it lacks the steering foundation files.
    """
    findings: list[LintFinding] = []
    for relative in sorted(snapshot):
        findings.extend(
            lint_document(
                document_from_bytes(relative, snapshot[relative]),
                config,
                workspace_root,
                profile=profile,
                directory=directory,
            )
        )
    if collection_level:
        findings.extend(collection_findings(profile, snapshot, config))
    return LintReport(tuple(findings), len(snapshot))


def lint_paths(
    paths: Iterable[Path],
    config: LintConfig,
    workspace_root: Path | None = None,
    *,
    profile: LintProfile = "steering",
    spec: FileSpec | None = None,
) -> LintReport:
    """Lint files or directories on disk. Directories are expanded with ``spec``.

    A directory is linted as a collection, so the practices that span a collection
    (foundation files, the always on budget, misplaced skills) are reported too,
    with paths relative to the workspace root when one is given.
    """
    findings: list[LintFinding] = []
    checked = 0
    for path in paths:
        if path.is_dir():
            files = discover_files(path, spec) if spec else discover_files(path)
            snapshot = {f.relative_to(path).as_posix(): f.read_bytes() for f in files}
            if profile == "steering":
                findings.extend(
                    prefix_report(
                        LintReport(tuple(_steering_extension_findings(path)), 0),
                        _label(path, workspace_root),
                    ).findings
                )
            report = lint_snapshot(
                snapshot,
                config,
                workspace_root,
                profile=profile,
                directory=path,
                collection_level=spec is not None,
            )
            findings.extend(prefix_report(report, _label(path, workspace_root)).findings)
            checked += report.checked
        elif path.is_file():
            findings.extend(
                lint_document(
                    document_from_bytes(_label(path, workspace_root), path.read_bytes()),
                    config,
                    workspace_root,
                    profile=profile,
                    directory=path.parent,
                )
            )
            checked += 1
    return LintReport(tuple(findings), checked)


def _label(path: Path, workspace_root: Path | None) -> str:
    if workspace_root and path.is_relative_to(workspace_root):
        return path.relative_to(workspace_root).as_posix()
    return str(path)


def prefix_report(report: LintReport, prefix: str) -> LintReport:
    """Return ``report`` with every finding path prefixed, for combined reports."""
    if not prefix or prefix == ".":
        return report
    base = prefix.rstrip("/")
    return LintReport(
        tuple(
            f._replace(path=base if f.path == "." else f"{base}/{f.path}") for f in report.findings
        ),
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
