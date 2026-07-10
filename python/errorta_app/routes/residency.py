"""F-INFRA-12 Phase B Slice 2 — residency configuration HTTP routes.

Exposes three endpoints:

- ``GET /residency`` — return the persisted residency config (with the
  cloud token nulled), the current tunnel state, and an optional
  upstream ``/healthz`` probe.
- ``PUT /residency`` — validate + persist a new residency configuration.
  For ``mode=cloud`` we probe the upstream before persisting; the probe
  must succeed or we return 400 and leave the previous state on disk.
- ``POST /residency/probe`` — fire a one-off probe against an arbitrary
  ``url`` (with optional bearer token) so the Settings panel can offer a
  "Test Connection" button before committing a switch.

The cloud access token is never written to disk and never echoed in
response bodies. The PUT response surfaces ``cloud_token: null`` even on
success.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from errorta_residency import config as residency_config
from errorta_residency import probe as residency_probe

router = APIRouter(prefix="/residency", tags=["residency"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

ResidencyMode = Literal["local", "ssh-remote", "cloud"]
TunnelState = Literal["down", "connecting", "up", "error"]


class SetResidencyRequest(BaseModel):
    mode: ResidencyMode
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_key_path: Optional[str] = None
    ssh_username: Optional[str] = None
    cloud_url: Optional[str] = None
    cloud_token: Optional[str] = None
    remote_sidecar_port: Optional[int] = None
    local_tunnel_port: Optional[int] = None


class ProbeRequest(BaseModel):
    url: str = Field(min_length=1)
    token: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redacted_state_dict(state: residency_config.ResidencyState) -> dict[str, Any]:
    """Return ``state`` as a dict with ``cloud_token`` forced to None.

    The persisted JSON already has ``cloud_token: null`` (see
    ``errorta_residency.config._save``), but ``ResidencyState`` returned
    from ``update()`` may carry the live token in memory. We redact here
    so neither GET nor PUT responses ever leak the token.
    """
    payload = dataclasses.asdict(state)
    payload["cloud_token"] = None
    return payload


def _tunnel_state_for(state: residency_config.ResidencyState) -> TunnelState:
    """Slice 2 stand-in for the shared tunnel state introduced in Slice 7.

    Until the Rust shell writes a live ``up`` / ``connecting`` / ``error``
    value into a shared struct, we report "down" for every mode. The
    frontend already knows to treat "down" + ``mode=ssh-remote`` as
    "tunnel not yet brought up" rather than as a hard failure.
    """
    return "down"


def _build_response(state: residency_config.ResidencyState) -> dict[str, Any]:
    """Compose the GET/PUT response body for ``state``.

    For ``mode=cloud`` we issue a fresh probe so the Settings panel can
    surface upstream health in the same trip. For ``mode=local`` and
    ``mode=ssh-remote`` we report ``remote_healthz: null`` (Slice 6
    introduces a real SSH-side probe wired into ``tunnel_state``).
    """
    remote_healthz: Optional[dict[str, Any]] = None
    if state.mode == "cloud" and state.cloud_url:
        result = residency_probe.probe_https_url(
            state.cloud_url,
            token=state.cloud_token,
        )
        # Surface the upstream's /healthz body if we got one; otherwise
        # surface None so the frontend renders an "upstream unreachable"
        # state instead of a partially-formed object.
        remote_healthz = result.get("body") if result.get("ok") else None

    return {
        "config": _redacted_state_dict(state),
        "tunnel_state": _tunnel_state_for(state),
        "remote_healthz": remote_healthz,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def get_residency() -> dict[str, Any]:
    """Return the persisted residency config, tunnel state, upstream healthz."""
    state = residency_config.load()
    return _build_response(state)


@router.put("")
def put_residency(body: SetResidencyRequest) -> dict[str, Any]:
    """Validate + persist a new residency configuration.

    Cloud mode is gated on a successful upstream probe; failures are
    surfaced as HTTP 400 with the probe error message so the Settings
    panel can show it inline.
    """
    payload: dict[str, Any] = body.model_dump(exclude_unset=False)

    if body.mode == "cloud":
        raise HTTPException(
            status_code=501,
            detail={
                "field": "mode",
                "error": "Cloud data-residency mode is not enabled until token auth ships.",
            },
        )

    # Cloud-mode pre-flight: validate URL shape, then probe. Both happen
    # before we touch the persisted state so a failed switch leaves the
    # previous mode intact on disk.
    if body.mode == "cloud":
        if not body.cloud_url:
            raise HTTPException(
                status_code=400,
                detail={"field": "cloud_url", "error": "cloud_url is required when mode='cloud'"},
            )
        try:
            cleaned_url = residency_probe.validate_https_url(body.cloud_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"field": "cloud_url", "error": str(exc)},
            ) from exc
        payload["cloud_url"] = cleaned_url

        probe_result = residency_probe.probe_https_url(
            cleaned_url, token=body.cloud_token
        )
        if not probe_result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={
                    "field": "cloud_url",
                    "error": probe_result.get("error") or "upstream unreachable",
                },
            )

    # Mode-specific field reset: when leaving ssh-remote / cloud, clear
    # the fields that no longer apply so a stale ``ssh_host`` doesn't
    # confuse the Settings panel after the user switches back to local.
    if body.mode == "local":
        payload.update(
            ssh_host=None,
            ssh_key_path=None,
            ssh_username=None,
            cloud_url=None,
            cloud_token=None,
            remote_sidecar_port=None,
            local_tunnel_port=None,
        )
    elif body.mode == "ssh-remote":
        payload.update(cloud_url=None, cloud_token=None)
    elif body.mode == "cloud":
        payload.update(
            ssh_host=None,
            ssh_key_path=None,
            ssh_username=None,
            remote_sidecar_port=None,
            local_tunnel_port=None,
        )

    # ``ssh_port`` defaults to 22 inside ResidencyState; only override
    # when the caller actually supplied a value, else config.update will
    # set it to None which fails validation.
    if payload.get("ssh_port") is None:
        payload.pop("ssh_port", None)

    try:
        new_state = residency_config.update(**payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"field": "config", "error": str(exc)},
        ) from exc

    return _build_response(new_state)


@router.post("/probe")
def post_probe(body: ProbeRequest) -> dict[str, Any]:
    """Probe an arbitrary ``url`` (with optional token) without persisting.

    Used by the Settings panel's "Test Connection" button. Never raises;
    the structured ``{ok, status, body, error}`` is returned verbatim.
    """
    try:
        cleaned_url = residency_probe.validate_https_url(body.url)
    except ValueError as exc:
        # Shape errors are surfaced as ``ok=False`` rather than HTTP 400
        # so the frontend has a single error-rendering path.
        return {"ok": False, "status": None, "body": None, "error": str(exc)}
    return residency_probe.probe_https_url(cleaned_url, token=body.token)
