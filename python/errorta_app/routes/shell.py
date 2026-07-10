"""F006 — Tauri shell polish / settings router.

Exposes:
  GET  /shell/status               — sidecar + Ollama health roll-up
  GET  /shell/processes            — list managed child PIDs (sidecar + Ollama)
  GET  /shell/sidecar/port         — return the port the sidecar is bound to
  GET  /shell/config/ollama-host   — current Ollama host (shell-tier override)
  POST /shell/config/ollama-host   — update Ollama host (forwards to F003 later)
  POST /shell/ready                — frontend ping that records cold-start time
  GET  /shell/cold-start           — cold-start measurement (seconds)
"""
from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from errorta_shell import config as shell_config
from errorta_shell import processes as shell_processes

router = APIRouter(prefix="/shell", tags=["shell"])


# --- Models -----------------------------------------------------------------


class ProcessPayload(BaseModel):
    pid: int
    name: str
    role: str
    status: str
    cpu_percent: float
    rss_bytes: int
    started_at: Optional[float] = None


class ProcessesResponse(BaseModel):
    processes: list[ProcessPayload]


class OllamaHealth(BaseModel):
    host: str
    reachable: bool
    version: Optional[str] = None
    error: Optional[str] = None


class SidecarHealth(BaseModel):
    pid: int
    uptime_seconds: float
    cold_start_seconds: Optional[float] = None


class StatusResponse(BaseModel):
    sidecar: SidecarHealth
    ollama: OllamaHealth
    processes: list[ProcessPayload]


class SidecarPortResponse(BaseModel):
    port: int
    source: str  # "env" | "default"


class OllamaHostBody(BaseModel):
    host: str = Field(min_length=1)


class OllamaHostResponse(BaseModel):
    host: str


class ColdStartResponse(BaseModel):
    cold_start_seconds: Optional[float] = None
    process_start_epoch: float


# --- Helpers ----------------------------------------------------------------


def _resolve_port() -> tuple[int, str]:
    raw = os.environ.get("ERRORTA_SIDECAR_PORT")
    if raw:
        try:
            return int(raw), "env"
        except ValueError:
            pass
    return 8770, "default"


def _probe_ollama(host: str) -> OllamaHealth:
    try:
        with httpx.Client(timeout=1.5) as client:
            r = client.get(f"{host.rstrip('/')}/api/version")
            r.raise_for_status()
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return OllamaHealth(host=host, reachable=True, version=body.get("version"))
    except Exception as e:  # noqa: BLE001
        return OllamaHealth(host=host, reachable=False, error=str(e)[:200])


def _processes_payload() -> list[ProcessPayload]:
    return [ProcessPayload(**p.to_dict()) for p in shell_processes.list_managed()]


# --- Endpoints --------------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    procs = _processes_payload()
    sidecar = SidecarHealth(
        pid=os.getpid(),
        uptime_seconds=round(shell_processes.uptime_seconds(), 3),
        cold_start_seconds=shell_config.cold_start_seconds(),
    )
    ollama = _probe_ollama(shell_config.get_ollama_host())
    return StatusResponse(sidecar=sidecar, ollama=ollama, processes=procs)


@router.get("/processes", response_model=ProcessesResponse)
def processes() -> ProcessesResponse:
    return ProcessesResponse(processes=_processes_payload())


@router.get("/sidecar/port", response_model=SidecarPortResponse)
def sidecar_port() -> SidecarPortResponse:
    port, source = _resolve_port()
    return SidecarPortResponse(port=port, source=source)


@router.get("/config/ollama-host", response_model=OllamaHostResponse)
def get_ollama_host() -> OllamaHostResponse:
    return OllamaHostResponse(host=shell_config.get_ollama_host())


@router.post("/config/ollama-host", response_model=OllamaHostResponse)
def set_ollama_host(body: OllamaHostBody) -> OllamaHostResponse:
    try:
        host = shell_config.set_ollama_host(body.host)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return OllamaHostResponse(host=host)


@router.post("/ready", response_model=ColdStartResponse)
def mark_ready() -> ColdStartResponse:
    shell_config.mark_ready()
    return ColdStartResponse(
        cold_start_seconds=shell_config.cold_start_seconds(),
        process_start_epoch=shell_config.process_start(),
    )


@router.get("/cold-start", response_model=ColdStartResponse)
def cold_start() -> ColdStartResponse:
    return ColdStartResponse(
        cold_start_seconds=shell_config.cold_start_seconds(),
        process_start_epoch=shell_config.process_start(),
    )
