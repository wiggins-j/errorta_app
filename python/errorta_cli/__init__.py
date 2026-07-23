"""errorta_cli — the headless terminal front-end for the Errorta Coding Council.

F147. A pure sidecar *client*: every capability is an existing loopback HTTP
route on the Errorta Python sidecar, called with the static origin header the
desktop app uses (``x-errorta-origin: tauri-ui``). The CLI owns exactly one
sidecar per ``ERRORTA_HOME`` and never re-implements engine logic.

Golden invariant #1 (see the F147 plan): this package imports NOTHING from
``errorta_council`` / ``errorta_app`` anywhere except inside ``serve.py``'s
in-process sidecar launch. Everything else talks to the sidecar over HTTP.
"""
from __future__ import annotations

__version__ = "0.1.0-alpha.5"

__all__ = ["__version__"]
