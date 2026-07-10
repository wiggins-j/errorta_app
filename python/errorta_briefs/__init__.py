"""errorta_briefs — Brief markdown schema + parser + collect lifecycle.

F008a-schema track. Defines the BriefConfig Pydantic model and a markdown
front-matter parser used by the brief-driven corpus collection wedge (F008).

F008d-lifecycle track. BriefState FSM (lifecycle) and CollectState persistence
(state) for resumable brief-driven collection runs.
"""
from __future__ import annotations

from errorta_briefs.schema import BriefConfig, SourceSpec

try:  # parser is part of F008a; tolerate absence so other tracks (e.g. F008d) load.
    from errorta_briefs.parser import BriefParseError, parse_brief_markdown
except ModuleNotFoundError:  # pragma: no cover - guard for parallel track development
    BriefParseError = None  # type: ignore[assignment]
    parse_brief_markdown = None  # type: ignore[assignment]

from errorta_briefs.compliance import (
    DEFAULT_LICENSE_ALLOWLIST,
    ComplianceGate,
    ComplianceRefusal,
)
from errorta_briefs.connector import (
    FatalError,
    RetryableError,
    SourceConnector,
    SourceDoc,
)

__all__ = [
    "BriefConfig",
    "BriefParseError",
    "ComplianceGate",
    "ComplianceRefusal",
    "DEFAULT_LICENSE_ALLOWLIST",
    "FatalError",
    "RetryableError",
    "SourceConnector",
    "SourceDoc",
    "SourceSpec",
    "parse_brief_markdown",
    "BriefRunner",
    "BriefState",
    "CONNECTOR_REGISTRY",
    "register_connector",
]

# F008e-runner-routes: BriefRunner orchestrator. BriefState is owned by the
# F008d lifecycle module; re-exported here for backwards compatibility with
# any consumer that does `from errorta_briefs import BriefState`.
from errorta_briefs.lifecycle import BriefState  # noqa: E402
from errorta_briefs.runner import (  # noqa: E402
    CONNECTOR_REGISTRY,
    BriefRunner,
    register_connector,
)
