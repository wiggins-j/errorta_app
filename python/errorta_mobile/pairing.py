"""Desktop-initiated mobile pairing sessions.

F065 state machine (owner-confirmation):

    start_pairing (desktop)   -> awaiting_device
    complete_pairing (phone)  -> awaiting_approval  (device DRAFT only; no token/device yet)
    approve_pairing (desktop) -> approved           (token minted; device created READ-ONLY)
    poll_status (phone)       -> consumed           (token delivered ONCE)
    deny_pairing (desktop)    -> denied
    cancel_pairing (desktop)  -> cancelled
    (TTL elapsed)             -> treated as expired

The session token is minted ONLY at approval — the secret never exists before
the human consents — and is delivered exactly once, to a poller that presents
the matching pairing token (constant-time compared). Pairing endpoints reachable
from the LAN are rate-limited + capped (ratelimit.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any

from . import config as mobile_config
from . import devices
from .ratelimit import RateLimited, RateLimiter, pairing_limiter

PAIRING_SCHEMA = "errorta.mobile_pairing.v1"
PAIRING_STORE_VERSION = 1
DEFAULT_TTL_SECONDS = 300
MAX_PENDING_APPROVALS = 3  # cap outstanding awaiting_approval sessions (DoS guard)
MAX_PIN_ATTEMPTS = 5
verify_pin_limiter = RateLimiter(max_failures=MAX_PIN_ATTEMPTS)


class PairingError(ValueError):
    def __init__(self, code: str, **meta: Any) -> None:
        super().__init__(code)
        self.code = code
        self.meta = meta


def _now_dt() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _to_wire(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _from_wire(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        _dt.timezone.utc
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pin_hash(salt: str, pin: str) -> str:
    return _sha256(f"{salt}{pin}")


def pairing_sessions_path() -> Path:
    return mobile_config.mobile_dir() / "pairing-sessions.json"


def connector_id_path() -> Path:
    return mobile_config.mobile_dir() / "connector-id"


def connector_id() -> str:
    path = connector_id_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value.startswith("mobconn_"):
            return value
    value = f"mobconn_{secrets.token_urlsafe(16)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return value


def current_cert_fingerprint() -> str | None:
    """DER SHA-256 of the LAN listener's TLS cert (the bytes iOS pins), or None
    if no cert exists. No dev placeholder — pairing refuses without a real cert
    when TLS is required."""
    from . import tls as mobile_tls

    cert_path = mobile_config.mobile_dir() / "tls" / mobile_tls.CERT_NAME
    if not cert_path.exists():
        return None
    try:
        return mobile_tls.cert_der_sha256(cert_path)
    except Exception:
        return None


def tls_cert_sha256() -> str | None:
    """Backwards-compatible alias."""
    return current_cert_fingerprint()


def _host_candidates(cfg: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    mode = str(cfg.get("bind_mode") or "disabled")
    explicit_host = cfg.get("explicit_host")
    if explicit_host:
        out.append({"kind": "explicit_host", "host": str(explicit_host)})
    if mode == "loopback_dev":
        out.append({"kind": "loopback_dev", "host": "127.0.0.1"})
    for network in cfg.get("allowed_networks") or []:
        if network == "lan" and mode in {"lan", "loopback_dev"}:
            # Advertise the exact IP the LAN listener is bound to (and that the
            # TLS cert's SAN covers) — NOT socket.gethostname(), which yields a
            # `*.local` mDNS name the phone may not resolve. The client connects
            # on the payload's top-level `port`.
            lan_addr = cfg.get("lan_bind_address")
            if lan_addr:
                host = str(lan_addr)
            else:
                try:
                    host = socket.gethostname()
                except OSError:
                    host = "localhost"
            out.append({"kind": "lan", "host": host})
        if network == "tailscale":
            # F071 — advertise the REAL Tailscale IPv4 (the listener binds it and
            # the cert SAN covers it), not the old "tailscale" placeholder. Shown
            # when a Tailscale listener is bound (`also_tailscale`) or in
            # tailscale bind-mode. The phone prefers this host off-LAN and falls
            # back to LAN at home (DesktopRecord.orderedHosts).
            ts = cfg.get("tailscale_bind_address")
            if ts and (cfg.get("also_tailscale") or mode == "tailscale"):
                out.append({"kind": "tailscale", "host": str(ts)})
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in out:
        key = (item["kind"], item["host"])
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return deduped


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".pairing-sessions-",
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


def load_sessions() -> list[dict[str, Any]]:
    path = pairing_sessions_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict) and isinstance(raw.get("sessions"), list):
        return [dict(item) for item in raw["sessions"] if isinstance(item, dict)]
    return []


def save_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _write_atomic(
        pairing_sessions_path(),
        {"format_version": PAIRING_STORE_VERSION, "sessions": sessions},
    )
    return sessions


def _is_expired(session: dict[str, Any], now: _dt.datetime) -> bool:
    try:
        return _from_wire(str(session["expires_at"])) <= now
    except Exception:
        return True


def _effective_state(session: dict[str, Any], now: _dt.datetime) -> str:
    """The session's state, treating an elapsed TTL as expired (unless already
    terminal)."""
    state = str(session.get("state") or "awaiting_device")
    if state in {"approved", "consumed", "denied", "cancelled"}:
        return state
    if _is_expired(session, now):
        return "expired"
    return state


def start_pairing(
    *,
    desktop_name: str = "Errorta Desktop",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    cfg = mobile_config.load()
    if not cfg.get("enabled") or cfg.get("bind_mode") == "disabled":
        raise PairingError("mobile_connector_disabled")
    if not cfg.get("pairing_enabled"):
        raise PairingError("mobile_pairing_disabled")
    fingerprint = current_cert_fingerprint()
    if cfg.get("require_tls") and not fingerprint:
        # Real-TLS-or-refuse: no usable cert ⇒ no pairing.
        raise PairingError("mobile_tls_unavailable")
    now = _now_dt()
    expires_at = now + _dt.timedelta(
        seconds=max(30, min(ttl_seconds, DEFAULT_TTL_SECONDS))
    )
    token = secrets.token_urlsafe(32)
    clear_pin: str | None = None
    pin_required = bool(cfg.get("pairing_pin_required"))
    pin_salt: str | None = None
    pin_sha256: str | None = None
    if pin_required:
        clear_pin = f"{secrets.randbelow(1_000_000):06d}"
        pin_salt = secrets.token_hex(16)
        pin_sha256 = _pin_hash(pin_salt, clear_pin)
    session = {
        "session_id": f"mobpair_{secrets.token_urlsafe(12)}",
        "connector_id": connector_id(),
        "desktop_name": desktop_name.strip() or "Errorta Desktop",
        "hosts": _host_candidates(cfg),
        "port": cfg["port"],
        "tls_cert_sha256": fingerprint,
        "pairing_token_sha256": _sha256(token),
        "pin_required": pin_required,
        "pin_salt": pin_salt,
        "pin_sha256": pin_sha256,
        "pin_attempts": 0,
        "state": "awaiting_device",
        "created_at": _to_wire(now),
        "expires_at": _to_wire(expires_at),
        "device_draft": None,
        "device_id": None,
        "session_token_sha256": None,
        "used_at": None,
        "approved_at": None,
        "denied_at": None,
        "cancelled_at": None,
    }
    sessions = load_sessions()
    sessions.append(session)
    save_sessions(sessions)
    payload = {
        "schema": PAIRING_SCHEMA,
        "connector_id": session["connector_id"],
        "desktop_name": session["desktop_name"],
        "hosts": session["hosts"],
        "port": session["port"],
        "tls_cert_sha256": session["tls_cert_sha256"],
        "pairing_token": token,
        "expires_at": session["expires_at"],
    }
    result = {
        "session_id": session["session_id"],
        "expires_at": session["expires_at"],
        "pairing_payload": payload,
    }
    if clear_pin is not None:
        result["pin"] = clear_pin
    return result


def cancel_pairing(session_id: str) -> dict[str, Any]:
    sessions = load_sessions()
    for idx, session in enumerate(sessions):
        if session.get("session_id") == session_id:
            updated = dict(session)
            updated["state"] = "cancelled"
            updated["cancelled_at"] = updated.get("cancelled_at") or _to_wire(_now_dt())
            sessions[idx] = updated
            save_sessions(sessions)
            return public_session(updated)
    raise KeyError(session_id)


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "connector_id": session.get("connector_id"),
        "desktop_name": session.get("desktop_name"),
        "state": _effective_state(session, _now_dt()),
        "hosts": list(session.get("hosts") or []),
        "port": session.get("port"),
        "tls_cert_sha256": session.get("tls_cert_sha256"),
        # The device DRAFT a phone submitted (so the desktop approval prompt can
        # show what it's approving) — never the session token.
        "device_draft": session.get("device_draft"),
        "device_id": session.get("device_id"),
        "requires_pin": bool(session.get("pin_required")),
        "pin_attempts_remaining": max(
            0,
            MAX_PIN_ATTEMPTS - int(session.get("pin_attempts") or 0),
        ),
        "created_at": session.get("created_at"),
        "expires_at": session.get("expires_at"),
        "approved_at": session.get("approved_at"),
        "denied_at": session.get("denied_at"),
        "cancelled_at": session.get("cancelled_at"),
    }


def list_public() -> list[dict[str, Any]]:
    return [public_session(session) for session in load_sessions()]


def get_public(session_id: str) -> dict[str, Any]:
    for session in load_sessions():
        if session.get("session_id") == session_id:
            return public_session(session)
    raise KeyError(session_id)


def _find_by_token(sessions: list[dict[str, Any]], pairing_token: str) -> int | None:
    """Index of the session whose pairing token matches (constant-time), or None."""
    token_hash = _sha256(pairing_token)
    for idx, session in enumerate(sessions):
        stored = str(session.get("pairing_token_sha256") or "")
        if stored and hmac.compare_digest(stored, token_hash):
            return idx
    return None


def complete_pairing(
    *,
    pairing_token: str,
    tls_cert_sha256_value: str,
    display_name: str,
    platform: str,
    public_key: str,
    source: str = "unknown",
) -> dict[str, Any]:
    """Phone presents the pairing token + cert fingerprint + its device info.
    Transitions the session to ``awaiting_approval`` and stores a device DRAFT.
    Does NOT create a device or mint a token — that happens only at desktop
    approval."""
    try:
        pairing_limiter.check(source)
    except RateLimited as exc:
        raise PairingError("pairing_rate_limited") from exc
    if not public_key.strip():
        raise PairingError("mobile_public_key_required")
    sessions = load_sessions()
    now = _now_dt()
    idx = _find_by_token(sessions, pairing_token)
    if idx is None:
        pairing_limiter.record_failure(source)
        raise PairingError("pairing_token_unknown")
    session = sessions[idx]
    state = _effective_state(session, now)
    if state == "cancelled":
        raise PairingError("pairing_token_cancelled")
    if state == "expired":
        raise PairingError("pairing_token_expired")
    if state != "awaiting_device":
        # Already submitted / approved / consumed.
        raise PairingError("pairing_token_used")
    expected_fp = session.get("tls_cert_sha256")
    # Enforce the pin only when the session actually has a cert fingerprint
    # (TLS in use). When None (loopback dev, no cert) there's nothing to pin.
    if expected_fp is not None and str(expected_fp) != str(tls_cert_sha256_value):
        pairing_limiter.record_failure(source)
        raise PairingError("pairing_tls_fingerprint_mismatch")
    # DoS guard: cap outstanding approvals so the desktop prompt can't be flooded.
    pending = sum(
        1 for s in sessions if _effective_state(s, now) == "awaiting_approval"
    )
    if pending >= MAX_PENDING_APPROVALS:
        raise PairingError("pairing_too_many_pending")
    updated = dict(session)
    updated["state"] = "awaiting_approval"
    updated["device_draft"] = {
        "display_name": display_name.strip() or "iPhone",
        "platform": platform.strip() or "ios",
        "public_key": public_key,
        "public_key_fingerprint": devices.public_key_fingerprint(public_key),
        "submitted_at": _to_wire(now),
    }
    sessions[idx] = updated
    save_sessions(sessions)
    pairing_limiter.record_success(source)
    return {
        "session_id": updated["session_id"],
        "state": "awaiting_approval",
        "requires_pin": bool(updated.get("pin_required")),
    }


def _mint_approved_session(
    *,
    session: dict[str, Any],
    session_token: str,
    now: _dt.datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    draft = session.get("device_draft") or {}
    record = devices.create(
        display_name=str(draft.get("display_name") or "iPhone"),
        platform=str(draft.get("platform") or "ios"),
        public_key=str(draft.get("public_key") or ""),
        session_token=session_token,
    )
    updated = dict(session)
    updated["state"] = "approved"
    updated["approved_at"] = _to_wire(now)
    updated["device_id"] = record["device_id"]
    updated["session_token_sha256"] = _sha256(session_token)
    return updated, record


def approve_pairing(session_id: str) -> dict[str, Any]:
    """Desktop confirms a device. Mints the session token (first time the secret
    exists) and creates the device record READ-ONLY. Returns the device + the
    capabilities granted (so the desktop prompt can show them)."""
    sessions = load_sessions()
    now = _now_dt()
    for idx, session in enumerate(sessions):
        if session.get("session_id") != session_id:
            continue
        state = _effective_state(session, now)
        if state == "expired":
            raise PairingError("pairing_token_expired")
        if state != "awaiting_approval":
            raise PairingError("pairing_not_awaiting_approval")
        if bool(session.get("pin_required")):
            raise PairingError("pairing_pin_required")
        session_token = secrets.token_urlsafe(32)
        updated, record = _mint_approved_session(
            session=session,
            session_token=session_token,
            now=now,
        )
        sessions[idx] = updated
        save_sessions(sessions)
        # The raw token is held in-memory only (delivered once on the phone's
        # next poll); only its sha256 is persisted above.
        _PENDING_TOKENS[session_id] = session_token
        return {
            "device": devices.public_projection(record),
            "capabilities": dict(record.get("capabilities") or {}),
        }
    raise KeyError(session_id)


def _record_pin_failure(
    sessions: list[dict[str, Any]],
    idx: int,
    limiter_key: str,
) -> None:
    session = dict(sessions[idx])
    attempts = int(session.get("pin_attempts") or 0) + 1
    session["pin_attempts"] = attempts
    verify_pin_limiter.record_failure(limiter_key)
    if attempts >= MAX_PIN_ATTEMPTS:
        session["state"] = "denied"
        session["denied_at"] = session.get("denied_at") or _to_wire(_now_dt())
        sessions[idx] = session
        save_sessions(sessions)
        _PENDING_TOKENS.pop(str(session.get("session_id") or ""), None)
        raise PairingError("pairing_pin_locked", attempts_remaining=0)
    sessions[idx] = session
    save_sessions(sessions)
    raise PairingError(
        "pairing_pin_mismatch",
        attempts_remaining=MAX_PIN_ATTEMPTS - attempts,
    )


def verify_pin(
    *,
    session_id: str,
    pairing_token: str,
    pin: str,
) -> dict[str, Any]:
    limiter_key = f"verify_pin:{session_id}"
    try:
        verify_pin_limiter.check(limiter_key)
    except RateLimited as exc:
        raise PairingError("pairing_pin_locked", attempts_remaining=0) from exc

    sessions = load_sessions()
    now = _now_dt()
    for idx, session in enumerate(sessions):
        if session.get("session_id") != session_id:
            continue
        state = _effective_state(session, now)
        if state == "expired":
            raise PairingError("pairing_token_expired")
        if state in {"approved", "consumed"}:
            return {"state": state}
        if state != "awaiting_approval":
            raise PairingError("pairing_not_awaiting_approval")
        if not bool(session.get("pin_required")):
            raise PairingError("pairing_pin_not_required")

        stored_token = str(session.get("pairing_token_sha256") or "")
        if not stored_token or not hmac.compare_digest(
            stored_token,
            _sha256(pairing_token),
        ):
            _record_pin_failure(sessions, idx, limiter_key)

        salt = str(session.get("pin_salt") or "")
        stored_pin = str(session.get("pin_sha256") or "")
        if not salt or not stored_pin or not hmac.compare_digest(
            stored_pin,
            _pin_hash(salt, pin),
        ):
            _record_pin_failure(sessions, idx, limiter_key)

        session_token = secrets.token_urlsafe(32)
        updated, _record = _mint_approved_session(
            session=session,
            session_token=session_token,
            now=now,
        )
        sessions[idx] = updated
        save_sessions(sessions)
        verify_pin_limiter.record_success(limiter_key)
        _PENDING_TOKENS[session_id] = session_token
        return {"state": "approved"}
    raise PairingError("pairing_session_not_found")


def deny_pairing(session_id: str) -> dict[str, Any]:
    sessions = load_sessions()
    for idx, session in enumerate(sessions):
        if session.get("session_id") == session_id:
            updated = dict(session)
            updated["state"] = "denied"
            updated["denied_at"] = updated.get("denied_at") or _to_wire(_now_dt())
            sessions[idx] = updated
            save_sessions(sessions)
            _PENDING_TOKENS.pop(session_id, None)
            return public_session(updated)
    raise KeyError(session_id)


# Session tokens minted at approval, held in-memory until the phone's next poll
# (delivered exactly once). Never persisted — only the sha256 is on disk.
_PENDING_TOKENS: dict[str, str] = {}


def poll_status(*, session_id: str, pairing_token: str, source: str = "unknown") -> dict[str, Any]:
    """Phone polls for its pairing outcome. On the first poll after approval,
    returns the session token ONCE (then the session is consumed). Requires the
    matching pairing token (constant-time)."""
    try:
        pairing_limiter.check(source)
    except RateLimited as exc:
        raise PairingError("pairing_rate_limited") from exc
    sessions = load_sessions()
    now = _now_dt()
    for idx, session in enumerate(sessions):
        if session.get("session_id") != session_id:
            continue
        stored = str(session.get("pairing_token_sha256") or "")
        if not stored or not hmac.compare_digest(stored, _sha256(pairing_token)):
            pairing_limiter.record_failure(source)
            raise PairingError("pairing_token_unknown")
        state = _effective_state(session, now)
        if state != "approved":
            return {"state": state}
        # Approved → deliver the token once.
        token = _PENDING_TOKENS.pop(session_id, None)
        updated = dict(session)
        updated["state"] = "consumed"
        updated["used_at"] = _to_wire(now)
        sessions[idx] = updated
        save_sessions(sessions)
        pairing_limiter.record_success(source)
        if token is None:
            # Token already delivered (double poll) or lost on restart.
            return {"state": "consumed", "session_token": None,
                    "device_id": session.get("device_id")}
        return {
            "state": "approved",
            "session_token": token,
            "device_id": session.get("device_id"),
        }
    raise PairingError("pairing_token_unknown")


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_PENDING_APPROVALS",
    "MAX_PIN_ATTEMPTS",
    "PAIRING_SCHEMA",
    "PairingError",
    "approve_pairing",
    "cancel_pairing",
    "complete_pairing",
    "connector_id",
    "connector_id_path",
    "current_cert_fingerprint",
    "deny_pairing",
    "get_public",
    "list_public",
    "load_sessions",
    "pairing_sessions_path",
    "poll_status",
    "public_session",
    "save_sessions",
    "start_pairing",
    "tls_cert_sha256",
    "verify_pin",
    "verify_pin_limiter",
]
