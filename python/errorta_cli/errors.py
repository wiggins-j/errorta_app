"""Typed CLI errors + a stable exit-code map.

The exit codes are a CONTRACT: CI scripts branch on them, so the mapping must
stay stable across releases (F147 spec §5.3, plan §2). ``0`` is success; every
failure class the sidecar can hand back gets a distinct non-zero code so a
non-interactive ``errorta <cmd> --json`` caller can tell *why* it failed without
parsing stderr.

    0   ok
    1   generic CLI error (unclassified)
    3   LockBusy           — 409 "a run is already in progress"
    4   ResidencyRefused   — this data plane is remote; run it where the data lives
    5   AlphaLocked        — 403 alpha_locked (gated build, not activated)
    6   OriginDenied       — 403 origin_not_authorized
    7   RunFailed          — a run reached a failing terminal stop_reason
    8   NotFound           — 404
    9   SidecarUnreachable — could not reach / spawn the sidecar
    10  ForeignSidecar     — a desktop app / foreign sidecar owns this ERRORTA_HOME
"""
from __future__ import annotations

# Exit-code constants (the stable contract). Kept as module-level ints so tests
# and callers can reference them by name.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_LOCK_BUSY = 3
EXIT_RESIDENCY = 4
EXIT_ALPHA_LOCKED = 5
EXIT_ORIGIN_DENIED = 6
EXIT_RUN_FAILED = 7
EXIT_NOT_FOUND = 8
EXIT_SIDECAR_UNREACHABLE = 9
EXIT_FOREIGN_SIDECAR = 10


class CliError(Exception):
    """Base class for every CLI-surfaced failure.

    Carries a stable ``exit_code`` (the process exit status a non-interactive
    invocation returns) and an optional machine-readable ``code`` echoed from
    the sidecar's error body.
    """

    exit_code: int = EXIT_GENERIC

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class LockBusy(CliError):
    """A run is already in progress for this project (409 run-lock)."""

    exit_code = EXIT_LOCK_BUSY


class ResidencyRefused(CliError):
    """A local-disk data-plane action is unavailable under remote residency."""

    exit_code = EXIT_RESIDENCY


class AlphaLocked(CliError):
    """A gated alpha build is locked; the answering surface refuses (403)."""

    exit_code = EXIT_ALPHA_LOCKED


class OriginDenied(CliError):
    """The sidecar rejected the request origin (403 origin_not_authorized)."""

    exit_code = EXIT_ORIGIN_DENIED


class RunFailed(CliError):
    """A run reached a failing terminal stop_reason."""

    exit_code = EXIT_RUN_FAILED


class NotFound(CliError):
    """The requested resource does not exist (404)."""

    exit_code = EXIT_NOT_FOUND


class SidecarUnreachable(CliError):
    """Could not reach an existing sidecar and could not / would not spawn one."""

    exit_code = EXIT_SIDECAR_UNREACHABLE


class ForeignSidecar(CliError):
    """A desktop app (or other foreign sidecar) is driving this ERRORTA_HOME.

    v1 is sole-owner: rather than race the app and corrupt in-flight work, the
    CLI refuses to spawn a second sidecar next to a foreign owner — for ALL
    commands, reads included (F147 spec §4.2). Reads proceed only by adopting an
    existing CLI-owned sidecar; the CLI never stands up a competing one.
    """

    exit_code = EXIT_FOREIGN_SIDECAR


# Every concrete CLI error class, for the exit-code contract test.
ERROR_CLASSES: tuple[type[CliError], ...] = (
    CliError,
    LockBusy,
    ResidencyRefused,
    AlphaLocked,
    OriginDenied,
    RunFailed,
    NotFound,
    SidecarUnreachable,
    ForeignSidecar,
)
