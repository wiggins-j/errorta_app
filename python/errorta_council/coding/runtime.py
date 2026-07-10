"""F101 S1 — coding runtime-preview profile/session store + detectors.

The durable runtime contract for a Coding project: how a generated project runs
(``coding_runtime_profile.v1``) and what happened when it last ran
(``RuntimeSession``). This module is PURE PERSISTENCE + DETECTION — it never
spawns a process, opens a socket, or wraps a sandbox. The sandboxed process
manager (S3, ``runtime_process.py``) is the only thing that executes generated
code, and it reads/writes through this store.

Ledger-local files under ``${ERRORTA_HOME}/council/coding-projects/<id>/``:

* ``runtime-profiles.json``  — profile_id -> profile (full-rewrite projection).
* ``runtime-sessions.jsonl`` — append-only session events; ``get_session``
  replays the last event per ``session_id`` (mirrors the backlog projection).

All writes are atomic (temp + rename, mode 0600) and serialized under the
existing per-project lock, mirroring the F099/F100 ledger writers
(``set_run_config`` / ``set_run_state``). Import surface is stdlib only — no
member, gateway, MCP, HTTP, or subprocess machinery (Council invariant 3).

The JSON shapes match ``docs/handoff/F101-frontend-engineer-handoff.md``
verbatim; the S0 canaries pin them.
"""
from __future__ import annotations

import ast
import json
import os
import platform
import re
import struct
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger import _atomic_write_json, _append_jsonl, _now, _read_jsonl, _split_unknown

PROFILE_SCHEMA = "coding_runtime_profile.v1"

PROFILE_KINDS = frozenset(
    {"static", "web", "api", "cli", "desktop", "binary", "container", "unknown"}
)
RUNTIME_MODES = frozenset({"static", "managed_local", "container"})
SANDBOX_CHOICES = frozenset({"auto", "seatbelt", "bwrap", "docker", "none"})
SESSION_STATES = frozenset(
    {"starting", "running", "healthy", "unhealthy", "crashed", "stopped"}
)
_CREATED_BY = frozenset({"pm", "dev", "user", "detector"})


class RuntimeError_(Exception):
    """Base class for runtime-profile validation/store failures."""


class RuntimeValidationError(RuntimeError_):
    """A profile failed structural/safety validation (fail-closed)."""


