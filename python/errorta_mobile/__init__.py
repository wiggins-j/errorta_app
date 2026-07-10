"""Errorta mobile companion connector.

The mobile connector is a narrow facade for paired phone clients. It is
disabled by default and intentionally separate from the general sidecar API.
"""
from __future__ import annotations

MOBILE_API_VERSION = 1

__all__ = ["MOBILE_API_VERSION"]

