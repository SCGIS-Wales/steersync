"""Front matter schema, organisational metadata, references and the phase 5 lint rules."""

import json
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from steersync import fm_rules, refs, schema
from steersync.cli import main
from steersync.config import Config, FrontMatterConfig, LintConfig
from steersync.lint import lint_document, lint_paths, lint_snapshot, sidecar_report
from steersync.schema.frontmatter import (
    FieldSpec,
    SkillFrontMatter,
    SteeringFrontMatter,
    validate_org_metadata,
)
from steersync.steering import FileSpec, document_from_bytes

LINT = Config().lint


def rules(text: str, path: str = "a.md", profile: str = "steering", **ctx: Path) -> set[str]:
    doc = document_from_bytes(path, text.encode())
    found = lint_document(doc, LINT, profile=profile, **ctx)  # type: ignore[arg-type]
    return {f.rule for f in found}


def findings(text: str, path: str = "a.md", profile: str = "steering") -> dict[str, list[str]]:
    doc = document_from_bytes(path, text.encode())
    out: dict[str, list[str]] = {}
    for f in lint_document(doc, LINT, profile=profile):  # type: ignore[arg-type]
        out.setdefault(f.rule, []).append(f.message)
    return out


# --- models ----------------------------------------------------------------------------


def test_steering_model_modes() -> None:
    assert SteeringFrontMatter().inclusion == "always"
    ok = SteeringFrontMatter.model_validate({"inclusion": "fileMatch", "fileMatchPattern": ["a"]})
    assert ok.file_match_pattern == ["a"]
    with pytest.raises(ValidationError, match="fileMatch requires"):
        SteeringFrontMatter.model_validate({"inclusion": "fileMatch"})
    with pytest.raises(ValidationError, match="auto requires"):
        SteeringFrontMatter.model_validate({"inclusion": "auto", "name": "x"})


def test_skill_model() -> None:
    SkillFrontMatter.model_validate(
        {"name": "release-notes", "description": "d", "allowed-tools": "x"}
    )
    with pytest.raises(ValidationError):
        SkillFrontMatter.model_validate({"name": "Release_Notes", "description": "d"})
    with pytest.raises(ValidationError):
        SkillFrontMatter.model_validate({"name": "a", "description": "d", "owner": "x"})


def test_org_metadata_validation() -> None:
    fields = Config().lint.frontmatter.metadata_fields
    today = date(2026, 11, 26)
    good = {
        "owner": "platform/api-guild",
        "tags": "api,security",
        "references": "#[[file:docs/a.md]] https://example.org/x",
        "review-by": "2027-03-31",
        "status": "active",
        "version": "1.2",
        "custom": "anything",
    }
    assert validate_org_metadata(good, fields, today=today) == []
    bad = {
        "owner": "has space",
        "references": "http://insecure",
        "review-by": "2026-01-01",
        "verified-on": "yesterday",
        "status": "done",
        "version": "v1",
        "tags": 3,
    }
    messages = " | ".join(i.message for i in validate_org_metadata(bad, fields, today=today))
    for fragment in (
        "metadata.owner 'has space' does not match",
        "metadata.references 'http://insecure'",
        "metadata.review-by 2026-01-01 has passed",
        "metadata.verified-on is not a YYYY-MM-DD date",
        "metadata.status must be one of draft",
        "metadata.version 'v1'",
        "metadata.tags is not a string",
    ):
        assert fragment in messages
    allowed = {"tags": FieldSpec(list=True, allowed=("api",))}
    (issue,) = validate_org_metadata({"tags": "api, web"}, allowed)
    assert "'web' is not one of" in issue.message


def test_schema_export_matches_the_committed_schemas(tmp_path: Path) -> None:
    """Golden: the models and schemas/*.json must not drift (regenerate with schema export)."""
    committed = Path(__file__).parent.parent / "schemas"
    for kind in schema.KINDS:
        expected = json.loads((committed / f"{kind}.schema.json").read_text())
        assert expected == json.loads(json.dumps(schema.json_schema(kind), sort_keys=True)), kind
    result = CliRunner().invoke(
        main, ["schema", "export", "--kind", "hook", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "hook.schema.json").read_text())["title"] == "HookFile"
    printed = CliRunner().invoke(main, ["schema", "export", "--kind", "steering"])
    assert "fileMatchPattern" in printed.output


# --- front matter rules ----------------------------------------------------------------------


def test_position_and_yaml() -> None:
    assert "fm-position" in rules("\ufeff---\ninclusion: manual\n---\n# T\n")
    assert "fm-position" in rules(" ---\ninclusion: manual\n---\n# T\n")
    assert "fm-position" in rules("\n---\ninclusion: manual\n---\n# T\n")
    assert "fm-yaml" in rules("---\n- a\n- b\n---\n# T\n")
    assert "fm-position" not in rules("\ufeff# No front matter\n")


