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


# The default cost note — a run spends real model budget. Other mutations (a
# provider-key write, a team apply) pass their own note so the refusal message is
# honest about the actual side effect.
_RUN_NOTE = "a run spends real model budget"


def confirm(
    ctx: Context,
    args: dict[str, Any],
    action: str,
    *,
    note: str = _RUN_NOTE,
    interactive_prompt: bool = True,
) -> bool:
    """Resolve whether ``action`` (e.g. "start a run") is authorized to proceed.

    Returns ``True`` to proceed, ``False`` if the user declined interactively.
    Raises :class:`CliError` (exit 1) when confirmation is required but ``--yes``
    was not given (``--json`` or non-interactive). Golden invariant #7.

    ``note`` customizes the parenthetical in the refusal message so a non-run
    mutation (e.g. writing a provider key) explains its own side effect instead of
    the run-budget default. ``interactive_prompt=False`` skips the ``y/N`` prompt
    interactively — used when the action is ALREADY an explicit deliberate step
    (e.g. the no-echo key prompt), so the gate only enforces ``--yes`` in the
    non-interactive / ``--json`` path.
    """
    if args.get("yes"):
        return True
    if ctx.json_mode:
        raise CliError(
            f"refusing to {action} in --json mode without --yes ({note}; "
            "pass --yes to confirm)",
            code="confirmation_required",
        )
    if not is_interactive():
        raise CliError(
            f"refusing to {action} without --yes (non-interactive; {note})",
            code="confirmation_required",
        )
    if not interactive_prompt:
        return True
    return prompt_yes_no(f"{action}? {note}")


def confirm_outward(ctx: Context, args: dict[str, Any], action: str,
                    details: list[str]) -> bool:
    """The EXTRA-STRONG gate for an OUTWARD-FACING publish (F147 §14).

    ``publish pr`` opens a real pull request; ``publish new-repo`` creates a real
    GitHub repository — both create/modify content the user's collaborators (or
    the whole world) can see. Unlike a run (which only spends the user's own
    budget), this must NEVER fire silently:

    * interactive: print EXACTLY what will happen (target repo, branch,
      private/public, title) then require an explicit ``y/N`` yes;
    * non-interactive OR ``--json``: REQUIRE ``--yes`` — a script can't open a PR
      or create a public repo by omission. The refusal echoes the same detail
      block so a CI author sees precisely what ``--yes`` would authorize.

    Returns ``True`` to proceed, ``False`` on an interactive decline.
    """
    detail_block = "\n".join(f"  - {line}" for line in details)
    if not args.get("yes"):
        if ctx.json_mode or not is_interactive():
            raise CliError(
                f"refusing to {action} without --yes. This will:\n{detail_block}\n"
                "This creates or modifies content on GitHub — pass --yes to authorize.",
                code="confirmation_required",
            )
        # Interactive: show the exact effect BEFORE the prompt so the yes is informed.
        print(f"About to {action}. This will:")
        print(detail_block)
        return prompt_yes_no(f"Proceed to {action}?")
    return True
