"""S8 packaging: frozen-safe self-re-exec + cli.spec parse sanity (F147 §11).

The multicall binary spawns its embedded sidecar by re-executing ITSELF with
``__serve__``. In a frozen PyInstaller binary ``sys.executable`` IS the binary
(no separate ``python``), so the spawn argv must be ``[<self>, "__serve__"]``;
in dev it re-execs the package entry ``python -m errorta_cli __serve__``. These
tests lock both forms and confirm ``main()`` routes a bare ``__serve__`` argv
straight to the sidecar without Typer parsing.

The ``cli.spec`` isn't built here (that needs the full PyInstaller + AIAR
toolchain — a maintainer/CI step); we only parse it to catch syntax drift.
"""
from __future__ import annotations

from pathlib import Path

from errorta_cli import app, sidecar

_REPO_PYTHON = Path(__file__).resolve().parents[2]  # .../python


# --------------------------------------------------------------------------- #
# Frozen-vs-dev self-re-exec argv.
# --------------------------------------------------------------------------- #

def test_serve_argv_frozen(monkeypatch) -> None:
    """A frozen binary re-execs itself: [sys.executable, "__serve__"]."""
    monkeypatch.setattr(sidecar.sys, "frozen", True, raising=False)
    monkeypatch.setattr(sidecar.sys, "executable", "/Applications/errorta")
    assert sidecar._serve_argv() == ["/Applications/errorta", "__serve__"]


def test_serve_argv_dev(monkeypatch) -> None:
    """In dev (not frozen) it re-execs the package entry via -m."""
    monkeypatch.setattr(sidecar.sys, "frozen", False, raising=False)
    monkeypatch.setattr(sidecar.sys, "executable", "/usr/bin/python3")
    assert sidecar._serve_argv() == [
        "/usr/bin/python3", "-m", "errorta_cli", "__serve__"
    ]


def test_main_routes_serve_without_typer(monkeypatch) -> None:
    """`errorta __serve__` boots the sidecar directly, bypassing Typer."""
    called: list[bool] = []
    monkeypatch.setattr(app.serve, "run", lambda: called.append(True))
    # If this went through Typer, `serve.run` (the real one) would try to import
    # the engine; the stub proves main() short-circuits on argv[1] == "__serve__".
    monkeypatch.setattr(app.sys, "argv", ["errorta", "__serve__"])
    app.main()
    assert called == [True]


# --------------------------------------------------------------------------- #
# cli.spec — syntax parse only (a real build is a maintainer/CI step).
# --------------------------------------------------------------------------- #

def test_cli_spec_parses() -> None:
    spec_path = _REPO_PYTHON / "cli.spec"
    assert spec_path.is_file(), f"cli.spec missing at {spec_path}"
    source = spec_path.read_text("utf-8")
    # compile() checks syntax without executing (exec needs the PyInstaller API).
    compile(source, "cli.spec", "exec")


def test_cli_spec_reuses_sidecar_aiar_finder_and_key_hiddenimports() -> None:
    """cli.spec must bundle AIAR the same way sidecar.spec does + the engine."""
    source = (_REPO_PYTHON / "cli.spec").read_text("utf-8")
    # The shared AIAR-editable-finder resolver (invariant #6 — reuse it).
    assert "_aiar_source_path" in source
    assert "__editable___aiar*_finder.py" in source
    # The engine entry the embedded `__serve__` boots, + AIAR itself.
    for needed in ("errorta_app.server", "\"aiar\"", "uvicorn", "cli_main.py"):
        assert needed in source, f"cli.spec should reference {needed}"
    # The CLI front-end's lazily-imported REPL dependency.
    assert "prompt_toolkit" in source


def test_cli_main_shim_imports() -> None:
    """The PyInstaller entry shim resolves to app.main (mirrors sidecar_main)."""
    import cli_main

    assert cli_main.main is app.main
