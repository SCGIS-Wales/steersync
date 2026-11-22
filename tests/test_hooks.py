import json
import os
import stat
from pathlib import Path

import pytest

from steersync import __version__, hooks
from steersync.config import Config
from tests.conftest import posix_only


@posix_only
def test_install_hook_fresh(config: Config, project: Path, env: dict[str, str]) -> None:
    result = hooks.install_pre_push_hook(config, project, env=env)
    assert result.action == "installed"
    assert result.chained is None
    assert result.path == project / ".git" / "hooks" / "pre-push"
    assert result.path.stat().st_mode & stat.S_IXUSR
    assert hooks.HOOK_MARKER in result.path.read_text()
    again = hooks.install_pre_push_hook(config, project, env=env)
    assert again.action == "unchanged"


def test_install_hook_chains_existing(config: Config, project: Path, env: dict[str, str]) -> None:
    hooks_dir = project / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    existing = hooks_dir / "pre-push"
    existing.write_text("#!/bin/sh\necho mine\n")
    result = hooks.install_pre_push_hook(config, project, env=env)
    assert result.action == "installed"
    assert result.chained == hooks_dir / "pre-push.local"
    assert result.chained.read_text() == "#!/bin/sh\necho mine\n"
    existing.write_text("#!/bin/sh\necho other\n")
    with pytest.raises(FileExistsError, match=r"pre-push\.local"):
        hooks.install_pre_push_hook(config, project, env=env)
    forced = hooks.install_pre_push_hook(config, project, force=True, env=env)
    assert forced.chained is not None
    assert forced.chained.read_text() == "#!/bin/sh\necho other\n"


def test_install_hook_updates_older_version(
    config: Config, project: Path, env: dict[str, str]
) -> None:
    hooks_dir = project / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "pre-push").write_text(f"#!/bin/sh\n{hooks.HOOK_MARKER} old\n")
    with pytest.raises(FileExistsError, match="--force"):
        hooks.install_pre_push_hook(config, project, env=env)
    assert hooks.install_pre_push_hook(config, project, force=True, env=env).action == "updated"


def test_hook_script_is_posix_sh() -> None:
    script = hooks.render_pre_push_hook()
    assert script.startswith("#!/bin/sh\n")
    assert "steersync push --from-hook" in script
    assert "STEERSYNC_ACTIVE" in script
    assert "core.hooksPath" not in script


def test_render_kiro_hooks() -> None:
    v3 = json.loads(hooks.render_kiro_hook("v3"))
    assert v3["version"] == "v1"
    assert v3["hooks"][0]["trigger"] == "SessionStart"
    command = v3["hooks"][0]["action"]["command"]
    assert command == hooks.SESSION_PULL_COMMAND
    assert f'--from "steersync=={__version__}"' in command
    assert '--python ">=3.14"' in command
    assert command.endswith("steersync pull --quiet --best-effort")
    v2 = json.loads(hooks.render_kiro_hook("v2", command="steersync pull", timeout=5))
    assert v2["hooks"]["agentSpawn"][0] == {"command": "steersync pull", "timeout_ms": 5000}


def test_render_mcp_config() -> None:
    data = json.loads(hooks.render_mcp_config(remote="git@h:a/b.git"))
    server = data["mcpServers"]["steersync"]
    assert server["command"] == "uvx"
    assert server["args"] == [
        "--python",
        ">=3.14",
        "--from",
        f"steersync[mcp]=={__version__}",
        "steersync",
        "mcp",
    ]
    assert "push_kiro_config" not in server["autoApprove"]
    assert "validate_kiro_config" in server["autoApprove"]
    assert hooks.mcp_server_entry(command="steersync")["args"] == ["mcp"]
    assert hooks.mcp_server_entry(command="/opt/bin/steersync-mcp")["args"] == []
    assert hooks.mcp_server_entry(command="pipx")["args"] == ["steersync", "mcp"]
    assert server["env"]["STEERSYNC_REMOTE"] == "git@h:a/b.git"
    assert server["env"]["GIT_TERMINAL_PROMPT"] == "0"
    default = json.loads(hooks.render_mcp_config())
    assert default["mcpServers"]["steersync"]["env"]["STEERSYNC_REMOTE"] == "${STEERSYNC_REMOTE}"
    assert "steersync-push" in hooks.render_pre_commit_config()


@posix_only
def test_hook_runs_under_sh(
    config: Config, project: Path, env: dict[str, str], tmp_path: Path
) -> None:
    """The generated script must run the chained hook and tolerate a missing steersync binary."""
    import subprocess

    result = hooks.install_pre_push_hook(config, project, env=env)
    chained = result.path.parent / "pre-push.local"
    marker = tmp_path / "chained-ran"
    chained.write_text(f"#!/bin/sh\ncat > {marker}\n")
    chained.chmod(chained.stat().st_mode | stat.S_IXUSR)
    completed = subprocess.run(
        [str(result.path), "origin", "ssh://git@h/a/b.git"],
        input="refs/heads/main 1 refs/heads/main 2\n",
        capture_output=True,
        text=True,
        env={**env, "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        check=False,
    )
    assert completed.returncode == 0
    assert marker.read_text() == "refs/heads/main 1 refs/heads/main 2\n"
    assert "not on PATH" in completed.stderr
    assert not any(p.name.startswith("steersync-prepush") for p in tmp_path.iterdir())
    assert os.access(result.path, os.X_OK)


def test_session_start_hook_command_parses(
    project: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command written to .kiro/hooks/steersync.json must be accepted by the CLI."""
    import shlex

    from click.testing import CliRunner

    from steersync.cli import main

    monkeypatch.chdir(project)
    monkeypatch.setenv("STEERSYNC_CONFIG", str(config_file))
    command = json.loads(hooks.render_kiro_hook("v3"))["hooks"][0]["action"]["command"]
    words = shlex.split(command)
    assert words[0] == "uvx"
    args = words[words.index("steersync") + 1 :]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
