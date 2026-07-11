"""PyInstaller entry point for the `errorta` multicall binary (F147 §11).

The frozen binary is BOTH the CLI and the embedded sidecar server:

* ``errorta ...``        → the Typer argv front-end / slash REPL (a sidecar client)
* ``errorta __serve__``  → the embedded uvicorn sidecar, run in-process

``errorta_cli.app.main`` already special-cases ``argv[1] == "__serve__"`` and
runs the sidecar without going through Typer, so the CLI can spawn its own
sidecar by re-executing *this* binary (``sys.executable`` is the frozen binary —
there is no separate ``python`` to shell out to). Keeping this file tiny mirrors
``sidecar_main.py``; all logic lives in ``errorta_cli.app``.
"""
from errorta_cli.app import main

if __name__ == "__main__":
    main()
