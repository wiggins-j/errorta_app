"""Managed-Ollama lifecycle helpers.

If Errorta installed Ollama (`managed_by_errorta=True`) and the API isn't
reachable at launch, kick the platform-appropriate "start it" command. We
do NOT touch user-installed Ollama — only the install we own.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import detect
from . import settings as settings_module


@dataclass
class RestartResult:
    attempted: bool
    succeeded: bool
    reason: str


def restart_if_managed_and_down() -> RestartResult:
    """Called at sidecar startup (or on demand) to honor the restart-on-crash rule.

    Acceptance criterion: managed Ollama restarted on next launch after crash.
    """
    s = settings_module.load()
    if not s.managed_by_errorta:
        return RestartResult(False, False, "Ollama is not managed by Errorta")
    if not s.expect_running:
        return RestartResult(False, False, "Managed Ollama not expected to be running")

    if detect.probe(s.host, timeout=1.0).reachable:
        return RestartResult(False, True, "Already reachable")

    ok = _platform_start(storage_path=s.storage_path)
    if not ok:
        return RestartResult(True, False, "Failed to spawn Ollama starter")

    ready = detect.wait_until_ready(s.host, total_timeout=20.0, interval=0.5)
    if ready:
        return RestartResult(True, True, "Restarted and reachable")
    return RestartResult(True, False, "Started but API did not respond in 20s")


def _build_env(storage_path: str | None) -> dict:
    """Inherit current env and overlay OLLAMA_MODELS when a storage path is set.

    This is the only place the user's storage_path setting actually takes
    effect: when Errorta (re)starts a managed Ollama process, we hand it
    OLLAMA_MODELS so subsequent `ollama pull` calls land in the chosen
    directory. The setting is otherwise a UI-only sticky value.
    """
    env = os.environ.copy()
    if storage_path:
        env["OLLAMA_MODELS"] = storage_path
    return env


def _resolve_ollama_exe() -> str:
    """Absolute path to ollama.exe on Windows. OllamaSetup.exe installs to
    %LOCALAPPDATA%\\Programs\\Ollama; fall back to PATH, then the bare name."""
    found = shutil.which("ollama.exe") or shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = os.path.join(local, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return "ollama.exe"


def _platform_start(storage_path: str | None = None) -> bool:
    env = _build_env(storage_path)
    try:
        if sys.platform == "darwin":
            # `open -a Ollama` inherits the launchd env, not ours, so on macOS
            # OLLAMA_MODELS only takes effect for managed installs where the
            # app was launched from a context that inherits this env. Document
            # this caveat in the settings UI.
            subprocess.Popen(
                ["open", "-a", "Ollama"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            return True
        if sys.platform.startswith("linux"):
            # Best-effort: try systemd user unit, then fall back to `ollama serve`.
            try:
                subprocess.run(
                    ["systemctl", "--user", "start", "ollama"],
                    check=True,
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                return True
        if sys.platform.startswith("win"):
            subprocess.Popen(
                ["cmd", "/c", "start", "", _resolve_ollama_exe(), "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False
