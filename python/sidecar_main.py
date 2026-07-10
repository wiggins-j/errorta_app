"""PyInstaller entry point for the Errorta sidecar.

PyInstaller bundles this script + all detected imports into a single
executable. At runtime the produced binary is what Tauri spawns as a child
process.

The `errorta_app.server.main()` function does the actual work — keeping this
file tiny. It already honors the `ERRORTA_SIDECAR_PORT` env var (which the
Tauri shell sets per-spawn to a freshly allocated free port), so no extra
plumbing is needed here.
"""
from errorta_app.server import main

if __name__ == "__main__":
    main()
