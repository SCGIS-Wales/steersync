from __future__ import annotations

from pathlib import Path

import pytest

from steersync.config import (
    ENV_CONFIG,
    Config,
    deep_merge,
    find_config_file,
    load_config,
    remote_host,
    validate_remote_url,
)
from steersync.errors import ConfigError


def test_defaults_load_without_file(env: dict[str, str]) -> None:
    config = load_config(env=env)
    assert config.remote.url == ""
    assert config.remote.default_branch == "main"
    assert config.branch.hash_length == 8
    assert config.lint.forbidden_patterns
    assert config.source_file is None


def test_file_merges_over_defaults(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "remote:\n  url: git@gitlab.example.com:acme/steering.git\nlint:\n  max_file_bytes: 2048\n"
    )
    config = load_config(env=env, explicit=path)
    assert config.remote.url == "git@gitlab.example.com:acme/steering.git"
    assert config.lint.max_file_bytes == 2048
    assert config.lint.require_h1 is True
    assert config.source_file == path


def test_env_overrides_file(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("remote:\n  url: ssh://git@a.example.com/x/y.git\n")
    env = {
        **env,
        "STEERSYNC_REMOTE": "ssh://git@b.example.com/x/y.git",
        "STEERSYNC_LOG_LEVEL": "debug",
        "STEERSYNC_STRICT": "yes",
    }
    config = load_config(env=env, explicit=path)
    assert config.remote.url.startswith("ssh://git@b.example.com")
    assert config.logging.level == "DEBUG"
    assert config.hook.strict is True


def test_config_discovery_order(env: dict[str, str], tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".kiro").mkdir(parents=True)
    ws_file = workspace / ".kiro" / "steersync.yaml"
    ws_file.write_text("remote:\n  url: ssh://git@ws.example.com/a/b.git\n")
    user_dir = Path(env["HOME"]) / ".config" / "steersync"
    user_dir.mkdir(parents=True)
    user_file = user_dir / "config.yaml"
    user_file.write_text("remote:\n  url: ssh://git@user.example.com/a/b.git\n")
    assert find_config_file(workspace, env) == ws_file
    assert find_config_file(None, env) == user_file
    env_file = tmp_path / "env.yaml"
    env_file.write_text("{}")
    assert find_config_file(workspace, {**env, ENV_CONFIG: str(env_file)}) == env_file
    with pytest.raises(ConfigError, match="missing"):
        find_config_file(None, {**env, ENV_CONFIG: str(tmp_path / "nope.yaml")})
    with pytest.raises(ConfigError, match="not found"):
        find_config_file(None, env, explicit=tmp_path / "nope.yaml")


def test_xdg_config_home_is_honoured(env: dict[str, str], tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "steersync").mkdir(parents=True)
    target = xdg / "steersync" / "config.yml"
    target.write_text("{}")
    assert find_config_file(None, {**env, "XDG_CONFIG_HOME": str(xdg)}) == target


@pytest.mark.parametrize(
    "text",
    [
        "remote: [1, 2",
        "- just\n- a list\n",
        "remote:\n  bogus: 1\n",
        "branch:\n  template: no-hash-here\n",
        "branch:\n  template: '{project}/{nope}-{hash}'\n",
        'remote:\n  push_options: ["merge_request.title=a\\nb"]\n',
        "remote:\n  push_options: ['x={unknown}']\n",
        "remote:\n  global_dir: ../escape\n",
        "remote:\n  allowed_hosts: ['bad host']\n",
        "paths:\n  workspace_steering: /abs\n",
        "git:\n  binary: --evil\n",
        "lint:\n  forbidden_patterns:\n    - id: broken\n      pattern: '('\n      message: m\n",
        "logging:\n  level: LOUD\n",
    ],
)
def test_invalid_configuration_is_rejected(env: dict[str, str], tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(env=env, explicit=path)


def test_empty_file_is_fine(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("")
    assert isinstance(load_config(env=env, explicit=path), Config)


def test_deep_merge() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    merged = deep_merge(base, {"a": {"c": 3}, "d": [2], "e": 4})
    assert merged == {"a": {"b": 1, "c": 3}, "d": [2], "e": 4}
    assert base == {"a": {"b": 1, "c": 2}, "d": [1]}


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@gitlab.example.com/acme/steering.git",
        "https://gitlab.example.com/acme/steering.git",
        "git@gitlab.example.com:acme/steering.git",
        "gitlab.example.com:acme/steering.git",
    ],
)
def test_valid_remote_urls(url: str) -> None:
    assert validate_remote_url(url) == url


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("", "empty"),
        ("--upload-pack=evil", "illegal"),
        ("ext::evil", "helper"),
        ("http://gitlab.example.com/acme/steering.git", "insecure"),
        ("git://gitlab.example.com/acme/steering.git", "insecure"),
        ("/srv/git/steering.git", "local remotes are disabled"),
        ("file:///srv/git/steering.git", "local remotes are disabled"),
        ("ssh://git@gitlab.example.com/a b.git", "illegal"),
        ("ssh:///no-host", "no host"),
        ("just-a-word", "unsupported"),
    ],
)
def test_invalid_remote_urls(url: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        validate_remote_url(url)


def test_local_remote_allowed_when_enabled(tmp_path: Path) -> None:
    assert validate_remote_url(str(tmp_path), allow_local=True) == str(tmp_path)
    assert remote_host(str(tmp_path)) is None


def test_allowed_hosts() -> None:
    validate_remote_url("git@gitlab.example.com:a/b.git", allowed_hosts=("gitlab.example.com",))
    with pytest.raises(ConfigError, match="allowed_hosts"):
        validate_remote_url("git@evil.example.com:a/b.git", allowed_hosts=("gitlab.example.com",))
    assert remote_host("https://GitLab.Example.com/a/b.git") == "gitlab.example.com"
    assert remote_host("git@gitlab.example.com:a/b.git") == "gitlab.example.com"


def test_collections_defaults_and_overrides(env: dict[str, str], tmp_path: Path) -> None:
    default = load_config(env=env)
    assert list(default.collections) == [
        "steering",
        "specs",
        "skills",
        "agents",
        "hooks",
        "settings",
    ]
    assert default.collections["steering"].lint == "steering"
    assert default.collections["specs"].global_path is None
    assert default.collections["skills"].max_file_bytes == 1_048_576
    path = tmp_path / "c.yaml"
    path.write_text(
        "collections:\n"
        "  steering:\n    include: ['**/*.md', '**/*.markdown']\n"
        "  specs:\n    enabled: false\n"
        "  runbooks:\n    workspace_path: docs/runbooks\n    lint: markdown\n"
    )
    config = load_config(env=env, explicit=path)
    assert config.collections["steering"].include == ("**/*.md", "**/*.markdown")
    assert config.collections["steering"].global_path == "steering"
    assert "specs" not in config.enabled_collections()
    assert config.collections["runbooks"].central_dir == "runbooks"
    assert list(config.select_collections(["runbooks", "steering"])) == ["runbooks", "steering"]
    assert list(config.select_collections(None)) == list(config.enabled_collections())
    with pytest.raises(ConfigError, match="disabled"):
        config.select_collections(["specs"])
    with pytest.raises(ConfigError, match="unknown"):
        config.select_collections(["missing"])


@pytest.mark.parametrize(
    "text",
    [
        "collections:\n  Bad Name:\n    workspace_path: x\n",
        "collections:\n  nowhere:\n    lint: generic\n",
        "collections:\n  dup:\n    workspace_path: .kiro/steering\n",
        "collections:\n  dup:\n    workspace_path: other\n    central_dir: steering\n",
        "collections:\n  dup:\n    global_path: skills\n",
        "collections:\n  esc:\n    workspace_path: ../outside\n",
        "collections:\n  abs:\n    workspace_path: /abs\n",
        "collections:\n  pat:\n    workspace_path: x\n    include: ['/abs/**']\n",
        "collections:\n  pat:\n    workspace_path: x\n    include: ['../**']\n",
        "collections:\n  pat:\n    workspace_path: x\n    include: []\n",
        "collections:\n  pat:\n    workspace_path: x\n    lint: nope\n",
        "collections:\n  pat:\n    workspace_path: x\n    bogus: 1\n",
        "collections:\n  steering:\n    max_depth: 0\n",
    ],
)
def test_invalid_collections_are_rejected(env: dict[str, str], tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(env=env, explicit=path)


def test_disabled_collections_may_share_paths(env: dict[str, str], tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "collections:\n  steering:\n    enabled: false\n"
        "  rules:\n    workspace_path: .kiro/steering\n    central_dir: steering\n    lint: steering\n"
    )
    config = load_config(env=env, explicit=path)
    assert "steering" not in config.enabled_collections()
    assert config.enabled_collections()["rules"].workspace_path == ".kiro/steering"


def test_legacy_path_keys_map_to_steering_collection(
    env: dict[str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("paths:\n  workspace_steering: kiro/rules\n  global_steering: rules\n")
    with caplog.at_level("WARNING", logger="steersync.config"):
        config = load_config(env=env, explicit=path)
    assert config.collections["steering"].workspace_path == "kiro/rules"
    assert config.collections["steering"].global_path == "rules"
    assert config.collections["specs"].workspace_path == ".kiro/specs"
    assert any("deprecated" in record.message for record in caplog.records)
