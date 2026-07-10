"""Load/save the canonical AIAR connection config plus legacy projections."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from errorta_app.paths import errorta_home

from .models import AiarRuntimeKind

VALID_KINDS: set[str] = {
    "local-aiar",
    "aiar-service",
    "errorta-sidecar-remote",
    "disconnected",
}


@dataclass(frozen=True)
class AiarConnectionConfig:
    kind: AiarRuntimeKind
    display_name: str | None = None
    base_url: str | None = None
    token: str | None = None
    timeout_s: float = 60.0
    verify_tls: bool = True
    preferred_model: str | None = None
    created_from: str | None = None
    updated_at: str | None = None
    schema_version: int = 1

    @property
    def configured(self) -> bool:
        if self.kind == "disconnected":
            return True
        if self.kind == "local-aiar":
            return True
        return bool((self.base_url or "").strip())

    def to_public_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["token"] = None
        out["token_configured"] = bool(self.token)
        return out


def config_path() -> Path:
    return errorta_home() / "aiar-connection.json"


def now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_url(value: str | None) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL")
    return text.rstrip("/")


def _from_dict(raw: dict[str, Any]) -> AiarConnectionConfig:
    kind = str(raw.get("kind") or "").strip()
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid AIAR runtime kind: {kind!r}")
    timeout = float(raw.get("timeout_s", 60.0) or 60.0)
    if timeout <= 0 or timeout > 600:
        raise ValueError("timeout_s must be in 0..600 seconds")
    token = raw.get("token")
    token_storage = raw.get("token_storage")
    if token is None and isinstance(token_storage, dict):
        # Spec-compatible placeholder: v0.8 stores inline-0600; future keychain
        # storage will resolve through this branch.
        inline = token_storage.get("token")
        token = inline if isinstance(inline, str) else None
    return AiarConnectionConfig(
        schema_version=int(raw.get("schema_version", 1) or 1),
        kind=kind,  # type: ignore[arg-type]
        display_name=_clean_str(raw.get("display_name")),
        base_url=_normalize_url(raw.get("base_url")),
        token=_clean_str(token),
        timeout_s=timeout,
        verify_tls=bool(raw.get("verify_tls", raw.get("verify", True))),
        preferred_model=_clean_str(raw.get("preferred_model")),
        created_from=_clean_str(raw.get("created_from")),
        updated_at=_clean_str(raw.get("updated_at")),
    )


def load_canonical() -> AiarConnectionConfig | None:
    p = config_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return _from_dict(raw)
    except (TypeError, ValueError):
        return None


def save_canonical(config: AiarConnectionConfig) -> AiarConnectionConfig:
    cfg = AiarConnectionConfig(
        schema_version=config.schema_version,
        kind=config.kind,
        display_name=config.display_name,
        base_url=_normalize_url(config.base_url),
        token=config.token,
        timeout_s=config.timeout_s,
        verify_tls=config.verify_tls,
        preferred_model=config.preferred_model,
        created_from=config.created_from,
        updated_at=now_iso_z(),
    )
    if not cfg.configured:
        raise ValueError("AIAR connection config is incomplete")
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(cfg)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".aiar-connection-", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if os.name == "posix":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return cfg
