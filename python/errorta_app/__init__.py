"""Errorta Python sidecar.

Thin web layer over the AIAR framework. Tauri spawns this as a child process
on app launch. The frontend talks to it over localhost HTTP.

See `docs/specs/F006-tauri-shell.md` for the architecture.
"""

# Spec 19: MIRROR of python/pyproject.toml's `version` (the canonical value).
# Never hand-edit — run `scripts/bump-version.sh X.Y.Z`, which rewrites all
# three declarations. tests/test_version_identity.py locks them byte-equal and
# scripts/release-cli.sh's preflight refuses to build on drift.
__version__ = "0.2.0-alpha.0"
