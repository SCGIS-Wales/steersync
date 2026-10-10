"""Exception types raised by steersync.

Each error carries a process exit code so the CLI can map failures consistently.
"""


class SteersyncError(Exception):
    """Base error for all steersync failures."""

    exit_code: int = 1


class ConfigError(SteersyncError):
    """Configuration is missing, malformed or fails validation."""

    exit_code = 2


class GitError(SteersyncError):
    """A git invocation failed or returned unexpected output."""

    exit_code = 3


class LintError(SteersyncError):
    """Steering documents failed validation."""

    exit_code = 4


class SyncConflictError(SteersyncError):
    """Local and remote steering changed in incompatible ways."""

    exit_code = 5


class UnsafePathError(SteersyncError):
    """A path escaped the directory it must stay inside."""

    exit_code = 6
