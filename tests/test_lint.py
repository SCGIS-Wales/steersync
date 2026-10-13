from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from steersync.config import Config, LintConfig, load_config
from steersync.lint import LintFinding, lint_document, lint_paths, lint_snapshot
from steersync.steering import document_from_bytes


def rules(report_findings: Iterable[LintFinding]) -> set[str]:
    return {f.rule for f in report_findings}


def lint_text(config: Config, text: str, workspace: Path | None = None) -> set[str]:
    return rules(
        lint_document(document_from_bytes("doc.md", text.encode("utf-8")), config.lint, workspace)
    )


def test_clean_document_has_no_findings(config: Config) -> None:
    assert lint_text(config, "---\ninclusion: always\n---\n# Title\n\nText.\n") == set()
    assert lint_text(config, "# Title\n\nText.\n") == set()


def test_front_matter_rules(config: Config) -> None:
    assert "front-matter-position" in lint_text(config, "\n---\ninclusion: manual\n---\n# T\n")
    assert "front-matter-yaml" in lint_text(config, "---\ninclusion: [x\n---\n# T\n")
    assert "inclusion-value" in lint_text(config, "---\ninclusion: sometimes\n---\n# T\n")
    assert "filematch-pattern-required" in lint_text(
        config, "---\ninclusion: fileMatch\n---\n# T\n"
    )
    assert "filematch-pattern-type" in lint_text(
        config, "---\ninclusion: fileMatch\nfileMatchPattern: 3\n---\n# T\n"
    )
    assert "filematch-pattern-type" in lint_text(
        config, "---\ninclusion: fileMatch\nfileMatchPattern: ['a', 2]\n---\n# T\n"
    )
    assert "filematch-pattern-unused" in lint_text(
        config, "---\ninclusion: manual\nfileMatchPattern: '*'\n---\n# T\n"
    )
    found = lint_text(config, "---\ninclusion: auto\nname: ''\n---\n# T\n")
    assert "auto-requires-metadata" in found
    assert "steering-auto-description" in lint_text(
        config, "---\ninclusion: auto\nname: api\ndescription: REST rules\n---\n# T\n"
    )
    assert (
        lint_text(
            config,
            "---\ninclusion: auto\nname: api\ndescription: Use when adding or reviewing REST "
            "endpoints\n---\n# T\n",
        )
        == set()
    )
    assert "unknown-key" in lint_text(config, "---\ninclusion: manual\nowner: me\n---\n# T\n")
    assert "unknown-key" not in lint_text(
        config, "---\ninclusion: manual\nx-steersync: {source: a}\n---\n# T\n"
    )


def test_body_rules(config: Config, tmp_path: Path) -> None:
    assert "require-h1" in lint_text(config, "no heading here\n")
    assert "forbidden-aws-access-key" in lint_text(config, "# T\n\nkey AKIAIOSFODNN7EXAMPLE\n")
    assert "forbidden-private-key" in lint_text(config, "# T\n\n-----BEGIN RSA PRIVATE KEY-----\n")
    assert "forbidden-github-token" in lint_text(config, "# T\n\nghp_" + "a" * 36 + "\n")
    assert "forbidden-generic-secret" in lint_text(
        config, "# T\n\npassword = 'abcdefghijklmnopqrstuv'\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    found = lint_text(
        config,
        "# T\n\n#[[file:src/app.py]] #[[file:src/app.py:3-4]] #[[file:missing.py]] #[[folder:../x]]\n",
        tmp_path,
    )
    assert "file-reference-missing" in found
    assert "file-reference-escape" in found
    assert lint_text(config, "# T\n\n#[[file:missing.py]]\n", None) == set()


def test_binary_size_and_nul(config: Config) -> None:
    assert rules(lint_document(document_from_bytes("b.md", b"\xff\xfe"), config.lint)) == {
        "encoding-utf8"
    }
    assert rules(lint_document(document_from_bytes("n.md", b"# T\0\n"), config.lint)) == {
        "encoding-utf8"
    }
    big = b"# T\n\n" + b"x" * (config.lint.max_file_bytes + 1)
    assert "max-size" in rules(lint_document(document_from_bytes("big.md", big), config.lint))


