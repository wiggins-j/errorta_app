"""Owner-confirmation "link this channel to a project" state machine.

A non-owner Slack user can *request* that a channel be linked to a
project; the link only takes effect once an owner *approves* it. Pending
and resolved link requests are persisted in ``links.json`` under
``config.slack_dir()``, written with the same atomic tmp+rename + 0600
discipline as ``errorta_slack.store``.

This module MUST NOT import ``slack_sdk`` or any other optional dependency
at module load time — the Slack bridge is strictly optional and disabled by
default, so the rest of the sidecar must keep working when ``slack-sdk``
isn't installed.

States: ``awaiting_owner`` -> ``approved`` | ``denied``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from errorta_slack import config, store


def _links_path() -> Path:
    return config.slack_dir() / "links.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return raw


def _write_json(path: Path, data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path`` with mode 0600.

    mkstemp in the same dir -> write -> chmod 0600 -> os.replace, mirroring
    ``errorta_slack.store``'s ``_write_json``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_links() -> dict[str, dict[str, Any]]:
    raw = _read_json(_links_path(), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def request_link(
    channel_id: str, project_id: str, requester_user_id: str, *, now: float | None = None
) -> str:
    """Create a pending link request and return its ``link_id``.

    The record starts in state ``"awaiting_owner"`` and is not bound to
    anything until an owner calls ``approve_link``. Ids are generated with
    ``uuid4().hex`` (unguessable), matching ``store.stage_confirmation``.
    """
    link_id = uuid.uuid4().hex
    record = {
        "id": link_id,
        "channel_id": channel_id,
        "project_id": project_id,
        "requester_user_id": requester_user_id,
        "created_at": now if now is not None else time.time(),
        "state": "awaiting_owner",
    }
    links = _load_links()
    links[link_id] = record
    _write_json(_links_path(), links)
    return link_id


def approve_link(link_id: str) -> dict[str, Any]:
    """Owner action: approve a pending link request.

    Transitions the record to ``"approved"`` and binds the channel to the
    project via ``store.bind_channel``. Raises ``KeyError`` if ``link_id``
    is unknown.
    """
    links = _load_links()
    record = dict(links[link_id])
    record["state"] = "approved"
    links[link_id] = record
    _write_json(_links_path(), links)

    store.bind_channel(record["channel_id"], record["project_id"])

    return record


def deny_link(link_id: str) -> dict[str, Any]:
    """Owner action: deny a pending link request. No binding is created.

    Raises ``KeyError`` if ``link_id`` is unknown.
    """
    links = _load_links()
    record = dict(links[link_id])
    record["state"] = "denied"
    links[link_id] = record
    _write_json(_links_path(), links)
    return record


def status(link_id: str) -> dict[str, Any] | None:
    """Return the link record for ``link_id``, or ``None`` if unknown."""
    return _load_links().get(link_id)


__all__ = ["request_link", "approve_link", "deny_link", "status"]
