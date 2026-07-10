"""Mobile handoff inbox for shared text, URLs, and dictated prompts."""
from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Literal

from . import config as mobile_config

INBOX_STORE_VERSION = 1
MAX_URL_LENGTH = 4096
MAX_TEXT_LENGTH = 20_000
MAX_TITLE_LENGTH = 300
InboxKind = Literal["url", "text"]
InboxStatus = Literal["pending", "archived"]


class InboxError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def inbox_path() -> Path:
    return mobile_config.mobile_dir() / "inbox-items.json"


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".inbox-items-",
        suffix=".json",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def load() -> list[dict[str, Any]]:
    try:
        raw = json.loads(inbox_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return [dict(item) for item in raw["items"] if isinstance(item, dict)]
    return []


def save(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _write_atomic(inbox_path(), {"format_version": INBOX_STORE_VERSION, "items": items})
    return items


def create(
    *,
    device_id: str,
    kind: str,
    text: str,
    title: str | None = None,
    source_app: str | None = None,
) -> dict[str, Any]:
    normalized_kind = _normalize_kind(kind)
    cleaned_text = text.strip()
    cleaned_title = (title or "").strip()
    if not cleaned_text:
        raise InboxError("mobile_inbox_text_required")
    if normalized_kind == "url" and len(cleaned_text) > MAX_URL_LENGTH:
        raise InboxError("mobile_inbox_url_too_large")
    if normalized_kind == "text" and len(cleaned_text) > MAX_TEXT_LENGTH:
        raise InboxError("mobile_inbox_text_too_large")
    if len(cleaned_title) > MAX_TITLE_LENGTH:
        raise InboxError("mobile_inbox_title_too_large")
    record = {
        "inbox_item_id": f"mob_inbox_{secrets.token_urlsafe(16)}",
        "device_id": device_id,
        "kind": normalized_kind,
        "title": cleaned_title or None,
        "text": cleaned_text,
        "source_app": (source_app or "").strip() or None,
        "created_at": _now(),
        "status": "pending",
    }
    items = load()
    items.append(record)
    save(items)
    return record


def list_items(
    *,
    device_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in {"pending", "archived"}:
        raise InboxError("mobile_inbox_status_unknown")
    out = []
    for item in load():
        if device_id is not None and item.get("device_id") != device_id:
            continue
        if status is not None and item.get("status") != status:
            continue
        out.append(public_projection(item))
    out.sort(key=lambda item: (item.get("created_at") or "", item.get("inbox_item_id") or ""))
    return out


def get_item(*, device_id: str, inbox_item_id: str) -> dict[str, Any] | None:
    for item in load():
        if (
            item.get("device_id") == device_id
            and item.get("inbox_item_id") == inbox_item_id
        ):
            return dict(item)
    return None


def archive(*, device_id: str, inbox_item_id: str) -> dict[str, Any]:
    items = load()
    for idx, item in enumerate(items):
        if (
            item.get("device_id") == device_id
            and item.get("inbox_item_id") == inbox_item_id
        ):
            updated = dict(item)
            updated["status"] = "archived"
            updated["archived_at"] = _now()
            items[idx] = updated
            save(items)
            return updated
    raise InboxError("mobile_inbox_item_not_found")


def public_projection(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "inbox_item_id": record.get("inbox_item_id"),
        "device_id": record.get("device_id"),
        "kind": record.get("kind"),
        "title": record.get("title"),
        "text": record.get("text"),
        "source_app": record.get("source_app"),
        "created_at": record.get("created_at"),
        "status": record.get("status"),
    }


def _normalize_kind(kind: str) -> InboxKind:
    if kind not in {"url", "text"}:
        raise InboxError("mobile_inbox_kind_unknown")
    return kind  # type: ignore[return-value]


__all__ = [
    "INBOX_STORE_VERSION",
    "InboxError",
    "InboxKind",
    "InboxStatus",
    "MAX_TEXT_LENGTH",
    "MAX_TITLE_LENGTH",
    "MAX_URL_LENGTH",
    "archive",
    "create",
    "get_item",
    "inbox_path",
    "list_items",
    "load",
    "public_projection",
    "save",
]
