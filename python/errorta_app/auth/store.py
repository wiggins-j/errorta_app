"""Persistent hash-only token store for the Service API."""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from errorta_app.paths import auth_tokens_path, revoked_tokens_path

STORE_VERSION = 1
TOKEN_PREFIX = "ert_"
TOKEN_HEX_BYTES = 16
DEFAULT_SCOPES = ("prompt", "meta")

_REVOKED_CACHE: set[str] | None = None


class AuthTokenError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_HEX_BYTES)


def mint_token_id() -> str:
    return "tok_" + secrets.token_hex(8)


def _write_atomic(path: Path, payload: dict[str, Any], *, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=prefix,
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
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _normalize_strings(values: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalize_scopes(values: list[str] | tuple[str, ...] | None) -> list[str]:
    scopes = _normalize_strings(values)
    return scopes or list(DEFAULT_SCOPES)


def _normalize_token_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    token_id = str(raw.get("id") or "").strip()
    digest = str(raw.get("token_sha256") or "").strip()
    app_slug = str(raw.get("app_slug") or "").strip()
    app_name = str(raw.get("app_name") or "").strip()
    if not token_id or not digest or not app_slug or not app_name:
        return None
    return {
        "id": token_id,
        "token_sha256": digest,
        "app_slug": app_slug,
        "app_name": app_name,
        "corpora": _normalize_strings(raw.get("corpora") or []),
        "scopes": _normalize_scopes(raw.get("scopes") or []),
        "issued_at": str(raw.get("issued_at") or now_iso()),
        "last_used_at": raw.get("last_used_at") if raw.get("last_used_at") else None,
    }


def load_tokens() -> list[dict[str, Any]]:
    path = auth_tokens_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    items = raw.get("tokens") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        record = _normalize_token_record(item)
        if record is not None:
            out.append(record)
    return out


def save_tokens(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in records:
        record = _normalize_token_record(item)
        if record is not None:
            normalized.append(record)
    _write_atomic(
        auth_tokens_path(),
        {"format_version": STORE_VERSION, "tokens": normalized},
        prefix=".tokens-",
    )
    return normalized


def load_revoked_ids(*, force: bool = False) -> set[str]:
    global _REVOKED_CACHE
    if _REVOKED_CACHE is not None and not force:
        return set(_REVOKED_CACHE)
    path = revoked_tokens_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        _REVOKED_CACHE = set()
        return set()
    items = raw.get("revoked") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        _REVOKED_CACHE = set()
        return set()
    _REVOKED_CACHE = {str(item).strip() for item in items if str(item).strip()}
    return set(_REVOKED_CACHE)


def save_revoked_ids(revoked: set[str]) -> set[str]:
    global _REVOKED_CACHE
    normalized = {str(item).strip() for item in revoked if str(item).strip()}
    _write_atomic(
        revoked_tokens_path(),
        {"format_version": STORE_VERSION, "revoked": sorted(normalized)},
        prefix=".revoked-tokens-",
    )
    _REVOKED_CACHE = set(normalized)
    return set(normalized)


def reset_state_for_tests() -> None:
    global _REVOKED_CACHE
    _REVOKED_CACHE = None


def public_projection(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "app_slug": record.get("app_slug"),
        "app_name": record.get("app_name"),
        "corpora": list(record.get("corpora") or []),
        "scopes": list(record.get("scopes") or []),
        "issued_at": record.get("issued_at"),
        "last_used_at": record.get("last_used_at"),
    }


def list_public_tokens() -> list[dict[str, Any]]:
    revoked = load_revoked_ids()
    return [
        public_projection(record)
        for record in load_tokens()
        if str(record.get("id") or "") not in revoked
    ]


def create_token(
    *,
    raw_token: str,
    app_slug: str,
    app_name: str,
    corpora: list[str],
    scopes: list[str] | None = None,
    issued_at: str | None = None,
    token_id: str | None = None,
) -> dict[str, Any]:
    if not app_slug.strip() or not app_name.strip():
        raise AuthTokenError("app_identity_required")
    record = {
        "id": token_id or mint_token_id(),
        "token_sha256": token_hash(raw_token),
        "app_slug": app_slug.strip(),
        "app_name": app_name.strip(),
        "corpora": _normalize_strings(corpora),
        "scopes": _normalize_scopes(scopes),
        "issued_at": issued_at or now_iso(),
        "last_used_at": None,
    }
    tokens = load_tokens()
    tokens.append(record)
    save_tokens(tokens)
    return record


def find_by_token(raw_token: str) -> dict[str, Any] | None:
    digest = token_hash(raw_token)
    revoked = load_revoked_ids()
    for record in load_tokens():
        stored = str(record.get("token_sha256") or "")
        if record.get("id") in revoked:
            continue
        if stored and hmac.compare_digest(stored, digest):
            return record
    return None


def update_last_used(token_id: str, *, used_at: str | None = None) -> dict[str, Any] | None:
    tokens = load_tokens()
    for idx, record in enumerate(tokens):
        if record.get("id") == token_id:
            updated = dict(record)
            updated["last_used_at"] = used_at or now_iso()
            tokens[idx] = updated
            save_tokens(tokens)
            return updated
    return None


def revoke_token(token_id: str) -> dict[str, Any]:
    records = load_tokens()
    if not any(record.get("id") == token_id for record in records):
        raise KeyError(token_id)
    revoked = load_revoked_ids()
    revoked.add(token_id)
    save_revoked_ids(revoked)
    record = next(record for record in records if record.get("id") == token_id)
    return public_projection(record)
