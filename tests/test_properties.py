"""Property based tests: path safety, change plans and rule robustness on arbitrary input."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from steersync import kiro_rules, kiro_version
from steersync.config import Config
from steersync.errors import UnsafePathError
from steersync.lint import lint_document
from steersync.paths import ensure_within
from steersync.steering import (
    FileSpec,
    document_from_bytes,
    plan_changes,
    validate_relative_path,
)

SEGMENT = st.sampled_from(["a", "b", "..", ".", "", ".git", ".kiro", "x.md", "~", "C:", "\\"])
RELATIVE = st.lists(SEGMENT, min_size=1, max_size=6).map("/".join)
JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)
KNOWN_KEYS = st.sampled_from(
    [
        "version",
        "hooks",
        "trigger",
        "action",
        "type",
        "command",
        "prompt",
        "timeout",
        "confirm",
        "permissions",
        "rules",
        "capability",
        "effect",
        "resources",
        "tools",
        "mcpServers",
        "env",
        "oauth",
        "steps",
        "branches",
        "maxIterations",
        "id",
        "name",
        "networkAccess",
        "enabled",
    ]
)
SHAPED = st.recursive(
    st.none() | st.booleans() | st.integers(-2, 2000) | st.sampled_from(["", "x", "Stop", "v1"]),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(KNOWN_KEYS, children, max_size=5)
    ),
    max_leaves=25,
)


FIELDS = Config().lint.agent_fields
LEGACY = Config().lint.legacy_agent_keys


@given(RELATIVE)
def test_validated_paths_never_escape(relative: str) -> None:
    try:
        safe = validate_relative_path(relative)
    except UnsafePathError:
        return
    parts = safe.split("/")
    assert ".." not in parts
    assert "" not in parts
    assert not safe.startswith("/")
    assert not any(part.startswith(".git") for part in parts)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(RELATIVE)
def test_ensure_within_contains_or_refuses(tmp_path: Path, relative: str) -> None:
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    try:
        target = ensure_within(base, Path(relative))
    except UnsafePathError:
        return
    resolved = base.resolve()
    assert target == resolved or resolved in target.parents


@given(st.lists(st.sampled_from(["a", "b", "c"]), min_size=0, max_size=4))
def test_literal_patterns_match_at_any_depth(folders: list[str]) -> None:
    spec = FileSpec(("**/AGENTS.md",), ("**/node_modules/**",))
    path = "/".join([*folders, "AGENTS.md"])
    assert spec.matcher()(path)
    assert not spec.matcher()("/".join([*folders, "node_modules", "AGENTS.md"]))
    assert spec.literal_names() == {"AGENTS.md"}


CONTENT = st.dictionaries(
    st.sampled_from(["a.md", "b.md", "c/d.md", "e.json"]), st.binary(max_size=4), max_size=4
)


@given(CONTENT, CONTENT, CONTENT, st.booleans())
def test_change_plans_partition_every_file(
    source: dict[str, bytes], target: dict[str, bytes], lock: dict[str, bytes], prune: bool
) -> None:
    from steersync.steering import file_digest

    lock_hashes = {k: file_digest(v) for k, v in lock.items()}
    plan = plan_changes(source, target, lock_hashes, prune=prune)
    buckets = [
        *plan.add,
        *plan.update,
        *plan.delete,
        *plan.conflict,
        *plan.unchanged,
        *plan.local_only,
    ]
    assert sorted(buckets) == sorted(set(source) | set(target))
    assert set(plan.add) <= set(source) - set(target)
    assert set(plan.delete) <= set(target) - set(source)
    if not prune:
        assert plan.delete == ()
    # A file edited locally is never planned as an update or a delete.
    for name, data in target.items():
        if name in lock_hashes and lock_hashes[name] != file_digest(data):
            assert name not in plan.update
            assert name not in plan.delete


@given(JSON)
def test_rules_never_raise_on_arbitrary_json(data: Any) -> None:
    kiro_rules.hook_file_issues(data)
    kiro_rules.mcp_issues(data)
    kiro_rules.sandbox_issues(data)
    kiro_rules.cli_settings_issues(data)
    kiro_rules.workflow_issues(data, "x.workflow.json")
    if isinstance(data, dict):
        kiro_rules.agent_issues(data, fields=FIELDS, legacy_keys=LEGACY)


@given(SHAPED)
def test_rules_never_raise_on_nearly_valid_shapes(data: Any) -> None:
    wrapped = {"version": "v1", "hooks": [data]} if not isinstance(data, dict) else data
    for issues in (
        kiro_rules.hook_file_issues(wrapped),
        kiro_rules.workflow_issues({"name": "x", "steps": [data]}, "x.workflow.json"),
        kiro_rules.mcp_issues({"mcpServers": {"s": data}}),
        kiro_rules.agent_issues(
            {"permissions": data, "resources": data, "hooks": data},
            fields=FIELDS,
            legacy_keys=LEGACY,
        ),
    ):
        for issue in issues:
            assert issue.severity in {"error", "warning"}
            assert issue.rule
            json.dumps(issue._asdict())


PROFILE_FILES = [
    ("agent", "a.json"),
    ("agent", "a.md"),
    ("hooks", "h.json"),
    ("settings", "mcp.json"),
    ("settings", "lsp.json"),
    ("settings", "sandbox.json"),
    ("settings", "cli.json"),
    ("workflow", "w.workflow.json"),
    ("workflow", "w.workflow.yaml"),
    ("skill", "s/SKILL.md"),
    ("steering", "s.md"),
]


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(SHAPED, st.sampled_from(PROFILE_FILES))
def test_every_profile_survives_arbitrary_documents(
    config: Config, data: Any, profile_file: tuple[str, str]
) -> None:
    profile, name = profile_file
    if name.endswith(".md"):
        text = (
            "---\n"
            + yaml.safe_dump(data if isinstance(data, dict) else {"x": data})
            + "---\nBody\n"
        )
    elif name.endswith(".yaml"):
        text = yaml.safe_dump(data)
    else:
        text = json.dumps(data)
    doc = document_from_bytes(name, text.encode())
    for finding in lint_document(doc, config.lint, profile=profile):  # type: ignore[arg-type]
        assert finding.severity in {"error", "warning"}


@given(st.text(max_size=200))
def test_kiroignore_and_settings_parsers_never_raise(text: str) -> None:
    kiro_rules.kiroignore_issues(text, root_file=True)
    kiro_version.parse_setting(text)
    kiro_version.parse_version(text)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"chat.agentEngine": "v3"}, {"chat.agentEngine": "v3"}),
        ({"chat": {"agentEngine": "v3", "x": {"y": 1}}}, {"chat.agentEngine": "v3", "chat.x.y": 1}),
    ],
)
def test_flatten_settings(data: dict[str, Any], expected: dict[str, Any]) -> None:
    assert dict(kiro_rules.flatten_settings(data)) == expected
