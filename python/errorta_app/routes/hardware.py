"""F002 — hardware scan + model recommendation router.

Endpoints:
  POST /hardware/scan    Run a fresh scan, persist + return the report.
  GET  /hardware/report  Return the last-persisted report (or 404).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from errorta_hwdetect import scanner

from ._residency_proxy import proxy_json_if_remote

router = APIRouter(prefix="/hardware", tags=["hardware"])


def _scan_or_proxy(method: str, path: str) -> dict:
    proxied = proxy_json_if_remote(method, path)
    if proxied is not None:
        return proxied
    if method == "POST":
        return scanner.scan()
    report = scanner.load_persisted()
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="No hardware report on disk. Run POST /hardware/scan first.",
        )
    return report


@router.post("/scan")
def scan_hardware() -> dict:
    """Run a fresh hardware scan and return GPU/CPU/RAM/disk/OS + recommendation.

    Persists to ~/.errorta/hardware.json as a side effect.
    """
    return _scan_or_proxy("POST", "/hardware/scan")


@router.get("/report")
def get_report() -> dict:
    """Return the last persisted scan report from ~/.errorta/hardware.json."""
    return _scan_or_proxy("GET", "/hardware/report")