def test_markdown_rules_and_severity(env: dict[str, str], tmp_path: Path) -> None:
    text = "---\ninclusion: always\n---\n# T\nno blank line after heading\n"
    default = load_config(env=env)
    report = lint_snapshot({"doc.md": text.encode()}, default.lint)
    assert any(
        f.rule.startswith("markdown-MD") and f.severity == "warning" for f in report.findings
    )
    assert report.ok
    strict_file = tmp_path / "c.yaml"
    strict_file.write_text("lint:\n  pymarkdown:\n    severity: error\n")
    strict = load_config(env=env, explicit=strict_file)
    assert not lint_snapshot({"doc.md": text.encode()}, strict.lint).ok
    off_file = tmp_path / "off.yaml"
    off_file.write_text("lint:\n  pymarkdown:\n    enabled: false\n")
    off = load_config(env=env, explicit=off_file)
    quiet = lint_snapshot({"doc.md": text.encode()}, off.lint)
    assert {f.rule for f in quiet.findings} == {"steering-foundation-missing"}


def test_markdown_scan_failure_is_reported(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    from pymarkdown.api import PyMarkdownApi, PyMarkdownApiException

    def boom(self: PyMarkdownApi, text: str) -> None:
        raise PyMarkdownApiException("kaput")

    monkeypatch.setattr(PyMarkdownApi, "scan_string", boom)
    assert "markdown-scan" in lint_text(config, "# T\n")


def test_lint_paths_and_report(config: Config, tmp_path: Path) -> None:
    steering = tmp_path / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "ok.md").write_text("# OK\n\nfine\n")
    (steering / "bad.md").write_text("---\ninclusion: nope\n---\n# Bad\n")
    report = lint_paths([steering, steering / "ok.md", tmp_path / "missing"], config.lint, tmp_path)
    assert report.checked == 3
    assert len(report.errors) == 1
    assert report.errors[0].path == ".kiro/steering/bad.md"
    assert report.errors[0].render().startswith(".kiro/steering/bad.md:2: error: [inclusion-value]")
    assert '"checked": 3' in report.to_json()
    assert not report.ok


def test_lint_config_defaults() -> None:
    assert LintConfig().max_file_bytes == 65536


def profile_rules(config: Config, name: str, profile: str, text: str | bytes) -> set[str]:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return rules(lint_document(document_from_bytes(name, raw), config.lint, profile=profile))  # type: ignore[arg-type]


def test_markdown_profile(config: Config) -> None:
    assert profile_rules(config, "specs/design.md", "markdown", "# Design\n\nText.\n") == set()
    assert profile_rules(config, "specs/design.md", "markdown", "no heading\n") == set()
    assert "forbidden-aws-access-key" in profile_rules(
        config, "specs/x.md", "markdown", "# T\n\nAKIAIOSFODNN7EXAMPLE\n"
    )
    assert "encoding-utf8" in profile_rules(config, "specs/x.md", "markdown", b"\xff\xfe")
    assert profile_rules(config, "specs/diagram.png", "markdown", b"\x89PNG\xff") == set()
    assert "inclusion-value" not in profile_rules(
        config, "specs/x.md", "markdown", "---\ninclusion: nope\n---\n# T\n"
    )


def test_skill_profile(config: Config) -> None:
    good = "---\nname: venafi\ndescription: Venafi runbook\n---\n# Venafi\n\nSteps.\n"
    assert profile_rules(config, "venafi/SKILL.md", "skill", good) == set()
    assert "skill-name-mismatch" in profile_rules(config, "other/SKILL.md", "skill", good)
    assert "skill-front-matter-required" in profile_rules(
        config, "venafi/SKILL.md", "skill", "# No front matter\n"
    )
    assert "skill-front-matter-required" in profile_rules(
        config, "venafi/SKILL.md", "skill", "\n---\nname: venafi\ndescription: d\n---\n# T\n"
    )
    assert "front-matter-yaml" in profile_rules(
        config, "venafi/SKILL.md", "skill", "---\nname: [\n---\n# T\n"
    )
    found = profile_rules(config, "venafi/SKILL.md", "skill", "---\nname: venafi\n---\n# T\n")
    assert "skill-requires-metadata" in found
    assert "encoding-utf8" in profile_rules(config, "venafi/SKILL.md", "skill", b"\xff")
    assert profile_rules(config, "venafi/scripts/run.sh", "skill", "#!/bin/sh\necho hi\n") == set()
    assert "forbidden-github-token" in profile_rules(
        config, "venafi/scripts/run.sh", "skill", "TOKEN=ghp_" + "a" * 36 + "\n"
    )
    assert profile_rules(config, "venafi/assets/logo.png", "skill", b"\x89PNG\x00\xff") == set()
    assert profile_rules(config, "venafi/references/r.md", "skill", "# Ref\n\nBody.\n") == set()


