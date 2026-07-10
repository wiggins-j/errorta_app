"""Platform-aware Ollama installer.

Phases (reported via progress queue):
  - downloading
  - verifying
  - installing
  - starting
  - ready
  - error

v0.1 scope:
  - macOS: download Ollama-darwin.zip, unzip into ~/Applications, `open -a Ollama`.
  - Linux: download install.sh, run with `sh` (lets Ollama's official script
           do its standard systemd-unit thing).
  - Windows: download OllamaSetup.exe, spawn it (UAC prompt comes from the
             OS, not us). Marked community-tier in v0.1.

Hashes are checked against known_hashes.json. If the pinned hash is the
sentinel zero, the installer refuses to run — we don't trust an unknown
binary by default.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import detect, settings as settings_module

_HASHES_PATH = Path(__file__).parent / "known_hashes.json"
_SENTINEL_HASH = "0" * 64


def _platform_key() -> str:
    s = sys.platform
    if s == "darwin":
        return "darwin"
    if s.startswith("linux"):
        return "linux"
    if s.startswith("win"):
        return "windows"
    return s


def _load_artifact() -> dict:
    raw = json.loads(_HASHES_PATH.read_text())
    key = _platform_key()
    artifacts = raw.get("artifacts", {})
    if key not in artifacts:
        raise RuntimeError(f"No pinned Ollama artifact for platform {key!r}")
    return artifacts[key]


@dataclass
class InstallProgress:
    phase: str = "idle"  # idle|downloading|verifying|installing|starting|ready|error
    percent: float = 0.0
    message: str = ""
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    # Snapshot of last detection after install.
    host: Optional[str] = None
    version: Optional[str] = None
    extra: dict = field(default_factory=dict)


class _Tracker:
    """Thread-safe progress tracker for a single install run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = InstallProgress()
        self._thread: Optional[threading.Thread] = None

    def snapshot(self) -> InstallProgress:
        with self._lock:
            # Return a shallow copy so callers can serialize safely.
            return InstallProgress(
                phase=self._state.phase,
                percent=self._state.percent,
                message=self._state.message,
                error=self._state.error,
                started_at=self._state.started_at,
                ended_at=self._state.ended_at,
                host=self._state.host,
                version=self._state.version,
                extra=dict(self._state.extra),
            )

    def set(self, **fields: object) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self._state, k, v)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, target, *args) -> None:
        if self.running():
            return
        self._state = InstallProgress(phase="downloading", started_at=time.time())
        self._thread = threading.Thread(
            target=target, args=args, name="errorta-ollama-install", daemon=True
        )
        self._thread.start()


_tracker = _Tracker()


def progress() -> InstallProgress:
    return _tracker.snapshot()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, tracker: _Tracker) -> None:
    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        seen = 0
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(chunk_size=128 * 1024):
                fh.write(chunk)
                seen += len(chunk)
                if total:
                    pct = min(60.0, (seen / total) * 60.0)  # download = 0..60%
                    tracker.set(percent=pct, message=f"Downloaded {seen // 1024} KB")


def _verify(path: Path, expected: str, tracker: _Tracker) -> None:
    tracker.set(phase="verifying", percent=65.0, message="Verifying download…")
    if expected == _SENTINEL_HASH:
        # Dev escape hatch: ERRORTA_OLLAMA_ALLOW_UNVERIFIED=1 downgrades the
        # refusal to a warning so contributors can exercise the install flow
        # before real hashes are pinned. Never set this in production builds.
        if os.environ.get("ERRORTA_OLLAMA_ALLOW_UNVERIFIED") == "1":
            tracker.set(
                message=(
                    "WARNING: running unverified installer "
                    "(ERRORTA_OLLAMA_ALLOW_UNVERIFIED=1). Dev only."
                ),
            )
            return
        raise RuntimeError(
            "Known-hash for this platform's Ollama artifact is not pinned in "
            "known_hashes.json. Refusing to run an unverified installer. "
            "(Dev override: set ERRORTA_OLLAMA_ALLOW_UNVERIFIED=1.)"
        )
    actual = _sha256_file(path)
    if actual.lower() != expected.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual}"
        )


