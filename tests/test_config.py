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
