"""The hidden ``__serve__`` entry — runs the embedded sidecar in-process.

F147 spec §11 (distribution). ``errorta`` is a self-contained multicall binary:
``errorta ...`` is the CLI, and ``errorta __serve__`` runs the uvicorn sidecar
in-process. The CLI spawns its sidecar by re-executing *itself* with
``__serve__`` (``sys.executable`` works even in a frozen PyInstaller binary,
which has no separate ``python`` to shell out to).

This module is the **only** place in ``errorta_cli`` that imports
``errorta_app`` (golden invariant #1). Everything else is a pure HTTP client.
The import is deferred to call time so merely importing ``errorta_cli`` never
pulls in the engine + AIAR.
"""
from __future__ import annotations


def run() -> None:
    """Boot the embedded Errorta sidecar (uvicorn) in this process.

    Reuses ``errorta_app.server.main()`` verbatim, which honors
    ``ERRORTA_SIDECAR_PORT`` (set by the CLI's spawn path) and binds loopback
    only. Raises ``ImportError`` if the engine isn't importable in this
    environment — the caller surfaces that as an unreachable sidecar.
    """
    from errorta_app.server import main  # deferred: invariant #1 boundary

    main()
