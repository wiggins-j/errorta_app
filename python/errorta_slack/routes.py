"""Task 10 — the Slack PM bridge's HTTP routes facade.

Mounted on the sidecar via ``server.py``'s guarded
``app.include_router(...)`` (see that module's boot section). Every
handler here reads/writes ``errorta_slack`` state directly
(``config``/``store``/``secrets``/``linking``) rather than reaching
through ``app.state``, so this router can be exercised standalone
against a bare ``FastAPI()`` app in tests.

This module MUST NOT import ``slack_sdk`` at module load time -- the
Slack bridge is strictly optional and disabled by default, and none of
these routes need a live Slack connection to serve config/status/link
actions. (``POST /slack/enable`` only *persists* the enabled flag; the
live bridge connection is exclusively owned by
``errorta_app.slack_lifecycle``, reconciled at sidecar boot/shutdown --
see that module's docstring for why routes never trigger it inline.)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from errorta_slack import config, linking, secrets, store

router = APIRouter(prefix="/slack", tags=["slack"])


class LinkApproveRequest(BaseModel):
    link_id: str


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@router.get("/status")
def status() -> dict[str, Any]:
    cfg = config.load()
    return {
        "enabled": cfg["enabled"],
        "bindings": store.list_bindings(),
        # Never the raw values -- secrets.mask() reduces each token to its
        # last 4 characters (or None if unconfigured). This is the ONLY
        # token-shaped value this bridge ever returns over HTTP.
        "tokens_masked": secrets.mask(),
    }


@router.post("/enable")
def enable() -> dict[str, Any]:
    tokens = secrets.load_tokens()
    if not tokens or not tokens.get("app_token") or not tokens.get("bot_token"):
        raise HTTPException(
            status_code=400,
            detail="slack_tokens_not_configured: save an app_token and bot_token first",
        )
    cfg = config.load()
    cfg["enabled"] = True
    config.save(cfg)
    return {"enabled": True}


@router.post("/disable")
def disable() -> dict[str, Any]:
    cfg = config.load()
    cfg["enabled"] = False
    config.save(cfg)
    return {"enabled": False}


@router.post("/link/approve")
def approve_link(body: LinkApproveRequest) -> dict[str, Any]:
    """Owner action: approve a pending "link this channel to a project"
    request, binding the channel. 404 if the link id is unknown, 409 if it
    was already resolved (approved or denied) -- a terminal link can never
    be re-approved, matching ``linking.approve_link``'s own guard."""
    try:
        return linking.approve_link(body.link_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="link_not_found") from None
    except linking.LinkingError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc


__all__ = ["router"]