def test_inclusion_patterns_and_auto() -> None:
    assert "fm-inclusion" in rules("---\ninclusion: Always\n---\n# T\n")
    comma = findings(
        '---\ninclusion: fileMatch\nfileMatchPattern: "src/**/*.py, tests/**"\n---\n# T\n'
    )
    assert 'fileMatchPattern: ["src/**/*.py", "tests/**"]' in comma["fm-filematch-comma"][0]
    relative = rules('---\ninclusion: fileMatch\nfileMatchPattern: ["./src/**", "/x"]\n---\n# T\n')
    assert "fm-filematch-relative" in relative
    assert "fm-auto-fields" in rules("---\ninclusion: auto\nname: x\n---\n# T\n")
    assert "fm-filematch-type" in rules(
        "---\ninclusion: fileMatch\nfileMatchPattern: 3\n---\n# T\n"
    )


def test_unknown_keys_point_at_metadata_or_the_sidecar() -> None:
    found = findings("---\ninclusion: manual\nowner: me\n---\n# T\n")
    assert "move it under metadata: as a string" in found["fm-unknown-key"][0]
    sidecar = FrontMatterConfig(metadata_placement="sidecar")
    hits = fm_rules.steering_hits({"owner": "me", "metadata": {}}, sidecar)
    assert any(".kiro/steersync.meta.yaml" in h.message for h in hits)
    assert [h.rule for h in fm_rules.metadata_hits({"metadata": {}}, sidecar)] == ["fm-metadata"]


def test_metadata_in_front_matter() -> None:
    text = "---\ninclusion: manual\nmetadata:\n  status: done\n---\n# T\n"
    assert "fm-metadata" in rules(text)
    assert "fm-metadata" in rules("---\ninclusion: manual\nmetadata: [a]\n---\n# T\n")
    clean = "---\ninclusion: manual\nmetadata:\n  owner: platform\n  tags: api\n---\n# T\n"
    assert "fm-metadata" not in rules(clean)


def test_knowledge_note_in_steering() -> None:
    note = "---\ntitle: CVE exposure\ndomain: apigee\nverified_on: 2026-11-20\n---\n# Note\n"
    found = findings(note)
    assert "would load on every turn" in found["fm-knowledge-in-steering"][0]
    assert "fm-knowledge-in-steering" not in rules("---\ninclusion: manual\ntitle: x\n---\n# T\n")


# --- references --------------------------------------------------------------------------------


def test_reference_parsing() -> None:
    text = "#[[file:a.md]] #[[file:b.md:3]] #[[file:c.md:2-4]] #[[folder:docs]] #[[spec:x]]"
    found, problems = refs.parse(text)
    assert [(r.kind, r.path, r.start, r.end) for r in found] == [
        ("file", "a.md", None, None),
        ("file", "b.md", 3, None),
        ("file", "c.md", 2, 4),
        ("folder", "docs", None, None),
    ]
    assert problems == []
    _, bad = refs.parse("#[[file:]]\n#[[file:a.md:5-2]]\n#[[folder:d:1]]\n#[[file:a.md:0]]")
    assert [p.rule for p in bad] == ["ref-syntax"] * 4
    assert [p.line for p in bad] == [1, 2, 3, 4]


def test_reference_resolution(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("one\ntwo\nthree\n")
    text = (
        "# T\n\n#[[file:docs/a.md:2-3]] #[[folder:docs]]\n#[[file:docs/a.md:9]]\n"
        "#[[file:missing.md]] #[[folder:nope]] #[[file:../escape.md]]\n"
    )
    doc = document_from_bytes("x.md", text.encode())
    found = [f for f in lint_document(doc, LINT, tmp_path) if f.rule.startswith("ref-")]
    messages = [f.message for f in found]
    assert len(found) == 4
    assert any("the file has 3 lines" in m for m in messages)
    assert any("no such file" in m for m in messages)
    assert any("no such folder" in m for m in messages)
    assert any("points outside" in m for m in messages)


def test_global_steering_and_agents_md_resolve_from_their_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "svc").mkdir(parents=True)
    (workspace / "svc" / "notes.md").write_text("x\n")
    agents = document_from_bytes("svc/AGENTS.md", b"# Agents\n\n#[[file:notes.md]]\n")
    found = lint_document(agents, LINT, workspace, profile="markdown")
    assert not [f for f in found if f.rule == "ref-unresolved"]
    home = tmp_path / "kiro" / "steering"
    home.mkdir(parents=True)
    (home / "shared.md").write_text("x\n")
    global_doc = document_from_bytes("g.md", b"# G\n\n#[[file:shared.md]]\n")
    resolved = lint_snapshot(
        {"g.md": global_doc.raw}, LINT, workspace, profile="steering", directory=home
    )
    assert "ref-unresolved" not in {f.rule for f in resolved.findings}


