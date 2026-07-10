"""F039 ToolGateway seam.

This package owns tool-use egress and execution. Council code may depend on
the Protocol and value types here, but concrete network/process/tool clients
must live behind a ToolGateway implementation.
"""
from __future__ import annotations

from .gateway import (
    DefaultToolGateway,
    FatalToolError,
    RetryableToolError,
    ToolCallRequest,
    ToolCallResult,
    ToolGateway,
    ToolGatewayError,
    ToolHandler,
)

__all__ = [
    "DefaultToolGateway",
    "FatalToolError",
    "RetryableToolError",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolGateway",
    "ToolGatewayError",
    "ToolHandler",
]
