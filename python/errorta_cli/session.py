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
    poll_interval: float | None = None
    # The working directory this invocation is bound to. Load-bearing for the S5
    # directory-binding commands (``new`` / ``import`` / ``open`` / ``switch``),
    # which write a ``.errorta-project`` pointer here. ``None`` means "resolve
    # ``Path.cwd()`` lazily" (see :meth:`bind_cwd`); tests pin it to an isolated
    # tmp dir so a pointer write never lands in the repo.
    cwd: Path | None = None
    # Set by ``registry.dispatch`` to the effective ``--json`` mode so a command's
    # ``call`` (not just its ``render``) can branch on it — e.g. the run command's
    # "--json requires --yes" gate and its block-to-done vs live-stream choice.
    json_mode: bool = False

    @classmethod
    def build(
        cls,
        *,
        home_override: str | None = None,
        verbosity: Verbosity | None = None,
        project_override: str | None = None,
        cwd: Path | None = None,
        poll_interval: float | None = None,
    ) -> Context:
        """Resolve ``ERRORTA_HOME`` and the cwd-bound project into a Context."""
        home = config.resolve_home(home_override)
        here = cwd or Path.cwd()
        pid = project_override or config.resolve_project_id(home, here)
        return cls(
            home=home,
            verbosity=verbosity or Verbosity(),
            project_id=pid,
            poll_interval=poll_interval,
            cwd=here,
        )

    def bind_cwd(self) -> Path:
        """The directory a ``.errorta-project`` pointer should be written into."""
        return self.cwd or Path.cwd()

    def switch_project(self, project_id: str | None) -> None:
        self.project_id = project_id
