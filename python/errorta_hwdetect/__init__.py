"""errorta_hwdetect — hardware detection + model recommendation engine.

Used by the F002 hardware route to scan the local machine and pick a
sensible Ollama model tier for it.
"""
from __future__ import annotations

__all__ = ["scanner", "recommender"]
