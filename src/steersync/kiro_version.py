"""Check that this machine runs a supported Kiro: the V3 harness at the configured minimum.

steersync supports Kiro CLI V3 (and Kiro IDE 1.x, which reads the same schema) and
nothing older. The minimum versions come from ``kiro.min_cli_version`` and
``kiro.min_ide_version``. Detection never fails a sync: every probe has a short timeout
and an unknown answer is not a problem.

Probes: ``kiro-cli --version``, ``kiro-cli settings -f json chat.agentEngine`` (falling
back to ``$KIRO_HOME/settings/cli.json``), ``chat.enableWorkflows``, and a scan of the
agent directories for keys V3 does not read. None of them start a chat session, so they
never consume Kiro credits.
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from steersync import kiro_rules
from steersync.paths import kiro_home
from steersync.steering import parse_front_matter

CLI_BINARY = "kiro-cli"
SUPPORTED_ENGINE = "v3"
PROBE_TIMEOUT_SECONDS = 3.0
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
CHECK_STAMP = "kiro-check.stamp"
ENV_NO_CHECK = "STEERSYNC_NO_KIRO_CHECK"
DOCS_URL = "https://kiro.dev/docs/cli/"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

EngineSource = Literal["kiro-cli", "settings-file", "default", "unknown"]
Runner = Callable[[Sequence[str]], tuple[int, str] | None]


@dataclass(frozen=True, slots=True)
class KiroStatus:
    """What steersync learned about the local Kiro installation."""

    cli_path: str | None
    cli_version: str | None
    engine: str | None
    engine_source: EngineSource
    min_cli_version: str
    min_ide_version: str
    # chat.enableWorkflows: workflow recipes (.kiro/workflows) load only when it is on.
    workflows_enabled: bool | None = None
    legacy_agents: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "legacy_agents": list(self.legacy_agents),
            "problems": list(self.problems),
            "supported": self.supported,
            "message": None if self.supported else problem_text(self),
        }


def default_runner(args: Sequence[str]) -> tuple[int, str] | None:
    """Run a probe command and return its exit code and output, or ``None`` on failure."""
    try:
        completed = subprocess.run(  # nosec B603
            list(args),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return completed.returncode, (completed.stdout or "").strip()


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Return the first ``X.Y.Z`` in ``text`` (``kiro-cli 3.5.0`` gives ``(3, 5, 0)``)."""
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def parse_setting(output: str) -> str | bool | None:
    """Parse ``kiro-cli settings`` output in JSON (``"v3"``) or plain (``v3 (global)``) form."""
    text = output.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        token = text.split()[0].strip("\"'")
        if token.lower() in {"true", "false"}:
            return token.lower() == "true"
        return token or None
    return value if isinstance(value, str | bool) else None


def _settings_file(env: Mapping[str, str]) -> Mapping[str, Any]:
    path = kiro_home(env) / "settings" / "cli.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError, ValueError:
        return {}
    return dict(kiro_rules.flatten_settings(data)) if isinstance(data, Mapping) else {}


def _setting(
    binary: str | None, key: str, run: Runner, file_settings: Mapping[str, Any]
) -> tuple[str | bool | None, EngineSource]:
    if binary is not None:
        result = run([binary, "settings", "-f", "json", key])
        if result is not None and result[0] == 0:
            value = parse_setting(result[1])
            if value is not None:
                return value, "kiro-cli"
    if key in file_settings:
        value = file_settings[key]
        if isinstance(value, str | bool):
            return value, "settings-file"
    return None, "unknown"


def find_legacy_agents(
    directories: Iterable[Path],
    legacy_keys: Mapping[str, str],
    upgraded_keys: Sequence[str] = (),
) -> tuple[str, ...]:
    """Agent files that still carry keys the V3 harness does not read."""
    found: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            data = _agent_data(path)
            if data is not None and any(
                key in data and not ("permissions" in data and key in upgraded_keys)
                for key in legacy_keys
            ):
                found.append(str(path))
    return tuple(found)


def _agent_data(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file() or path.name.startswith("."):
        return None
    try:
        text = path.read_text("utf-8")
    except OSError, UnicodeDecodeError:
        return None
    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, Mapping) else None
    if path.suffix == ".md":
        front = parse_front_matter(text)
        return front.metadata if front.present and not front.error else None
    return None


