"""Residency-aware ownership rules for the model gateway."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GatewayOwner:
    residency_mode: str
    gateway_owner: str
    secret_location: str
    audit_location: str
    local_proxy_may_call_remote: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def gateway_owner() -> GatewayOwner:
    """Return where provider calls are allowed to originate.

    In SSH-remote mode the laptop sidecar must not send corpus-derived payloads
    to Anthropic/OpenAI. The remote sidecar on the SSH host owns secrets,
    provider clients, audit, and budget. When running on the remote host itself
    the residency config is expected to be local/default, so this block only
    applies to the local proxy process.
    """
    try:
        from errorta_residency import config as residency_config

        state = residency_config.load()
    except Exception:
        state = None

    mode = getattr(state, "mode", "local")
    if mode == "ssh-remote":
        return GatewayOwner(
            residency_mode="ssh-remote",
            gateway_owner="remote-sidecar",
            secret_location="remote-host",
            audit_location="remote-errorta-home",
            local_proxy_may_call_remote=False,
        )
    if mode == "cloud":
        return GatewayOwner(
            residency_mode="cloud",
            gateway_owner="cloud-sidecar",
            secret_location="cloud-sidecar",
            audit_location="cloud-errorta-home",
            local_proxy_may_call_remote=False,
        )
    return GatewayOwner(
        residency_mode="local",
        gateway_owner="local-sidecar",
        secret_location="local-secret-store",
        audit_location="local-errorta-home",
        local_proxy_may_call_remote=True,
    )
