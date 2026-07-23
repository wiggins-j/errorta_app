"""Errorta Python sidecar.

Thin web layer over the AIAR framework. Tauri spawns this as a child process
on app launch. The frontend talks to it over localhost HTTP.

See `docs/specs/F006-tauri-shell.md` for the architecture.
"""

__version__ = "0.1.0-alpha.8"
