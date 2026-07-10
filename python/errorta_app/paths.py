"""Single source of truth for where Errorta keeps user data on disk.

Before F-INFRA-12, every module that needed to write somewhere
under ``~/.errorta`` rolled its own resolution. Three different
environment variables ended up in use (``ERRORTA_HOME``,
``ERRORTA_STATE_DIR``, ``ERRORTA_DATA_DIR``) and several modules
honored none of them at all. The result was that operators couldn't
reliably move Errorta's data off the primary disk — the obvious
operator complaint a user actually hit in a session that motivated
this work (dev Mac at 98% disk capacity because models + ChromaDB +
corpora all sat on the same volume).

This module is the consolidation: **one canonical env var
(``ERRORTA_HOME``), one helper (``errorta_home()``), and every
path under it derived from that one helper.** The two legacy env
vars (``ERRORTA_STATE_DIR``, ``ERRORTA_DATA_DIR``) are still read
for backward compatibility, but they emit a deprecation warning at
startup if they're set without ``ERRORTA_HOME`` also being set.

AIAR's storage (ChromaDB + grounding store + embedding model
cache) is **not** controlled from here. AIAR exposes its own env
vars (``AIAR_DB_PATH`` for the ChromaDB sqlite file; the
sentence-transformers cache obeys ``HF_HOME``). The
``docs/data-residency.md`` operator guide tells users how to move
both halves together.

Public API:
    errorta_home() -> Path                   # base dir, default ~/.errorta
    corpora_dir() -> Path                    # errorta_home() / "corpora"
    verdicts_log_path() -> Path              # judge verdict log
    hardware_json_path() -> Path             # cached F002 scan result
    ollama_settings_path() -> Path           # managed-Ollama state
    grounding_json_path() -> Path            # local stub grounding store
    grounding_embeddings_path() -> Path      # F024 embedding store
    data_residency_path() -> Path            # F-INFRA-12 residency config
    model_gateway_dir() -> Path              # F030 gateway state directory
    model_gateway_settings_path() -> Path    # F030 gateway settings
    model_gateway_audit_path() -> Path       # F030 gateway audit log
    model_gateway_budget_path() -> Path      # F030 gateway budget ledger

All helpers ``mkdir(parents=True, exist_ok=True)`` the parent dir
on demand, so callers don't have to.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Canonical env var. If set, every Errorta data path is rooted here.
_CANONICAL_ENV = "ERRORTA_HOME"

# Legacy env vars — still honored for backward compatibility, with a
# deprecation warning at first access. Order matters: the first one set
# wins, and ``ERRORTA_HOME`` always beats the legacy ones.
_LEGACY_ENVS = ("ERRORTA_STATE_DIR", "ERRORTA_DATA_DIR")

_warned: set[str] = set()


def errorta_home() -> Path:
    """Return the base directory Errorta writes data into.

    Resolution order:

    1. ``$ERRORTA_HOME`` if set and non-empty.
    2. The first non-empty legacy env var (``ERRORTA_STATE_DIR``,
       ``ERRORTA_DATA_DIR``) — once, with a one-time deprecation
       warning logged.
    3. ``~/.errorta`` (the default since v0.1).

    The directory is created if missing.
    """
    raw = os.environ.get(_CANONICAL_ENV, "").strip()
    if raw:
        base = Path(raw).expanduser()
    else:
        legacy = _read_legacy_with_warning()
        if legacy is not None:
            base = legacy
        else:
            base = Path.home() / ".errorta"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _read_legacy_with_warning() -> Optional[Path]:
    for name in _LEGACY_ENVS:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        if name not in _warned:
            log.warning(
                "%s is set but ERRORTA_HOME is not. %s is a legacy env "
                "var; ERRORTA_HOME is the canonical replacement and will "
                "win if both are set. Please migrate your launch scripts. "
                "(This warning fires once per process.)",
                name,
                name,
            )
            _warned.add(name)
        return Path(raw).expanduser()
    return None


# Convenience derivations. Each helper builds on errorta_home() so that
# moving ERRORTA_HOME moves everything atomically.


def corpora_dir() -> Path:
    p = errorta_home() / "corpora"
    p.mkdir(parents=True, exist_ok=True)
    return p


def corpus_dir(name: str) -> Path:
    p = corpora_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def verdicts_log_path() -> Path:
    p = errorta_home() / "verdicts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def hardware_json_path() -> Path:
    p = errorta_home() / "hardware.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ollama_settings_path() -> Path:
    p = errorta_home() / "ollama.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def grounding_json_path() -> Path:
    p = errorta_home() / "grounding.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def grounding_embeddings_path() -> Path:
    p = errorta_home() / "grounding_embeddings.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def data_residency_path() -> Path:
    p = errorta_home() / "data-residency.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def model_gateway_dir() -> Path:
    p = errorta_home() / "model-gateway"
    p.mkdir(parents=True, exist_ok=True)
    return p


def model_gateway_settings_path() -> Path:
    p = model_gateway_dir() / "policy.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def model_gateway_audit_path() -> Path:
    p = model_gateway_dir() / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def model_gateway_budget_path() -> Path:
    p = model_gateway_dir() / "budget-ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def agent_context_dir() -> Path:
    p = errorta_home() / "agent-context"
    p.mkdir(parents=True, exist_ok=True)
    return p


def auth_tokens_path() -> Path:
    p = errorta_home() / "tokens.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def revoked_tokens_path() -> Path:
    p = errorta_home() / "revoked-tokens.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def auth_audit_path() -> Path:
    p = errorta_home() / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# --- F-DIST-01 alpha delivery -------------------------------------------------
# device.json / license.json / telemetry.json hold the private-alpha identity,
# license token, and consented telemetry queue. All written 0600 (see
# errorta_alpha.storage). Only license.json is retired at v1.0; device.json and
# telemetry.json persist as the anonymous install id + opt-in telemetry store.

def alpha_device_path() -> Path:
    p = errorta_home() / "device.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def alpha_license_path() -> Path:
    p = errorta_home() / "license.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def alpha_telemetry_path() -> Path:
    p = errorta_home() / "telemetry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
