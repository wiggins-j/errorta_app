"""F039 — register the built-in ToolGateway handlers.

Registration is NOT a grant: a handler being registered only means the backend
exists. Each call is still gated per-room by tool_policy + F041 first-use
consent in the scheduler before the gateway is ever invoked. Call
``register_builtins()`` at sidecar startup (and tests call it directly).
"""
from __future__ import annotations

from .. import registry
from .code import CodeReadHandler, CodeWriteHandler
from .code_exec import CodeExecHandler
from .web import WebFetchHandler, WebSearchHandler

_BUILTINS = {
    "web_fetch": WebFetchHandler,
    "web_search": WebSearchHandler,
    "code_read": CodeReadHandler,
    "code_write": CodeWriteHandler,
    "code_exec": CodeExecHandler,
}


def register_builtins() -> None:
    for tool_id, handler_cls in _BUILTINS.items():
        registry.register(tool_id, handler_cls)


__all__ = ["register_builtins"]
