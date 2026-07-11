"""Grep-guard for golden invariant #1: client-only.

``errorta_cli`` imports NOTHING from ``errorta_app`` / ``errorta_council`` except
inside ``serve.py`` (the in-process sidecar launch). We scan import *statements*
(not docstrings/comments), so prose that names the engine packages is fine.
"""
from __future__ import annotations

import re
from pathlib import Path

import errorta_cli

_IMPORT_ENGINE = re.compile(r"^\s*(?:from|import)\s+errorta_(?:app|council)\b", re.MULTILINE)


def test_only_serve_imports_the_engine() -> None:
    root = Path(errorta_cli.__file__).resolve().parent
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if py.name == "serve.py":
            continue
        if _IMPORT_ENGINE.search(py.read_text("utf-8")):
            offenders.append(str(py.relative_to(root)))
    assert offenders == [], f"engine import outside serve.py: {offenders}"


def test_serve_is_the_boundary() -> None:
    # serve.py is allowed to (and does) import the engine — that's the seam.
    src = (Path(errorta_cli.__file__).resolve().parent / "serve.py").read_text("utf-8")
    assert "errorta_app.server" in src
