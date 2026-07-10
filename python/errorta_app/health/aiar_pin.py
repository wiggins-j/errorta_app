"""Detect how AIAR is installed in this Python environment.

The Errorta sidecar imports AIAR as a Python dependency. During development
that's an editable install (`pip install -e ../../aiar`); for release builds
it's a pinned PyPI version (`aiar-rag==0.2.*`). The frontend surfaces this
as a small badge so a developer can tell at a glance which mode they're in.

Note on the name: the import package is `aiar` (so `import aiar` works in
both cases), but the PyPI distribution name is `aiar-rag` because `aiar`
was already taken by a different project on PyPI. We probe both distribution
names when inspecting the install — `aiar-rag` first (the canonical PyPI
name), falling back to `aiar` for any legacy local checkouts that haven't
been renamed yet.

Canonical signal: PEP 610's `direct_url.json` written by pip into the dist-info
of any package installed from a local path or VCS. If `dir_info.editable` is
True, it's an editable install. If the package is importable but no editable
marker is present, treat it as a pinned/regular install.

F-INFRA-12 Phase B Slice 3: when residency.mode is ``ssh-remote`` or
``cloud``, the local Python environment doesn't authoritatively know the
upstream AIAR version. We probe the remote sidecar's ``/healthz`` and
return ``source: "remote"`` with an ``upstream`` block carrying the
remote's reported version, source, and the URL we probed.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal, Optional, TypedDict

log = logging.getLogger(__name__)

Source = Literal["editable", "pinned", "absent", "remote"]


class AiarPin(TypedDict, total=False):
    available: bool
    version: str | None
    source: Source
    upstream: Optional[dict[str, Any]]


def _fetch_remote_aiar_pin(url: str, token: Optional[str] = None) -> AiarPin:
    """GET ``{url}/healthz`` and translate the upstream's ``aiar_pin`` block.

    Never raises. Network errors, non-2xx responses, missing/malformed
    ``aiar_pin`` block all collapse to
    ``{"available": False, "version": None, "source": "remote",
       "upstream": {"url": url, "error": "<reason>"}}``.

    On success, returns
    ``{"available": <bool>, "version": <upstream.version>,
       "source": "remote",
       "upstream": {"url": url, "version": <upstream.version>,
                    "source": <upstream.source>}}``.
    """
    # Lazy import to avoid a hard module-import cycle and so the local
    # mode path never pays the cost of touching errorta_residency.
    try:
        from errorta_residency import probe as residency_probe
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "available": False,
            "version": None,
            "source": "remote",
            "upstream": {"url": url, "error": f"probe import failed: {exc}"},
        }

    result = residency_probe.probe_https_url(url, token=token, timeout_s=2.0)
    if not result.get("ok"):
        return {
            "available": False,
            "version": None,
            "source": "remote",
            "upstream": {
                "url": url,
                "error": result.get("error") or "upstream unreachable",
            },
        }

    body = result.get("body") or {}
    upstream_pin = body.get("aiar_pin") if isinstance(body, dict) else None
    if not isinstance(upstream_pin, dict):
        return {
            "available": False,
            "version": None,
            "source": "remote",
            "upstream": {
                "url": url,
                "error": "upstream /healthz did not include aiar_pin",
            },
        }

    upstream_version = upstream_pin.get("version")
    upstream_source = upstream_pin.get("source")
    upstream_available = bool(upstream_pin.get("available"))

    return {
        "available": upstream_available,
        "version": upstream_version,
        "source": "remote",
        "upstream": {
            "url": url,
            "version": upstream_version,
            "source": upstream_source,
        },
    }


def _local_aiar_pin() -> AiarPin:
    """The pre-Slice-3 local-mode behavior, unchanged.

    Returned as an ``AiarPin`` with ``upstream`` absent (the field is
    optional via ``total=False``).
    """
    try:
        import aiar  # type: ignore  # noqa: F401
    except ImportError:
        return {"available": False, "version": None, "source": "absent"}

    version = getattr(aiar, "__version__", None)

    try:
        # Imported lazily so the ImportError path above stays cheap.
        from importlib.metadata import PackageNotFoundError, distribution

        # Try the canonical PyPI distribution name first ("aiar-rag"), then
        # fall back to the import-name-as-dist-name ("aiar") for legacy
        # editable checkouts where pyproject.toml still says name = "aiar".
        dist = None
        for dist_name in ("aiar-rag", "aiar"):
            try:
                dist = distribution(dist_name)
                break
            except PackageNotFoundError:
                continue
        if dist is None:
            log.debug("aiar importable but no dist metadata; defaulting to 'editable'")
            return {"available": True, "version": version, "source": "editable"}

        raw = dist.read_text("direct_url.json")
        if not raw:
            # No direct_url marker → installed from an index (PyPI), i.e. pinned.
            return {"available": True, "version": version, "source": "pinned"}

        data = json.loads(raw)
        dir_info = data.get("dir_info") or {}
        if dir_info.get("editable") is True:
            return {"available": True, "version": version, "source": "editable"}

        # direct_url.json exists but not flagged editable (e.g. installed from
        # a local path or VCS url non-editable). Closer to pinned than editable.
        return {"available": True, "version": version, "source": "pinned"}

    except Exception as exc:  # pragma: no cover — defensive fallback
        log.debug("aiar_pin inspection failed (%s); defaulting to 'editable'", exc)
        return {"available": True, "version": version, "source": "editable"}


def check_aiar_pin() -> AiarPin:
    """Return a structured report on the AIAR install for the active residency.

    Dispatches on ``errorta_residency.config`` mode:

    - ``local`` (default) — inspect the local Python environment (legacy
      behavior). Returns ``{available, version, source}`` with ``upstream``
      absent.
    - ``ssh-remote`` — probe ``http://127.0.0.1:{local_tunnel_port}/healthz``
      (the local end of the SSH tunnel) and return ``source: "remote"``
      with an ``upstream`` block. Slice 7 stands the tunnel up; Slice 3
      already wires the probe so a stub or mocked client exercises the
      same path.
    - ``cloud`` — probe ``{cloud_url}/healthz`` with the in-memory token.

    Exotic installers (Conda, Nix, uv without --link-mode) may not write
    direct_url.json. In that case we conservatively classify as 'editable'
    (so the dev badge stays visible) and log a debug note.
    """
    # Lazy import: avoids any chance of an import cycle with
    # ``errorta_residency`` (which imports from ``errorta_app.paths``).
    try:
        from errorta_residency import config as residency_config
    except Exception:
        # If the residency module isn't importable for any reason, fall
        # back to local-mode behavior so /healthz keeps working.
        return _local_aiar_pin()

    try:
        state = residency_config.load()
    except Exception:
        return _local_aiar_pin()

    if state.mode == "local":
        return _local_aiar_pin()

    if state.mode == "ssh-remote":
        port = state.local_tunnel_port
        if not port:
            return {
                "available": False,
                "version": None,
                "source": "remote",
                "upstream": {
                    "url": None,
                    "error": "ssh-remote mode missing local_tunnel_port",
                },
            }
        url = f"http://127.0.0.1:{port}"
        return _fetch_remote_aiar_pin(url, token=None)

    if state.mode == "cloud":
        if not state.cloud_url:
            return {
                "available": False,
                "version": None,
                "source": "remote",
                "upstream": {
                    "url": None,
                    "error": "cloud mode missing cloud_url",
                },
            }
        return _fetch_remote_aiar_pin(state.cloud_url, token=state.cloud_token)

    # Unknown mode — degrade to local behavior so /healthz never breaks.
    return _local_aiar_pin()
