"""Consent-gated Service API pairing state machine."""

from __future__ import annotations

import datetime as _dt
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from errorta_app.paths import auth_tokens_path

from . import audit, store
from .ratelimit import RateLimited, pairing_limiter

PAIRING_STORE_VERSION = 1
DEFAULT_TTL_SECONDS = 300
DELIVERY_TTL_SECONDS = 120
MAX_PENDING = 3
VALID_SCOPES = {"prompt", "meta"}

_PENDING_TOKENS: dict[str, str] = {}


class PairingError(ValueError):
    def __init__(self, code: str, **meta: Any) -> None:
        super().__init__(code)
        self.code = code
        self.meta = meta


def _sessions_path() -> Path:
    path = auth_tokens_path().with_name("service-auth-pairing-sessions.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now_dt() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _to_wire(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _from_wire(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        _dt.timezone.utc,
    )


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".service-auth-pairing-",
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


def _normalize_strings(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalize_scopes(values: list[str] | None) -> list[str]:
    scopes = _normalize_strings(values)
    if not scopes:
        return ["prompt", "meta"]
    unknown = [scope for scope in scopes if scope not in VALID_SCOPES]
    if unknown:
        raise PairingError("scope_unsupported", scope=unknown[0])
    return scopes


def _effective_state(session: dict[str, Any], now: _dt.datetime) -> str:
    state = str(session.get("state") or "pending")
    if state in {"accepted", "consumed", "denied", "expired"}:
        return state
    try:
        if _from_wire(str(session["expires_at"])) <= now:
            return "expired"
    except Exception:
        return "expired"
    return state


def _session_matches(session: dict[str, Any], session_id: str) -> bool:
    stored = str(session.get("session_id") or "")
    return bool(stored) and hmac.compare_digest(stored, session_id)


def load_sessions() -> list[dict[str, Any]]:
    try:
        raw = json.loads(_sessions_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    items = raw.get("sessions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def save_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _write_atomic(
        _sessions_path(),
        {"format_version": PAIRING_STORE_VERSION, "sessions": sessions},
    )
    return sessions


def reset_state_for_tests() -> None:
    _PENDING_TOKENS.clear()
    pairing_limiter.reset()


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "status": _effective_state(session, _now_dt()),
        "app_slug": session.get("app_slug"),
        "app_name": session.get("app_name"),
        "requested_corpora": list(session.get("requested_corpora") or []),
        "requested_scopes": list(session.get("requested_scopes") or []),
        "approved_corpora": list(session.get("approved_corpora") or []),
        "approved_scopes": list(session.get("approved_scopes") or []),
        "created_at": session.get("created_at"),
        "expires_at": session.get("expires_at"),
        "issued_at": session.get("issued_at"),
        "denied_at": session.get("denied_at"),
        "consumed_at": session.get("consumed_at"),
        "delivery_expires_at": session.get("delivery_expires_at"),
        "token_delivery_state": session.get("token_delivery_state"),
        "token_id": session.get("token_id"),
    }


def list_public_sessions() -> list[dict[str, Any]]:
    return [public_session(session) for session in load_sessions()]


def start_pairing(
    *,
    app_slug: str,
    app_name: str,
    requested_corpora: list[str],
    requested_scopes: list[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    source: str = "unknown",
) -> dict[str, Any]:
    try:
        pairing_limiter.check(source)
    except RateLimited as exc:
        raise PairingError("pairing_rate_limited", retry_after=exc.retry_after_seconds) from exc
    try:
        session = _start_pairing_unchecked(
            app_slug=app_slug,
            app_name=app_name,
            requested_corpora=requested_corpora,
            requested_scopes=requested_scopes,
            ttl_seconds=ttl_seconds,
        )
    except PairingError:
        pairing_limiter.record_failure(source)
        raise
    pairing_limiter.record_success(source)
    return session


def _start_pairing_unchecked(
    *,
    app_slug: str,
    app_name: str,
    requested_corpora: list[str],
    requested_scopes: list[str],
    ttl_seconds: int,
) -> dict[str, Any]:
    slug = app_slug.strip()
    name = app_name.strip()
    if not slug or not name:
        raise PairingError("app_identity_required")
    corpora = _normalize_strings(requested_corpora)
    scopes = _normalize_scopes(requested_scopes)
    sessions = load_sessions()
    now = _now_dt()
    pending = sum(1 for item in sessions if _effective_state(item, now) == "pending")
    if pending >= MAX_PENDING:
        raise PairingError("pairing_too_many_pending")
    expires_at = now + _dt.timedelta(seconds=max(30, min(ttl_seconds, DEFAULT_TTL_SECONDS)))
    session = {
        "session_id": "pair_" + secrets.token_urlsafe(16),
        "app_slug": slug,
        "app_name": name,
        "requested_corpora": corpora,
        "requested_scopes": scopes,
        "approved_corpora": [],
        "approved_scopes": [],
        "state": "pending",
        "created_at": _to_wire(now),
        "expires_at": _to_wire(expires_at),
        "issued_at": None,
        "denied_at": None,
        "consumed_at": None,
        "delivery_expires_at": None,
        "token_delivery_state": None,
        "token_id": None,
    }
    sessions.append(session)
    save_sessions(sessions)
    return public_session(session)


def _find_session(sessions: list[dict[str, Any]], session_id: str) -> int | None:
    for idx, session in enumerate(sessions):
        if _session_matches(session, session_id):
            return idx
    return None


def approve_pairing(
    session_id: str,
    *,
    corpora: list[str] | None = None,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    sessions = load_sessions()
    idx = _find_session(sessions, session_id)
    if idx is None:
        raise KeyError(session_id)
    now = _now_dt()
    session = sessions[idx]
    state = _effective_state(session, now)
    if state == "expired":
        session = dict(session)
        session["state"] = "expired"
        sessions[idx] = session
        save_sessions(sessions)
        raise PairingError("pairing_expired")
    if state != "pending":
        raise PairingError("pairing_not_pending")

    requested_corpora = _normalize_strings(session.get("requested_corpora") or [])
    requested_scopes = _normalize_scopes(session.get("requested_scopes") or [])
    approved_corpora = _normalize_strings(corpora) if corpora is not None else requested_corpora
    approved_scopes = _normalize_scopes(scopes) if scopes is not None else requested_scopes
    for corpus in approved_corpora:
        if corpus not in requested_corpora:
            raise PairingError("corpus_not_requested", corpus=corpus)
    for scope in approved_scopes:
        if scope not in requested_scopes:
            raise PairingError("scope_not_requested", scope=scope)

    raw_token = store.mint_token()
    issued_at = _to_wire(now)
    delivery_expires_at = _to_wire(
        min(
            _from_wire(str(session["expires_at"])),
            now + _dt.timedelta(seconds=DELIVERY_TTL_SECONDS),
        )
    )
    token_record = store.create_token(
        raw_token=raw_token,
        app_slug=str(session["app_slug"]),
        app_name=str(session["app_name"]),
        corpora=approved_corpora,
        scopes=approved_scopes,
        issued_at=issued_at,
    )
    updated = dict(session)
    updated["state"] = "accepted"
    updated["approved_corpora"] = approved_corpora
    updated["approved_scopes"] = approved_scopes
    updated["issued_at"] = issued_at
    updated["delivery_expires_at"] = delivery_expires_at
    updated["token_delivery_state"] = "pending_delivery"
    updated["token_id"] = token_record["id"]
    sessions[idx] = updated
    save_sessions(sessions)
    _PENDING_TOKENS[session_id] = raw_token
    audit.record_event(
        "pair.accepted",
        app_slug=updated["app_slug"],
        corpora=approved_corpora,
        scopes=approved_scopes,
        token_id=token_record["id"],
    )
    return public_session(updated)


def deny_pairing(session_id: str) -> dict[str, Any]:
    sessions = load_sessions()
    idx = _find_session(sessions, session_id)
    if idx is None:
        raise KeyError(session_id)
    updated = dict(sessions[idx])
    updated["state"] = "denied"
    updated["denied_at"] = updated.get("denied_at") or _to_wire(_now_dt())
    sessions[idx] = updated
    save_sessions(sessions)
    _PENDING_TOKENS.pop(session_id, None)
    return public_session(updated)


def poll_status(session_id: str, *, source: str = "unknown") -> dict[str, Any]:
    try:
        pairing_limiter.check(source)
    except RateLimited as exc:
        raise PairingError("pairing_rate_limited", retry_after=exc.retry_after_seconds) from exc
    sessions = load_sessions()
    idx = _find_session(sessions, session_id)
    if idx is None:
        pairing_limiter.record_failure(source)
        raise KeyError(session_id)
    session = sessions[idx]
    now = _now_dt()
    state = _effective_state(session, now)
    if state == "expired" and session.get("state") != "expired":
        session = dict(session)
        session["state"] = "expired"
        sessions[idx] = session
        save_sessions(sessions)
    if state != "accepted":
        return {"session_id": session_id, "status": state, "expires_at": session.get("expires_at")}

    token = _PENDING_TOKENS.pop(session_id, None)
    updated = dict(session)
    updated["state"] = "consumed"
    updated["consumed_at"] = _to_wire(now)
    try:
        delivery_expired = _from_wire(str(session.get("delivery_expires_at"))) <= now
    except Exception:
        delivery_expired = False
    token_lost = token is None or delivery_expired
    updated["token_delivery_state"] = "lost" if token_lost else "delivered"
    sessions[idx] = updated
    save_sessions(sessions)
    pairing_limiter.record_success(source)
    body = {
        "session_id": session_id,
        "status": "accepted",
        "corpora": list(session.get("approved_corpora") or []),
        "scopes": list(session.get("approved_scopes") or []),
        "issued_at": session.get("issued_at"),
    }
    if token_lost:
        body["token_lost"] = True
    else:
        body["token"] = token
    return body


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "DELIVERY_TTL_SECONDS",
    "MAX_PENDING",
    "PairingError",
    "VALID_SCOPES",
    "approve_pairing",
    "deny_pairing",
    "list_public_sessions",
    "load_sessions",
    "poll_status",
    "public_session",
    "reset_state_for_tests",
    "save_sessions",
    "start_pairing",
]
