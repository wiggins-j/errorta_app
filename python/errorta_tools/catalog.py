"""F045 — tool catalog: stable families, metadata, and policy hints.

The catalog is metadata-first (per the F045 spec): it describes the tool
families the ToolGateway can expose, their egress class, timeouts, output
caps, and approval hints — independent of whether a concrete handler is
registered yet. Policy and UI key on this stable surface as tools grow.

Council never imports this for egress; it's read by the settings/catalog
routes and (later) the F046 work rail. Actual invocation still flows through
``ToolGateway.invoke()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Stable tool families (F045 §"Tool families"). Room ``tool_policy`` grants
# these by id; the F039 schema's ``enabled_tool_ids()`` returns the granted set.
TOOL_FAMILIES = (
    "web_fetch",
    "web_search",
    "code_read",
    "code_write",
    "code_exec",
    "terminal",
    "artifact",
    "policy",
)

# Source class the tool's output enters parent context as (mirrors the
# context-router source classes; child/tool output is always "untrusted data").
_SOURCE_TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class ToolMetadata:
    tool_id: str
    family: str
    egress_class: str  # local | remote_eligible | remote
    default_timeout_seconds: int
    max_output_bytes: int
    requires_approval: bool
    source_class: str = _SOURCE_TOOL_RESULT
    display_name: str = ""
    description: str = ""
    backend: str = "builtin"  # builtin | mcp
    server_id: str | None = None  # set for MCP-provided tools

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "tool_id": self.tool_id,
            "family": self.family,
            "egress_class": self.egress_class,
            "default_timeout_seconds": self.default_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "requires_approval": self.requires_approval,
            "source_class": self.source_class,
            "display_name": self.display_name or self.tool_id,
            "description": self.description,
            "backend": self.backend,
        }
        if self.server_id is not None:
            d["server_id"] = self.server_id
        return d


# Built-in F039 tool families. These are metadata only; a concrete handler is
# registered separately (errorta_tools.registry) when the slice that ships the
# tool lands. egress_class is the *maximum* a tool may use; the gateway still
# enforces per-call alignment.
_BUILTIN: dict[str, ToolMetadata] = {
    "web_fetch": ToolMetadata(
        tool_id="web_fetch", family="web_fetch", egress_class="remote",
        default_timeout_seconds=30, max_output_bytes=2_000_000,
        requires_approval=True,
        display_name="Web fetch",
        description="Fetch a single URL (SSRF/domain-guarded).",
    ),
    "web_search": ToolMetadata(
        tool_id="web_search", family="web_search", egress_class="remote",
        default_timeout_seconds=30, max_output_bytes=1_000_000,
        requires_approval=True,
        display_name="Web search",
        description="Query a configured search backend (e.g. SearXNG).",
    ),
    "code_read": ToolMetadata(
        tool_id="code_read", family="code_read", egress_class="local",
        default_timeout_seconds=15, max_output_bytes=2_000_000,
        requires_approval=False,
        display_name="Code read",
        description="Read files inside the run workspace.",
    ),
    "code_write": ToolMetadata(
        tool_id="code_write", family="code_write", egress_class="local",
        default_timeout_seconds=15, max_output_bytes=1_000_000,
        requires_approval=True,
        display_name="Code write",
        description="Propose/write files inside the run workspace.",
    ),
    "code_exec": ToolMetadata(
        tool_id="code_exec", family="code_exec", egress_class="local",
        default_timeout_seconds=120, max_output_bytes=2_000_000,
        requires_approval=True,
        display_name="Code exec",
        description="Run commands/tests in the sandboxed run workspace.",
    ),
}

# Runtime-registered MCP tool metadata (populated by mcp config/discovery).
_MCP: dict[str, ToolMetadata] = {}


def register_mcp_tool(meta: ToolMetadata) -> None:
    if meta.backend != "mcp" or not meta.server_id:
        raise ValueError("mcp_tool_requires_backend_and_server")
    _MCP[meta.tool_id] = meta


def clear_mcp_tools() -> None:
    _MCP.clear()


def get_metadata(tool_id: str) -> ToolMetadata | None:
    return _BUILTIN.get(tool_id) or _MCP.get(tool_id)


def all_metadata() -> list[ToolMetadata]:
    return sorted(
        [*_BUILTIN.values(), *_MCP.values()], key=lambda m: m.tool_id
    )


def _granted_tool_ids(tool_policy: dict[str, Any] | None) -> set[str]:
    """Which tool ids a room's ``tool_policy`` grants.

    Mirrors ``CouncilRoom.ToolPolicy.enabled_tool_ids()`` from the raw dict so
    this module never imports ``errorta_council``. A family is granted when its
    sub-policy has ``enabled: true``.
    """
    policy = tool_policy or {}
    granted: set[str] = set()
    for family in TOOL_FAMILIES:
        sub = policy.get(family)
        if isinstance(sub, dict) and bool(sub.get("enabled")):
            granted.add(family)
    # MCP tools may be granted by explicit id list.
    explicit = policy.get("enabled_tool_ids")
    if isinstance(explicit, list):
        granted.update(str(t) for t in explicit)
    return granted


def filter_for_room(
    *,
    tool_policy: dict[str, Any] | None,
    configured_tool_ids: set[str] | None = None,
) -> list[ToolMetadata]:
    """Catalog entries that are BOTH policy-granted for the room AND configured.

    ``configured_tool_ids`` is the set with a usable backend (a registered
    handler, or an MCP server whose circuit is not permanently down). When
    None, only builtins are considered configured (MCP requires explicit
    configuration). A disabled/un-granted tool is never returned — the catalog
    cannot present an ungranted tool as available.
    """
    granted = _granted_tool_ids(tool_policy)
    configured = (
        configured_tool_ids
        if configured_tool_ids is not None
        else set(_BUILTIN.keys())
    )
    out: list[ToolMetadata] = []
    for meta in all_metadata():
        if meta.tool_id in granted and meta.tool_id in configured:
            out.append(meta)
    return out


def __all_families_dict() -> dict[str, Any]:  # pragma: no cover - convenience
    return {f: True for f in TOOL_FAMILIES}
