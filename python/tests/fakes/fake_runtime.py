"""FakeRuntime — in-memory implementation of the F101 runtime-preview contract.

Lets the Run & Preview panel (S2, frontend) and the backend route tests develop
in parallel BEFORE the real profile store (S1) and sandboxed process manager (S3)
land. The shapes match ``docs/handoff/F101-frontend-engineer-handoff.md`` exactly
(``coding_runtime_profile.v1`` + ``RuntimeSession``); the flip from this fake to
the real store/process-manager is a provider swap, and the contract-targeted
canary tests stay green.

Deterministic by construction: ``start`` returns a session in ``starting`` and
each subsequent ``get_session`` advances the state machine one step
(starting -> running -> healthy, then sticks at healthy). No real process, no
sockets, no sandbox, no filesystem writes — purely in memory.

This is a TEST DOUBLE: it never executes generated code. The real S3 manager is
the only thing that spawns children, and it does so through the F039 sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROFILE_SCHEMA = "coding_runtime_profile.v1"

# The frozen route table (handoff doc). The canary tests pin against this so a
# silent route rename can't drift the FE/BE seam. Each entry is
# (method, suffix-under-/coding/projects/{id}). ``{pid}`` = profile_id,
# ``{sid}`` = session_id.
RUNTIME_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/runtime/profiles"),
    ("PUT", "/runtime/profiles/{pid}"),
    ("POST", "/runtime/detect"),
    ("POST", "/runtime/{pid}/setup"),
    ("POST", "/runtime/{pid}/start"),
    ("POST", "/runtime/{pid}/stop"),
    ("GET", "/runtime/sessions/{sid}"),
    ("GET", "/runtime/sessions/{sid}/logs"),
    ("POST", "/runtime/{pid}/health-check"),
    ("POST", "/runtime/{pid}/test"),
)

# Exact key sets of the two frozen JSON shapes (handoff doc). Pinned by canaries.
PROFILE_KEYS = frozenset({
    "schema_version", "profile_id", "project_id", "kind", "runtime_mode",
    "working_dir", "setup", "start", "stop", "health", "demo", "ports",
    "env_required", "tests", "sandbox", "safety_warnings", "created_by",
    "updated_at",
})
SESSION_KEYS = frozenset({
    "session_id", "profile_id", "state", "pgid", "started_at", "ended_at",
    "allocated_ports", "sandbox_backend", "health_status", "log_ref",
    "exit_code", "error",
})

PROFILE_KINDS = ("static", "web", "api", "cli", "desktop", "container", "unknown")
RUNTIME_MODES = ("static", "managed_local", "container")
SESSION_STATES = (
    "starting", "running", "healthy", "unhealthy", "crashed", "stopped",
)
SANDBOX_CHOICES = ("auto", "seatbelt", "bwrap", "docker", "none")
SANDBOX_BACKENDS = ("seatbelt", "bwrap", "docker", "none")


def canned_profile(
    *,
    project_id: str = "demo",
    profile_id: str = "default",
    kind: str = "web",
    runtime_mode: str = "managed_local",
    sandbox: str = "auto",
    preferred_port: int = 5173,
) -> dict[str, Any]:
    """A ``coding_runtime_profile.v1`` dict in the frozen shape."""
    return {
        "schema_version": PROFILE_SCHEMA,
        "profile_id": profile_id,
        "project_id": project_id,
        "kind": kind,
        "runtime_mode": runtime_mode,
        "working_dir": ".",
        "setup": [["npm", "install"]],
        "start": ["npm", "run", "dev"],
        "stop": None,
        "health": {
            "type": "http",
            "url": "http://127.0.0.1:{port}",
            "timeout_seconds": 20,
        },
        "demo": {"type": "url", "url": "http://127.0.0.1:{port}"},
        "ports": [
            {"name": "web", "container_port": None, "preferred": preferred_port}
        ],
        "env_required": [],
        "tests": ["unit", "typecheck"],
        "sandbox": sandbox,
        "safety_warnings": [],
        "created_by": "detector",
        "updated_at": "2026-06-20T00:00:00Z",
    }


def canned_session(
    *,
    session_id: str,
    profile_id: str = "default",
    state: str = "starting",
    sandbox_backend: str = "seatbelt",
    allocated_ports: list[int] | None = None,
) -> dict[str, Any]:
    """A ``RuntimeSession`` dict in the frozen shape."""
    return {
        "session_id": session_id,
        "profile_id": profile_id,
        "state": state,
        "pgid": 12345,
        "started_at": "2026-06-20T00:00:00Z",
        "ended_at": None,
        "allocated_ports": list(allocated_ports or [5173]),
        "sandbox_backend": sandbox_backend,
        "health_status": None,
        "log_ref": f"runtime/{session_id}.log",
        "exit_code": None,
        "error": None,
    }


@dataclass
class FakeRuntime:
    """In-memory backend matching the frozen runtime route surface.

    Methods mirror the route handlers' delegated calls (S1/S3). State is held in
    plain dicts; sessions advance deterministically on each ``get_session``.
    """

    project_id: str = "demo"
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    sandbox_backend: str = "seatbelt"
    _seq: int = 0

    def __post_init__(self) -> None:
        if not self.profiles:
            prof = canned_profile(project_id=self.project_id)
            self.profiles[prof["profile_id"]] = prof

    # -- profile CRUD (S1 surface) ---------------------------------------- #
    def list_profiles(self) -> dict[str, Any]:
        return {"profiles": list(self.profiles.values())}

    def put_profile(self, profile_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        stored = dict(profile)
        stored["schema_version"] = PROFILE_SCHEMA
        stored["profile_id"] = profile_id
        stored["project_id"] = self.project_id
        self.profiles[profile_id] = stored
        return {"profile": stored}

    def detect(self) -> dict[str, Any]:
        return {"proposed": [canned_profile(project_id=self.project_id)]}

    # -- process lifecycle (S3 surface) ----------------------------------- #
    def _new_session_id(self) -> str:
        self._seq += 1
        return f"sess-{self._seq:04d}"

    def setup(self, profile_id: str) -> dict[str, Any]:
        # Setup runs the (sandboxed) install step; the fake returns a session in
        # ``starting`` like start does, so the panel can show setup progress.
        return self.start(profile_id)

    def start(self, profile_id: str) -> dict[str, Any]:
        sid = self._new_session_id()
        sess = canned_session(
            session_id=sid, profile_id=profile_id,
            sandbox_backend=self.sandbox_backend, state="starting",
        )
        self.sessions[sid] = sess
        return {"session": dict(sess)}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        sess = self.sessions.get(session_id)
        if sess is None:
            return None
        self._advance(sess)
        return {"session": dict(sess)}

    def _advance(self, sess: dict[str, Any]) -> None:
        order = ["starting", "running", "healthy"]
        state = sess.get("state")
        if state in order and state != "healthy":
            nxt = order[order.index(state) + 1]
            sess["state"] = nxt
            if nxt == "healthy":
                sess["health_status"] = {"ok": True, "detail": "200 OK"}

    def stop(self, profile_id: str) -> dict[str, Any]:
        for sess in self.sessions.values():
            if sess.get("profile_id") == profile_id and sess.get("ended_at") is None:
                sess["state"] = "stopped"
                sess["ended_at"] = "2026-06-20T00:01:00Z"
                sess["exit_code"] = 0
        return {"stopped": True}

    def get_logs(self, session_id: str) -> dict[str, Any]:
        return {
            "lines": [
                "> demo@0.0.0 dev",
                "> vite",
                "  VITE v5.0.0  ready in 312 ms",
                "  Local:   http://127.0.0.1:5173/",
            ],
            "truncated": False,
        }

    def health_check(self, profile_id: str) -> dict[str, Any]:
        return {"health_status": {"ok": True, "detail": "200 OK"}}

    def run_test(self, profile_id: str, kind: str) -> dict[str, Any]:
        return {
            "result": {
                "kind": kind,
                "passed": True,
                "profile_id": profile_id,
                "detail": "fake runtime test passed",
            }
        }


__all__ = [
    "FakeRuntime",
    "PROFILE_SCHEMA",
    "PROFILE_KEYS",
    "SESSION_KEYS",
    "RUNTIME_ROUTES",
    "PROFILE_KINDS",
    "RUNTIME_MODES",
    "SESSION_STATES",
    "SANDBOX_CHOICES",
    "SANDBOX_BACKENDS",
    "canned_profile",
    "canned_session",
]
