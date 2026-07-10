"""F003 — Ollama detection, installation, and managed lifecycle.

Helper package for the Errorta sidecar's /ollama routes. Split out from
errorta_app.routes.ollama so installer logic, hash-verification, and
settings persistence are independently testable.
"""
from __future__ import annotations

__all__ = ["detect", "installer", "lifecycle", "settings"]
