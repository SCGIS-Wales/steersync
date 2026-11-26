"""The knowledge collection: off by default, note schema, kn-* rules, agents, init, pull."""

import json
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from steersync import kiro_rules, kn_rules, sync
from steersync.cli import main
from steersync.config import Config, KnowledgeLintConfig, load_config
from steersync.lint import lint_document, lint_snapshot
from steersync.schema.knowledge import KnowledgeNote
from steersync.steering import document_from_bytes
from tests.conftest import CentralCommit, central_project

LINT = Config.model_validate(
    __import__("yaml").safe_load(
        __import__("steersync.config", fromlist=["x"]).default_config_text()
    )
).lint
NOTE = """---
title: "Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)"
domain: apigee
type: exposure-record
status: partial
verified_on: 2026-11-20
review_by: 2026-12-05
aliases: [CVE-2026-114938, Java4Shell]
tags: [apigee-hybrid, cve]
related: [udeploy-pin.md]
---

# Apigee Hybrid exposure to CVE-2026-114938 (Java4Shell)

Summary: 1.17.0 to 1.17.3 bundle an affected JDK; 1.17.4 fixes it. Java4Shell does not
affect 1.16.

## Questions this note answers

- Which Hybrid versions are exposed?
"""


def kn(text: str, path: str = "apigee/java4shell.md") -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    doc = document_from_bytes(path, text.encode())
    for f in lint_document(doc, LINT, profile="knowledge"):
        found.setdefault(f.rule, []).append(f.message)
    return found


def test_collection_is_off_by_default_and_configurable() -> None:
    knowledge = Config.model_validate(
        __import__("yaml").safe_load(
            __import__("steersync.config", fromlist=["x"]).default_config_text()
        )
    ).collections["knowledge"]
    assert knowledge.enabled is False
    assert knowledge.workspace_path == ".kiro/knowledge"
    assert knowledge.lint == "knowledge"


def test_note_model() -> None:
    KnowledgeNote.model_validate(
        {"title": "t", "domain": "d", "type": "x", "status": "s", "verified_on": "2026-11-20"}
    )
    with pytest.raises(ValidationError):
        KnowledgeNote.model_validate({"title": "t"})
    with pytest.raises(ValidationError):
        KnowledgeNote.model_validate(
            {
                "title": "t",
                "domain": "d",
                "type": "x",
                "status": "s",
                "verified_on": "2026-11-20",
                "inclusion": "always",
            }
        )


def test_a_good_note_is_clean_today() -> None:
    hits = kn_rules.note_hits(
        {
            "title": "t",
            "domain": "d",
            "type": "x",
            "status": "s",
            "verified_on": "2026-11-20",
        },
        "---\n---\n",
        "# T\n\nSummary.\n",
        KnowledgeLintConfig(),
        today=date(2026, 11, 26),
    )
    assert hits == []


def test_note_rules() -> None:
    assert not {"kn-schema", "kn-inclusion", "kn-summary"} & set(kn(NOTE))
    missing = kn("---\ntitle: x\n---\n# X\n\nSummary.\n")
    assert any("domain" in m for m in missing["kn-schema"])
    steering = kn(NOTE.replace("domain: apigee", "domain: apigee\ninclusion: always"))
    assert "kn-inclusion" in steering
    assert "kn-summary" in kn(NOTE.replace("# Apigee Hybrid", "Apigee Hybrid"))
    big = NOTE.replace("tags: [apigee-hybrid, cve]", "tags: [" + ", ".join(["t"] * 300) + "]")
    assert "kn-frontmatter-size" in kn(big)
    long_section = NOTE + "\n## Detail\n\n" + "word " * 300 + "\n"
    assert any("'Detail'" in m for m in kn(long_section)["kn-section-size"])
    absent = kn(NOTE.replace("aliases: [CVE-2026-114938, Java4Shell]", "aliases: [GCP-2026-082]"))
    assert any("GCP-2026-082" in m for m in absent["kn-identifiers-in-body"])
    assert "kn-schema" in kn("# No front matter\n")


def test_stale_notes() -> None:
    config = KnowledgeLintConfig(max_age_days=30)
    hits = kn_rules.note_hits(
        {"review_by": "2026-01-01", "verified_on": "2026-01-01"},
        "",
        "# T\n\nS.\n",
        config,
        today=date(2026, 11, 26),
    )
    stale = [h.message for h in hits if h.rule == "kn-stale"]
    assert len(stale) == 2


