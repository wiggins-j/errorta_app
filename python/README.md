# Errorta Python sidecar

The Python half of Errorta. A thin FastAPI server that wraps AIAR's framework
and exposes its API to the Tauri frontend.

## Dev setup

```bash
# From the repo root:
cd python

# Create venv
python3 -m venv .venv
source .venv/bin/activate

# Install Errorta sidecar in editable mode + dev tools
pip install -e ".[dev]"

# Install AIAR as an editable local dependency (the repo lives at ../../aiar
# on your dev machine when both are checked out side-by-side).
pip install -e ../../aiar

# Run the sidecar directly (binds to 127.0.0.1:8770 by default)
python -m errorta_app.server
```

The Tauri frontend (`tauri dev` in the repo root) will then connect to this
sidecar via `http://127.0.0.1:8770`.

## Production builds

PyInstaller produces a single executable that Tauri's sidecar plugin spawns:

```bash
cd python
source .venv/bin/activate
pyinstaller sidecar.spec
# Output: dist/errorta-sidecar
```

The CI build matrix runs this on macOS, Windows, and Linux runners and copies
the platform-appropriate binary into `src-tauri/binaries/` before the Tauri
bundle step.

## Layout

```
python/
  pyproject.toml          # deps + editable install
  sidecar.spec            # PyInstaller spec
  sidecar_main.py         # PyInstaller entry script
  errorta_app/            # The actual code
    __init__.py
    server.py             # FastAPI app + /healthz + main()
  tests/                  # (TBD)
```
