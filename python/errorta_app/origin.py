"""F147 S9a — trusted request-origin values for loopback mutations.

Coding/Council/settings mutations are gated on an ``x-errorta-origin`` header so
a mutation must come from a first-party front-end rather than a stray
cross-origin fetch (the sidecar binds ``127.0.0.1`` only, and CORS blocks a
browser page; this header is the belt-and-suspenders on top).

Historically the only trusted value was ``tauri-ui`` (the desktop webview). S9a
adds ``cli`` so a CLI-initiated mutation is *distinguishable in audit/logs* from
a GUI one while remaining loopback-trusted. This is not a privilege change — both
values are equally trusted and both are only reachable over loopback; it simply
lets the origin be recorded honestly once the CLI starts sending ``cli`` (a
future slice; the CLI still sends ``tauri-ui`` today, which stays valid).
"""
from __future__ import annotations

from fastapi import HTTPException, Request

# Trusted loopback mutation origins. ``tauri-ui`` = desktop webview (also what the
# CLI spoofs today); ``cli`` = the headless CLI once it advertises itself.
TRUSTED_MUTATION_ORIGINS = frozenset({"tauri-ui", "cli"})


def origin_of(request: Request) -> str:
    return request.headers.get("x-errorta-origin", "").lower()


def is_trusted_mutation_origin(request: Request) -> bool:
    return origin_of(request) in TRUSTED_MUTATION_ORIGINS


def require_ui_or_cli_origin(request: Request) -> None:
    """Reject a mutation whose ``x-errorta-origin`` is neither ``tauri-ui`` nor
    ``cli``. Raises 403 ``origin_not_authorized`` (unchanged wire contract)."""
    if not is_trusted_mutation_origin(request):
        raise HTTPException(status_code=403, detail="origin_not_authorized")


__all__ = [
    "TRUSTED_MUTATION_ORIGINS",
    "is_trusted_mutation_origin",
    "origin_of",
    "require_ui_or_cli_origin",
]