def test_collection_duplicates_and_related() -> None:
    other = NOTE.replace("related: [udeploy-pin.md]", "related: [missing.md]").replace(
        "aliases: [CVE-2026-114938, Java4Shell]", "aliases: [Java4Shell]"
    )
    report = lint_snapshot(
        {
            "apigee/java4shell.md": NOTE.encode(),
            "apigee/udeploy-pin.md": other.encode(),
        },
        LINT,
        profile="knowledge",
    )
    rules = [(f.path, f.rule) for f in report.findings]
    assert ("apigee/java4shell.md", "kn-duplicate") in rules
    assert ("apigee/udeploy-pin.md", "kn-related-unresolved") in rules
    assert ("apigee/java4shell.md", "kn-related-unresolved") not in rules


def test_secret_scanner_runs_over_notes() -> None:
    token = "glpat-" + "x" * 24
    assert "forbidden-gitlab-token" in kn(NOTE + f"\nToken {token}\n")


# --- agents ------------------------------------------------------------------------------------


def agent_rules(data: dict[str, object], *, markdown: bool = False) -> list[kiro_rules.Issue]:
    return kiro_rules.agent_issues(
        data, fields=LINT.agent_fields, legacy_keys=LINT.legacy_agent_keys, markdown=markdown
    )


def test_knowledge_base_agent_rules(tmp_path: Path) -> None:
    base = {
        "type": "knowledgeBase",
        "source": "file://./.kiro/knowledge",
        "name": "team",
        "indexType": "best",
        "autoUpdate": True,
    }
    good: dict[str, object] = {"name": "a", "tools": ["knowledge"], "resources": [base]}
    assert agent_rules(good) == []
    messages = " | ".join(
        i.message
        for i in agent_rules(
            {
                "name": "a",
                "tools": ["read"],
                "excludedTools": ["knowledge"],
                "resources": [
                    base,
                    {**base, "source": "https://x", "autoUpdate": "yes"},
                    {"type": "knowledgeBase", "source": "file://k"},
                ],
            }
        )
    )
    for fragment in (
        "source must be a file:// folder",
        "autoUpdate is a boolean",
        "knowledgeBase needs name",
        "name 'team' is used twice",
        "needs the knowledge tool",
        "excludedTools",
    ):
        assert fragment in messages
    assert any("9790" in i.message for i in agent_rules(good, markdown=True))
    doc = document_from_bytes("a.json", json.dumps(good).encode())
    missing = [
        f.message
        for f in lint_document(doc, LINT, tmp_path, profile="agent")
        if f.rule == "agent-knowledge-base"
    ]
    assert missing == [
        "file://./.kiro/knowledge is not a folder in this workspace; pull or create it"
    ]


# --- init and pull -----------------------------------------------------------------------------


def enable_knowledge(config_file: Path, folder: str = ".kiro/notes-kb") -> None:
    with config_file.open("a") as handle:
        handle.write(
            f"collections:\n  knowledge:\n    enabled: true\n    workspace_path: {folder}\n"
        )


def test_init_offers_the_templates_when_enabled(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    plan = CliRunner().invoke(main, ["init", "--plan", "--json"])
    assert "knowledge.routing" not in plan.output
    enable_knowledge(config_file)
    plan = CliRunner().invoke(main, ["init", "--plan", "--json"])
    steps = {s["id"]: s for s in json.loads(plan.stdout)["steps"]}
    assert steps["knowledge.routing"]["default"] is False
    applied = CliRunner().invoke(main, ["init", "--yes", "--only", "knowledge"])
    assert applied.exit_code == 0, applied.output
    routing = (project / ".kiro" / "steering" / "knowledge-routing.md").read_text()
    assert "indexed from `.kiro/notes-kb/`" in routing
    agent = json.loads((project / ".kiro" / "agents" / "platform-knowledge.json").read_text())
    assert agent["resources"][2]["source"] == "file://./.kiro/notes-kb"


def test_pull_reminds_to_update_the_index(
    project: Path,
    config_file: Path,
    env: dict[str, str],
    central_commit: CentralCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steersync.cli import knowledge_reminder

    enable_knowledge(config_file, ".kiro/knowledge")
    config = load_config(env=env, explicit=config_file)

    def add_note(work: Path) -> None:
        folder = central_project(work, "knowledge") / "apigee"
        folder.mkdir(parents=True)
        (folder / "note.md").write_text(NOTE)

    central_commit(add_note, "knowledge")
    result = sync.pull(config, workspace_root=project, env=env, include_global=False)
    assert (project / ".kiro" / "knowledge" / "apigee" / "note.md").exists()
    assert knowledge_reminder(result) == (
        "knowledge/ changed. In Kiro run /knowledge update and wait for it to finish."
    )
    again = sync.pull(config, workspace_root=project, env=env, include_global=False)
    assert knowledge_reminder(again) is None
    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
