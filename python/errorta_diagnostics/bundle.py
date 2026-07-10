"""Diagnostic bundle orchestrator.

``build_bundle(dest_path, ...)`` reads local-only state from ``~/.errorta``
and the in-memory log buffer, applies the redaction pipeline, and writes a
zip archive at ``dest_path``. No network primitives are imported here.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import platform
import sys
import zipfile
from collections import deque
from pathlib import Path
from typing import Any

from .log_buffer import LogBuffer
from .redact import apply_pipeline

log = logging.getLogger(__name__)

BUNDLE_FILES = (
    "manifest.json",
    "README.txt",
    "hardware.json",
    "versions.json",
    "aiar_pin.json",
    "aiar-runtime.json",
    "env.json",
    "verdicts-summary.json",
    "grounding-summary.json",
    "data-residency.json",
    "model-gateway-settings.json",
    "model-gateway-audit-summary.json",
    "agent-context-summary.json",
    "pm-working-memory.json",
    "lifecycle.json",
    "sidecar.log",
    "redaction-manifest.json",
    "user-note.txt",
)

# F-INFRA-12 Phase B Slice 10 — field-level allow-list for data-residency.json.
# Fields in this set are passed through verbatim from the on-disk JSON; every
# other field is replaced with the literal "<redacted>". The JSON shape stays
# intact so support engineers can see WHICH fields were filled in without
# learning the values. Notable: ssh_host, ssh_key_path, ssh_username, cloud_url,
# and cloud_token MUST stay redacted. Paths leak username/key pseudo-identity;
# operators may run private infra at private domains; tokens are tokens.
DATA_RESIDENCY_ALLOWED_FIELDS = frozenset(
    {
        "mode",
        "ssh_port",
        "remote_sidecar_port",
        "tunnel_state",
        "updated_at",
    }
)

ENV_ALLOWLIST = ("LANG", "LC_ALL", "TERM", "SHELL", "OLLAMA_HOST", "ERRORTA_SIDECAR_PORT")

VERDICT_KEEP_FIELDS = ("id", "created_at", "rating", "failure_tags", "latency_ms")

_README = (
    "Errorta diagnostic bundle\n"
    "=========================\n\n"
    "This archive was generated locally by Errorta. It contains a redacted\n"
    "snapshot of sidecar state for support and self-diagnosis.\n\n"
    "Contents:\n"
    "  manifest.json           — what produced this bundle\n"
    "  README.txt              — this file\n"
    "  hardware.json           — last hardware scan result\n"
    "  versions.json           — app + AIAR + Python + platform versions\n"
    "  aiar_pin.json           — local AIAR package install state\n"
    "  aiar-runtime.json       — active AIAR runtime + capabilities (no URL/token)\n"
    "  env.json                — allowlisted environment variables\n"
    "  verdicts-summary.json   — last 200 verdict ids/ratings (no bodies)\n"
    "  grounding-summary.json  — grounding store size + last-modified\n"
    "  data-residency.json     — active residency mode (host/url/token redacted)\n"
    "  model-gateway-settings.json — model routing settings (no provider keys)\n"
    "  model-gateway-audit-summary.json — last gateway audit ids/statuses only\n"
    "  agent-context-summary.json — capsule ids and hashes only (no raw state)\n"
    "  pm-working-memory.json — PM memory refs/status only (no raw memory text)\n"
    "  sidecar.log             — in-memory log buffer at export time\n"
    "  redaction-manifest.json — counts of redactions applied\n"
    "  user-note.txt           — note the user supplied at export\n"
)


# --- Helpers ----------------------------------------------------------------


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _errorta_home() -> Path:
    """Compatibility shim. New code should import from ``errorta_app.paths``."""
    from errorta_app.paths import errorta_home
    return errorta_home()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.debug("could not read %s: %s", path, e)
        return None


def _read_json(path: Path) -> Any:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _tail_jsonl(path: Path, n: int) -> list[dict[str, Any]]:
    text = _read_text(path)
    if not text:
        return []
    keep: deque[dict[str, Any]] = deque(maxlen=n)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            keep.append(obj)
    return list(keep)


def _verdict_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        verdict = rec.get("verdict") if isinstance(rec.get("verdict"), dict) else {}
        out.append(
            {
                "id": rec.get("id"),
                "created_at": rec.get("created_at"),
                "rating": verdict.get("rating") if verdict else rec.get("rating"),
                "failure_tags": (
                    verdict.get("failure_tags")
                    if verdict and "failure_tags" in verdict
                    else rec.get("failure_tags") or []
                ),
                "latency_ms": (
                    verdict.get("latency_ms")
                    if verdict and "latency_ms" in verdict
                    else rec.get("latency_ms")
                ),
            }
        )
    return out


def _grounding_summary(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    last_modified: str | None = None
    try:
        st = path.stat()
        last_modified = _dt.datetime.fromtimestamp(
            st.st_mtime, tz=_dt.timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        pass
    if isinstance(data, dict):
        # Conventional shape: { "<signature>": {...} }
        signatures = len(data)
    elif isinstance(data, list):
        signatures = len(data)
    else:
        signatures = 0
    return {"signatures": signatures, "last_modified": last_modified}


def _aiar_pin() -> dict[str, Any]:
    try:
        from errorta_app.health.aiar_pin import check_aiar_pin

        pin = check_aiar_pin()
        return dict(pin) if pin else {"available": False, "version": None, "source": "absent"}
    except Exception as e:  # pragma: no cover - defensive
        log.debug("aiar_pin lookup failed: %s", e)
        return {"available": False, "version": None, "source": "absent"}


def _aiar_runtime_payload() -> dict[str, Any]:
    """Active AIAR runtime metadata for diagnostics.

    This is intentionally narrower than ``/aiar/status``: the UI needs the
    selected base URL; a support bundle does not. Keep only booleans, runtime
    kind, backend id when it is not just a URL, model readiness, and capability
    flags. Tokens and private hostnames never enter this payload.
    """
    try:
        from errorta_aiar_connection import resolve_aiar_runtime

        runtime = resolve_aiar_runtime()
    except Exception as exc:
        return {
            "runtime_kind": "disconnected",
            "connected": False,
            "error_code": "aiar_runtime_probe_failed",
            "error_message": str(exc)[:160],
        }
    backend_id = runtime.backend_id
    if isinstance(backend_id, str) and backend_id.startswith(("http://", "https://")):
        backend_id = "<redacted-url>"
    display_name = runtime.display_name
    if "." in display_name or "://" in display_name:
        display_name = "<redacted>"
    return {
        "runtime_kind": runtime.kind,
        "connected": runtime.connected,
        "display_name": display_name,
        "backend_id": backend_id,
        "base_url_configured": bool(runtime.base_url),
        "token_configured": bool(runtime.token),
        "capabilities": runtime.capabilities.to_dict(),
        "active_model": runtime.active_model,
        "active_model_ready": runtime.active_model_ready,
        "available_model_count": len(runtime.available_models),
        "corpus_count": runtime.corpus_count,
        "config_source": runtime.config_source,
        "status_source": runtime.status_source,
        "error_code": runtime.error_code,
    }


def _versions() -> dict[str, Any]:
    try:
        from errorta_app import __version__ as app_version
    except Exception:
        app_version = "unknown"
    try:
        import aiar  # type: ignore

        aiar_version = getattr(aiar, "__version__", None)
    except Exception:
        aiar_version = None
    return {
        "app": app_version,
        "aiar": aiar_version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def _env_snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val is not None:
            out[key] = val
    path = os.environ.get("PATH")
    if path is not None:
        out["PATH"] = {"segments": len([s for s in path.split(os.pathsep) if s])}
    return out


# --- Redaction over JSON ----------------------------------------------------


def _redact_obj(obj: Any, counts: dict[str, int], *, home: str, username: str, corpus_roots: list[str]) -> Any:
    """Recursively walk a JSON-shaped value, redacting strings in place."""
    if isinstance(obj, str):
        redacted, c = apply_pipeline(obj, home=home, username=username, corpus_roots=corpus_roots)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return redacted
    if isinstance(obj, list):
        return [_redact_obj(v, counts, home=home, username=username, corpus_roots=corpus_roots) for v in obj]
    if isinstance(obj, dict):
        return {
            k: _redact_obj(v, counts, home=home, username=username, corpus_roots=corpus_roots)
            for k, v in obj.items()
        }
    return obj


def _redact_text(text: str, counts: dict[str, int], *, home: str, username: str, corpus_roots: list[str]) -> str:
    redacted, c = apply_pipeline(text, home=home, username=username, corpus_roots=corpus_roots)
    for k, v in c.items():
        counts[k] = counts.get(k, 0) + v
    return redacted


# --- Data residency ---------------------------------------------------------


def _residency_field_redact(raw: Any) -> dict[str, Any]:
    """Apply the field-level allow-list to a parsed data-residency.json dict.

    Every key that is not in ``DATA_RESIDENCY_ALLOWED_FIELDS`` is replaced
    with the literal string ``"<redacted>"``. The JSON shape is preserved —
    only values are masked. Non-dict inputs (or missing files) are treated
    as a Local-mode default ``{"mode": "local"}`` so the bundle entry stays
    self-describing.
    """
    if not isinstance(raw, dict):
        return {"mode": "local"}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in DATA_RESIDENCY_ALLOWED_FIELDS:
            out[key] = value
        else:
            out[key] = "<redacted>"
    # If the on-disk file did not advertise a mode (corrupt / partial), force
    # the safe default rather than leak whatever junk was there.
    if "mode" not in out:
        out["mode"] = "local"
    return out


def _data_residency_payload(edir: Path) -> dict[str, Any]:
    """Read + redact data-residency.json under ``edir``.

    Missing files yield ``{"mode": "local"}`` (the v0.5 default) so the
    bundle entry is self-describing rather than a "file not found" error.
    """
    raw = _read_json(edir / "data-residency.json")
    if raw is None:
        return {"mode": "local"}
    return _residency_field_redact(raw)


def _model_gateway_settings_payload(edir: Path) -> dict[str, Any]:
    raw = _read_json(edir / "model-gateway" / "policy.json")
    if raw is None:
        raw = _read_json(edir / "model-gateway.json")
    if not isinstance(raw, dict):
        return {"global_mode": "local_only"}
    # Gateway settings intentionally must not contain provider keys. Still
    # defensively mask key/token/account-ish fields if a future slice adds
    # more structure than this summary expects.
    def _mask(value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, child in value.items():
                lowered = str(key).lower()
                if any(term in lowered for term in ("key", "token", "secret", "account")):
                    out[key] = "<redacted>"
                else:
                    out[key] = _mask(child)
            return out
        if isinstance(value, list):
            return [_mask(child) for child in value]
        return value

    return _mask(raw)


def _model_gateway_audit_summary(edir: Path) -> list[dict[str, Any]]:
    audit_path = edir / "model-gateway" / "audit.jsonl"
    if not audit_path.exists():
        audit_path = edir / "model-gateway-audit.jsonl"
    records = _tail_jsonl(audit_path, 50)
    out: list[dict[str, Any]] = []
    for rec in records:
        out.append(
            {
                "request_id": rec.get("request_id"),
                "ts": rec.get("ts"),
                "role": rec.get("role"),
                "provider": rec.get("provider"),
                "model": rec.get("model"),
                "egress_policy": rec.get("egress_policy"),
                "egress_class": rec.get("egress_class"),
                "status": rec.get("status"),
                "fallback_used": bool(rec.get("fallback_used")),
                "payload_sha256": rec.get("payload_sha256"),
                "tokens": rec.get("tokens") if isinstance(rec.get("tokens"), dict) else {},
                "estimated_cost_usd": rec.get("estimated_cost_usd"),
            }
        )
    return out


def _agent_context_summary(edir: Path) -> dict[str, Any]:
    try:
        from errorta_agent_context.store import AgentContextStore

        return AgentContextStore(edir / "agent-context").metadata_for_diagnostics()
    except Exception:
        return {"capsule_count": 0, "capsules": []}


def _pm_working_memory_summary(edir: Path) -> dict[str, Any]:
    """F099 diagnostics: PM memory refs/status only, never raw memory content."""
    root = edir / "council" / "coding-projects"
    if not root.is_dir():
        return {"available": False, "projects": []}
    try:
        from errorta_council.coding.ledger import LedgerStore, list_projects
        from errorta_project_grounding.pm_working_memory import pm_working_memory_status
    except Exception as exc:
        return {"available": False, "error": str(exc), "projects": []}
    projects: list[dict[str, Any]] = []
    for project in list_projects(root=root):
        project_id = str(project.get("id") or "")
        if not project_id:
            continue
        try:
            status = pm_working_memory_status(LedgerStore(project_id, root=root))
        except Exception as exc:
            status = {
                "project_id": project_id,
                "status": "unavailable",
                "warnings": [str(exc)[:120]],
            }
        projects.append({
            "project_id": project_id,
            "status": status.get("status"),
            "memory_ref": status.get("memory_ref"),
            "corpus_id": status.get("corpus_id"),
            "aiar_mirror_status": status.get("aiar_mirror_status"),
            "aiar_retrieval_status": status.get("aiar_retrieval_status"),
            "last_generated_at": status.get("last_generated_at"),
            "last_mirrored_at": status.get("last_mirrored_at"),
            "warnings": list(status.get("warnings") or []),
        })
    return {"available": bool(projects), "projects": projects}


# --- Public API -------------------------------------------------------------


def _lifecycle_metadata(log_buffer: "LogBuffer | None") -> dict[str, Any]:
    """F048 sidecar lifecycle metadata + a capped/redacted recent log tail,
    for inclusion in the diagnostic bundle. Fail-soft."""
    try:
        from errorta_diagnostics import lifecycle as _lifecycle

        out: dict[str, Any] = _lifecycle.sidecar_lifecycle()
        if log_buffer is not None:
            out["recent_log_tail"] = _lifecycle.redacted_log_tail(
                log_buffer, lines=40
            )
        return out
    except Exception:
        return {}


def build_bundle(
    dest_path: str | Path,
    *,
    user_note: str = "",
    log_buffer: LogBuffer | None = None,
    corpus_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Build a redacted diagnostic zip at ``dest_path``.

    Returns a dict ``{path, sha256, redaction_manifest, files}``. Writes
    atomically: a temp file in the same directory is renamed into place.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    home = os.environ.get("HOME") or str(_home())
    username = os.environ.get("USER") or ""
    roots = list(corpus_roots or [])

    counts: dict[str, int] = {
        "home_path": 0,
        "username": 0,
        "ips": 0,
        "tokens": 0,
        "corpus_paths": 0,
    }

    edir = _errorta_home()

    # Inputs --------------------------------------------------------------
    hardware = _read_json(edir / "hardware.json") or {}
    verdict_records = _tail_jsonl(edir / "verdicts.jsonl", 200)
    verdicts = _verdict_summary(verdict_records)
    grounding = _grounding_summary(edir / "grounding.json")
    versions = _versions()
    aiar_pin = _aiar_pin()
    aiar_runtime = _aiar_runtime_payload()
    env_snap = _env_snapshot()
    # data-residency.json gets field-level masking (allow-list of safe scalars)
    # BEFORE the general text-redaction pipeline runs. This way ssh_host / etc.
    # are already "<redacted>" by the time the generic pipeline walks the
    # bundle for $HOME / IP / token substitutions — defense in depth.
    residency = _data_residency_payload(edir)
    gateway_settings = _model_gateway_settings_payload(edir)
    gateway_audit = _model_gateway_audit_summary(edir)
    agent_context = _agent_context_summary(edir)
    pm_working_memory = _pm_working_memory_summary(edir)
    lifecycle_meta = _lifecycle_metadata(log_buffer)
    log_text = log_buffer.text() if log_buffer is not None else ""

    # Redact --------------------------------------------------------------
    def R(value: Any) -> Any:
        return _redact_obj(value, counts, home=home, username=username, corpus_roots=roots)

    hardware_r = R(hardware)
    verdicts_r = R(verdicts)
    grounding_r = R(grounding)
    versions_r = R(versions)
    aiar_pin_r = R(aiar_pin)
    aiar_runtime_r = R(aiar_runtime)
    env_r = R(env_snap)
    residency_r = R(residency)
    gateway_settings_r = R(gateway_settings)
    gateway_audit_r = R(gateway_audit)
    agent_context_r = R(agent_context)
    pm_working_memory_r = R(pm_working_memory)
    lifecycle_r = R(lifecycle_meta)
    log_r = _redact_text(log_text, counts, home=home, username=username, corpus_roots=roots)
    note_r = _redact_text(user_note or "", counts, home=home, username=username, corpus_roots=roots)

    manifest = {
        "schema": "errorta.diagnostics/1",
        "generated_at": _now_iso(),
        "app_version": versions.get("app"),
        "files": list(BUNDLE_FILES),
    }

    redaction_manifest = {
        "rules": {k: int(v) for k, v in counts.items()},
        "generated_at": _now_iso(),
    }

    files_payload: dict[str, str] = {
        "manifest.json": json.dumps(manifest, indent=2, sort_keys=True),
        "README.txt": _README,
        "hardware.json": json.dumps(hardware_r, indent=2, sort_keys=True),
        "versions.json": json.dumps(versions_r, indent=2, sort_keys=True),
        "aiar_pin.json": json.dumps(aiar_pin_r, indent=2, sort_keys=True),
        "aiar-runtime.json": json.dumps(aiar_runtime_r, indent=2, sort_keys=True),
        "env.json": json.dumps(env_r, indent=2, sort_keys=True),
        "verdicts-summary.json": json.dumps(verdicts_r, indent=2, sort_keys=True),
        "grounding-summary.json": json.dumps(grounding_r, indent=2, sort_keys=True),
        "data-residency.json": json.dumps(residency_r, indent=2, sort_keys=True),
        "model-gateway-settings.json": json.dumps(
            gateway_settings_r, indent=2, sort_keys=True
        ),
        "model-gateway-audit-summary.json": json.dumps(
            gateway_audit_r, indent=2, sort_keys=True
        ),
        "agent-context-summary.json": json.dumps(
            agent_context_r, indent=2, sort_keys=True
        ),
        "pm-working-memory.json": json.dumps(
            pm_working_memory_r, indent=2, sort_keys=True
        ),
        "lifecycle.json": json.dumps(lifecycle_r, indent=2, sort_keys=True),
        "sidecar.log": log_r,
        "redaction-manifest.json": json.dumps(redaction_manifest, indent=2, sort_keys=True),
        "user-note.txt": note_r,
    }

    # Write atomically ----------------------------------------------------
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in BUNDLE_FILES:
                zf.writestr(name, files_payload[name])

        sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return {
        "path": str(dest),
        "sha256": sha,
        "redaction_manifest": redaction_manifest,
        "files": list(BUNDLE_FILES),
    }
