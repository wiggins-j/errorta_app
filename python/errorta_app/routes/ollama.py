"""F003 — Ollama detection + install + lifecycle router.

Endpoints:
  GET  /ollama/health          probe configured host
  POST /ollama/install         start bundled install (background thread)
  GET  /ollama/install-progress phase-aware progress for the running install
  GET  /ollama/settings        read persisted settings (host, storage, managed)
  PUT  /ollama/settings        update host / storage path
  POST /ollama/restart         restart managed Ollama if it crashed
  GET  /ollama/models          list installed models / check presence (F110)
  POST /ollama/pull            pull a model with SSE progress (F110)
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from errorta_ollama import detect, installer, lifecycle
from errorta_ollama import pull as pull_module
from errorta_ollama import settings as settings_module

from ._residency_proxy import proxy_json_if_remote, refuse_local_dataplane_if_remote

router = APIRouter(prefix="/ollama", tags=["ollama"])
_LOG = logging.getLogger("errorta_app.routes.ollama")


# ---------- response models ----------


class HealthResponse(BaseModel):
    reachable: bool
    host: str
    version: Optional[str] = None
    error: Optional[str] = None
    managed_by_errorta: bool = False
    needs_install: bool = False
    platform_supported: bool = True


class InstallProgressResponse(BaseModel):
    phase: str
    percent: float
    message: str
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    host: Optional[str] = None
    version: Optional[str] = None


class SettingsResponse(BaseModel):
    host: str
    storage_path: Optional[str] = None
    managed_by_errorta: bool
    installed_version: Optional[str] = None
    last_install_at: Optional[str] = None
    expect_running: bool


class SettingsUpdate(BaseModel):
    host: Optional[str] = Field(default=None, description="Custom Ollama base URL")
    storage_path: Optional[str] = Field(default=None, description="OLLAMA_MODELS path")


class RestartResponse(BaseModel):
    attempted: bool
    succeeded: bool
    reason: str


class ModelsResponse(BaseModel):
    """Installed-models check (F110). ``installed`` reflects the queried model."""

    models: List[str]
    queried: Optional[str] = None
    installed: bool = False


class PullRequest(BaseModel):
    model: str = Field(..., description="Model reference to pull, e.g. 'llama3.2:3b'")


# ---------- endpoints ----------


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    proxied = proxy_json_if_remote("GET", "/ollama/health")
    if proxied is not None:
        return HealthResponse.model_validate(proxied)
    # F063 B1: a local health probe must never 500. detect.probe is fail-soft;
    # this wrapper also covers settings.load() / installer probe failures.
    try:
        s = settings_module.load()
        result = detect.probe(s.host)
        return HealthResponse(
            reachable=result.reachable,
            host=result.host,
            version=result.version,
            error=result.error,
            managed_by_errorta=s.managed_by_errorta,
            needs_install=(not result.reachable) and installer.platform_supported(),
            platform_supported=installer.platform_supported(),
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never 500
        _LOG.warning("ollama health probe failed: %s", exc)
        # platform_supported() must not be able to re-raise out of the handler
        # (it may be what failed above) — default to False if it does.
        try:
            supported = installer.platform_supported()
        except Exception:  # noqa: BLE001
            supported = False
        return HealthResponse(
            reachable=False, host="", version=None, error=str(exc),
            managed_by_errorta=False, needs_install=False,
            platform_supported=supported,
        )


@router.post("/install", response_model=InstallProgressResponse)
def install() -> InstallProgressResponse:
    proxied = proxy_json_if_remote("POST", "/ollama/install")
    if proxied is not None:
        return InstallProgressResponse.model_validate(proxied)
    s = settings_module.load()
    # If already reachable, skip — acceptance: no re-install prompt when reachable.
    if detect.probe(s.host).reachable:
        return InstallProgressResponse(
            phase="ready",
            percent=100.0,
            message="Ollama is already reachable; install skipped.",
            host=s.host,
        )
    if not installer.platform_supported():
        raise HTTPException(
            status_code=501,
            detail=f"Bundled install not supported on {installer.platform_label()}",
        )
    p = installer.start_install(host=s.host)
    return _to_progress_response(p)


@router.get("/install-progress", response_model=InstallProgressResponse)
def install_progress() -> InstallProgressResponse:
    proxied = proxy_json_if_remote("GET", "/ollama/install-progress")
    if proxied is not None:
        return InstallProgressResponse.model_validate(proxied)
    return _to_progress_response(installer.progress())


@router.get("/settings", response_model=SettingsResponse)
def get_settings() -> SettingsResponse:
    proxied = proxy_json_if_remote("GET", "/ollama/settings")
    if proxied is not None:
        return SettingsResponse.model_validate(proxied)
    s = settings_module.load()
    return SettingsResponse(
        host=s.host,
        storage_path=s.storage_path,
        managed_by_errorta=s.managed_by_errorta,
        installed_version=s.installed_version,
        last_install_at=s.last_install_at,
        expect_running=s.expect_running,
    )


@router.put("/settings", response_model=SettingsResponse)
def put_settings(payload: SettingsUpdate) -> SettingsResponse:
    proxied = proxy_json_if_remote(
        "PUT",
        "/ollama/settings",
        json_body=payload.model_dump(exclude_none=True),
    )
    if proxied is not None:
        return SettingsResponse.model_validate(proxied)
    updates: dict = {}
    if payload.host is not None:
        host = payload.host.strip()
        if not host.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="host must start with http:// or https://")
        updates["host"] = host.rstrip("/")
    if payload.storage_path is not None:
        # Empty string clears the override.
        updates["storage_path"] = payload.storage_path.strip() or None
    s = settings_module.update(**updates) if updates else settings_module.load()
    return SettingsResponse(
        host=s.host,
        storage_path=s.storage_path,
        managed_by_errorta=s.managed_by_errorta,
        installed_version=s.installed_version,
        last_install_at=s.last_install_at,
        expect_running=s.expect_running,
    )


@router.post("/restart", response_model=RestartResponse)
def restart() -> RestartResponse:
    proxied = proxy_json_if_remote("POST", "/ollama/restart")
    if proxied is not None:
        return RestartResponse.model_validate(proxied)
    r = lifecycle.restart_if_managed_and_down()
    return RestartResponse(attempted=r.attempted, succeeded=r.succeeded, reason=r.reason)


@router.get("/models", response_model=ModelsResponse)
def list_models(model: Optional[str] = None) -> ModelsResponse:
    """List installed Ollama models; if ``model`` is given, report its presence.

    F110 — the frontend uses this to skip a pull when the recommended model is
    already on disk. Read-only (no data-plane write) so it does NOT take the
    residency-refusal guard; instead it proxies to the active remote sidecar.
    """
    queried = None
    if model is not None:
        try:
            queried = pull_module.validate_model_name(model)
        except pull_module.InvalidModelName as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    path = "/ollama/models"
    if queried is not None:
        path = f"{path}?{urlencode({'model': queried})}"

    proxied = proxy_json_if_remote("GET", path)
    if proxied is not None:
        return ModelsResponse.model_validate(proxied)
    models = pull_module.installed_models()
    installed = False
    if queried is not None:
        installed = pull_module.is_model_installed(queried)
    return ModelsResponse(models=models, queried=queried, installed=installed)


@router.post("/pull")
def pull(payload: PullRequest) -> StreamingResponse:
    """Pull a model with SSE progress, mirroring the export-run stream shape.

    Frames (each separated by a blank line):
      event: hello\\ndata: {}            once at the start
      data: {"event":"progress","status":...,"percent":...}
      data: {"event":"done","model":...,"message":...}
      data: {"event":"error","error":...}

    Residency: a model pull writes to local disk (the model store), so it is a
    local-dataplane write and is refused under remote residency (fail-closed).
    The model name is validated before any subprocess argv is built.
    """
    # Fail-closed under remote residency BEFORE streaming starts (so the client
    # gets a clean 409, not a streamed error).
    refuse_local_dataplane_if_remote("/ollama/pull")

    # Validate the model name up front so an injection attempt is a 400, not a
    # streamed error frame.
    try:
        model = pull_module.validate_model_name(payload.model)
    except pull_module.InvalidModelName as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _sse(obj: dict) -> bytes:
        return f"data: {json.dumps(obj)}\n\n".encode("utf-8")

    def gen():
        yield b"event: hello\ndata: {}\n\n"
        result = None
        try:
            for frame in _iter_pull(model):
                if isinstance(frame, pull_module.PullProgress):
                    yield _sse(
                        {
                            "event": "progress",
                            "status": frame.status,
                            "percent": frame.percent,
                        }
                    )
                else:
                    result = frame
        except Exception as exc:  # noqa: BLE001 — degrade to a clean error frame
            _LOG.warning("ollama pull stream failed for %r: %s", model, exc)
            yield _sse({"event": "error", "error": str(exc)})
            return

        if result is None or not result.succeeded:
            err = (result.error or result.message) if result else "pull failed"
            yield _sse({"event": "error", "error": err})
            return

        yield _sse(
            {"event": "done", "model": result.model, "message": result.message}
        )

    return StreamingResponse(gen(), media_type="text/event-stream")


def _iter_pull(model: str):
    """Bridge ``pull_module.pull_model``'s callback API to a generator.

    Yields each ``PullProgress`` as it arrives, then yields the final
    ``PullResult`` last. Uses a thread + queue so progress streams live rather
    than buffering until the pull completes.
    """
    import queue
    import threading

    q: "queue.Queue" = queue.Queue()
    _SENTINEL = object()

    def run() -> None:
        try:
            res = pull_module.pull_model(model, on_progress=q.put)
            q.put(("__result__", res))
        except Exception as exc:  # noqa: BLE001
            q.put(("__error__", exc))
        finally:
            q.put(_SENTINEL)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while True:
        item = q.get()
        if item is _SENTINEL:
            break
        if isinstance(item, tuple) and item and item[0] == "__result__":
            yield item[1]
        elif isinstance(item, tuple) and item and item[0] == "__error__":
            raise item[1]
        else:
            yield item  # a PullProgress
    t.join(timeout=1.0)


def _to_progress_response(p: installer.InstallProgress) -> InstallProgressResponse:
    return InstallProgressResponse(
        phase=p.phase,
        percent=p.percent,
        message=p.message,
        error=p.error,
        started_at=p.started_at,
        ended_at=p.ended_at,
        host=p.host,
        version=p.version,
    )
