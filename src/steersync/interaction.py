"""Decide once per command whether steersync may prompt.

steersync never requires a prompt: every question has a flag. It prompts only on a
terminal, outside CI, without ``--no-input`` or ``STEERSYNC_NO_INPUT`` and for text output.
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Output = Literal["text", "json"]


@dataclass(frozen=True)
class Interaction:
    interactive: bool
    assume_yes: bool
    force: bool
    dry_run: bool
    output: Output


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() not in {"", "0", "false", "no"}


def resolve_interaction(
    *,
    yes: bool,
    no_input: bool,
    force: bool,
    dry_run: bool,
    output: Output,
    env: Mapping[str, str] | None = None,
    stdin_tty: bool | None = None,
) -> Interaction:
    """The interaction mode for one command run."""
    environ = os.environ if env is None else env
    tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
    interactive = (
        tty
        and not no_input
        and output == "text"
        and not _env_flag(environ, "STEERSYNC_NO_INPUT")
        and not _env_flag(environ, "CI")
    )
    return Interaction(interactive, yes, force, dry_run, output)
