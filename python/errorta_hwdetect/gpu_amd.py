"""AMD GPU detection via pyamdgpuinfo (Linux only)."""
from __future__ import annotations

import platform
from typing import Optional


def detect() -> Optional[dict]:
    if platform.system() != "Linux":
        return None
    try:
        import pyamdgpuinfo  # type: ignore
    except Exception:
        return None
    try:
        count = pyamdgpuinfo.detect_gpus()
        if count == 0:
            return None
        gpu = pyamdgpuinfo.get_gpu(0)
        name = gpu.name if hasattr(gpu, "name") else "AMD GPU"
        vram_bytes = gpu.memory_info.get("vram_size", 0) if hasattr(gpu, "memory_info") else 0
        vram_gb = round(vram_bytes / (1024**3), 1)
        return {
            "vendor": "amd",
            "model": name,
            "vram_gb": vram_gb,
            "driver": None,
            "unified_memory": False,
        }
    except Exception:
        return None
