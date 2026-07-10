"""F101-03 S1 — the ``Launcher`` seam.

The universal Run front door is a *dispatcher over a Launcher registry*, not a
growing switch (spec D1): "run a new kind of thing" = "register a new Launcher."
S1 ships the seam plus thin adapters over the *existing* runtime modalities — a
static/served site, a web/api server, and a one-shot CLI transcript — each a
straight pass-through to the already-shipped ``RuntimeProcessManager`` methods.
No process/lifecycle logic moves here; dispatch just becomes uniform so later
slices can add ``DesktopLauncher`` / ``BinaryLauncher`` without touching the
front door.

A ``LaunchPlan`` is grounded before it ever reaches a launcher (see
``runtime_resolve``); the launcher only starts what the resolver already proved
real. The richer protocol (``can_launch`` / trust tiers / ``observe`` /
``teardown`` over ``HostFacts``) arrives with the modalities that need it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..runtime import RuntimeSession
    from ..runtime_process import RuntimeProcessManager
    from ..runtime_resolve import LaunchPlan


@runtime_checkable
class Launcher(Protocol):
    """Runs a grounded ``LaunchPlan`` for one modality via the process manager.

    S1's minimal surface: ``modality`` (the registry key) + ``launch``. Later
    slices widen this with capability/tier/observe/teardown for the windowed and
    native-binary modalities; existing launchers keep this thin shape.
    """
    modality: str

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        ...


class StaticLauncher:
    """Serve a static/SPA site over loopback — the existing managed-local start
    (a stdlib ``http.server`` bound to 127.0.0.1)."""
    modality = "static"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        return mgr.start(plan.profile_id)


class ServerLauncher:
    """Run a long-lived web/api server (Node, Flask, FastAPI, …) — the existing
    managed-local start (port allocation, health probe, log pump)."""
    modality = "server"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        return mgr.start(plan.profile_id)


class CliLauncher:
    """Run a one-shot CLI/script once as a time-boxed transcript — the existing
    ``run_cli`` path (F101-02)."""
    modality = "cli"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        return mgr.run_cli(plan.profile_id)


class DesktopLauncher:
    """Run a GUI app that opens its own OS window (pygame / Tk / PyQt / …).

    F101-03 S2: the managed-local start with ``display=True`` — the F039 sandbox
    grants window-server access (T1 sandboxed-windowed) without re-opening
    network or out-of-workspace writes. When no windowing sandbox is available
    the run records ``sandbox_backend="none"`` and a reduced-isolation (T2)
    warning; the tier is stamped on the session and shown before Run.
    """
    modality = "desktop"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        return mgr.start(plan.profile_id, display=True)


class ContainerLauncher:
    """Run a Docker image (F101-03 S6 — the container is the isolation, so it
    runs outside the F039 OS sandbox and fails closed without a daemon).

    Behind the seam this is the existing managed start for a ``container``
    runtime mode — no behavior change; only dispatch becomes uniform. A
    ``ServiceLauncher`` (long-running, no window) is deliberately not a separate
    class: a service is a ``server`` profile with no demo URL, already handled by
    ServerLauncher — adding a redundant launcher would be surface without value.
    """
    modality = "container"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        return mgr.start(plan.profile_id)


class _NotBuiltLauncher:
    """A declared-but-not-built extension point (F101-03 S8). It exists so the
    seam's cross-OS / mobile modalities are documented and discoverable, but any
    attempt to run one refuses with a concrete reason — never a silent no-op and
    never an ungrounded guess. No detector emits these modalities today."""
    modality = ""
    _reason = "not_built"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        from ..runtime_process import RuntimeProcessError
        raise RuntimeProcessError(
            f"{self.modality}_not_built: this run type is a declared future "
            "launcher and is not implemented yet")


class EmulationLauncher(_NotBuiltLauncher):
    """Cross-OS execution via Wine / a VM (run a Windows .exe on macOS, etc.).
    Declared here so the matching-host refusal (S4/S5) has a named escape hatch;
    building it is out of v1 scope."""
    modality = "emulation"


class MobileLauncher(_NotBuiltLauncher):
    """iOS / Android simulators. Declared extension point; not built in v1."""
    modality = "mobile"


def _host_matches(req: dict, host: dict) -> bool:
    if req.get("os") != host.get("os"):
        return False
    req_arch = req.get("arch")
    return req_arch in ("universal", "unknown") or req_arch == host.get("arch")


class BinaryLauncher:
    """Run a compiled native executable (F101-03 S4).

    Refuses a foreign-OS/arch binary with a concrete reason (matching-host rule,
    D5 — emulation is a declared future launcher), then runs a matching binary as
    a time-boxed transcript under the F039 sandbox (a console program). A GUI
    binary path is deferred to a later slice.
    """
    modality = "binary"

    def launch(self, mgr: "RuntimeProcessManager",
               plan: "LaunchPlan") -> "RuntimeSession":
        from ..runtime import current_host_platform
        from ..runtime_process import RuntimeProcessError
        req = plan.host_requirements or {}
        host = current_host_platform()
        if req and not _host_matches(req, host):
            raise RuntimeProcessError(
                f"binary_host_mismatch: needs {req.get('os')}/{req.get('arch')}, "
                f"host is {host['os']}/{host['arch']}")
        return mgr.run_cli(plan.profile_id)


# F101-03 S5 — the host/residency matrix: which modalities a given host can run
# (D5). Local = full; a remote/SSH runtime host = a headless subset (server/cli/
# static tunnelled back, per F089); a windowed app needs a display; a native
# binary needs a matching, non-remote host.
_HEADLESS_MODALITIES = frozenset({"static", "server", "cli", "container"})


def can_launch(plan: "LaunchPlan", host) -> tuple[bool, str | None]:
    """Return ``(ok, reason)`` for running ``plan`` on ``host`` (a HostFacts).
    ``reason`` is a structured refusal string when ok is False (never a bare
    error) — the panel shows it instead of a broken run."""
    modality = plan.modality
    if modality in _HEADLESS_MODALITIES:
        return True, None
    if modality == "desktop":
        if host.is_remote:
            return False, "remote_host_has_no_display"
        if not host.has_display:
            return False, "no_display_on_host"
        return True, None
    if modality == "binary":
        if host.is_remote:
            return False, "cannot_ship_binary_to_remote_host"
        req = plan.host_requirements or {}
        if req and not _host_matches(req, {"os": host.os, "arch": host.arch}):
            return False, (f"binary_host_mismatch: needs {req.get('os')}/"
                           f"{req.get('arch')}, host is {host.os}/{host.arch}")
        return True, None
    if modality in ("emulation", "mobile"):
        return False, f"{modality}_not_built"
    return True, None


_REGISTRY: dict[str, Launcher] = {}


def register(launcher: Launcher) -> None:
    _REGISTRY[launcher.modality] = launcher


def get_launcher(modality: str) -> Launcher | None:
    return _REGISTRY.get(modality)


def registered_modalities() -> list[str]:
    return sorted(_REGISTRY)


for _launcher in (StaticLauncher(), ServerLauncher(), CliLauncher(),
                  DesktopLauncher(), BinaryLauncher(), ContainerLauncher(),
                  EmulationLauncher(), MobileLauncher()):
    register(_launcher)


__all__ = [
    "Launcher",
    "StaticLauncher",
    "ServerLauncher",
    "CliLauncher",
    "DesktopLauncher",
    "BinaryLauncher",
    "ContainerLauncher",
    "EmulationLauncher",
    "MobileLauncher",
    "register",
    "get_launcher",
    "registered_modalities",
    "can_launch",
]
