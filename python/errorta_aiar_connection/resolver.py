"""Resolve the single active AIAR runtime."""

from __future__ import annotations

from .config import AiarConnectionConfig, load_canonical
from .models import AiarRuntime, disconnected
from .status import probe_aiar_service, probe_local_aiar, probe_remote_sidecar


def _legacy_remote_config() -> AiarConnectionConfig | None:
    try:
        from errorta_project_grounding.remote_adapter import remote_aiar_config

        cfg = remote_aiar_config()
    except Exception:
        cfg = None
    if cfg is None:
        return None
    return AiarConnectionConfig(
        kind="aiar-service",
        display_name=None,
        base_url=cfg.base_url,
        token=getattr(cfg, "token", None),
        timeout_s=float(getattr(cfg, "timeout_s", 60.0) or 60.0),
        verify_tls=bool(getattr(cfg, "verify", True)),
        created_from="remote-aiar.json",
    )


def _legacy_residency_config() -> AiarConnectionConfig | None:
    try:
        from errorta_residency import config as residency_config

        state = residency_config.load()
    except Exception:
        return None
    mode = getattr(state, "mode", "local")
    if mode == "ssh-remote":
        port = getattr(state, "local_tunnel_port", None)
        if not port:
            return AiarConnectionConfig(
                kind="errorta-sidecar-remote",
                display_name=getattr(state, "ssh_host", None) or "Remote Errorta sidecar",
                base_url=None,
                created_from="data-residency.json",
            )
        return AiarConnectionConfig(
            kind="errorta-sidecar-remote",
            display_name=getattr(state, "ssh_host", None) or "Remote Errorta sidecar",
            base_url=f"http://127.0.0.1:{port}",
            created_from="data-residency.json",
        )
    if mode == "cloud" and getattr(state, "cloud_url", None):
        return AiarConnectionConfig(
            kind="errorta-sidecar-remote",
            display_name="Cloud Errorta sidecar",
            base_url=state.cloud_url,
            token=getattr(state, "cloud_token", None),
            created_from="data-residency.json",
        )
    return None


def _probe_config(config: AiarConnectionConfig, *, source: str) -> AiarRuntime:
    if config.kind == "aiar-service":
        return probe_aiar_service(config, config_source=source)
    if config.kind == "errorta-sidecar-remote":
        return probe_remote_sidecar(config, config_source=source)
    if config.kind == "local-aiar":
        return probe_local_aiar(config_source=source)
    return disconnected(display_name="AIAR disconnected", config_source=source)


def resolve_aiar_config() -> tuple[AiarConnectionConfig | None, str]:
    """Select the active AIAR config WITHOUT probing the network.

    Returns ``(config, source)``. ``config`` is ``None`` when the selection has no
    single concrete config to probe: ``source == "ambiguous_legacy"`` (two
    conflicting legacy backends) or ``source == "none"`` (nothing configured —
    the caller should fall back to a local probe).

    Shared by :func:`resolve_aiar_runtime` (which then probes) and hot-path
    callers (e.g. ``active_remote_adapter``) that only need the routing decision
    and must NOT block on the network — a slow/unreachable backend would
    otherwise stall corpus listing / retrieval for up to ``timeout_s`` × probes.
    """
    canonical = load_canonical()
    if canonical is not None:
        return canonical, "canonical"

    legacy_remote = _legacy_remote_config()
    legacy_residency = _legacy_residency_config()
    if legacy_remote is not None and legacy_residency is not None:
        if (
            legacy_remote.base_url
            and legacy_residency.base_url
            and legacy_remote.base_url.rstrip("/") == legacy_residency.base_url.rstrip("/")
        ):
            return legacy_remote, "ambiguous_legacy_resolved"
        return None, "ambiguous_legacy"
    if legacy_remote is not None:
        return legacy_remote, "legacy_remote_aiar"
    if legacy_residency is not None:
        return legacy_residency, "legacy_residency"
    return None, "none"


def resolve_aiar_runtime() -> AiarRuntime:
    config, source = resolve_aiar_config()
    if source == "ambiguous_legacy":
        return disconnected(
            display_name="Choose AIAR backend",
            config_source="ambiguous_legacy",
            error_code="ambiguous_legacy",
            error_message=(
                "Both remote-aiar.json and remote data residency are configured. "
                "Choose one AIAR backend in Settings."
            ),
        )
    if config is not None:
        return _probe_config(config, source=source)

    local = probe_local_aiar(config_source="local_probe")
    if local.connected:
        return local
    return disconnected(
        display_name="AIAR disconnected",
        config_source="none",
        error_code=local.error_code or "aiar_disconnected",
        error_message=local.error_message,
    )
