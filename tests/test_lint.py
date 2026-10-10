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
    assert (
        lint_text(config, "---\ninclusion: auto\nname: api\ndescription: REST rules\n---\n# T\n")
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
    assert lint_snapshot({"doc.md": text.encode()}, off.lint).findings == ()


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