def test_json_and_hooks_profiles(config: Config) -> None:
    assert profile_rules(config, "lsp.json", "json", '{"languages": {}}\n') == set()
    assert "json-syntax" in profile_rules(config, "lsp.json", "json", "{nope")
    assert "encoding-utf8" in profile_rules(config, "lsp.json", "json", b"\xff")
    assert profile_rules(config, "notes.txt", "json", "{nope") == set()
    assert "forbidden-github-token" in profile_rules(
        config, "mcp.json", "json", '{"env": {"GITHUB_TOKEN": "ghp_' + "a" * 36 + '"}}'
    )
    hook = '{"version": "v1", "hooks": [{"name": "t", "trigger": "Stop", "action": {"type": "command", "command": "x"}}]}'
    assert profile_rules(config, "h.json", "hooks", hook) == set()
    assert "json-syntax" in profile_rules(config, "h.json", "hooks", "{")
    assert "hook-schema" in profile_rules(config, "h.json", "hooks", '{"hooks": {}}')
    assert "hook-schema" in profile_rules(config, "h.json", "hooks", "[]")
    assert "hook-schema" in profile_rules(
        config, "h.json", "hooks", '{"version": "v1", "hooks": [1]}'
    )
    found = profile_rules(
        config,
        "h.json",
        "hooks",
        '{"version": "v2", "hooks": [{"name": "t", "trigger": "onStop", "action": {"type": "shell"}}]}',
    )
    assert {"hook-schema", "hook-trigger", "hook-action"} <= found
    assert profile_rules(config, "README.md", "hooks", "# Hooks\n") == set()


def test_agent_and_generic_profiles(config: Config) -> None:
    assert profile_rules(config, "entra.json", "agent", '{"name": "entra"}') == set()
    assert "agent-name" in profile_rules(config, "entra.json", "agent", '{"description": "x"}')
    assert "agent-schema" in profile_rules(config, "entra.json", "agent", "[]")
    assert "agent-deprecated-key" in profile_rules(
        config, "entra.json", "agent", '{"name": "e", "toolsSettings": {}}'
    )
    assert "json-syntax" in profile_rules(config, "entra.json", "agent", "{")
    assert "encoding-utf8" in profile_rules(config, "entra.json", "agent", b"\xff")
    assert profile_rules(config, "prompts/entra.md", "agent", "# Prompt\n\nText.\n") == set()
    assert profile_rules(config, "notes.txt", "agent", "anything") == set()
    assert profile_rules(config, "blob.bin", "generic", b"\x00\xff") == set()
    assert "forbidden-private-key" in profile_rules(
        config, "key.txt", "generic", "-----BEGIN RSA PRIVATE KEY-----\n"
    )


def test_size_limit_override_and_report_helpers(config: Config) -> None:
    from steersync.lint import LintReport, merge_reports, prefix_report

    big = b"x" * 2048
    assert "max-size" in rules(
        lint_document(
            document_from_bytes("a.bin", big), config.lint, profile="generic", max_file_bytes=1024
        )
    )
    assert "max-size" not in rules(
        lint_document(document_from_bytes("a.bin", big), config.lint, profile="generic")
    )
    report = lint_snapshot({"x.md": b"no heading\n"}, config.lint, profile="steering")
    prefixed = prefix_report(report, ".kiro/steering/")
    assert prefixed.findings[0].path == ".kiro/steering/x.md"
    assert prefix_report(report, "") is report
    merged = merge_reports([report, prefixed, LintReport((), 2)])
    assert merged.checked == 4
    assert len(merged.findings) == 2 * len(report.findings)
