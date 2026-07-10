"""F006 — Errorta shell helpers (process inventory + config).

Lives in a sibling package to errorta_app so the routes module can import it
without circular wiring through the FastAPI app.
"""
from __future__ import annotations

__all__ = ["processes", "config"]
