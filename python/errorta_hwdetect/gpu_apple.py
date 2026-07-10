"""Apple Silicon GPU detection via system_profiler.

On Apple Silicon, GPU memory is unified with system RAM. We report the
total system RAM as the effective VRAM ceiling.
"""
from __future__ import annotations

import json
import platform
import subprocess
from typing import Optional

import psutil


def detect() -> Optional[dict]:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None

    displays = data.get("SPDisplaysDataType", [])
    if not displays:
        return None
    gpu = displays[0]
    model = gpu.get("sppci_model") or gpu.get("_name") or "Apple GPU"

    is_apple_silicon = platform.machine() == "arm64"
    ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
    # On Apple Silicon, treat unified memory as the effective VRAM ceiling
    # (with some headroom reserved for the OS in practice).
    vram_gb = ram_gb if is_apple_silicon else 0.0

    return {
        "vendor": "apple" if is_apple_silicon else "apple-intel",
        "model": model,
        "vram_gb": vram_gb,
        "driver": None,
        "unified_memory": is_apple_silicon,
    }
