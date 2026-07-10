"""Errorta-owned model gateway policy, budget, and audit layer.

F030 keeps remote-provider setup out of AIAR. This package is the sidecar-owned
choke point for deciding whether a model call may stay local, use a remote
support model, or be blocked before any provider SDK/network path can initialize.
"""
from __future__ import annotations

from .policy import GatewayPolicy, RoutePolicy
from .router import GatewayPlan, GatewayRequest, plan_request

__all__ = [
    "GatewayPlan",
    "GatewayPolicy",
    "GatewayRequest",
    "RoutePolicy",
    "plan_request",
]