# --- steering collection rules ---------------------------------------------------------------


def test_steering_extension_size_and_duplicates(tmp_path: Path) -> None:
    steering = tmp_path / ".kiro" / "steering"
    steering.mkdir(parents=True)
    rule = "- Use the error envelope for every non 2xx response.\n"
    (steering / "a.md").write_text("# A\n\n" + rule)
    (steering / "b.md").write_text("# B\n\n" + rule.upper())
    (steering / "notes.txt").write_text("not loaded")
    (steering / "big.md").write_text("# Big\n\n" + "x" * (LINT.kiro_limits.file_chars_truncate + 1))
    report = lint_paths(
        [steering], LINT, tmp_path, profile="steering", spec=FileSpec(("**/*.md",), (), None)
    )
    by_rule: dict[str, list[str]] = {}
    for f in report.findings:
        by_rule.setdefault(f.rule, []).append(f.path)
    assert by_rule["steering-ext"] == [".kiro/steering/notes.txt"]
    assert sorted(by_rule["steering-dup-rule"]) == [".kiro/steering/a.md", ".kiro/steering/b.md"]
    assert ".kiro/steering/big.md" in by_rule["steering-size"]
    assert ".kiro/steering" in by_rule["steering-size"]  # the always included total


# --- skills and specs --------------------------------------------------------------------------


def test_skill_name_and_size() -> None:
    long_body = "line\n" * (LINT.budgets.skill_lines + 1)
    text = f"---\nname: Release_Notes\ndescription: d\n---\n# R\n{long_body}"
    found = findings(text, "skills/release-notes/SKILL.md", "skill")
    assert "lowercase letters" in found["skill-name"][0]
    assert "same as its directory" in found["skill-name"][0]
    assert "skill-size" in found
    clean = "---\nname: release-notes\ndescription: d\nmetadata:\n  owner: dx\n---\n# R\n"
    assert not {"skill-name", "skill-size", "fm-metadata"} & set(
        findings(clean, "skills/release-notes/SKILL.md", "skill")
    )
    tokens = "word " * (LINT.budgets.skill_tokens * LINT.budgets.chars_per_token)
    sized = fm_rules.skill_size_hits(tokens, LINT.budgets)
    assert any("tokens" in h.message for h in sized)


def test_spec_ears_is_info_and_never_fails() -> None:
    text = (
        "# Requirements\n\n## Acceptance Criteria\n\n"
        "1. WHEN a user signs in THE system SHALL issue a token\n"
        "2. Tokens expire after an hour\n\n## Notes\n\n- free text\n"
    )
    report = lint_snapshot({"tls/requirements.md": text.encode()}, LINT, profile="spec")
    ears = [f for f in report.findings if f.rule == "spec-ears"]
    assert [(f.severity, f.line) for f in ears] == [("info", 6)]
    assert report.ok
    assert len(report.infos) == 1
    assert json.loads(report.to_json())["infos"] == 1


# --- sidecar ---------------------------------------------------------------------------------------


def test_sidecar(tmp_path: Path) -> None:
    lint = LintConfig()
    assert sidecar_report(tmp_path, lint).findings == ()
    (tmp_path / ".kiro" / "agents").mkdir(parents=True)
    (tmp_path / ".kiro" / "agents" / "dev.json").write_text("{}")
    sidecar = tmp_path / ".kiro" / "steersync.meta.yaml"
    sidecar.write_text(
        '".kiro/agents/dev.json": { owner: "platform", tags: "agents" }\n'
        '".kiro/hooks/gone.json": { status: "done" }\n'
        '".kiro/x.json": "not a map"\n'
    )
    messages = [f.message for f in sidecar_report(tmp_path, lint).findings]
    assert "'.kiro/hooks/gone.json' does not exist" in messages
    assert any("metadata.status must be one of" in m for m in messages)
    assert "'.kiro/x.json' must map to a metadata map" in messages
    assert not any("dev.json:" in m for m in messages)
    sidecar.write_text("[a, b]\n")
    assert [f.rule for f in sidecar_report(tmp_path, lint).findings] == ["fm-metadata"]
    sidecar.write_text("{broken: [\n")
    assert [f.rule for f in sidecar_report(tmp_path, lint).findings] == ["fm-yaml"]
