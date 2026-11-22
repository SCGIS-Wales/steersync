"""Detect whether this machine runs the Kiro CLI V3 agent harness and remind when not.

Kiro CLI 3.0.0 made V3 (the unified harness Kiro IDE 1.x and Kiro Web already run)
the default for interactive sessions; V2 still runs but receives no new features.
Detection never fails a sync: every probe has a short timeout and an unknown answer
simply means no reminder.

Probes, in order: ``kiro-cli --version``, ``kiro-cli settings -f json chat.agentEngine``
(falling back to ``$KIRO_HOME/settings/cli.json``), ``chat.enableAutoAgentUpgrade``,
and a scan of agent directories for configurations V3 does not load. None of the
probes start a chat session, so they never consume Kiro credits.
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from steersync import kiro_rules
from steersync.paths import kiro_home
from steersync.steering import parse_front_matter

CLI_BINARY = "kiro-cli"
V3_MIN_VERSION: tuple[int, int, int] = (3, 0, 0)
PROBE_TIMEOUT_SECONDS = 3.0
REMINDER_INTERVAL_SECONDS = 24 * 60 * 60
REMINDER_STAMP = "v3-reminder.stamp"
ENV_NO_REMINDER = "STEERSYNC_NO_V3_REMINDER"
DOCS_URL = "https://kiro.dev/docs/cli/v3/"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_V2_RULES = frozenset({"agent-v2-format", "hook-trigger-alias"})

EngineSource = Literal["kiro-cli", "settings-file", "default", "unknown"]
Runner = Callable[[Sequence[str]], tuple[int, str] | None]


@dataclass(frozen=True, slots=True)
class HarnessStatus:
    """What steersync learned about the local Kiro CLI harness."""

    cli_path: str | None
    cli_version: str | None
    engine: str | None
    engine_source: EngineSource
    auto_upgrade: bool | None
    # acp.agentEngine: the harness `kiro-cli acp` (IDE and editor integrations) starts;
    # chat.agentEngine does not apply to it.
    acp_engine: str | None = None
    v2_agents: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default=())

    @property
    def on_v3(self) -> bool:
        """True when interactive kiro-cli sessions run the V3 harness."""
        return self.cli_path is not None and not _cli_reasons(self)

    @property
    def needs_reminder(self) -> bool:
        return bool(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "v2_agents": list(self.v2_agents),
            "reasons": list(self.reasons),
            "on_v3": self.on_v3,
            "needs_reminder": self.needs_reminder,
            "message": reminder_text(self) if self.needs_reminder else None,
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


def find_v2_agents(directories: Iterable[Path]) -> tuple[str, ...]:
    """Return agent files the V3 harness skips or that still use 2.x hook triggers."""
    found: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            data = _agent_data(path)
            if data is None:
                continue
            if any(issue.rule in _V2_RULES for issue in kiro_rules.agent_issues(data)):
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


def detect_harness(
    env: Mapping[str, str] | None = None,
    *,
    agent_dirs: Iterable[Path] = (),
    run: Runner = default_runner,
    which: Callable[[str], str | None] = shutil.which,
) -> HarnessStatus:
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
    upgrade_value, _ = _setting(binary, "chat.enableAutoAgentUpgrade", run, file_settings)
    auto_upgrade = upgrade_value if isinstance(upgrade_value, bool) else None
    acp_value, _ = _setting(binary, "acp.agentEngine", run, file_settings)
    acp_engine = acp_value.lower() if isinstance(acp_value, str) else None
    dirs = [*agent_dirs, kiro_home(environment) / "agents"]
    status = HarnessStatus(
        cli_path=binary,
        cli_version=version,
        engine=engine,
        engine_source=source,
        auto_upgrade=auto_upgrade,
        acp_engine=acp_engine,
        v2_agents=find_v2_agents(dict.fromkeys(dirs)),
    )
    reasons = [*_cli_reasons(status)]
    if status.v2_agents:
        reasons.append(
            f"{len(status.v2_agents)} agent configuration(s) still use the V2 format"
            + (" and automatic agent upgrade is off" if auto_upgrade is False else "")
        )
    return replace(status, reasons=tuple(reasons))


def _cli_reasons(status: HarnessStatus) -> list[str]:
    if status.cli_path is None:
        return []  # Kiro IDE only: there is no CLI harness to switch.
    reasons: list[str] = []
    parsed = parse_version(status.cli_version or "")
    if parsed is not None and parsed < V3_MIN_VERSION:
        reasons.append(f"kiro-cli {status.cli_version} predates 3.0.0, where V3 became the default")
    if status.engine in {"v1", "v2"}:
        label = "the retired Classic harness" if status.engine == "v1" else "the V2 harness"
        reasons.append(f"chat.agentEngine is {status.engine!r}, so kiro-cli runs {label}")
    if status.acp_engine in {"v1", "v2"}:
        reasons.append(
            f"acp.agentEngine is {status.acp_engine!r}, so `kiro-cli acp` (editor and IDE "
            "integrations) starts the old harness"
        )
    return reasons


def reminder_text(status: HarnessStatus) -> str:
    """The reminder shown by doctor, the SessionStart hook and the MCP tool."""
    lines = [
        "Kiro CLI V3 is the default agent harness since Kiro CLI 3.0.0.",
        *(f"- {reason}" for reason in status.reasons),
    ]
    parsed = parse_version(status.cli_version or "")
    if parsed is not None and parsed < V3_MIN_VERSION:
        lines.append("Update kiro-cli to 3.x first.")
    lines.append(
        "Run `kiro-cli settings chat.agentEngine v3`, start `kiro-cli`, then run "
        "`/upgrade-agent`. For a shared agent directory run `kiro-cli agent upgrade <dir>`."
    )
    if status.acp_engine in {"v1", "v2"}:
        lines.append("For editor integrations run `kiro-cli settings acp.agentEngine v3`.")
    lines.append(f"See {DOCS_URL}")
    return "\n".join(lines)


def reminder_due(cache_dir: Path, env: Mapping[str, str], *, now: float | None = None) -> bool:
    """Return True at most once per day; records the time when it does."""
    if env.get(ENV_NO_REMINDER, "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    stamp = cache_dir / REMINDER_STAMP
    current = time.time() if now is None else now
    try:
        last = float(stamp.read_text("utf-8").strip())
    except OSError, ValueError:
        last = 0.0
    if current - last < REMINDER_INTERVAL_SECONDS:
        return False
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{current:.0f}\n", encoding="utf-8")
    except OSError:
        return False
    return True