def detect(
    env: Mapping[str, str] | None = None,
    *,
    min_cli_version: str,
    min_ide_version: str,
    legacy_keys: Mapping[str, str],
    upgraded_keys: Sequence[str] = (),
    agent_dirs: Iterable[Path] = (),
    run: Runner = default_runner,
    which: Callable[[str], str | None] = shutil.which,
) -> KiroStatus:
    """Probe the local Kiro CLI and agent directories; never raises."""
    environment = os.environ if env is None else env
    binary = which(CLI_BINARY)
    version: str | None = None
    if binary is not None:
        result = run([binary, "--version"])
        parsed = parse_version(result[1]) if result is not None and result[0] == 0 else None
        version = ".".join(str(p) for p in parsed) if parsed else None
    file_settings = _settings_file(environment)
    engine_value, source = _setting(binary, "chat.agentEngine", run, file_settings)
    engine = engine_value.lower() if isinstance(engine_value, str) else None
    if engine is None and version is not None:
        source = "default"
    workflows_value, _ = _setting(binary, "chat.enableWorkflows", run, file_settings)
    dirs = [*agent_dirs, kiro_home(environment) / "agents"]
    status = KiroStatus(
        cli_path=binary,
        cli_version=version,
        engine=engine,
        engine_source=source,
        min_cli_version=min_cli_version,
        min_ide_version=min_ide_version,
        workflows_enabled=workflows_value if isinstance(workflows_value, bool) else None,
        legacy_agents=find_legacy_agents(dict.fromkeys(dirs), legacy_keys, upgraded_keys),
    )
    return replace(status, problems=tuple(problems(status)))


def problems(status: KiroStatus) -> list[str]:
    """Why this machine or its agents are below the supported V3 baseline."""
    found: list[str] = []
    if status.cli_path is not None:
        installed = parse_version(status.cli_version or "")
        minimum = parse_version(status.min_cli_version)
        if installed is not None and minimum is not None and installed < minimum:
            found.append(
                f"kiro-cli {status.cli_version} is installed; steersync needs "
                f"{status.min_cli_version} or later"
            )
        if status.engine is not None and status.engine != SUPPORTED_ENGINE:
            found.append(
                f"chat.agentEngine is {status.engine!r}; steersync supports only the V3 harness"
            )
    if status.legacy_agents:
        found.append(
            f"{len(status.legacy_agents)} agent file(s) use keys V3 does not read: "
            + ", ".join(status.legacy_agents)
        )
    return found


def problem_text(status: KiroStatus) -> str:
    """What is below the baseline and how to fix it, for doctor, the hook and MCP."""
    lines = ["Kiro is below the version steersync supports:"]
    lines += [f"- {problem}" for problem in status.problems]
    installed = parse_version(status.cli_version or "")
    minimum = parse_version(status.min_cli_version)
    if installed is not None and minimum is not None and installed < minimum:
        lines.append(f"Upgrade kiro-cli to {status.min_cli_version} or later ({DOCS_URL}).")
    if status.engine is not None and status.engine != SUPPORTED_ENGINE:
        lines.append("Run `kiro-cli settings chat.agentEngine v3`.")
    if status.legacy_agents:
        lines.append(
            "Open each agent in Kiro and run /upgrade-agent, then commit the result "
            "(steersync lint names each key and its V3 replacement)."
        )
    lines.append(
        f"Supported: Kiro CLI {status.min_cli_version} or later with the V3 harness, "
        f"Kiro IDE {status.min_ide_version} or later."
    )
    return "\n".join(lines)


def check_due(cache_dir: Path, env: Mapping[str, str], *, now: float | None = None) -> bool:
    """Return True at most once per day; records the time when it does."""
    if env.get(ENV_NO_CHECK, "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    stamp = cache_dir / CHECK_STAMP
    current = time.time() if now is None else now
    try:
        last = float(stamp.read_text("utf-8").strip())
    except OSError, ValueError:
        last = 0.0
    if current - last < CHECK_INTERVAL_SECONDS:
        return False
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{current:.0f}\n", encoding="utf-8")
    except OSError:
        return False
    return True
