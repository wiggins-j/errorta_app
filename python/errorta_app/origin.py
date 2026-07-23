"""F147 S9a / R3 — trusted request-origin + bearer-token guard for mutations.

Coding/Council/settings mutations are gated on an ``x-errorta-origin`` header so
a mutation must come from a first-party front-end rather than a stray
cross-origin fetch (the sidecar binds ``127.0.0.1`` only, and CORS blocks a
browser page; this header is the belt-and-suspenders on top).

Historically the only trusted value was ``tauri-ui`` (the desktop webview). S9a
adds ``cli`` so a CLI-initiated mutation is *distinguishable in audit/logs* from
a GUI one while remaining loopback-trusted. This is not a privilege change — both
values are equally trusted and both are only reachable over loopback.

**R3 — real auth on the mutation surface.** The origin header alone is *not* a
secret: any local process can send ``x-errorta-origin: cli`` (or ``tauri-ui``)
and start runs that execute real commands. R3 mints a per-sidecar bearer token
(``${ERRORTA_HOME}/sidecar-token``, 0600) at spawn and passes it to the sidecar
process via ``ERRORTA_SIDECAR_TOKEN``. Every mutation guard validates that token
*in addition to* its own origin policy (defense-in-depth).

There are TWO independent guard families, and each keeps its OWN origin policy —
R3 layers the SAME token check onto both without changing either policy:

  * :func:`require_ui_or_cli_origin` — the shared guard used by ``coding.py`` and
    ``gateway.py``. Accepts ``tauri-ui`` OR ``cli`` (the CLI sends ``cli``).
  * the per-route ``_require_tauri_origin`` guards local to ``settings.py``,
    ``council.py``, ``alpha.py``, ``aiar_connection.py`` and ``auth.py`` — each
    accepts ``tauri-ui`` ONLY (stricter; these are desktop-only surfaces). R3
    does NOT loosen them to ``cli``; it only adds the token check.

The token decision is shared by :func:`validate_sidecar_token`, which is
origin-agnostic (it never inspects the origin — the caller's own origin check
runs first). Its behavior depends on the enforcement mode:

**GRACE mode (default — ``ERRORTA_SIDECAR_TOKEN_ENFORCE`` unset/falsey):**

  * sidecar has NO token configured    → allow (origin-only; a desktop-spawned
    or pre-R3 sidecar)
  * token configured, VALID bearer      → allow
  * token configured, NO bearer         → allow (an old CLI that predates R3
    still works against a new sidecar)
  * token configured, INVALID bearer    → 403 ``token_invalid``

**ENFORCE mode (``ERRORTA_SIDECAR_TOKEN_ENFORCE`` truthy):**

  * sidecar has NO token configured    → allow (nothing to enforce; a
    desktop-spawned / pre-R3 sidecar cannot present a token it never minted)
  * token configured, VALID bearer      → allow
  * token configured, NO bearer         → 403 ``token_required`` (a trusted
    origin ALONE is no longer sufficient)
  * token configured, INVALID bearer    → 403 ``token_invalid``

Grace mode lets new and old CLIs coexist during the alpha; enforce mode is the
opt-in hard gate that actually closes the "trusted origin is not a secret"
vulnerability. The comparison is constant-time (``hmac.compare_digest`` over
bytes) so a wrong token can't be guessed by timing and a non-ASCII bearer fails
closed with 403 rather than raising. The enforcement flag is read LIVE from the
environment (the CLI spawns the sidecar with ``{**os.environ}``, so the operator
setting ``ERRORTA_SIDECAR_TOKEN_ENFORCE`` in their shell propagates to the
sidecar automatically — no separate spawn plumbing). Mobile/tunnel + Service API
auth are entirely separate and untouched.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

# R3 — the env var the spawned sidecar reads to learn its own bearer token
# (set by ``errorta_cli.sidecar.spawn``). Kept as a module constant so tests and
# the CLI agree on the name.
SIDECAR_TOKEN_ENV = "ERRORTA_SIDECAR_TOKEN"

# R3 — the env var that flips the token check from GRACE (default) to ENFORCE.
# Read live from the environment on every request (see ``token_enforced``). The
# sidecar inherits it from the spawning CLI's environment (spawn copies
# ``os.environ``), so no explicit spawn plumbing is required.
SIDECAR_TOKEN_ENFORCE_ENV = "ERRORTA_SIDECAR_TOKEN_ENFORCE"

# Trusted loopback mutation origins. ``tauri-ui`` = desktop webview; ``cli`` =
# the headless CLI. Only the SHARED guard accepts both; the per-route
# ``_require_tauri_origin`` guards accept ``tauri-ui`` only.
TRUSTED_MUTATION_ORIGINS = frozenset({"tauri-ui", "cli"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def origin_of(request: Request) -> str:
    return request.headers.get("x-errorta-origin", "").lower()


def is_trusted_mutation_origin(request: Request) -> bool:
    return origin_of(request) in TRUSTED_MUTATION_ORIGINS


def configured_sidecar_token() -> str | None:
    """The bearer token this sidecar was booted with, or ``None`` if it has none.

    Read from ``ERRORTA_SIDECAR_TOKEN`` — set by the CLI at spawn. A sidecar with
    no token (a desktop-spawned one, or a pre-R3 build) returns ``None`` and the
    guard falls back to origin-only (there is no token to enforce)."""
    token = (os.environ.get(SIDECAR_TOKEN_ENV) or "").strip()
    return token or None


def token_enforced() -> bool:
    """Is the hard token gate ON? (``ERRORTA_SIDECAR_TOKEN_ENFORCE`` truthy.)

    Read live so an operator can flip enforcement without a rebuild. Default OFF
    (grace) for alpha backward-compat."""
    raw = (os.environ.get(SIDECAR_TOKEN_ENFORCE_ENV) or "").strip().lower()
    return raw in _TRUTHY


def bearer_token(request: Request) -> str | None:
    """Extract the ``Authorization: Bearer <token>`` value, or ``None``."""
    raw = request.headers.get("authorization", "")
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def validate_sidecar_token(request: Request) -> None:
    """Origin-AGNOSTIC bearer-token check shared by *every* mutation guard.

    This is the single reusable token decision. It NEVER inspects the origin —
    each caller runs its own origin policy first (the shared
    :func:`require_ui_or_cli_origin`, or a per-route ``_require_tauri_origin``)
    and then calls this to layer token auth on top without changing that policy.

    Raises 403 ``token_required`` (enforce mode, no bearer presented) or 403
    ``token_invalid`` (a bearer was presented but does not match, OR is malformed
    / non-ASCII). See the module docstring for the full grace/enforce truth
    table. A sidecar with no token configured is always a no-op (origin-only)."""
    configured = configured_sidecar_token()
    if configured is None:
        # This sidecar has no token (desktop-spawned or pre-R3) → nothing to
        # enforce. Origin-only, in both grace AND enforce mode.
        return

    presented = bearer_token(request)
    if presented is None:
        if token_enforced():
            # ENFORCE: a trusted origin alone is NOT sufficient — the caller must
            # present the bearer this sidecar was booted with.
            raise HTTPException(status_code=403, detail="token_required")
        # GRACE: a trusted origin with no bearer (an old CLI) still works.
        return

    # A token WAS presented → it must be valid, in BOTH modes. Constant-time
    # compare over bytes so a wrong token can't be recovered by timing and a
    # non-ASCII / malformed bearer fails closed (403) instead of raising
    # TypeError → 500.
    if not hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8")):
        raise HTTPException(status_code=403, detail="token_invalid")


def require_ui_or_cli_origin(request: Request) -> None:
    """Shared mutation guard: trusted (``tauri-ui`` OR ``cli``) origin + token.

    Used by ``coding.py`` and ``gateway.py`` (the routes the CLI drives, which is
    why it accepts ``cli``). Raises 403 ``origin_not_authorized`` for an
    untrusted origin, then delegates the bearer-token decision to
    :func:`validate_sidecar_token`.

    NOTE: this is NOT the only mutation guard. ``settings.py`` / ``council.py`` /
    ``alpha.py`` / ``aiar_connection.py`` / ``auth.py`` keep their own stricter
    ``tauri-ui``-only guards; each of those calls :func:`validate_sidecar_token`
    directly, so token auth now covers the ENTIRE mutation surface while every
    route retains its own origin policy."""
    if not is_trusted_mutation_origin(request):
        raise HTTPException(status_code=403, detail="origin_not_authorized")
    validate_sidecar_token(request)


__all__ = [
    "SIDECAR_TOKEN_ENFORCE_ENV",
    "SIDECAR_TOKEN_ENV",
    "TRUSTED_MUTATION_ORIGINS",
    "bearer_token",
    "configured_sidecar_token",
    "is_trusted_mutation_origin",
    "origin_of",
    "require_ui_or_cli_origin",
    "token_enforced",
    "validate_sidecar_token",
]