def _install_macos(downloaded: Path, tracker: _Tracker) -> None:
    tracker.set(phase="installing", percent=75.0, message="Unpacking Ollama.app…")
    extract_dir = downloaded.parent / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(downloaded) as zf:
        zf.extractall(extract_dir)
    app_src = next(extract_dir.glob("Ollama.app"), None)
    if app_src is None:
        raise RuntimeError("Ollama.app missing from downloaded archive")
    # Prefer system /Applications if writable, else ~/Applications.
    targets = [Path("/Applications"), Path.home() / "Applications"]
    dest_root = next((t for t in targets if _writable(t)), None)
    if dest_root is None:
        raise RuntimeError("No writable Applications directory found")
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_app = dest_root / "Ollama.app"
    if dest_app.exists():
        shutil.rmtree(dest_app)
    shutil.move(str(app_src), str(dest_app))
    tracker.set(phase="starting", percent=85.0, message="Starting Ollama…")
    subprocess.Popen(["open", "-a", str(dest_app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _install_linux(downloaded: Path, tracker: _Tracker) -> None:
    # Ollama's install.sh internally calls sudo, which prompts on a TTY.
    # Spawned from this sidecar there is no controlling TTY, so the prompt
    # would hang indefinitely. We surface a clear message and cap the call
    # with a hard timeout so the install thread can fail cleanly instead of
    # wedging forever. v0.1 documents the Linux fallback in the install
    # prompt UI; this is a best-effort path for sudo-less or pre-authed
    # environments only.
    tracker.set(
        phase="installing",
        percent=75.0,
        message=(
            "Running install.sh… (Ollama's installer calls sudo. If you are "
            "not already authenticated, the install may time out — open a "
            "terminal and run: curl -fsSL https://ollama.com/install.sh | sh)"
        ),
    )
    try:
        subprocess.run(
            ["sh", str(downloaded)],
            check=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Ollama install.sh timed out (likely waiting for a sudo password "
            "with no TTY). Please install Ollama manually from a terminal: "
            "curl -fsSL https://ollama.com/install.sh | sh"
        ) from exc
    tracker.set(phase="starting", percent=88.0, message="Starting Ollama service…")
    # `ollama serve` is auto-started by their installer's systemd unit; nothing
    # else for us to do here.


def _install_windows(downloaded: Path, tracker: _Tracker) -> None:
    tracker.set(phase="installing", percent=75.0, message="Launching installer (accept UAC)…")
    # Spawn the installer; UAC prompt is OS-driven. We don't wait for the GUI
    # to close, but we do wait for the API to come up below.
    subprocess.Popen([str(downloaded)])


def _writable(path: Path) -> bool:
    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
    import os as _os

    return _os.access(path, _os.W_OK)


def _run_install(host: str) -> None:
    tracker = _tracker
    try:
        artifact = _load_artifact()
        with tempfile.TemporaryDirectory(prefix="errorta-ollama-") as td:
            workdir = Path(td)
            dest = workdir / artifact["filename"]
            tracker.set(phase="downloading", percent=0.0, message="Downloading Ollama…")
            _download(artifact["url"], dest, tracker)
            _verify(dest, artifact["sha256"], tracker)

            key = _platform_key()
            if key == "darwin":
                _install_macos(dest, tracker)
            elif key == "linux":
                _install_linux(dest, tracker)
            elif key == "windows":
                _install_windows(dest, tracker)
            else:
                raise RuntimeError(f"Unsupported platform: {key}")

        tracker.set(phase="starting", percent=92.0, message="Waiting for Ollama API…")
        ready = detect.wait_until_ready(host, total_timeout=90.0, interval=0.75)
        if not ready:
            raise RuntimeError("Ollama did not become reachable within 90 seconds")

        info = detect.probe(host)
        settings_module.update(
            managed_by_errorta=True,
            expect_running=True,
            installed_version=info.version,
            last_install_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            host=host,
        )
        tracker.set(
            phase="ready",
            percent=100.0,
            message="Ollama is ready.",
            ended_at=time.time(),
            host=host,
            version=info.version,
        )
    except Exception as e:  # noqa: BLE001 — surface everything to the UI
        tracker.set(
            phase="error",
            error=str(e),
            message=f"Install failed: {e}",
            ended_at=time.time(),
        )


def start_install(host: str = settings_module.DEFAULT_HOST) -> InstallProgress:
    """Kick off install on a background thread; returns the initial progress."""
    if _tracker.running():
        return _tracker.snapshot()
    _tracker.start(_run_install, host)
    return _tracker.snapshot()


def platform_supported() -> bool:
    return _platform_key() in {"darwin", "linux", "windows"}


def platform_label() -> str:
    return f"{platform.system()} {platform.release()}"
