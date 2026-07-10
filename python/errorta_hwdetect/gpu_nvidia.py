"""NVIDIA GPU detection via NVML."""
from __future__ import annotations

from typing import Optional


def detect() -> Optional[dict]:
    """Return GPU info dict or None if no NVIDIA GPU / NVML unavailable."""
    try:
        import pynvml  # type: ignore
    except Exception:
        return None
    try:
        pynvml.nvmlInit()
    except Exception:
        return None
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            return None
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        vram_gb = round(mem.total / (1024**3), 1)
        try:
            driver = pynvml.nvmlSystemGetDriverVersion()
            if isinstance(driver, bytes):
                driver = driver.decode("utf-8", errors="replace")
        except Exception:
            driver = None
        return {
            "vendor": "nvidia",
            "model": name,
            "vram_gb": vram_gb,
            "driver": driver,
            "unified_memory": False,
        }
    except Exception:
        return None
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
