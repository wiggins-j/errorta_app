"""Invariant 1: egress + coupling boundaries.

- ``errorta_council`` never imports ``errorta_alpha`` (Council stays free of the
  app-level check-in egress).
- ``errorta_alpha`` never imports ``aiar`` or ``errorta_council`` (it is a
  self-contained delivery layer).
- Only ``errorta_alpha.client`` performs HTTP to the check-in service.

Static source scans — deterministic, and independent of test ordering / already-
imported modules.
"""
from __future__ import annotations

import pathlib

import errorta_alpha
import errorta_council

_ALPHA_DIR = pathlib.Path(errorta_alpha.__file__).resolve().parent
_COUNCIL_DIR = pathlib.Path(errorta_council.__file__).resolve().parent


def _py_files(root: pathlib.Path):
    return [p for p in root.rglob("*.py")]


def _imports(text: str, module: str) -> bool:
    """True if any *import statement* pulls in ``module`` (ignores comments /
    docstrings that merely mention the name)."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(f"import {module}") or s.startswith(f"from {module}"):
            return True
    return False


def test_council_never_imports_alpha():
    offenders = [
        str(p) for p in _py_files(_COUNCIL_DIR)
        if _imports(p.read_text(encoding="utf-8"), "errorta_alpha")
    ]
    assert offenders == [], f"errorta_council must not import errorta_alpha: {offenders}"


def test_alpha_never_imports_council_or_aiar():
    offenders = []
    for p in _py_files(_ALPHA_DIR):
        text = p.read_text(encoding="utf-8")
        for module in ("errorta_council", "aiar"):
            if _imports(text, module):
                offenders.append((str(p), module))
    assert offenders == [], f"errorta_alpha must stay decoupled: {offenders}"


def test_only_client_module_references_httpx():
    users = [
        p.name for p in _py_files(_ALPHA_DIR)
        if "httpx" in p.read_text(encoding="utf-8")
    ]
    assert users == ["client.py"], f"only client.py may use httpx, got {users}"
