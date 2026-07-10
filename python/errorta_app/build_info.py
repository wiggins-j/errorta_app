"""Build provenance — so a running sidecar can say which commit it was built from.

This is the backbone of the "is my app stale?" check. A bundled app drifts from
the source repo the moment new code lands; without a stamped commit the only
symptom is confusing downstream failures (missing routes 404, features silently
absent). Here every build records its git commit, ``/healthz`` reports it, and
``scripts/app-doctor.sh`` compares it to the repo HEAD.

Resolution order (most authoritative first):
  1. a bundled ``_build_info.json`` next to this module (written by
     ``scripts/build-sidecar.sh`` at build time; found via ``sys._MEIPASS`` in a
     frozen PyInstaller app, or the package dir from source);
  2. ``ERRORTA_BUILD_COMMIT`` / ``ERRORTA_BUILT_AT`` env vars;
  3. a live ``git rev-parse`` when running from a source checkout;
  4. ``unknown`` (never raises — provenance is best-effort).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_BUILD_INFO_FILENAME = "_build_info.json"


def _bundled_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    paths = [here / _BUILD_INFO_FILENAME]
    # PyInstaller unpacks bundled data under sys._MEIPASS/<pkg>/...
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "errorta_app" / _BUILD_INFO_FILENAME)
    return paths


def _from_bundle() -> dict | None:
    for path in _bundled_paths():
        try:
            if path.is_file():
                data = json.loads(path.read_text("utf-8"))
                if isinstance(data, dict) and data.get("commit"):
                    return data
        except Exception:
            continue
    return None


def _from_env() -> dict | None:
    commit = (os.environ.get("ERRORTA_BUILD_COMMIT") or "").strip()
    if not commit:
        return None
    return {
        "commit": commit,
        "built_at": (os.environ.get("ERRORTA_BUILT_AT") or "").strip() or None,
        "dirty": (os.environ.get("ERRORTA_BUILD_DIRTY") or "").strip().lower()
        in ("1", "true", "yes"),
        "source": "env",
    }


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def _from_git() -> dict | None:
    # Frozen binaries are not inside a git tree — this only fires from source.
    if getattr(sys, "frozen", False):
        return None
    commit = _git("rev-parse", "HEAD")
    if not commit:
        return None
    dirty = bool(_git("status", "--porcelain"))
    return {"commit": commit, "built_at": None, "dirty": dirty, "source": "git"}


@lru_cache(maxsize=1)
def build_info() -> dict:
    """Best-effort build provenance for this sidecar. Never raises."""
    info = _from_bundle() or _from_env() or _from_git() or {
        "commit": None, "built_at": None, "dirty": False, "source": "unknown"}
    commit = info.get("commit")
    info.setdefault("source", "unknown")
    info.setdefault("built_at", None)
    info.setdefault("dirty", False)
    info["commit_short"] = (str(commit)[:12] if commit else None)
    return info


def features() -> dict:
    """Capability surfaces THIS build exposes. An older build simply omits keys
    it predates (e.g. ``grounding``), so the frontend can detect drift and ask
    the user to update instead of failing on a missing route."""
    feats = {
        "coding": True,
        "council": True,
        "briefs": True,
        "judge": True,
        # F129: backend validates + binds Multi-member routes before every
        # route-dependent policy/context/gateway boundary.
        "model_assignment_ready": True,
        # F-DIST-01: alpha delivery / licensing / telemetry surfaces present.
        # Whether the gate is actually ON is a build-time decision reported via
        # GET /alpha/status, not here.
        "alpha_delivery": True,
    }
    try:
        import errorta_project_grounding  # noqa: F401
        feats["grounding"] = True
    except Exception:
        feats["grounding"] = False
    return feats


__all__ = ["build_info", "features"]
