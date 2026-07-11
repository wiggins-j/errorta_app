"""Per-session state shared by both front-ends (argv + REPL).

A :class:`Context` bundles the resolved ``ERRORTA_HOME``, the active project id
(cwd-bound by default), the verbosity dial, and the resolved sidecar handle. The
argv front-end builds one per command; the REPL builds one at start and mutates
it as the user switches projects or verbosity.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config
from .sidecar import SidecarHandle
from .verbosity import Verbosity


@dataclass
class Context:
    """Ambient state a command needs beyond its own arguments."""

    home: Path
    verbosity: Verbosity
    project_id: str | None = None
    handle: SidecarHandle | None = None

    @classmethod
    def build(
        cls,
        *,
        home_override: str | None = None,
        verbosity: Verbosity | None = None,
        project_override: str | None = None,
        cwd: Path | None = None,
    ) -> Context:
        """Resolve ``ERRORTA_HOME`` and the cwd-bound project into a Context."""
        home = config.resolve_home(home_override)
        pid = project_override or config.resolve_project_id(home, cwd)
        return cls(
            home=home,
            verbosity=verbosity or Verbosity(),
            project_id=pid,
        )

    def switch_project(self, project_id: str | None) -> None:
        self.project_id = project_id