# --------------------------------------------------------------------------- #
# Data model — exact key sets from the frozen contract (S0 canaries pin these).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuntimeProfile:
    profile_id: str
    project_id: str
    kind: str = "unknown"
    runtime_mode: str = "managed_local"
    working_dir: str = "."
    setup: list[list[str]] = field(default_factory=list)
    start: list[str] = field(default_factory=list)
    stop: list[str] | None = None
    health: dict[str, Any] = field(default_factory=dict)
    demo: dict[str, Any] = field(default_factory=dict)
    ports: list[dict[str, Any]] = field(default_factory=list)
    env_required: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    sandbox: str = "auto"
    safety_warnings: list[str] = field(default_factory=list)
    created_by: str = "detector"
    updated_at: str = ""
    schema_version: str = PROFILE_SCHEMA
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "runtime_mode": self.runtime_mode,
            "working_dir": self.working_dir,
            "setup": [list(step) for step in self.setup],
            "start": list(self.start),
            "stop": list(self.stop) if self.stop is not None else None,
            "health": dict(self.health),
            "demo": dict(self.demo),
            "ports": [dict(p) for p in self.ports],
            "env_required": list(self.env_required),
            "tests": list(self.tests),
            "sandbox": self.sandbox,
            "safety_warnings": list(self.safety_warnings),
            "created_by": self.created_by,
            "updated_at": self.updated_at,
        }
        d.update(self._extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuntimeProfile":
        known, extras = _split_unknown(cls, raw)
        known.setdefault("schema_version", PROFILE_SCHEMA)
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class RuntimeSession:
    session_id: str
    profile_id: str
    state: str = "starting"
    pgid: int | None = None
    started_at: str = ""
    ended_at: str | None = None
    allocated_ports: list[int] = field(default_factory=list)
    sandbox_backend: str = "none"
    health_status: dict[str, Any] | None = None
    log_ref: str | None = None
    exit_code: int | None = None
    error: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "state": self.state,
            "pgid": self.pgid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "allocated_ports": list(self.allocated_ports),
            "sandbox_backend": self.sandbox_backend,
            "health_status": dict(self.health_status) if self.health_status is not None else None,
            "log_ref": self.log_ref,
            "exit_code": self.exit_code,
            "error": self.error,
        }
        d.update(self._extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuntimeSession":
        known, extras = _split_unknown(cls, raw)
        known["allocated_ports"] = list(known.get("allocated_ports") or [])
        return cls(**known, _extras=extras)


# --------------------------------------------------------------------------- #
# Validation — fail-closed. A profile that fails here is never partially stored.
# --------------------------------------------------------------------------- #
def _require_str_argv(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise RuntimeValidationError(f"{what} must be a list of strings")
    return [str(a) for a in value]


def validate_profile(raw: dict[str, Any], *, profile_id: str, project_id: str) -> RuntimeProfile:
    """Validate + normalize an inbound profile dict (PUT/detect output).

    Forces ``schema_version``/``profile_id``/``project_id`` from the trusted
    caller; checks enums, argv-array shapes, and the security boundary on
    ``working_dir`` (worktree-relative only — no absolute or parent-traversal
    dir; mirrors the F087-10 test-command cwd guard). Unknown keys round-trip
    via ``_extras`` so an editor never loses fields it didn't model.
    """
    if not isinstance(raw, dict):
        raise RuntimeValidationError("profile must be an object")

    kind = str(raw.get("kind", "unknown"))
    if kind not in PROFILE_KINDS:
        raise RuntimeValidationError(f"invalid kind: {kind!r}")
    runtime_mode = str(raw.get("runtime_mode", "managed_local"))
    if runtime_mode not in RUNTIME_MODES:
        raise RuntimeValidationError(f"invalid runtime_mode: {runtime_mode!r}")
    sandbox = str(raw.get("sandbox", "auto"))
    if sandbox not in SANDBOX_CHOICES:
        raise RuntimeValidationError(f"invalid sandbox: {sandbox!r}")
    created_by = str(raw.get("created_by", "user"))
    if created_by not in _CREATED_BY:
        raise RuntimeValidationError(f"invalid created_by: {created_by!r}")

    working_dir = str(raw.get("working_dir", "."))
    if working_dir.startswith("/") or working_dir.startswith("\\") or ".." in Path(working_dir).parts:
        raise RuntimeValidationError("working_dir must be worktree-relative")

    setup_raw = raw.get("setup", []) or []
    if not isinstance(setup_raw, list):
        raise RuntimeValidationError("setup must be a list of argv arrays")
    setup = [_require_str_argv(step, "setup step") for step in setup_raw]

    start = _require_str_argv(raw.get("start", []) or [], "start")

    stop_raw = raw.get("stop")
    stop = _require_str_argv(stop_raw, "stop") if stop_raw is not None else None

    health = raw.get("health") or {}
    demo = raw.get("demo") or {}
    if not isinstance(health, dict) or not isinstance(demo, dict):
        raise RuntimeValidationError("health and demo must be objects")

    ports_raw = raw.get("ports", []) or []
    if not isinstance(ports_raw, list) or not all(isinstance(p, dict) for p in ports_raw):
        raise RuntimeValidationError("ports must be a list of objects")
    ports = [dict(p) for p in ports_raw]

    env_required = raw.get("env_required", []) or []
    if not isinstance(env_required, list) or not all(isinstance(e, str) for e in env_required):
        raise RuntimeValidationError("env_required must be a list of strings")

    tests = raw.get("tests", []) or []
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        raise RuntimeValidationError("tests must be a list of strings")

    safety = raw.get("safety_warnings", []) or []
    if not isinstance(safety, list) or not all(isinstance(s, str) for s in safety):
        raise RuntimeValidationError("safety_warnings must be a list of strings")

    # A managed_local/container runtime with no start argv is unrunnable; a
    # static profile legitimately has none (Errorta opens the artifact directly).
    if runtime_mode != "static" and not start:
        raise RuntimeValidationError("start argv required for a non-static runtime")

    _, extras = _split_unknown(RuntimeProfile, raw)

    return RuntimeProfile(
        profile_id=str(profile_id),
        project_id=str(project_id),
        kind=kind,
        runtime_mode=runtime_mode,
        working_dir=working_dir,
        setup=setup,
        start=start,
        stop=stop,
        health=dict(health),
        demo=dict(demo),
        ports=ports,
        env_required=[str(e) for e in env_required],
        tests=[str(t) for t in tests],
        sandbox=sandbox,
        safety_warnings=[str(s) for s in safety],
        created_by=created_by,
        updated_at=_now(),
        schema_version=PROFILE_SCHEMA,
        _extras=extras,
    )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class RuntimeProfileStore:
    """Read/write a project's runtime profiles + session log.

    Constructed from a ``LedgerStore`` (``for_ledger``) so it shares the
    project's directory and per-project lock. Profiles are a full-rewrite
    projection (one JSON doc keyed by profile_id); sessions are append-only and
    projected last-event-per-id (mirrors the backlog).
    """

    def __init__(self, project_dir: Path, lock: Any) -> None:
        self._dir = Path(project_dir)
        self._lock = lock

    @classmethod
    def for_ledger(cls, store: Any) -> "RuntimeProfileStore":
        return cls(store.dir, store.lock)

    @property
    def _profiles_path(self) -> Path:
        return self._dir / "runtime-profiles.json"

    @property
    def _sessions_path(self) -> Path:
        return self._dir / "runtime-sessions.jsonl"

    # -- profiles ---------------------------------------------------------- #
    def _profiles_raw(self) -> dict[str, Any]:
        if not self._profiles_path.exists():
            return {}
        try:
            raw = json.loads(self._profiles_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def list_profiles(self) -> list[RuntimeProfile]:
        return [RuntimeProfile.from_dict(p) for p in self._profiles_raw().values()]

    def get_profile(self, profile_id: str) -> RuntimeProfile | None:
        raw = self._profiles_raw().get(profile_id)
        return RuntimeProfile.from_dict(raw) if isinstance(raw, dict) else None

    def upsert_profile(self, profile: RuntimeProfile) -> RuntimeProfile:
        with self._lock:
            profiles = self._profiles_raw()
            profiles[profile.profile_id] = profile.to_dict()
            _atomic_write_json(self._profiles_path, profiles)
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            profiles = self._profiles_raw()
            if profile_id not in profiles:
                return False
            del profiles[profile_id]
            _atomic_write_json(self._profiles_path, profiles)
        return True

    # -- sessions (append-only event log; last event per session_id wins) -- #
    def append_session(self, session: RuntimeSession) -> RuntimeSession:
        with self._lock:
            _append_jsonl(self._sessions_path, session.to_dict())
        return session

    def update_session(self, session_id: str, **patch: Any) -> RuntimeSession:
        with self._lock:
            cur = self._projected_session(session_id)
            if cur is None:
                raise RuntimeError_(f"unknown session: {session_id}")
            raw = cur.to_dict()
            raw.update(patch)
            updated = RuntimeSession.from_dict(raw)
            _append_jsonl(self._sessions_path, updated.to_dict())
            return updated

    def _projected_session(self, session_id: str) -> RuntimeSession | None:
        found: RuntimeSession | None = None
        for raw in _read_jsonl(self._sessions_path):
            if raw.get("session_id") == session_id:
                found = RuntimeSession.from_dict(raw)
        return found

    def get_session(self, session_id: str) -> RuntimeSession | None:
        return self._projected_session(session_id)

    def list_sessions(self) -> list[RuntimeSession]:
        proj: dict[str, RuntimeSession] = {}
        order: list[str] = []
        for raw in _read_jsonl(self._sessions_path):
            sid = raw.get("session_id")
            if not sid:
                continue
            if sid not in proj:
                order.append(sid)
            proj[sid] = RuntimeSession.from_dict(raw)
        return [proj[sid] for sid in order]

    def new_session_id(self) -> str:
        return f"rs-{uuid.uuid4().hex[:12]}"

    # -- runtime test evidence (F101 S4; append-only, head-bound) ----------- #
    @property
    def _runtime_tests_path(self) -> Path:
        return self._dir / "runtime-tests.jsonl"

    def record_runtime_test(self, *, kind: str, profile_id: str,
                            session_id: str, passed: bool, head: str,
                            detail: str = "") -> dict[str, Any]:
        """Append one runtime-test verdict, bound to profile id + session id +
        the worktree ``head`` it ran against (same staleness discipline as the
        F087-10 test-run records — a pass against an old head is stale)."""
        rec = {
            "runtime_test_id": f"rt-{uuid.uuid4().hex[:12]}",
            "kind": str(kind), "profile_id": str(profile_id),
            "session_id": str(session_id), "passed": bool(passed),
            "head": str(head), "detail": str(detail)[:2000], "at": _now(),
        }
        with self._lock:
            _append_jsonl(self._runtime_tests_path, rec)
        return rec

    def list_runtime_tests(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._runtime_tests_path)


RUNTIME_TEST_KINDS = ("runtime_start", "health_check", "demo_smoke",
                      "cli_transcript", "launch")


def latest_runtime_evidence(rstore: "RuntimeProfileStore", *,
                            current_head: str) -> dict[str, Any]:
    """Project the latest runtime-test verdict per (profile_id, kind), tagging
    each with ``fresh`` = it passed AND ran against ``current_head``. A pass
    against an older head is surfaced but is NOT fresh — the same head-binding
    rule the merge gate uses for F087-10 evidence. Runtime evidence is a WARN
    surface (D5): it is reported, not a merge blocker, until a project opts up.
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in rstore.list_runtime_tests():
        key = (str(rec.get("profile_id")), str(rec.get("kind")))
        latest[key] = rec  # last event per (profile, kind) wins
    results: list[dict[str, Any]] = []
    any_fresh = False
    for rec in latest.values():
        fresh = bool(rec.get("passed")) and bool(current_head) and \
            rec.get("head") == current_head
        any_fresh = any_fresh or fresh
        results.append({
            "kind": rec.get("kind"), "profile_id": rec.get("profile_id"),
            "session_id": rec.get("session_id"), "passed": rec.get("passed"),
            "head": rec.get("head"), "fresh": fresh,
            "detail": rec.get("detail", ""), "at": rec.get("at"),
        })
    results.sort(key=lambda r: (str(r["profile_id"]), str(r["kind"])))
    return {"results": results, "any_fresh_pass": any_fresh,
            "current_head": current_head}


# --------------------------------------------------------------------------- #
# Demo-repair brief (F101 S5): compose a dev task from a failed runtime so the
# Coding Team has the profile commands + session outcome + redacted log tail to
# act on, and the reviewer has the runtime commands to vet for safety.
# --------------------------------------------------------------------------- #
def _fmt_argv(argv: list[str] | None) -> str:
    return " ".join(str(a) for a in argv) if argv else "(none)"


def build_repair_brief(
    *,
    profile: "RuntimeProfile | None",
    session: "RuntimeSession | None",
    log_lines: list[str] | None,
    max_log_lines: int = 40,
) -> tuple[str, str]:
    """Return ``(title, detail)`` for a runtime-repair dev task. ``detail`` is a
    plain-text brief: the runtime commands (for the reviewer to vet), the last
    session's outcome, and a bounded, already-redacted log tail. Pure — the
    caller supplies the (redacted) logs and the bound session."""
    pid = profile.profile_id if profile is not None else "default"
    state = f" ({session.state})" if session is not None else ""
    title = f"Fix runtime preview: {pid}{state}"

    lines: list[str] = [
        "The managed runtime preview did not reach a healthy state. Repair the "
        "runtime profile and/or the project so it starts and passes its health "
        "check, then re-run the runtime tests.",
        "",
    ]
    if profile is not None:
        lines += [
            f"Profile: {profile.profile_id} (kind={profile.kind}, "
            f"mode={profile.runtime_mode}, sandbox={profile.sandbox})",
            f"  working_dir: {profile.working_dir}",
            f"  setup: {' && '.join(_fmt_argv(s) for s in profile.setup) or '(none)'}",
            f"  start: {_fmt_argv(profile.start)}",
        ]
        if profile.stop:
            lines.append(f"  stop:  {_fmt_argv(profile.stop)}")
        if profile.health:
            lines.append(f"  health: {profile.health}")
        lines.append("")
    if session is not None:
        lines += [
            f"Last session: {session.session_id} state={session.state} "
            f"sandbox={session.sandbox_backend} exit_code={session.exit_code}",
        ]
        if session.error:
            lines.append(f"  error: {session.error}")
        if session.health_status:
            lines.append(f"  health_status: {session.health_status}")
        lines.append("")
    tail = list(log_lines or [])[-max_log_lines:]
    if tail:
        lines.append(f"Recent logs (redacted, last {len(tail)}):")
        lines += [f"  {ln}" for ln in tail]
        lines.append("")
    lines.append(
        "Reviewer: confirm the setup/start commands are scoped and safe "
        "(no destructive or out-of-workspace operations) before approving.")
    return title, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Detection — scan a coding workspace and propose runtime profiles.
# --------------------------------------------------------------------------- #
def _profile(
    *, profile_id: str, project_id: str, kind: str, runtime_mode: str,
    setup: list[list[str]] | None = None, start: list[str] | None = None,
    health: dict[str, Any] | None = None, demo: dict[str, Any] | None = None,
    ports: list[dict[str, Any]] | None = None, working_dir: str = ".",
    safety_warnings: list[str] | None = None,
) -> RuntimeProfile:
    return RuntimeProfile(
        profile_id=profile_id, project_id=project_id, kind=kind,
        runtime_mode=runtime_mode, working_dir=working_dir,
        setup=setup or [], start=start or [], stop=None,
        health=health or {"type": "none"}, demo=demo or {},
        ports=ports or [], env_required=[], tests=[], sandbox="auto",
        safety_warnings=safety_warnings or [], created_by="detector",
        updated_at=_now(),
    )


def _http_health(port_placeholder: str = "{port}") -> dict[str, Any]:
    return {"type": "http", "url": f"http://127.0.0.1:{port_placeholder}", "timeout_seconds": 20}


def _http_demo(port_placeholder: str = "{port}") -> dict[str, Any]:
    return {"type": "url", "url": f"http://127.0.0.1:{port_placeholder}"}


def _web_ports(preferred: int) -> list[dict[str, Any]]:
    return [{"name": "web", "container_port": None, "preferred": preferred}]


def _web_ports_fixed(preferred: int) -> list[dict[str, Any]]:
    """A web port the app OWNS: it hardcodes this port and ignores the ``PORT``
    env, so Errorta can't move it — the runtime must target this exact port for
    health/demo instead of allocating an ephemeral one (see
    ``RuntimeProcessManager._resolve_listen_port``)."""
    return [{"name": "web", "container_port": None, "preferred": preferred,
             "fixed": True}]


def _valid_port(raw: str) -> int | None:
    try:
        p = int(raw)
    except (TypeError, ValueError):
        return None
    return p if 1 <= p <= 65535 else None


# The port a Python web entrypoint binds, plus whether it's FIXED (the app
# hardcodes it and ignores the injected ``PORT`` env, so Errorta must target it
# exactly) or env-driven (the app reads ``os.environ["PORT"]`` and will honor the
# port Errorta allocates + injects). Order: the PORT-env default (env-driven —
# NOT fixed), then a framework ``.run(port=N)`` literal (fixed), then Flask's bare
# ``app.run()`` whose default 5000 is also hardcoded (fixed). Read-only,
# best-effort.
_PORT_ENV_RE = re.compile(
    r"""(?:os\.environ\.get|os\.getenv)\(\s*['"]PORT['"]\s*,\s*['"]?(\d{1,5})""")
_RUN_PORT_RE = re.compile(r"""\.run\([^)]*?\bport\s*=\s*(\d{1,5})""")
_FLASK_IMPORT_RE = re.compile(r"""^\s*(?:from\s+flask\b|import\s+flask\b)""", re.MULTILINE)
_DOT_RUN_RE = re.compile(r"""\.run\(""")


def _detect_listen_port(source: str) -> tuple[int, bool] | None:
    """Return ``(port, fixed)`` or ``None`` if no port is readable. ``fixed`` is
    True when the app hardcodes the port (ignores ``PORT`` env)."""
    # Strip line comments so a commented-out ``# app.run(port=9999)`` isn't
    # mistaken for the real bind. (A port inside a string literal is a rarer false
    # positive we accept.) First readable literal wins; a file with several
    # servers picks the first — best-effort, not semantic.
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    # Check the LITERAL run-port first. ``_RUN_PORT_RE`` only matches a bare digit
    # directly after ``port=``, so ``app.run(port=int(os.environ.get("PORT", 5000)))``
    # does NOT match here (the arg is an expression) and correctly falls through to
    # the env case below. Checking it first also stops an UNRELATED
    # ``os.getenv("PORT", …)`` elsewhere in the file from mislabeling a
    # hardcoded-port app as env-driven.
    m = _RUN_PORT_RE.search(code)
    if m is not None and (p := _valid_port(m.group(1))) is not None:
        return p, True  # hardcoded literal -> app owns the port
    m = _PORT_ENV_RE.search(code)
    if m is not None and (p := _valid_port(m.group(1))) is not None:
        return p, False  # reads PORT env -> Errorta controls the port
    if _FLASK_IMPORT_RE.search(code) and _DOT_RUN_RE.search(code):
        return 5000, True  # Flask's bare app.run() default port, also hardcoded.
    return None


def _read_entry_port(path: Path) -> tuple[int, bool] | None:
    """The ``(port, fixed)`` read from a web entrypoint file, or None if
    unreadable / not found. Never raises."""
    try:
        source = path.read_text("utf-8", errors="ignore")
    except OSError:
        return None
    return _detect_listen_port(source)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _detect_node(root: Path, project_id: str) -> RuntimeProfile | None:
    pkg_path = root / "package.json"
    if not pkg_path.exists():
        return None
    pkg = _read_json(pkg_path)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    deps = {}
    for key in ("dependencies", "devDependencies"):
        d = pkg.get(key)
        if isinstance(d, dict):
            deps.update(d)

    # F101-03 S6: Electron is a desktop app (opens its own window), not a web
    # server — a `package.json` with an electron dep + a "main" entry.
    if ("electron" in deps or "electron-builder" in deps) and pkg.get("main"):
        start = ["npm", "start"] if "start" in scripts else ["npx", "electron", "."]
        return _profile(
            profile_id="default", project_id=project_id, kind="desktop",
            runtime_mode="managed_local", setup=[["npm", "install"]], start=start,
            health={"type": "none"}, demo={"type": "window", "toolkit": "electron"},
            safety_warnings=["Desktop app — opens an Electron window."],
        )

    has_next = "next" in deps or any((root / f"next.config.{e}").exists() for e in ("js", "mjs", "ts"))
    has_vite = "vite" in deps or any((root / f"vite.config.{e}").exists() for e in ("js", "mjs", "ts"))

    if has_next:
        preferred = 3000
    elif has_vite:
        preferred = 5173
    else:
        preferred = 3000

    # Choose the run script by preference; fall back to a bare start.
    script = next((s for s in ("dev", "start", "preview") if s in scripts), None)
    if script is None and not (has_next or has_vite):
        # A package.json with no runnable script and no known web framework is a
        # weak signal — skip it rather than propose an unrunnable web profile.
        return None
    start = ["npm", "run", script] if script else (["npx", "next", "dev"] if has_next else ["npx", "vite"])

    return _profile(
        profile_id="default", project_id=project_id, kind="web",
        runtime_mode="managed_local", setup=[["npm", "install"]], start=start,
        health=_http_health(), demo=_http_demo(), ports=_web_ports(preferred),
    )


# F101-03: GUI toolkits whose import marks a Python file as a desktop app that
# opens its own OS window (not a web server or a stdout CLI).
_GUI_TOOLKITS = (
    "pygame", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "kivy",
    "arcade", "pyglet", "wx",
)
# Prefer conventional entrypoint names when several .py files qualify.
_PY_ENTRY_ORDER = {"game.py": 0, "main.py": 1, "app.py": 2, "__main__.py": 3}


def _imports_gui_toolkit(source: str) -> str | None:
    for toolkit in _GUI_TOOLKITS:
        if re.search(rf"^[ \t]*(?:import|from)[ \t]+{re.escape(toolkit)}\b",
                     source, re.MULTILINE):
            return toolkit
    return None


# F101-03 S? — top-level import name -> the PyPI package that provides it. This
# is how the runtime *grounds* a dependency install on the code itself: a bare
# ``game.py`` that ``import pygame`` needs ``pip install pygame`` even though the
# generator never wrote a requirements.txt. Curated on purpose (grounded-or-
# refuse): only a RECOGNIZED third-party import becomes an install — an unknown
# import is never turned into a guessed package name that would fail the install.
# Standard-library modules (incl. ``tkinter``) are intentionally absent: they
# need no install. Keep this list additive; when the generator learns to emit a
# requirements.txt that manifest wins over this scan (see ``_py_setup``).
_PYPI_BY_IMPORT: dict[str, str] = {
    # GUI toolkits (the desktop-app class this fixes).
    "pygame": "pygame", "PyQt5": "PyQt5", "PyQt6": "PyQt6",
    "PySide2": "PySide2", "PySide6": "PySide6", "kivy": "kivy",
    "arcade": "arcade", "pyglet": "pyglet", "wx": "wxPython",
    # Common libraries a generated demo reaches for. import name != pkg name
    # for the aliased ones (PIL/Pillow, cv2/opencv-python, …).
    "numpy": "numpy", "pandas": "pandas", "scipy": "scipy",
    "matplotlib": "matplotlib", "sklearn": "scikit-learn",
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp",
    "flask": "Flask", "fastapi": "fastapi", "uvicorn": "uvicorn",
    "starlette": "starlette", "pydantic": "pydantic",
    "PIL": "Pillow", "cv2": "opencv-python", "bs4": "beautifulsoup4",
    "yaml": "PyYAML", "dotenv": "python-dotenv", "rich": "rich",
    "sqlalchemy": "SQLAlchemy", "redis": "redis", "jinja2": "Jinja2",
    "click": "click", "typer": "typer",
}

_TEST_FILE_RE = re.compile(r"(?:^test_.*\.py$|_test\.py$|^conftest\.py$)")


def _top_level_imports(source: str) -> set[str]:
    """The set of top-level import roots in ``source`` (``import a.b`` -> ``a``,
    ``from a.b import c`` -> ``a``). Relative imports are ignored (intra-project).
    Best-effort: unparseable source yields an empty set, never a raise."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def _scan_pip_installs(root: Path) -> list[list[str]]:
    """A single grounded ``pip install`` step for the recognized third-party
    packages the project's *non-test* root ``*.py`` files import, or ``[]``.

    This is the manifest-less fallback: it reads what the code actually imports
    (grounding), maps only KNOWN imports to packages (refuse-to-guess), and skips
    test files so a dev-only ``import pytest`` never becomes a runtime dep."""
    packages: list[str] = []
    seen: set[str] = set()
    for path in _root_py_files(root):
        if _TEST_FILE_RE.search(path.name):
            continue
        try:
            source = path.read_text("utf-8", errors="ignore")
        except OSError:
            continue
        for module in _top_level_imports(source):
            pkg = _PYPI_BY_IMPORT.get(module)
            if pkg is not None and pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)
    return [["pip", "install", *packages]] if packages else []


def _py_setup(root: Path) -> list[list[str]]:
    """The install step for a Python project. A declared manifest wins (it may
    pin exact versions); absent one, we ground the install on the imports the
    code actually uses so a bare script's third-party deps still get installed."""
    if (root / "requirements.txt").exists():
        return [["pip", "install", "-r", "requirements.txt"]]
    if (root / "pyproject.toml").exists():
        return [["pip", "install", "-e", "."]]
    return _scan_pip_installs(root)


def _root_py_files(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.glob("*.py") if p.is_file()),
        key=lambda p: (_PY_ENTRY_ORDER.get(p.name, 9), p.name),
    )


_MAIN_GUARD_RE = re.compile(
    r"""^[ \t]*if[ \t]+__name__[ \t]*==[ \t]*['"]__main__['"][ \t]*:""",
    re.MULTILINE,
)


def _lone_main_guarded_script(root: Path) -> Path | None:
    """A root ``*.py`` with an ``if __name__ == '__main__':`` guard, preferring
    conventional names. Read-only; None if none qualifies."""
    for path in _root_py_files(root):
        try:
            source = path.read_text("utf-8", errors="ignore")
        except OSError:
            continue
        if _MAIN_GUARD_RE.search(source):
            return path
    return None


def _detect_desktop_python(root: Path, project_id: str) -> RuntimeProfile | None:
    """F101-03: a Python file importing a GUI toolkit is a desktop app that
    opens its own window (e.g. a bare ``game.py`` importing ``pygame`` — the
    pokemon case F101's detectors missed). Read-only scan of root ``*.py``."""
    for path in _root_py_files(root):
        try:
            source = path.read_text("utf-8", errors="ignore")
        except OSError:
            continue
        toolkit = _imports_gui_toolkit(source)
        if toolkit is not None:
            return _profile(
                profile_id="default", project_id=project_id, kind="desktop",
                runtime_mode="managed_local", setup=_py_setup(root),
                start=["python", path.name], health={"type": "none"},
                demo={"type": "window", "toolkit": toolkit},
                safety_warnings=[f"Desktop app — opens a {toolkit} window."],
            )
    return None


def _detect_godot(root: Path, project_id: str) -> RuntimeProfile | None:
    """F101-03: a Godot project (``project.godot`` at root) runs via the Godot
    engine, which opens its own OS window — a ``desktop`` runtime. Grounded on
    ``project.godot``; the ``godot`` engine binary is resolved from the host PATH
    at spawn (the same host assumption cargo/go/docker already make)."""
    if not (root / "project.godot").exists():
        return None
    return _profile(
        profile_id="default", project_id=project_id, kind="desktop",
        runtime_mode="managed_local", start=["godot", "--path", "."],
        health={"type": "none"}, demo={"type": "window", "toolkit": "godot"},
        safety_warnings=[
            "Desktop app — runs the Godot engine, which opens its own window. "
            "Requires the Godot engine on the host PATH."],
    )


# --------------------------------------------------------------------------- #
# F101-03 — additional common ecosystems. One small grounded detector each; the
# matching grounding rule lives in ``runtime_resolve.ground_start``. Every start
# command is a real, standard invocation for that ecosystem — never a guess (the
# engine/tool binary is a host-PATH dependency resolved at spawn, the same
# assumption cargo/go/docker already make).
# --------------------------------------------------------------------------- #
def _first_glob(root: Path, patterns: tuple[str, ...]) -> str | None:
    """The name of the first file matching any of ``patterns`` at ``root`` (in
    pattern order, then name order), or None. Read-only."""
    for pat in patterns:
        for p in sorted(root.glob(pat)):
            if p.is_file():
                return p.name
    return None


def _glob_names(root: Path, patterns: tuple[str, ...]) -> list[str]:
    """The names of all files matching any of ``patterns`` at ``root``. Read-only."""
    names: list[str] = []
    for pat in patterns:
        names += sorted(p.name for p in root.glob(pat) if p.is_file())
    return names


# A ``love.<api>`` call (love.graphics, love.draw, …). Requires a letter/underscore
# after the dot and no word char / dot before ``love`` so ``glove.left``, a bare
# ``love.`` at end-of-token, or ``I love. this`` in prose don't match.
_LOVE_API_RE = re.compile(r"(?<![\w.])love\.[A-Za-z_]")


def _detect_deno(root: Path, project_id: str) -> RuntimeProfile | None:
    """A Deno project, keyed on its ``deno.json``/``deno.jsonc`` marker (a bare
    ``main.js``/``main.ts`` is NOT a Deno signal — it's just as likely browser or
    Node code): a runnable task if the config declares one, else a conventional
    ``main``/``mod`` entry run via ``deno run``. Deno caches its deps on first
    run, so there's no separate setup step."""
    cfg = next((c for c in ("deno.json", "deno.jsonc") if (root / c).exists()), None)
    if cfg is None:
        return None
    data = _read_json(root / cfg)
    tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    task = next((t for t in ("start", "dev", "serve") if t in tasks), None)
    if task is None and len(tasks) == 1:
        # A single named task (any name) is the unambiguous entry to run.
        task = next(iter(tasks))
    if task is not None:
        argv = ["deno", "task", task]
        return _profile(
            profile_id="default", project_id=project_id, kind="cli",
            runtime_mode="managed_local", start=argv, health={"type": "none"},
            demo={"type": "command", "command": argv})
    entry = next((e for e in ("main.ts", "main.js", "mod.ts", "index.ts")
                  if (root / e).exists()), None)
    if entry is not None:
        argv = ["deno", "run", "-A", entry]
        return _profile(
            profile_id="default", project_id=project_id, kind="cli",
            runtime_mode="managed_local", start=argv, health={"type": "none"},
            demo={"type": "command", "command": argv})
    return None


def _detect_love(root: Path, project_id: str) -> RuntimeProfile | None:
    """A LÖVE (love2d) game: a ``main.lua`` that uses the ``love`` API (or a
    ``conf.lua`` alongside it). Runs via the ``love`` engine, which opens its own
    window — a ``desktop`` runtime grounded on ``main.lua``."""
    main_lua = root / "main.lua"
    if not main_lua.exists():
        return None
    is_love = (root / "conf.lua").exists()
    if not is_love:
        try:
            # A real ``love.<api>`` call — not a bare substring, which would
            # false-positive on ``glove.left``, ``-- I love. this``, or a string.
            is_love = bool(_LOVE_API_RE.search(
                main_lua.read_text("utf-8", errors="ignore")))
        except OSError:
            is_love = False
    if not is_love:
        return None
    return _profile(
        profile_id="default", project_id=project_id, kind="desktop",
        runtime_mode="managed_local", start=["love", "."],
        health={"type": "none"}, demo={"type": "window", "toolkit": "love2d"},
        safety_warnings=[
            "Desktop app — runs the LÖVE engine, which opens its own window. "
            "Requires the LÖVE (love2d) engine on the host PATH."])


def _detect_ruby(root: Path, project_id: str) -> RuntimeProfile | None:
    """A Ruby project: Rails (``bin/rails``) and Rack (``config.ru``) run as web
    servers; a lone ``main.rb``/``app.rb`` runs as a CLI. Setup installs gems when
    a ``Gemfile`` is present."""
    setup = [["bundle", "install"]] if (root / "Gemfile").exists() else []
    if (root / "bin" / "rails").exists():
        return _profile(
            profile_id="default", project_id=project_id, kind="web",
            runtime_mode="managed_local", setup=setup,
            start=["bin/rails", "server", "-b", "127.0.0.1", "-p", "{port}"],
            health=_http_health(), demo=_http_demo(), ports=_web_ports(3000))
    if (root / "config.ru").exists():
        return _profile(
            profile_id="default", project_id=project_id, kind="web",
            runtime_mode="managed_local", setup=setup,
            start=["rackup", "-o", "127.0.0.1", "-p", "{port}"],
            health=_http_health(), demo=_http_demo(), ports=_web_ports(9292))
    entry = next((e for e in ("main.rb", "app.rb") if (root / e).exists()), None)
    if entry is not None:
        argv = ["ruby", entry]
        return _profile(
            profile_id="default", project_id=project_id, kind="cli",
            runtime_mode="managed_local", setup=setup, start=argv,
            health={"type": "none"}, demo={"type": "command", "command": argv})
    return None


def _detect_php(root: Path, project_id: str) -> RuntimeProfile | None:
    """A PHP web app: Laravel (``artisan``) serves via ``php artisan serve``; a
    plain site (``index.php``) serves via PHP's built-in server. Setup installs
    Composer deps when a ``composer.json`` is present."""
    setup = [["composer", "install"]] if (root / "composer.json").exists() else []
    if (root / "artisan").exists():
        return _profile(
            profile_id="default", project_id=project_id, kind="web",
            runtime_mode="managed_local", setup=setup,
            start=["php", "artisan", "serve", "--host=127.0.0.1", "--port={port}"],
            health=_http_health(), demo=_http_demo(), ports=_web_ports(8000))
    if (root / "index.php").exists():
        return _profile(
            profile_id="default", project_id=project_id, kind="web",
            runtime_mode="managed_local", setup=setup,
            start=["php", "-S", "127.0.0.1:{port}"],
            health=_http_health(), demo=_http_demo(), ports=_web_ports(8000))
    return None


def _detect_dotnet(root: Path, project_id: str) -> RuntimeProfile | None:
    """A .NET / C# / F# project runs via ``dotnet run``. Requires exactly one
    project file (``*.csproj``/``*.fsproj``) at root and targets it explicitly
    with ``--project`` — a bare ``dotnet run`` fails ("Couldn't find a project" /
    "more than one project or solution file") when a ``.sln`` or a second project
    is also present, so an ambiguous / solution-only layout is refused rather than
    handed a command that fails at spawn. Classified as ``cli`` — an ASP.NET app
    runs the same way but its bound port isn't reliably knowable."""
    projs = _glob_names(root, ("*.csproj", "*.fsproj"))
    if len(projs) != 1:
        return None
    argv = ["dotnet", "run", "--project", projs[0]]
    return _profile(
        profile_id="default", project_id=project_id, kind="cli",
        runtime_mode="managed_local", start=argv, health={"type": "none"},
        demo={"type": "command", "command": argv})


# A Gradle build applies the ``application`` plugin (or declares a ``mainClass``)
# exactly when the ``./gradlew run`` task exists — so we only propose ``run`` when
# we can see that, rather than guessing a task that may not be defined. Matches
# the ``application`` plugin token in every common form — Groovy ``id 'application'``
# / ``apply plugin: 'application'``, the Kotlin-DSL bare accessor
# ``plugins { application }``, an ``application { ... }`` block — plus ``mainClass``.
# The lookbehind keeps ``configureApplication``/``myapplication`` from matching.
_GRADLE_APP_RE = re.compile(r"(?<![\w.])application\b|mainClass")


def _detect_gradle(root: Path, project_id: str) -> RuntimeProfile | None:
    """A Gradle JVM app (Java/Kotlin) with the ``application`` plugin runs via
    ``./gradlew run``. Requires the Gradle wrapper (grounding) AND an application
    entrypoint (so the ``run`` task actually exists)."""
    if not (root / "gradlew").exists():
        return None
    build = next((b for b in ("build.gradle", "build.gradle.kts")
                  if (root / b).exists()), None)
    if build is None:
        return None
    try:
        text = (root / build).read_text("utf-8", errors="ignore")
    except OSError:
        return None
    if not _GRADLE_APP_RE.search(text):
        return None
    argv = ["./gradlew", "run"]
    return _profile(
        profile_id="default", project_id=project_id, kind="cli",
        runtime_mode="managed_local", start=argv, health={"type": "none"},
        demo={"type": "command", "command": argv})


def _detect_python(root: Path, project_id: str) -> RuntimeProfile | None:
    has_pyproject = (root / "pyproject.toml").exists()
    has_reqs = (root / "requirements.txt").exists()
    app_py = (root / "app.py").exists()
    main_py = (root / "main.py").exists()
    dunder_main = (root / "__main__.py").exists()

    if not (has_pyproject or has_reqs or app_py or main_py or dunder_main):
        # F101-03: no conventional entrypoint, but a lone ``__main__``-guarded
        # script (e.g. a bare ``tool.py``) is still runnable as a CLI — fixes the
        # bare-file miss. Non-GUI (desktop is detected before this).
        script = _lone_main_guarded_script(root)
        if script is not None:
            argv = ["python", script.name]
            return _profile(
                profile_id="default", project_id=project_id, kind="cli",
                runtime_mode="managed_local", setup=_py_setup(root), start=argv,
                health={"type": "none"},
                demo={"type": "command", "command": argv},
            )
        return None

    setup = _py_setup(root)

    if app_py:
        # A web app entrypoint (Flask/FastAPI convention). Read the port the
        # entrypoint actually binds (a hardcoded ``app.run(port=5000)``, a
        # ``os.environ.get("PORT", N)`` default, or Flask's bare-run 5000) so
        # health and the demo URL probe the real port — not a guessed 8000 that
        # leaves a working app looking crashed. A hardcoded port is FIXED (the app
        # ignores the injected PORT, so the runtime targets it exactly instead of
        # allocating an ephemeral one). 8000 stays the env-driven fallback.
        detected = _read_entry_port(root / "app.py")
        ports = (_web_ports_fixed(detected[0]) if detected and detected[1]
                 else _web_ports(detected[0] if detected else 8000))
        return _profile(
            profile_id="default", project_id=project_id, kind="api",
            runtime_mode="managed_local", setup=setup,
            start=["python", "app.py"], health=_http_health(),
            demo=_http_demo(), ports=ports,
        )
    if main_py or dunder_main:
        argv = ["python", "main.py"] if main_py else ["python", "-m", "."]
        return _profile(
            profile_id="default", project_id=project_id, kind="cli",
            runtime_mode="managed_local", setup=setup, start=argv,
            health={"type": "none"},
            demo={"type": "command", "command": argv},
        )
    # Has packaging metadata but no obvious entrypoint — propose an unknown CLI
    # shell the user can fill in, rather than guessing.
    return _profile(
        profile_id="default", project_id=project_id, kind="unknown",
        runtime_mode="managed_local", setup=setup, start=[],
        health={"type": "none"}, demo={},
        safety_warnings=["No entrypoint detected — set the start command."],
    )


def _detect_static(root: Path, project_id: str, *, has_primary: bool) -> RuntimeProfile | None:
    if not (root / "index.html").exists():
        return None
    # F101-01: a static / SPA site is SERVED over loopback HTTP, not opened via
    # file:// (file:// breaks ES-module + relative-fetch SPAs — opaque/null
    # origin). Promote static to a managed_local runtime whose `start` is the
    # stdlib http.server run from the confined working_dir; it then inherits the
    # entire managed-local lifecycle (port allocation, sandboxed spawn, log pump,
    # http health probe, group teardown) with NO new lifecycle code — only the
    # detector's output shape changes.
    #
    # If a managed runtime (a web framework) already owns "default", the static
    # served runtime is a SECONDARY proposal under its own id.
    pid = "static" if has_primary else "default"
    return _profile(
        profile_id=pid, project_id=project_id, kind="static",
        runtime_mode="managed_local", working_dir=".", setup=[],
        start=["python", "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
        health=_http_health(), demo=_http_demo(), ports=_web_ports(8000),
    )


# --------------------------------------------------------------------------- #
# F101-03 S4 — native binaries + build-then-run manifests.
# --------------------------------------------------------------------------- #
def _host_os() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    return sys.platform


def _host_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine or "unknown"


def current_host_platform() -> dict[str, str]:
    """The runtime host's ``{os, arch}`` — what a native binary must match to
    run here (F101-03 D5)."""
    return {"os": _host_os(), "arch": _host_arch()}


def _host_has_display() -> bool:
    """Whether the runtime host can show a window. On Linux that means an X11 /
    Wayland display is present; macOS/Windows sidecars run in the user's GUI
    session, so a window is available there."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return sys.platform in ("darwin", "win32") or sys.platform.startswith("win")


@dataclass(frozen=True)
class HostFacts:
    """What the runtime host can do — gates the offered modality matrix (D5).

    ``is_remote`` is always False today: runtime execution is local/loopback by
    design (there is no remote-runtime data plane; a remote AIAR residency does
    not move runtime exec off-box). A future remote-runtime slice flips it, at
    which point the desktop/binary refusals below become live."""
    has_display: bool
    os: str
    arch: str
    is_remote: bool = False

    @classmethod
    def local(cls) -> "HostFacts":
        plat = current_host_platform()
        return cls(has_display=_host_has_display(), os=plat["os"],
                   arch=plat["arch"], is_remote=False)

    def to_dict(self) -> dict[str, Any]:
        return {"has_display": self.has_display, "os": self.os,
                "arch": self.arch, "is_remote": self.is_remote}


def _macho_arch(cputype: int) -> str:
    abi64 = cputype & 0x01000000
    base = cputype & ~0x01000000
    if base == 12:   # CPU_TYPE_ARM
        return "arm64" if abi64 else "arm"
    if base == 7:    # CPU_TYPE_X86
        return "x86_64" if abi64 else "x86"
    return "unknown"


_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "arm64", 0x28: "arm", 0x03: "x86"}
_MACHO_THIN = {
    b"\xcf\xfa\xed\xfe": "<", b"\xce\xfa\xed\xfe": "<",
    b"\xfe\xed\xfa\xcf": ">", b"\xfe\xed\xfa\xce": ">",
}


def binary_host_requirements(path: Path) -> dict[str, str] | None:
    """The ``{os, arch}`` a native executable needs, read from its Mach-O / ELF
    header — or None if ``path`` isn't a recognized native binary. Read-only."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(20)
    except OSError:
        return None
    if len(head) < 8:
        return None
    if head[:4] == b"\x7fELF":
        # A real ELF header is >= 52 bytes; the ``e_machine`` field lives at
        # bytes 18:20. A truncated/corrupt ``+x`` file that starts with the ELF
        # magic but is shorter than 20 bytes would make ``head[18:20]`` a short
        # slice and crash ``struct.unpack`` — refuse it (not a runnable binary)
        # rather than 500 the detect/run route (invariant: refuse, never crash).
        if len(head) < 20:
            return None
        endian = "<" if head[5] == 1 else ">"
        machine = struct.unpack(endian + "H", head[18:20])[0]
        return {"os": "linux", "arch": _ELF_MACHINES.get(machine, "unknown")}
    magic = head[:4]
    if magic in _MACHO_THIN:
        cputype = struct.unpack(_MACHO_THIN[magic] + "I", head[4:8])[0]
        return {"os": "macos", "arch": _macho_arch(cputype)}
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):  # fat / universal
        return {"os": "macos", "arch": "universal"}
    return None


_BINARY_SEARCH_DIRS = (".", "bin", "dist", "build")
_BINARY_NAME_ORDER = {"main": 0, "app": 1, "run": 2, "start": 3}


def _detect_native_binary(root: Path, project_id: str) -> RuntimeProfile | None:
    """A compiled executable (Mach-O / ELF) at root or in bin/dist/build ⇒ a
    ``binary`` runtime, recording the os/arch it needs (BinaryLauncher refuses a
    foreign host). Read-only header sniff — never executed here."""
    candidates: list[Path] = []
    for sub in _BINARY_SEARCH_DIRS:
        base = root / sub if sub != "." else root
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_file() and os.access(entry, os.X_OK):
                candidates.append(entry)
    candidates.sort(key=lambda p: (_BINARY_NAME_ORDER.get(p.stem.lower(), 9), str(p)))
    for path in candidates:
        req = binary_host_requirements(path)
        if req is None:
            continue
        rel = path.relative_to(root).as_posix()
        return RuntimeProfile(
            profile_id="default", project_id=project_id, kind="binary",
            runtime_mode="managed_local", working_dir=".", start=[f"./{rel}"],
            health={"type": "none"},
            demo={"type": "command", "command": [f"./{rel}"]},
            safety_warnings=[
                f"Native binary ({req['os']}/{req['arch']}) — runs only on a "
                "matching host."],
            created_by="detector", updated_at=_now(),
            _extras={"host_requirements": req},
        )
    return None


def _detect_build_manifest(root: Path, project_id: str) -> RuntimeProfile | None:
    """Rust / Go / Make projects run via their build tool (``cargo run`` /
    ``go run`` / ``make``). Grounded on the manifest, not the built artifact
    (which doesn't exist until the build runs)."""
    if (root / "Cargo.toml").exists():
        return _profile(
            profile_id="default", project_id=project_id, kind="cli",
            runtime_mode="managed_local", start=["cargo", "run"],
            health={"type": "none"},
            demo={"type": "command", "command": ["cargo", "run"]},
        )
    if (root / "go.mod").exists():
        return _profile(
            profile_id="default", project_id=project_id, kind="cli",
            runtime_mode="managed_local", start=["go", "run", "."],
            health={"type": "none"},
            demo={"type": "command", "command": ["go", "run", "."]},
        )
    makefile = next((n for n in ("Makefile", "makefile") if (root / n).exists()), None)
    if makefile is not None:
        try:
            targets = (root / makefile).read_text("utf-8", errors="ignore")
        except OSError:
            targets = ""
        target = next((t for t in ("run", "start") if re.search(rf"^{t}[ \t]*:",
                                                                 targets, re.MULTILINE)), None)
        if target is not None:
            return _profile(
                profile_id="default", project_id=project_id, kind="cli",
                runtime_mode="managed_local", start=["make", target],
                health={"type": "none"},
                demo={"type": "command", "command": ["make", target]},
            )
    return None


def _container_image_name(project_id: str) -> str:
    """A docker-safe (lowercase) image tag for a Dockerfile-only project."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", project_id.lower()).strip("-") or "app"
    return f"errorta-preview-{slug}"


def _detect_container(root: Path, project_id: str) -> RuntimeProfile | None:
    has_dockerfile = (root / "Dockerfile").exists()
    has_compose = any((root / n).exists() for n in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"))
    if not (has_dockerfile or has_compose):
        return None
    # F101 S6: container runtime is now EXECUTABLE — propose real docker commands.
    # The runtime runs OUTSIDE the F039 OS sandbox (the container is the
    # isolation) and fails closed when docker is unavailable.
    note = (
        "Container runtime requires Docker and runs outside the F039 OS sandbox "
        "(the container is the isolation). Review the commands and align the "
        "published port with what the container listens on.")
    if has_compose:
        setup: list[list[str]] = []
        start = ["docker", "compose", "up", "--build"]
        stop: list[str] | None = ["docker", "compose", "down"]
    else:  # Dockerfile only
        image = _container_image_name(project_id)
        name = image  # a stable container name so teardown is deterministic
        setup = [["docker", "build", "-t", image, "."]]
        # --rm auto-removes on a clean stop; the explicit `docker rm -f` on
        # teardown also removes it if the run client was SIGKILLed (grace
        # exceeded) — so a container never leaks detached.
        start = ["docker", "run", "--rm", "--name", name,
                 "-p", "127.0.0.1:{port}:8000", image]
        stop = ["docker", "rm", "-f", name]
    return RuntimeProfile(
        profile_id="container", project_id=project_id, kind="container",
        runtime_mode="container", working_dir=".", setup=setup, start=start,
        stop=stop, health=_http_health(), demo=_http_demo(),
        ports=_web_ports(8000), env_required=[], tests=[], sandbox="auto",
        safety_warnings=[note], created_by="detector", updated_at=_now())


def detect(workspace_root: str | Path, *, project_id: str) -> list[RuntimeProfile]:
    """Scan a coding workspace and propose runtime profiles (best-effort).

    Detection is read-only and never executes anything. Returns an ordered list
    of proposals (the first managed/static runtime owns ``profile_id="default"``;
    secondary signals get descriptive ids). An empty list means "No runnable
    demo detected" — a first-class honest state, not an error.
    """
    root = Path(workspace_root)
    proposals: list[RuntimeProfile] = []

    # Desktop (GUI window) is checked before the generic Python detector so a
    # pygame/Tk file classifies as `desktop`, not a stdout `cli` (F101-03).
    primary = (
        _detect_node(root, project_id)
        or _detect_deno(root, project_id)
        or _detect_desktop_python(root, project_id)
        # Engine-driven desktop games (``project.godot`` / a LÖVE ``main.lua``) are
        # unambiguous signals — checked before the generic language detectors.
        or _detect_godot(root, project_id)
        or _detect_love(root, project_id)
        or _detect_python(root, project_id)
        or _detect_ruby(root, project_id)
        or _detect_php(root, project_id)
        or _detect_dotnet(root, project_id)
        or _detect_gradle(root, project_id)
        # Build-from-source (cargo/go/make) before a bare compiled binary so a
        # source project runs via its build tool (F101-03 S4).
        or _detect_build_manifest(root, project_id)
        or _detect_native_binary(root, project_id)
    )
    if primary is not None:
        proposals.append(primary)

    static = _detect_static(root, project_id, has_primary=primary is not None)
    if static is not None:
        proposals.append(static)

    container = _detect_container(root, project_id)
    if container is not None:
        proposals.append(container)

    return proposals


__all__ = [
    "PROFILE_SCHEMA",
    "PROFILE_KINDS",
    "RUNTIME_MODES",
    "SANDBOX_CHOICES",
    "SESSION_STATES",
    "RuntimeProfile",
    "RuntimeSession",
    "RuntimeProfileStore",
    "RuntimeValidationError",
    "RuntimeError_",
    "RUNTIME_TEST_KINDS",
    "validate_profile",
    "detect",
    "latest_runtime_evidence",
    "build_repair_brief",
    "current_host_platform",
    "binary_host_requirements",
    "HostFacts",
]
