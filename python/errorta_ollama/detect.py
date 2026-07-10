"""Detect whether Ollama is reachable at a given host."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

_LOG = logging.getLogger("errorta_ollama.detect")


@dataclass
class DetectionResult:
    reachable: bool
    host: str
    version: Optional[str] = None
    error: Optional[str] = None


def _tags_url(host: str) -> str:
    return host.rstrip("/") + "/api/tags"


def _version_url(host: str) -> str:
    return host.rstrip("/") + "/api/version"


def probe(host: str, timeout: float = 1.5) -> DetectionResult:
    """HTTP-probe the Ollama API at host. Short timeout — startup path."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(_tags_url(host))
            if r.status_code != 200:
                return DetectionResult(
                    reachable=False,
                    host=host,
                    error=f"HTTP {r.status_code}",
                )
            # Best-effort version fetch; non-fatal.
            version = None
            try:
                v = client.get(_version_url(host))
                if v.status_code == 200:
                    version = (v.json() or {}).get("version")
            except httpx.HTTPError:
                pass
            return DetectionResult(reachable=True, host=host, version=version)
    except httpx.HTTPError as e:
        return DetectionResult(reachable=False, host=host, error=str(e))
    except Exception as e:  # noqa: BLE001 - a probe must never raise (F063 B1)
        # A malformed host, OSError, or any non-httpx error must degrade to
        # "unreachable", never propagate as a 500 from /ollama/health.
        _LOG.warning("ollama probe failed for host %r: %s", host, e)
        return DetectionResult(reachable=False, host=host, error=str(e))


def wait_until_ready(host: str, total_timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Poll until Ollama responds or timeout elapses. Used after install."""
    import time

    deadline = time.monotonic() + total_timeout
    while time.monotonic() < deadline:
        if probe(host, timeout=1.0).reachable:
            return True
        time.sleep(interval)
    return False
