"""Shared safety helpers for the S3 mutating commands (F147 §8, plan §4).

Two cross-cutting guarantees every run-control mutation must uphold:

* **Sole-owner guard** (golden invariant #5) — before writing run state / spawning
  a worker, re-check that no foreign desktop app owns this ``ERRORTA_HOME``. This is
  defense-in-depth behind ``sidecar.resolve()``'s spawn-time refusal: it catches the
  case where the CLI already adopted its own sidecar and the app was launched
  afterwards. Delegates to :func:`errorta_cli.sidecar.require_sole_owner` (imported
  into this module's namespace so tests can spy on it).
* **Confirmation gate** (golden invariant #7) — starting/steering a run spends real
  model budget, so a mutation never fires without an explicit yes. Interactive:
  prompt ``y/N``. Non-interactive OR ``--json``: require ``--yes`` (error otherwise)
  so a script can't fire a run by accident.

The origin header is NOT handled here — the ``SidecarClient`` attaches
``x-errorta-origin: tauri-ui`` to EVERY request (invariant #2), so a mutation's
``post_json`` is already authorized.
"""
from __future__ import annotations

import sys
from typing import Any

from ..errors import CliError
from ..session import Context
from ..sidecar import require_sole_owner  # re-exported so tests can monkeypatch here


def guard_sole_owner(ctx: Context) -> None:
    """Refuse to co-drive a store a foreign desktop app owns (invariant #5).

    Raises :class:`~errorta_cli.errors.ForeignSidecar` (exit 10) when a foreign
    ``Errorta.app`` / sidecar is detected. A no-op when the CLI is sole owner.
    """
    require_sole_owner(ctx.home, ctx.handle)


def is_interactive() -> bool:
    """True only when BOTH stdin and stdout are real TTYs.

    A seam so tests (captured stdio) read as non-interactive without patching.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, AttributeError):  # pragma: no cover — closed stdio
        return False


def prompt_yes_no(question: str) -> bool:
    """Ask a ``y/N`` question at the terminal (seam; monkeypatched in tests)."""
    try:
        answer = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:  # pragma: no cover — stdin closed mid-prompt
        return False
    return answer in ("y", "yes")


def confirm(ctx: Context, args: dict[str, Any], action: str) -> bool:
    """Resolve whether ``action`` (e.g. "start a run") is authorized to proceed.

    Returns ``True`` to proceed, ``False`` if the user declined interactively.
    Raises :class:`CliError` (exit 1) when confirmation is required but ``--yes``
    was not given (``--json`` or non-interactive). Golden invariant #7.
    """
    if args.get("yes"):
        return True
    if ctx.json_mode:
        raise CliError(
            f"refusing to {action} in --json mode without --yes "
            "(a run spends real model budget; pass --yes to confirm)",
            code="confirmation_required",
        )
    if not is_interactive():
        raise CliError(
            f"refusing to {action} without --yes "
            "(non-interactive; a run spends real model budget)",
            code="confirmation_required",
        )
    return prompt_yes_no(f"{action}? this spends real model budget")
