"""F007 — welcome corpus support package.

Holds the downloader, ingest bridge, and the pinned SHA-256 hash file for
the on-demand "Welcome to Errorta" tarball.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PINNED_HASH_PATH = PACKAGE_DIR / "pinned_hash.json"
