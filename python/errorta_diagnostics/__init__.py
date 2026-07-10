"""F-INFRA-06 — Local diagnostic bundle export.

Produces a redacted, zipped diagnostic bundle entirely on the local machine.
No network primitives are used anywhere in this package; the static lint test
in ``tests/test_diagnostics.py`` enforces that no ``socket`` / ``httpx`` /
``urllib.request`` / ``requests`` imports appear in any module here.
"""
from __future__ import annotations

from .bundle import build_bundle

__all__ = ["build_bundle"]
