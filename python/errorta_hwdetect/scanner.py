"""Top-level hardware scanner.

Collects GPU/CPU/RAM/disk/OS info, calls the recommender, persists the
result to ~/.errorta/hardware.json, and returns the structured report.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

import psutil

from . import gpu_amd, gpu_apple, gpu_nvidia
from .recommender import recommend


def _hardware_json_path() -> Path:
    """Resolved lazily so ERRORTA_HOME overrides are honored at call time
    rather than module-import time."""
    from errorta_app.paths import hardware_json_path
    return hardware_json_path()




def _detect_gpu() -> dict[str, Any]:
    """Try each GPU detector; fall back to CPU-only stub with warning."""
    for fn in (gpu_nvidia.detect, gpu_apple.detect, gpu_amd.detect):
        try:
            info = fn()
        except Exception:
            info = None
        if info:
            return info
    return {
        "vendor": "none",
        "model": "No GPU detected",
        "vram_gb": 0.0,
        "driver": None,
        "unified_memory": False,
        "warning": "GPU detection failed or no supported GPU found; CPU-only mode.",
    }


def _detect_cpu() -> dict[str, Any]:
    model = platform.processor() or platform.machine() or "unknown"
    cores = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 0
    avx = False
    avx2 = False
    # Best-effort detection without py-cpuinfo. On Linux read /proc/cpuinfo;
    # on macOS use sysctl; otherwise mark unknown (False).
    try:
        if platform.system() == "Linux":
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                text = f.read().lower()
            avx = " avx " in text or "\tavx " in text or text.endswith(" avx")
            avx2 = "avx2" in text
        elif platform.system() == "Darwin":
            import subprocess

            out = subprocess.run(
                ["sysctl", "-a"], capture_output=True, text=True, timeout=2
            )
            if out.returncode == 0:
                low = out.stdout.lower()
                avx = "hw.optional.avx1_0: 1" in low or "avx1.0" in low
                avx2 = "hw.optional.avx2_0: 1" in low
            # On Apple Silicon, AVX is irrelevant (ARM NEON instead).
            if platform.machine() == "arm64":
                avx = False
                avx2 = False
    except Exception:
        pass
    return {"model": model, "cores": cores, "avx": avx, "avx2": avx2}


def _detect_disk() -> float:
    """Free GB on the home volume."""
    try:
        usage = shutil.disk_usage(str(Path.home()))
        return round(usage.free / (1024**3), 1)
    except Exception:
        return 0.0


def _detect_os() -> dict[str, Any]:
    sysname = platform.system()
    name = {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}.get(sysname, sysname)
    version = platform.mac_ver()[0] if sysname == "Darwin" else platform.release()
    return {"name": name, "version": version, "arch": platform.machine()}


def scan() -> dict[str, Any]:
    """Run a full scan and return the structured report."""
    gpu = _detect_gpu()
    cpu = _detect_cpu()
    ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
    disk_free_gb = _detect_disk()
    os_info = _detect_os()

    rec = recommend(
        vram_gb=float(gpu.get("vram_gb") or 0.0),
        vendor=str(gpu.get("vendor") or "none"),
        unified_memory=bool(gpu.get("unified_memory")),
        ram_gb=ram_gb,
    )

    report: dict[str, Any] = {
        "scanned_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "gpu": gpu,
        "ram_gb": ram_gb,
        "disk_free_gb": disk_free_gb,
        "cpu": cpu,
        "os": os_info,
        "recommendation": rec,
    }
    _persist(report)
    return report


def _persist(report: dict[str, Any]) -> None:
    try:
        _hardware_json_path().parent.mkdir(parents=True, exist_ok=True)
        _hardware_json_path().write_text(json.dumps(report, indent=2))
    except OSError:
        # Persisting is best-effort; the scan still succeeds.
        pass


def load_persisted() -> dict[str, Any] | None:
    """Read the last-saved report, or None if missing/unreadable."""
    if not _hardware_json_path().exists():
        return None
    try:
        return json.loads(_hardware_json_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
