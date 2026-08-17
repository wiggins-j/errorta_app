"""Task 3 — Slack channel provisioning for the Slack studio manager.

Creates a project-bound Slack channel (``conversations_create``), invites
the studio's users into it (best-effort), and sets a topic from the
project's purpose (best-effort, cosmetic).

``web_client`` is injected by the caller (a real ``slack_sdk.WebClient``
in production, a fake in tests) — this module never constructs one itself
and, per this package's optionality discipline (see
``errorta_slack/__init__.py``), MUST NOT import ``slack_sdk`` at module
load time. ``SlackApiError`` is recognized by duck-typing
(``getattr(exc, "response", {})``) rather than importing the exception
class, so this module works against any fake that raises an
exception shaped like one.
"""
from __future__ import annotations

import re
from typing import Any

_SLUG_RUN = re.compile(r"[^a-z0-9_-]+")
_DASH_RUN = re.compile(r"-{2,}")
_MAX_NAME_LEN = 80
_MAX_TOPIC_LEN = 250
_MAX_NAME_TAKEN_RETRIES = 9  # base name, then suffixes -2 .. -9


class ProvisioningError(Exception):
    """Raised when channel creation fails for a reason the caller must
    surface to the user (e.g. missing OAuth scope, invalid name) — as
    opposed to ``name_taken``, which this module retries internally."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def derive_channel_name(title: str) -> str:
    """Slack channel names: lowercase, ``[a-z0-9_-]``, <=80 chars.

    Any run of characters outside that set collapses to a single ``-``;
    leading/trailing and repeated dashes are stripped/collapsed. An empty
    result (e.g. from an all-punctuation or blank title) falls back to
    ``"proj"``.
    """
    slug = _SLUG_RUN.sub("-", title.lower())
    slug = _DASH_RUN.sub("-", slug).strip("-")
    slug = slug[:_MAX_NAME_LEN].strip("-")
    return slug or "proj"


def _is_slack_api_error(exc: Exception) -> dict[str, Any] | None:
    """Duck-types a ``slack_sdk.errors.SlackApiError``-shaped exception.

    Returns its ``.response`` dict (or ``{}`` if the response has no
    usable mapping) when ``exc`` looks like one, else ``None``."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        return dict(response)
    except (TypeError, ValueError):
        # Some SDK response objects are mapping-like but not directly
        # dict()-able; fall back to attribute/getitem access via a thin
        # wrapper so callers can still do response.get("error").
        try:
            return {"error": response["error"]}  # type: ignore[index]
        except Exception:
            return {}


def _slack_error_code(exc: Exception) -> str | None:
    response = _is_slack_api_error(exc)
    if response is None:
        return None
    return response.get("error")


def _suffixed_name(base: str, n: int) -> str:
    suffix = f"-{n}"
    trimmed = base[: _MAX_NAME_LEN - len(suffix)].rstrip("-")
    return f"{trimmed}{suffix}"


def _create_channel_with_retry(web_client: Any, base_name: str) -> dict[str, Any]:
    """Calls ``conversations_create``, retrying with a numeric suffix on
    ``name_taken`` (bounded). Any other failure — including exhausting the
    retries — is wrapped in ``ProvisioningError``."""
    name = base_name
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_NAME_TAKEN_RETRIES + 1):
        try:
            return web_client.conversations_create(name=name, is_private=False)
        except Exception as exc:  # noqa: BLE001 - duck-typed Slack error
            code = _slack_error_code(exc)
            if code is None:
                # Not a Slack-API-shaped error at all; don't mask it as a
                # provisioning failure with a fabricated code.
                raise
            if code != "name_taken":
                raise ProvisioningError(code, f"conversations_create failed: {code}") from exc
            last_exc = exc
            name = _suffixed_name(base_name, attempt + 1)
    # Exhausted all bounded retries and it's still taken.
    code = _slack_error_code(last_exc) if last_exc else "name_taken"
    raise ProvisioningError(
        code or "name_taken", "conversations_create: name_taken (retries exhausted)"
    ) from last_exc


def create_project_channel(
    web_client: Any,
    *,
    title: str,
    invite_user_ids: list[str],
    purpose: str = "",
) -> dict[str, Any]:
    """Creates a project channel, invites users, and sets a topic.

    Returns ``{"channel_id": str, "name": str}``. Channel creation errors
    other than ``name_taken`` raise ``ProvisioningError`` (with ``.code``
    set to the Slack error code) so the caller can give the user a clear
    message. Invite and topic-setting are both best-effort: failures there
    never prevent returning the created channel.
    """
    base_name = derive_channel_name(title)
    resp = _create_channel_with_retry(web_client, base_name)
    channel = resp["channel"]
    channel_id = channel["id"]
    name = channel["name"]

    if invite_user_ids:
        try:
            web_client.conversations_invite(channel=channel_id, users=invite_user_ids)
        except Exception as exc:  # noqa: BLE001 - best-effort invite
            code = _slack_error_code(exc)
            if code not in ("already_in_channel", "cant_invite_self"):
                raise

    if purpose:
        try:
            web_client.conversations_setTopic(channel=channel_id, topic=purpose[:_MAX_TOPIC_LEN])
        except Exception:  # noqa: BLE001 - cosmetic, always swallowed
            pass

    return {"channel_id": channel_id, "name": name}
