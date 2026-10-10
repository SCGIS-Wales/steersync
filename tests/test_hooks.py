from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from steersync import hooks
from steersync.config import Config


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
    assert v3["hooks"][0]["action"]["command"] == "steersync pull --quiet"
    v2 = json.loads(hooks.render_kiro_hook("v2", command="steersync pull", timeout=5))
    assert v2["hooks"]["agentSpawn"][0] == {"command": "steersync pull", "timeout_ms": 5000}


def test_render_mcp_config() -> None:
    data = json.loads(hooks.render_mcp_config(remote="git@h:a/b.git"))
    server = data["mcpServers"]["steersync"]
    assert server["command"] == "uvx"
    assert server["args"] == ["steersync", "mcp"]
    assert server["env"]["STEERSYNC_REMOTE"] == "git@h:a/b.git"
    assert server["env"]["GIT_TERMINAL_PROMPT"] == "0"
    default = json.loads(hooks.render_mcp_config())
    assert default["mcpServers"]["steersync"]["env"]["STEERSYNC_REMOTE"] == "${STEERSYNC_REMOTE}"
    assert "steersync-push" in hooks.render_pre_commit_config()


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
