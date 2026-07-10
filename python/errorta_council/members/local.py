"""Local CouncilMember adapter — routes generate() through the LocalGateway.

This adapter does NOT touch Ollama directly. Invariant 3: only gateway_local
talks to Ollama. The scheduler builds the LocalCouncilModelRequest and calls
the gateway; this adapter exists for the F031-00 CouncilMember Protocol.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalMemberAdapter:
    member_id: str
    model: str
    route_id: str
    provider_class: str = "local"
    provider: str = "local"
    role: str = "member"
