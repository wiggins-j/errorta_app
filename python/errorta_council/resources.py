"""LocalResourceGuard — Phase 1 minimal slice (F031-11).

Phase 1 scope:
  - Ollama reachability + model-installed checks.
  - Sequential default (max_concurrent_local_turns = 1; enforced by scheduler).
  - Stale-hardware-scan → warning, NOT block.
  - No auto-pull anywhere in the path (invariant 4 prep).
  - provider == "fake" skips the resource check entirely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    classification: str    # "fits" | "warning" | "unavailable" | "blocked_by_policy"
    reason_code: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoomResourcePreview:
    ollama_reachable: bool
    hardware_scan_present: bool
    per_member: list[dict[str, Any]] = field(default_factory=list)


class _GatewayMetadataReader(Protocol):
    async def is_reachable(self) -> bool: ...
    async def list_installed_models(self) -> list[str]: ...


def _is_local_member(member: dict) -> bool:
    """Only local Ollama members are gated by the Ollama reachability +
    installed-model checks. Remote/subscription members (anthropic, openai,
    google, custom, claude_cli, codex_cli, …) live elsewhere — their models
    aren't installed locally and their failures surface at the gateway call,
    not here. ``fake`` is handled separately (skipped entirely).
    """
    provider = (member.get("provider") or "").lower()
    if provider == "local":
        return True
    route = member.get("gateway_route_id") or ""
    return route.startswith("local.") or route.startswith("local/")


class LocalResourceGuard:
    def __init__(
        self,
        *,
        gateway: _GatewayMetadataReader,
        hardware_scan_present: bool,
        ollama_pull: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._gateway = gateway
        self._hardware_scan_present = hardware_scan_present
        # Phase 1: ollama_pull is captured but never called.
        self._ollama_pull = ollama_pull

    async def admit(self, *, proposal: Any, member: dict) -> AdmissionResult:
        if member.get("provider") == "fake":
            return AdmissionResult(admitted=True, classification="fits", reason_code=None)
        if not _is_local_member(member):
            # Remote/subscription member — the local Ollama guard does not
            # apply; misconfiguration surfaces at the gateway call.
            warnings = [] if self._hardware_scan_present else ["hardware_scan_absent"]
            return AdmissionResult(
                admitted=True,
                classification="fits" if self._hardware_scan_present else "warning",
                reason_code=None, warnings=warnings,
            )
        reachable = await self._gateway.is_reachable()
        if not reachable:
            return AdmissionResult(
                admitted=False,
                classification="unavailable",
                reason_code="local_provider_unavailable",
            )
        installed = set(await self._gateway.list_installed_models())
        if member.get("model") not in installed:
            return AdmissionResult(
                admitted=False,
                classification="unavailable",
                reason_code="local_model_missing",
            )
        warnings: list[str] = []
        classification = "fits"
        if not self._hardware_scan_present:
            warnings.append("hardware_scan_absent")
            classification = "warning"
        return AdmissionResult(
            admitted=True, classification=classification, reason_code=None, warnings=warnings
        )

    async def preview(self, *, room: dict) -> RoomResourcePreview:
        reachable = await self._gateway.is_reachable()
        installed = set(await self._gateway.list_installed_models()) if reachable else set()
        per_member: list[dict[str, Any]] = []
        for m in room.get("members", []):
            if m.get("provider") == "fake":
                per_member.append({"member_id": m["id"], "classification": "fits", "reason_code": None})
                continue
            if not _is_local_member(m):
                per_member.append({
                    "member_id": m["id"],
                    "classification": "fits" if self._hardware_scan_present else "warning",
                    "reason_code": None,
                })
                continue
            if not reachable:
                per_member.append({
                    "member_id": m["id"],
                    "classification": "unavailable",
                    "reason_code": "local_provider_unavailable",
                })
                continue
            if m.get("model") not in installed:
                per_member.append({
                    "member_id": m["id"],
                    "classification": "unavailable",
                    "reason_code": "local_model_missing",
                })
                continue
            per_member.append({
                "member_id": m["id"],
                "classification": "fits" if self._hardware_scan_present else "warning",
                "reason_code": None,
            })
        return RoomResourcePreview(
            ollama_reachable=reachable,
            hardware_scan_present=self._hardware_scan_present,
            per_member=per_member,
        )

    def release(self, turn_id: str) -> None:
        # Phase 1 minimal slice has no reservation to release.
        return None
