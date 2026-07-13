"""F101 S3 — sandboxed managed-local runtime process manager.

This is the only module in F101 that EXECUTES generated code. It starts a
Coding project as a child process **through the F039 sandbox** (D2), confines it
to a loopback port, streams capped + redacted logs, polls a health check, and —
critically — tears the process GROUP down reliably on stop / project-switch /
app-exit (D3), reusing the F089 SSH-tunnel teardown discipline. Sessions are
never auto-resumed across a sidecar restart: a restart leaves no orphaned
servers and no bound ports, and any session left non-terminal on disk is
reconciled to ``crashed``.

Security posture (spec §"Security and trust"):

* Every child argv is wrapped through ``errorta_tools.runner.sandbox`` using the
  profile's ``sandbox`` field. ``auto`` picks the best available backend;
  ``none`` runs a bare child that is LOGGED and FLAGGED on the session
  (``sandbox_backend="none"``) so the UI can show reduced isolation. An
  explicit-but-unavailable backend BLOCKS — never a silent downgrade.
* The child env is a small allowlist (no sidecar secrets are inherited);
  filesystem writes are confined to the workspace + a synthetic per-run
  home/tmp. Network IS allowed (a dev server must bind its port and installs
  must fetch) — the isolation here is filesystem confinement, not an air gap.
* Ports are loopback-only; privileged ports are refused (an ephemeral loopback
  port is allocated instead).
* Logs are redacted (tokens / home path) and capped, stored under Errorta home,
  never inside the user's repo.

The egress boundary is ``errorta_tools`` (where the sandbox shells out and the
health probe makes its HTTP call): this module reaches ``subprocess`` / ``httpx``
ONLY through ``errorta_tools.runner.preview`` — so ``errorta_council`` stays free
of direct process/HTTP imports (Council invariant 3, enforced by the import-lint
guards). The orchestration here (lifecycle state machine, process-group
teardown, log capping, threading) is a sanctioned execution primitive parallel
to the F087-10 test runner; it imports no member / gateway / MCP machinery.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime import RuntimeProfile, RuntimeProfileStore, RuntimeSession

# Terminal states a session can rest in; everything else is "live".
_TERMINAL = frozenset({"stopped", "crashed"})

# Log cap (bytes). Past this the log file stops growing and ``truncated`` is set.
_LOG_CAP_BYTES = 1_000_000
_LOG_TAIL_LINES = 500

# Health-poll cadence and the SIGTERM->SIGKILL grace window. Module-level so
# tests can shrink them without spawning slow children.
_POLL_INTERVAL = 0.25
_GRACE_SECONDS = 5.0
_PROBE_TIMEOUT = 2.0

# F146 Slice C: the bounded startup window for a delivery launch probe. "Did the
# delivered program get past startup without a traceback?" — startup-only (past
# the first frames without crashing), NOT a full session. Module-level so tests
# can shrink it without waiting on a real server. Clamped to a sane band in
# ``launch_probe``. NOTE (spec §"GUI launch evidence"): a crash that only fires
# AFTER this window (e.g. heavy asset loading before ``pygame.font.init()``) is
# out of scope here — deeper probing is a deliberate follow-up; the window is a
# little generous to catch the common startup-time crash.
_LAUNCH_PROBE_SECONDS = 12.0

# F152: a widened window for an HTTP-serving profile. A JS dev server's first
# compile is routinely 10-30s, so the 12s CLI window is too short to fairly demand
# a served response; the probe still early-exits the moment the app answers.
_LAUNCH_HTTP_PROBE_SECONDS = 45.0

# F153 (G2): language-agnostic startup-crash signatures. The pre-F153 probe scanned
# only for the CPython ``"Traceback (most recent call last)"`` string, so a Node /
# Rust / Go / TS crash exited "clean". These are high-precision crash/compile
# phrases (NOT bare "error"/"fail" substrings) scanned over the log tail; a match
# classifies a launch as crashed. F152 reuses this to enrich a served-500 finding
# (once an HTTP request triggers a lazy compile, "Failed to compile" lands here).
_CRASH_SIGNATURES = (
    "Traceback (most recent call last)",  # CPython
    "Failed to compile",                   # Next.js / webpack
    "Module not found",                    # webpack / node
    "Cannot find module",                  # node
    "SyntaxError",                         # node / babel
    "ReferenceError",                      # node
    "error TS",                            # tsc
    "ERROR in ",                           # webpack
    "panicked at",                         # Rust
    "goroutine ",                          # Go panic dump
    "Exception in thread",                 # JVM
)


def _has_crash_signature(tail: str) -> tuple[bool, str]:
    """(matched, signature) if the log tail contains a known startup-crash phrase.

    F153 (G2) / F152: framework-agnostic replacement for the CPython-only traceback
    grep. Only safe to apply to a process that has EXITED (a legitimate non-zero
    CLI usage exit prints none of these; a real crash in any common runtime does).
    For a STILL-RUNNING process use ``_survived_crash_signature`` — the broad set
    would false-positive on a healthy server's request logs."""
    for sig in _CRASH_SIGNATURES:
        if sig in tail:
            return True, sig
    return False, ""


# F153 review: markers safe for a STILL-RUNNING process. A live server logs
# generic runtime errors (SyntaxError on a malformed request body, an
# "Exception in thread" from a recovered worker) as normal operation, so the broad
# set above would flip a healthy survivor to "crashed". These two phrases instead
# mean "broken at startup" and do NOT appear in a healthy server's request logs.
_SURVIVED_CRASH_SIGNATURES = (
    "Traceback (most recent call last)",  # a Python startup crash logged but swallowed
    "Failed to compile",                   # a dev server that compiled-but-broke
)


def _survived_crash_signature(tail: str) -> tuple[bool, str]:
    for sig in _SURVIVED_CRASH_SIGNATURES:
        if sig in tail:
            return True, sig
    return False, ""


# F146 Slice C: runtime kinds whose delivered program is meant to KEEP RUNNING
# (a window/server). For these, exiting during the startup window — even with a
# clean non-zero code and no traceback — is a launch failure (it failed to stay
# up). A one-shot ``cli``/``binary`` that exits non-zero without a traceback ran
# to completion with a status (a usage/validation exit), which is NOT a startup
# crash — it is classified like ``run_cli`` (not a finding), so a CLI that needs
# args isn't misreported as crashed.
_LONG_RUNNING_KINDS = frozenset({"desktop", "web", "api"})

# F146 Slice C: headless drivers overlaid on the child env for a launch probe so
# a GUI / game program can get past startup without a real window or audio
# device (SDL/pygame, Qt, matplotlib). This catches a STARTUP crash (e.g. the
# ``pygame.font`` import error) while not requiring a display — render-time
# issues that need a real window are explicitly out of scope (spec §"GUI launch
# evidence"). Never carries a secret; purely selects offscreen backends.
_HEADLESS_ENV = {
    "SDL_VIDEODRIVER": "dummy",
    "SDL_AUDIODRIVER": "dummy",
    "PYGAME_HIDE_SUPPORT_PROMPT": "1",
    "QT_QPA_PLATFORM": "offscreen",
    "MPLBACKEND": "Agg",
    "ERRORTA_HEADLESS": "1",
}

# Child env allowlist: what a node/python dev tool genuinely needs to run, minus
# anything that could carry a sidecar secret. HOME/TMP are SYNTHETIC per run.
_ENV_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "SYSTEMROOT",
)

# F101 S6: a container runtime shells out to the docker CLI, which needs to find
# its daemon/config. These name endpoints, not secrets, and are injected ONLY
# for a container runtime — a managed_local dev server has no docker role.
_DOCKER_ENV_PASSTHROUGH = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")

# F101-03 T1 (sandboxed windowed): a GUI app finds the OS window server through
# these. On Linux they name the X11/Wayland display; on macOS Cocoa reaches
# WindowServer via mach (no env needed) so these are simply absent. They name
# display endpoints, not secrets, and are injected ONLY for a desktop runtime.
_DISPLAY_ENV_PASSTHROUGH = (
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
    "XDG_SESSION_TYPE",
)


# --------------------------------------------------------------------------- #
# Module-level live-process registry (the sidecar owns all preview processes).
# --------------------------------------------------------------------------- #
@dataclass
class _Live:
    session_id: str
    project_id: str
    proc: Any = None  # errorta_tools.runner.preview spawns/owns the Popen type
    pgid: int | None = None
    port: int | None = None
    log_path: Path | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    stopping: bool = False
    threads: list[threading.Thread] = field(default_factory=list)
    # F101 S6: an explicit teardown command (e.g. `docker compose down`) run on
    # stop/switch/exit so a container runtime leaves no containers behind.
    stop_argv: list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None


_LIVE: dict[str, _Live] = {}
_LIVE_LOCK = threading.Lock()


class RuntimeProcessError(Exception):
    """A runtime process operation failed (e.g. profile not found)."""


# --------------------------------------------------------------------------- #
# Sandbox / port / env / redaction primitives
# --------------------------------------------------------------------------- #
def resolve_sandbox_backend(requested: str) -> str:
    """Resolve a profile's ``sandbox`` field to a concrete backend.

    ``auto`` -> best available (seatbelt/bwrap), else ``none``. An explicit
    backend must be available or this raises ``SandboxUnavailable`` (fail
    closed — never a silent downgrade to ``none``).
    """
    from errorta_tools.runner.sandbox import (
        SANDBOX_BWRAP,
        SANDBOX_NONE,
        SANDBOX_SEATBELT,
        SandboxUnavailable,
        is_available,
        normalize_backend,
    )
    req = (requested or "auto").strip().lower()
    if req == "auto":
        for backend in (SANDBOX_SEATBELT, SANDBOX_BWRAP):
            if is_available(backend):
                return backend
        return SANDBOX_NONE
    backend = normalize_backend(req)  # raises SandboxUnavailable on unknown
    if backend == SANDBOX_NONE:
        return SANDBOX_NONE
    if not is_available(backend):
        raise SandboxUnavailable(f"sandbox_unavailable_{backend}")
    return backend


def allocate_loopback_port(preferred: int | None) -> int:
    """Allocate a loopback TCP port. Tries ``preferred`` (if non-privileged and
    free), else an ephemeral 127.0.0.1:0 port. Privileged ports (<1024) are
    refused — an ephemeral port is allocated instead.
    """
    if preferred and 1024 <= int(preferred) <= 65535:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", int(preferred)))
            return int(preferred)
        except OSError:
            pass
        finally:
            s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _sub_port(value: str, port: int | None) -> str:
    if port is None:
        return value
    return value.replace("{port}", str(port))


def _child_env(*, runner_home: Path, runner_tmp: Path, port: int | None,
               include_docker: bool = False, include_display: bool = False) -> dict[str, str]:
    env: dict[str, str] = {}
    keys = _ENV_PASSTHROUGH
    if include_docker:
        keys += _DOCKER_ENV_PASSTHROUGH
    if include_display:
        keys += _DISPLAY_ENV_PASSTHROUGH
    for key in keys:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["HOME"] = str(runner_home)
    env["TMPDIR"] = str(runner_tmp)
    env["TMP"] = str(runner_tmp)
    env["TEMP"] = str(runner_tmp)
    env["NODE_ENV"] = "development"
    if port is not None:
        env["PORT"] = str(port)
    return env


# KEY=VALUE where KEY names a secret-ish var. Masks the value, keeps the name.
_ENV_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|AUTH)[A-Z0-9_]*)=\S+"
)


def redact_log_line(line: str) -> str:
    """Redact secret-shaped tokens, the home path, and KEY=VALUE secret
    assignments from one log line before it is written to disk."""
    from errorta_diagnostics.redact import redact_home_path, redact_tokens
    out, _ = redact_tokens(line)
    out, _ = redact_home_path(out)
    out = _ENV_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", out)
    return out


# --------------------------------------------------------------------------- #
# Managed Python venv (so "install deps for Preview" actually works)
# --------------------------------------------------------------------------- #
# A ``managed_local`` Python runtime gets a per-project virtualenv on a stable,
# sandbox-writable path shared by BOTH setup and start. Without it, a
# ``pip install`` setup step has nowhere writable to land (the child can only
# write the workspace + a per-run synthetic HOME) and — even if it did — a later
# ``start`` session's different synthetic HOME could not import it. The venv
# closes both gaps: setup installs into it, start runs the interpreter from it,
# and generated projects import their third-party deps without polluting the
# sidecar's own environment.
#
# Interpreter/pip tokens are rewritten to the venv's ABSOLUTE binaries rather
# than injected via PATH, because ``preview._resolve_common_tool`` resolves a
# bare ``python``/``pip`` against the sidecar's PATH before sandboxing — a PATH
# prepend would be ignored, an absolute path is honored.
_PY_INTERP_TOKENS = frozenset({"python", "python3", "py"})
_PIP_TOKENS = frozenset({"pip", "pip3"})


def _argv_basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _venv_python(venv_dir: Path) -> Path:
    """The interpreter inside a venv (``Scripts`` on Windows, ``bin`` elsewhere)."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _is_pip_install_step(step: list[str]) -> bool:
    """True if a setup ``step`` is a pip install (``pip install …`` or
    ``python -m pip install …``) — the trigger for standing up a venv."""
    if not step:
        return False
    toks = [str(a).lower() for a in step]
    base = _argv_basename(toks[0])
    if base in _PIP_TOKENS:
        return "install" in toks
    if base in _PY_INTERP_TOKENS:
        return "pip" in toks and "install" in toks
    return False


def _rewrite_argv_to_venv(argv: list[str], venv_python: Path) -> list[str]:
    """Rewrite a python/pip ``argv`` to run through the venv's interpreter.

    ``pip …`` / ``pip3 …`` -> ``<venv>/bin/python -m pip …``;
    ``python …`` / ``python3 …`` / ``py …`` -> ``<venv>/bin/python …``. Anything
    else (already-absolute paths, non-python tools) is returned unchanged."""
    if not argv:
        return list(argv)
    argv = [str(a) for a in argv]
    base = _argv_basename(argv[0]).lower()
    if base in _PIP_TOKENS:
        return [str(venv_python), "-m", "pip", *argv[1:]]
    if base in _PY_INTERP_TOKENS:
        return [str(venv_python), *argv[1:]]
    return argv


def _setup_succeeded(session: RuntimeSession) -> bool:
    """A terminal setup session that installed cleanly: it ``stopped`` with exit
    code 0. Anything else is not a success and the caller must NOT start on it:
    a ``crashed`` setup (failed install / spawn failure / unavailable sandbox), or
    a ``stopped`` with ``exit_code=None`` — which ``_run_setup`` produces when the
    install was CANCELLED mid-run (``python -m venv`` already created the venv
    interpreter, so a venv-existence re-check alone would wrongly pass). Requiring
    exit 0 closes that fail-closed hole; the auto-setup gate always has ≥1 step, so
    a clean install always lands exit 0."""
    return session.state == "stopped" and session.exit_code == 0


# --------------------------------------------------------------------------- #
# Process manager (one per project)
# --------------------------------------------------------------------------- #
class RuntimeProcessManager:
    def __init__(self, *, project_id: str, rstore: RuntimeProfileStore,
                 workspace_root: Path, work_root: Path) -> None:
        self.project_id = project_id
        self.rstore = rstore
        self.workspace_root = Path(workspace_root).resolve()
        self.work_root = Path(work_root)

    @classmethod
    def for_project(cls, project_id: str) -> "RuntimeProcessManager":
        from .ledger import LedgerStore
        from .workspace import CodingWorkspace
        store = LedgerStore(project_id)
        proj = store.get_project()  # raises ProjectNotFound -> caller maps 404
        ws = CodingWorkspace(project_id, store)
        ws.set_target(proj.target)
        if not ws.exists():
            raise RuntimeProcessError("no_worktree")
        rstore = RuntimeProfileStore.for_ledger(store)
        mgr = cls(project_id=project_id, rstore=rstore,
                  workspace_root=ws.root(), work_root=store.dir)
        mgr.reconcile_orphans()
        return mgr

    # -- working dir resolution (security: confined to the workspace) ------ #
    def _resolve_working_dir(self, profile: RuntimeProfile) -> Path:
        target = (self.workspace_root / profile.working_dir).resolve()
        if not (target == self.workspace_root or target.is_relative_to(self.workspace_root)):
            raise RuntimeProcessError("working_dir escapes the workspace")
        return target

    def _log_dir(self) -> Path:
        d = self.work_root / "runtime-logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run_dirs(self, session_id: str) -> tuple[Path, Path]:
        base = self.work_root / "runtime-work" / session_id
        home = base / "home"
        tmp = base / "tmp"
        home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        return home, tmp

    # -- managed Python venv (per-project, stable across sessions) ---------- #
    def _venv_dir(self, profile_id: str) -> Path:
        """The per-profile venv path. Under ``work_root`` (not the workspace, so
        it never shows up in the user's repo/diffs) and stable across sessions,
        so a ``pip install`` from setup is still there when ``start`` runs."""
        seg = re.sub(r"[^A-Za-z0-9._-]", "_", profile_id or "default")
        return self.work_root / "runtime-venv" / seg

    def _prepare_setup_steps(
        self, profile: RuntimeProfile,
    ) -> tuple[list[list[str]], list[str] | None]:
        """Return ``(steps, extra_writable)`` for a setup run.

        If any declared step is a pip install, the project gets a venv: a
        ``python -m venv`` bootstrap is prepended when the venv doesn't exist
        yet, every step's ``python``/``pip`` is rewritten to the venv's
        interpreter, and the venv dir is returned as an extra sandbox-writable
        path. Non-install setups (e.g. a codegen script) pass through untouched
        with no venv."""
        steps = [[str(a) for a in step] for step in (profile.setup or [])]
        if not any(_is_pip_install_step(step) for step in steps):
            return steps, None
        venv_dir = self._venv_dir(profile.profile_id)
        venv_py = _venv_python(venv_dir)
        prepared: list[list[str]] = []
        if not venv_py.exists():
            prepared.append(["python", "-m", "venv", str(venv_dir)])
        prepared.extend(_rewrite_argv_to_venv(step, venv_py) for step in steps)
        return prepared, [str(venv_dir)]

    def _prepare_run_argv(
        self, profile_id: str, argv: list[str],
    ) -> tuple[list[str], list[str] | None]:
        """Return ``(argv, extra_writable)`` for a start/CLI run. When a venv
        exists for this profile, run the command through it so its installed
        deps are importable, and expose the venv dir as sandbox-writable (for
        ``.pyc`` writes):

        * a python invocation (``python``/``python3``/``py``) -> the venv
          interpreter;
        * a console script the venv itself provides (``uvicorn``/``gunicorn``/
          ``flask``/``streamlit`` from a hand-edited profile) -> that venv
          executable, so it imports the venv's packages, not the sidecar's.

        When no venv exists yet — or the tool isn't python and the venv doesn't
        provide it — the argv is unchanged (same as before this feature)."""
        if not argv:
            return list(argv), None
        venv_dir = self._venv_dir(profile_id)
        venv_py = _venv_python(venv_dir)
        if not venv_py.exists():
            return list(argv), None
        if _argv_basename(str(argv[0])).lower() in _PY_INTERP_TOKENS:
            return _rewrite_argv_to_venv(argv, venv_py), [str(venv_dir)]
        venv_tool = venv_py.parent / _argv_basename(str(argv[0]))
        if venv_tool.exists():
            return [str(venv_tool), *[str(a) for a in argv[1:]]], [str(venv_dir)]
        return list(argv), None

    def _setup_pending_venv_missing(
        self, profile: RuntimeProfile, profile_id: str
    ) -> bool:
        """True when the profile installs its deps into a per-project venv but that
        venv doesn't exist yet — i.e. setup was never run. Starting anyway would fall
        back to the sidecar's interpreter and crash with a confusing
        ``ModuleNotFoundError`` (the pygame case), so the caller blocks with a clear
        'run setup first' instead. False for non-venv projects (nothing to install)
        and once setup has built the venv."""
        steps = [[str(a) for a in step] for step in (profile.setup or [])]
        if not any(_is_pip_install_step(step) for step in steps):
            return False
        return not _venv_python(self._venv_dir(profile_id)).exists()

    # -- orphan reconciliation (no auto-resume across restart, D3) ---------- #
    def reconcile_orphans(self) -> int:
        """Mark any on-disk session left non-terminal by a previous sidecar (not
        in this process's live registry) as ``crashed`` — its process is gone."""
        n = 0
        for sess in self.rstore.list_sessions():
            if sess.state in _TERMINAL:
                continue
            with _LIVE_LOCK:
                live = sess.session_id in _LIVE
            if live:
                continue
            self.rstore.update_session(
                sess.session_id, state="crashed",
                error="sidecar_restarted_no_resume", ended_at=_now())
            n += 1
        return n

    # -- run-mode resolution (shared by start + setup) --------------------- #
    def _resolve_run_mode(
        self, profile: RuntimeProfile, profile_id: str
    ) -> tuple[str | None, str | None, bool, RuntimeSession | None]:
        """Return ``(spawn_backend, recorded_backend, is_container, blocked)``.

        A container runtime is isolated by the container itself, so the docker
        child is NOT wrapped in the F039 OS sandbox — it is spawned unwrapped
        (spawn backend ``none``) while the session honestly records the
        ``docker`` tier; docker must be available or the run is blocked
        (``blocked`` is the crashed session to return). A managed_local runtime
        resolves the F039 sandbox as before (an explicit-but-unavailable backend
        blocks). On a block, the first three values are ``None``/``False``.
        """
        if profile.runtime_mode == "container":
            from errorta_tools.runner.sandbox import SANDBOX_DOCKER, is_available
            if not is_available(SANDBOX_DOCKER):
                return None, None, True, self._record_blocked(
                    profile_id, reason="container_runtime_requires_docker")
            return "none", "docker", True, None
        try:
            backend = resolve_sandbox_backend(profile.sandbox)
        except Exception as exc:  # SandboxUnavailable et al.
            return None, None, False, self._record_blocked(profile_id, reason=str(exc))
        return backend, backend, False, None

    # -- start a managed local runtime ------------------------------------- #
    def start(self, profile_id: str, *, display: bool | None = None,
              auto_setup: bool = True) -> RuntimeSession:
        """Start a managed-local runtime. ``display`` grants the F039 sandbox
        access to the OS window server (F101-03 T1 — a GUI app draws its own
        window) without re-opening network / out-of-workspace writes; it is
        inferred from a ``desktop``-kind profile when not given explicitly.

        ``auto_setup`` (default) makes a single Run self-sufficient: a
        venv-backed project whose deps aren't installed yet runs setup inline
        (creating the venv + installing) before starting, instead of failing
        with ``setup_required``. Pass ``auto_setup=False`` to keep setup an
        explicit separate step (the manual "Run setup" path)."""
        profile = self.rstore.get_profile(profile_id)
        if profile is None:
            raise RuntimeProcessError("profile_not_found")
        if not profile.start:
            raise RuntimeProcessError("profile has no start command")
        # Setup gate: a project that installs deps into a per-project venv must run
        # setup first, else start would use the sidecar's interpreter and crash with
        # a confusing ModuleNotFoundError. Auto-run it inline (a failed setup returns
        # its own crashed session); block only if it's still pending afterwards.
        if self._setup_pending_venv_missing(profile, profile_id):
            if auto_setup:
                setup_session = self._setup_sync(profile_id)
                if not _setup_succeeded(setup_session):
                    return setup_session
                profile = self.rstore.get_profile(profile_id) or profile
            if self._setup_pending_venv_missing(profile, profile_id):
                return self._record_blocked(profile_id, reason=(
                    "setup_required: this project installs its dependencies into a "
                    "per-project virtual environment. Run setup before starting."))
        if display is None:
            display = profile.kind == "desktop"

        spawn_backend, recorded_backend, container, blocked = \
            self._resolve_run_mode(profile, profile_id)
        if blocked is not None:
            return blocked

        sid = self.rstore.new_session_id()
        port = self._resolve_listen_port(profile)
        home, tmp = self._run_dirs(sid)
        cwd = self._resolve_working_dir(profile)
        argv = [_sub_port(a, port) for a in profile.start]
        # Run through the managed venv (if setup built one) so installed deps
        # are importable; otherwise the argv is unchanged.
        argv, extra_writable = self._prepare_run_argv(profile_id, argv)
        env = _child_env(runner_home=home, runner_tmp=tmp, port=port,
                         include_docker=container, include_display=display)
        stop_argv = [_sub_port(a, port) for a in (profile.stop or [])] or None

        # F101-03 trust tier: T0 sandboxed-headless, T1 sandboxed-windowed
        # (display granted under a real OS sandbox), T2 reduced isolation (ran
        # without an OS sandbox). Stamped on the session + shown before Run.
        if recorded_backend == "none":
            trust_tier = 2
        elif display:
            trust_tier = 1
        else:
            trust_tier = 0

        log_path = self._log_dir() / f"{sid}.log"
        safety = list(profile.safety_warnings)
        if recorded_backend == "none":
            safety.append("Runtime ran without an OS sandbox (reduced isolation).")

        extras: dict[str, Any] = {"trust_tier": trust_tier}
        if display:
            extras["display"] = True
        if safety:
            extras["safety_warnings"] = safety
        session = RuntimeSession(
            session_id=sid, profile_id=profile_id, state="starting",
            started_at=_now(), allocated_ports=[port] if port is not None else [],
            sandbox_backend=recorded_backend, log_ref=f"runtime-logs/{sid}.log",
            _extras=extras,
        )
        self.rstore.append_session(session)

        try:
            proc = self._spawn(argv, backend=spawn_backend, cwd=cwd, home=home,
                               tmp=tmp, port=port, log_path=log_path, append=False,
                               env=env, display=display,
                               extra_writable=extra_writable)
        except Exception as exc:
            self.rstore.update_session(sid, state="crashed",
                                       error=f"spawn_failed: {exc}", ended_at=_now())
            return self.rstore.get_session(sid)

        pgid = _safe_getpgid(proc)
        live = _Live(session_id=sid, project_id=self.project_id, proc=proc,
                     pgid=pgid, port=port, log_path=log_path,
                     stop_argv=stop_argv, cwd=str(cwd), env=env)
        with _LIVE_LOCK:
            _LIVE[sid] = live
        self.rstore.update_session(sid, pgid=pgid)

        mon = threading.Thread(target=self._monitor, args=(live, profile),
                               name=f"runtime-mon-{sid}", daemon=True)
        live.threads.append(mon)
        mon.start()
        return self.rstore.get_session(sid)

    # -- run a one-shot CLI transcript (F101-02) --------------------------- #
    def run_cli(
        self, profile_id: str, *, args: list[str] | None = None,
        timeout_seconds: float | None = None, auto_setup: bool = True,
    ) -> RuntimeSession:
        """Run a ``managed_local`` profile's ``start`` argv (plus optional
        argv-only ``args``) once, time-boxed, under the F039 sandbox, and land a
        terminal ``RuntimeSession`` carrying the captured (capped + redacted)
        transcript + exit code. The CLI analog of "open the demo in a browser".

        Reuses the start machinery (sandbox resolution, ``_spawn`` + log pump,
        ``_teardown_live`` for the timeout kill) — no new process machinery. A
        non-zero exit is a *completed run that failed its task*: it lands
        ``state="stopped"`` with the real ``exit_code`` and ``_extras.passed=False``;
        ``crashed`` is reserved for a spawn failure. A timeout group-kills the
        child and lands ``state="stopped"`` with ``error="timed_out"`` and
        ``exit_code=None``.
        """
        profile = self.rstore.get_profile(profile_id)
        if profile is None:
            raise RuntimeProcessError("profile_not_found")
        if profile.runtime_mode != "managed_local":
            raise RuntimeProcessError("cli_run_requires_managed_local")
        if not profile.start:
            raise RuntimeProcessError("profile has no start command")
        # Setup gate (same as start()): a venv-backed project must run setup first,
        # else the CLI would use the sidecar's interpreter and fail on a missing dep.
        # Auto-run it inline (default) so a single Run is self-sufficient.
        if self._setup_pending_venv_missing(profile, profile_id):
            if auto_setup:
                setup_session = self._setup_sync(profile_id)
                if not _setup_succeeded(setup_session):
                    return setup_session
                profile = self.rstore.get_profile(profile_id) or profile
            if self._setup_pending_venv_missing(profile, profile_id):
                return self._record_blocked(profile_id, reason=(
                    "setup_required: this project installs its dependencies into a "
                    "per-project virtual environment. Run setup before starting."))

        # CLI is always F039-sandboxed (managed_local only — container/static
        # rejected above). An explicit-but-unavailable backend blocks.
        spawn_backend, recorded_backend, container, blocked = \
            self._resolve_run_mode(profile, profile_id)
        if blocked is not None:
            return blocked

        # Time-box: per-request override wins over the per-profile
        # demo.timeout_seconds, defaulting to 60s; clamped to 1..600.
        demo_timeout = None
        if isinstance(profile.demo, dict):
            demo_timeout = profile.demo.get("timeout_seconds")
        try:
            raw_timeout = float(
                timeout_seconds if timeout_seconds is not None
                else (demo_timeout if demo_timeout is not None else 60.0))
        except (TypeError, ValueError):
            raw_timeout = 60.0
        timeout = _clamp(raw_timeout, 1.0, 600.0)

        sid = self.rstore.new_session_id()
        home, tmp = self._run_dirs(sid)
        cwd = self._resolve_working_dir(profile)
        # A CLI normally has no port; substitute {port}->no-op for parity.
        argv = [_sub_port(a, None) for a in profile.start] + list(args or [])
        env = _child_env(runner_home=home, runner_tmp=tmp, port=None,
                         include_docker=container)
        log_path = self._log_dir() / f"{sid}.log"

        # The effective argv, redacted, for honest display (a secret passed as a
        # CLI arg must not surface in the session). Computed BEFORE the venv
        # rewrite so the transcript shows the readable command, not the venv path.
        redacted_argv = [redact_log_line(a) for a in argv]
        # Run through the managed venv (if setup built one) so installed deps
        # are importable; otherwise the argv is unchanged.
        argv, extra_writable = self._prepare_run_argv(profile_id, argv)

        safety = list(profile.safety_warnings)
        if recorded_backend == "none":
            safety.append("Runtime ran without an OS sandbox (reduced isolation).")
        extras: dict[str, Any] = {"kind": "cli_transcript", "argv": redacted_argv}
        if safety:
            extras["safety_warnings"] = safety

        session = RuntimeSession(
            session_id=sid, profile_id=profile_id, state="starting",
            started_at=_now(), allocated_ports=[],
            sandbox_backend=recorded_backend, log_ref=f"runtime-logs/{sid}.log",
            _extras=extras,
        )
        self.rstore.append_session(session)

        try:
            proc = self._spawn(argv, backend=spawn_backend, cwd=cwd, home=home,
                               tmp=tmp, port=None, log_path=log_path,
                               append=False, env=env, extra_writable=extra_writable)
        except Exception as exc:
            self.rstore.update_session(sid, state="crashed",
                                       error=f"spawn_failed: {exc}", ended_at=_now())
            return self.rstore.get_session(sid)

        pgid = _safe_getpgid(proc)
        live = _Live(session_id=sid, project_id=self.project_id, proc=proc,
                     pgid=pgid, port=None, log_path=log_path,
                     cwd=str(cwd), env=env)
        with _LIVE_LOCK:
            _LIVE[sid] = live
        self.rstore.update_session(sid, pgid=pgid)

        deadline = time.monotonic() + timeout
        mon = threading.Thread(target=self._monitor_cli, args=(live, deadline),
                               name=f"runtime-cli-{sid}", daemon=True)
        live.threads.append(mon)
        mon.start()
        return self.rstore.get_session(sid)

    def _monitor_cli(self, live: _Live, deadline: float) -> None:
        """Wait for a one-shot CLI child to exit OR for the time-box deadline.
        On exit: terminal ``stopped`` (exit_code captured; non-zero is a failed
        run, not a crash) + ``_extras.passed``. On deadline: group-kill via
        ``_teardown_live`` and land ``stopped`` with ``error="timed_out"``."""
        sid = live.session_id
        self._set_state(live, "running")
        while not live.stop_event.is_set():
            rc = live.proc.poll() if live.proc else None
            if rc is not None:
                if not live.stopping:
                    self._set_state(live, "stopped", exit_code=rc,
                                    ended_at=_now(), passed=(rc == 0))
                self._unregister(sid)
                return
            if time.monotonic() >= deadline:
                live.stopping = True
                _teardown_live(live)  # SIGTERM -> grace -> SIGKILL on the group
                self.rstore.update_session(
                    sid, state="stopped", error="timed_out", exit_code=None,
                    ended_at=_now(), passed=False)
                self._unregister(sid)
                return
            time.sleep(_POLL_INTERVAL)
        # stop_event set externally (teardown): owner sets the terminal state.
        self._unregister(sid)

    # -- run setup (sandboxed install) ------------------------------------- #
    def setup(self, profile_id: str) -> RuntimeSession:
        """Run the profile's setup steps in the background (the manual 'Run setup'
        button): returns immediately with a ``starting`` session the caller polls."""
        return self._run_setup_session(profile_id, inline=False)

    def _setup_sync(self, profile_id: str) -> RuntimeSession:
        """Run setup to completion inline and return the terminal session. Used by
        ``start``/``run_cli`` auto-setup so a single Run installs deps then runs."""
        return self._run_setup_session(profile_id, inline=True)

    def _run_setup_session(self, profile_id: str, *, inline: bool) -> RuntimeSession:
        profile = self.rstore.get_profile(profile_id)
        if profile is None:
            raise RuntimeProcessError("profile_not_found")
        # F101 S6: a container setup step (e.g. `docker build`) must run unwrapped
        # with the docker env, exactly like a container start — it can't reach the
        # docker socket from inside the F039 sandbox.
        spawn_backend, recorded_backend, container, blocked = \
            self._resolve_run_mode(profile, profile_id)
        if blocked is not None:
            return blocked

        sid = self.rstore.new_session_id()
        home, tmp = self._run_dirs(sid)
        cwd = self._resolve_working_dir(profile)
        env = _child_env(runner_home=home, runner_tmp=tmp, port=None,
                         include_docker=container)
        log_path = self._log_dir() / f"{sid}.log"
        session = RuntimeSession(
            session_id=sid, profile_id=profile_id, state="starting",
            started_at=_now(), sandbox_backend=recorded_backend,
            log_ref=f"runtime-logs/{sid}.log", _extras={"kind": "setup"},
        )
        self.rstore.append_session(session)
        live = _Live(session_id=sid, project_id=self.project_id, log_path=log_path,
                     cwd=str(cwd), env=env)
        with _LIVE_LOCK:
            _LIVE[sid] = live
        setup_args = (live, profile, spawn_backend, cwd, home, tmp, log_path, env)
        if inline:
            # Run the steps on this thread and return the terminal session, so the
            # caller can decide whether to proceed to start.
            self._run_setup(*setup_args)
            return self.rstore.get_session(sid)
        t = threading.Thread(
            target=self._run_setup, name=f"runtime-setup-{sid}", daemon=True,
            args=setup_args)
        live.threads.append(t)
        t.start()
        return self.rstore.get_session(sid)

    def _run_setup(self, live: _Live, profile: RuntimeProfile, backend: str,
                   cwd: Path, home: Path, tmp: Path, log_path: Path,
                   env: dict[str, str]) -> None:
        sid = live.session_id
        self._set_state(live, "running")
        if not (profile.setup or []):
            self._set_state(live, "stopped", exit_code=0)
            self._unregister(sid)
            return
        # Stand up a per-project venv when the setup installs deps, so the
        # install has a stable writable target that ``start`` shares.
        steps, extra_writable = self._prepare_setup_steps(profile)
        if extra_writable:
            Path(extra_writable[0]).mkdir(parents=True, exist_ok=True)
        for step in steps:
            if live.stop_event.is_set():
                self._set_state(live, "stopped", exit_code=None)
                self._unregister(sid)
                return
            try:
                proc = self._spawn(step, backend=backend,
                                   cwd=cwd, home=home, tmp=tmp, port=None,
                                   log_path=log_path, append=True, env=env,
                                   extra_writable=extra_writable)
            except Exception as exc:
                self._set_state(live, "crashed", error=f"spawn_failed: {exc}")
                self._unregister(sid)
                return
            live.proc = proc
            live.pgid = _safe_getpgid(proc)
            rc = proc.wait()
            if rc != 0:
                self._set_state(live, "crashed", exit_code=rc,
                                error=f"setup step exited {rc}")
                self._unregister(sid)
                return
        self._set_state(live, "stopped", exit_code=0)
        self._unregister(sid)

    # -- F146 Slice C: bounded headless launch probe (delivery evidence) --- #
    def _wait_terminal(self, session_id: str, *, timeout: float,
                       should_cancel: Any = None) -> RuntimeSession | None:
        """Poll an async session (setup) until it rests in a terminal state, or
        return None on timeout / cancel. Bounded by ``timeout`` — the caller
        decides what a timeout means (for a launch probe, an un-finishing setup is
        a verify error, fail-closed). A cancel aborts the wait (the launch probe
        then treats the setup as not-succeeded -> cannot_verify)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sess = self.rstore.get_session(session_id)
            if sess is not None and sess.state in _TERMINAL:
                return sess
            if should_cancel is not None:
                try:
                    if should_cancel():
                        return sess  # non-terminal -> caller blocks (cannot_verify)
                except Exception:  # noqa: BLE001 — a raising cancel-probe aborts the wait
                    return sess
            time.sleep(_POLL_INTERVAL)
        return self.rstore.get_session(session_id)

    def launch_probe(
        self, profile_id: str, *, head: str,
        timeout_seconds: float | None = None,
        setup_timeout_seconds: float = 600.0,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        """F146 Slice C — LAUNCH the delivered program once, bounded + headless,
        and classify launched-clean vs crashed-with-traceback. This is the crash
        catcher that per-PR review + unit tests miss (the ``pygame.font`` case):
        a runnable project is not truly ``done`` until its delivered head launches
        without crashing on startup.

        Never rubber-stamps: the verdict comes from a REAL launch of the exact
        delivered ``head`` under the F039 sandbox — a ``launch`` runtime-test is
        recorded bound to that head, ``passed`` only on a clean launch. Returns::

            {"status": "clean"|"crashed"|"cannot_verify"|"skipped",
             "detail": str, "session_id": str|None}

        Fail-closed: a spawn / sandbox / setup failure (an INABILITY to launch) is
        ``cannot_verify`` (block ``done``, record no clean evidence); a real
        startup crash / non-zero exit is ``crashed`` (a code finding). ``skipped``
        means the profile is not a launchable ``managed_local`` runtime (the caller
        treats it as vacuously clean).
        """
        profile = self.rstore.get_profile(profile_id)
        if profile is None or not profile.start:
            return {"status": "skipped", "detail": "no start command",
                    "session_id": None}
        if profile.runtime_mode != "managed_local":
            # Slice C launches managed_local runtimes only (container/static have
            # their own paths). Not a crash — just not probed here.
            return {"status": "skipped", "detail": "not a managed_local runtime",
                    "session_id": None}

        # Setup gate: stand up the per-project venv if the project installs deps
        # and setup was never run — else start would use the SIDECAR interpreter
        # and crash with a confusing ModuleNotFoundError (the exact false-crash we
        # must not misreport). A setup failure is an INABILITY to launch ->
        # cannot_verify (block done, no clean evidence recorded).
        if self._setup_pending_venv_missing(profile, profile_id):
            setup_sess = self.setup(profile_id)
            term = self._wait_terminal(setup_sess.session_id,
                                       timeout=setup_timeout_seconds,
                                       should_cancel=should_cancel)
            if term is None or not _setup_succeeded(term):
                st = getattr(term, "state", "unknown")
                err = getattr(term, "error", "") or ""
                return {"status": "cannot_verify",
                        "detail": f"runtime setup did not succeed ({st}): {err}"[:2000],
                        "session_id": getattr(setup_sess, "session_id", None)}

        # Resolve the sandbox (fail-closed: an explicit-but-unavailable backend
        # BLOCKS — never a silent unsandboxed launch of delivered code).
        spawn_backend, recorded_backend, container, blocked = \
            self._resolve_run_mode(profile, profile_id)
        if blocked is not None:
            return {"status": "cannot_verify",
                    "detail": f"sandbox unavailable: {blocked.error}",
                    "session_id": blocked.session_id}

        # F152: an HTTP-serving profile gets a widened default window (a JS dev
        # server's first compile is 10-30s) — but the probe early-exits the moment
        # the app answers, so a fast app pays nothing. An explicit override wins.
        is_http = str((profile.health or {}).get("type")) == "http"
        default_window = (_LAUNCH_HTTP_PROBE_SECONDS if is_http
                          else _LAUNCH_PROBE_SECONDS)
        window = _clamp(
            float(timeout_seconds if timeout_seconds is not None
                  else default_window), 1.0, 120.0)
        # A desktop app draws its own window; under headless drivers it still
        # doesn't need the OS window server, so we do NOT grant display (keeps the
        # probe at T0 sandboxed-headless — the CI-safe posture).
        sid = self.rstore.new_session_id()
        port = self._resolve_listen_port(profile)
        home, tmp = self._run_dirs(sid)
        cwd = self._resolve_working_dir(profile)
        argv = [_sub_port(a, port) for a in profile.start]
        argv, extra_writable = self._prepare_run_argv(profile_id, argv)
        env = _child_env(runner_home=home, runner_tmp=tmp, port=port,
                         include_docker=container)
        env.update(_HEADLESS_ENV)  # offscreen drivers so startup doesn't need a display
        log_path = self._log_dir() / f"{sid}.log"

        session = RuntimeSession(
            session_id=sid, profile_id=profile_id, state="starting",
            started_at=_now(), allocated_ports=[port] if port is not None else [],
            sandbox_backend=recorded_backend, log_ref=f"runtime-logs/{sid}.log",
            _extras={"kind": "launch_probe"},
        )
        self.rstore.append_session(session)

        reader_threads: list[Any] = []
        try:
            proc = self._spawn(argv, backend=spawn_backend, cwd=cwd, home=home,
                               tmp=tmp, port=port, log_path=log_path, append=False,
                               env=env, extra_writable=extra_writable,
                               reader_sink=reader_threads)
        except Exception as exc:  # noqa: BLE001 — a spawn failure is a verify error
            self.rstore.update_session(sid, state="crashed",
                                       error=f"spawn_failed: {exc}", ended_at=_now())
            return {"status": "cannot_verify", "detail": f"spawn failed: {exc}",
                    "session_id": sid}

        pgid = _safe_getpgid(proc)
        live = _Live(session_id=sid, project_id=self.project_id, proc=proc,
                     pgid=pgid, port=port, log_path=log_path,
                     cwd=str(cwd), env=env)
        live.threads.extend(reader_threads)
        with _LIVE_LOCK:
            _LIVE[sid] = live
        self.rstore.update_session(sid, pgid=pgid, state="running")

        # Bounded synchronous observe: did it get past startup without crashing,
        # within the window? A server/desktop that keeps running IS the intended
        # state — surviving the window is a clean launch. Wrapped so the child
        # GROUP is ALWAYS torn down (no leaked process/port) even on an
        # unexpected error between spawn and classification.
        #
        # F152: for an HTTP-serving profile we ALSO request the app each tick (this
        # both triggers a lazy first compile — Next.js/Vite compile per route on
        # first request — and observes whether it actually serves). Any response
        # <500 (2xx/3xx/4xx: it compiled and is routing) => served_ok, early-exit
        # clean. A response that is only ever >=500 through the window is a
        # compile/load error (the reported failure). Connection-refused just means
        # "not up yet" — keep polling.
        health_url = (_sub_port(str((profile.health or {}).get("url", "")), port)
                      if is_http else "")
        deadline = time.monotonic() + window
        rc: int | None = None
        survived = False
        cancelled = False
        http_served_ok = False       # saw a <500 response — up and serving
        http_saw_error = False       # saw a >=500 response — server error
        http_error_detail = ""
        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                if should_cancel is not None:
                    try:
                        stop = bool(should_cancel())
                    except Exception:  # noqa: BLE001 — a raising cancel-probe is
                        stop = True    # fail-closed: treat as cancelled, not "keep going"
                    if stop:
                        cancelled = True
                        break
                if health_url and not http_served_ok:
                    try:
                        _ok, _detail = _probe(health_url)
                    except Exception:  # noqa: BLE001 — our probe erroring is not an
                        _detail = ""   # app crash; ignore and keep observing
                    if _detail.isdigit():
                        code = int(_detail)
                        if code < 500:
                            http_served_ok = True
                            break  # up and serving — clean, no need to wait out the window
                        http_saw_error = True
                        http_error_detail = f"HTTP {code}"
                if time.monotonic() >= deadline:
                    survived = True
                    break
                time.sleep(_POLL_INTERVAL)
        finally:
            # Tear the child GROUP down (join the log pump so the tail is flushed
            # before we classify). Idempotent — safe on every path.
            _teardown_live(live)

        logs = self.get_logs(sid)
        tail_lines = logs.get("lines", [])[-40:]
        tail = "\n".join(tail_lines)
        # F153 (G2): language-agnostic crash detection for an EXITED process
        # (supersedes the CPython-only "Traceback" grep). `surv_crash` is the
        # narrow, high-precision variant used only while the process is STILL
        # RUNNING (the broad set would false-positive on a live server's logs).
        has_crash, crash_sig = _has_crash_signature(tail)
        surv_crash, surv_sig = _survived_crash_signature(tail)
        long_running = profile.kind in _LONG_RUNNING_KINDS
        win = int(window)

        if cancelled:
            self.rstore.update_session(sid, state="stopped", error="cancelled",
                                       ended_at=_now())
            return {"status": "cannot_verify", "detail": "launch probe cancelled",
                    "session_id": sid}
        # --- F152: HTTP-serving verdicts (only when we got HTTP signal) ---------
        if http_served_ok:
            # Answered <500 — the app is up AND its route compiled + serves. The
            # strongest possible clean signal (stronger than "process alive").
            self.rstore.update_session(sid, state="stopped", exit_code=None,
                                       error="probe_served_ok", ended_at=_now())
            status, passed, detail = "clean", True, f"served a healthy response at {health_url}"
        elif is_http and http_saw_error and not http_served_ok:
            # Bound a port but only ever answered >=500 through the window — a
            # compile/load error (the delivered site does not serve). Once the
            # request triggered a lazy compile, the framework's own error
            # ("Failed to compile ...") is now in the tail.
            self.rstore.update_session(sid, state="crashed", exit_code=rc,
                                       ended_at=_now())
            status, passed = "crashed", False
            sig_line = f" ({surv_sig})" if surv_crash else ""
            detail = (f"served only errors ({http_error_detail}){sig_line} at "
                      f"{health_url} — the app does not serve; log tail:\n{tail}")
        # --- F153: process-exit / survival classification -----------------------
        elif survived and surv_crash:
            # Still running at the window end but printed a high-precision crash /
            # compile-failure signature during startup — a failure the app logged
            # but did not exit on. Narrow markers only (`_survived_crash_signature`)
            # so a healthy server's normal error logs don't flip it to crashed.
            self.rstore.update_session(sid, state="crashed", exit_code=None,
                                       ended_at=_now())
            status, passed = "crashed", False
            detail = (f"printed a crash signature ({surv_sig}) during startup "
                      f"(still running at {win}s); log tail:\n{tail}")
        elif survived:
            # Ran the whole startup window without exiting or crashing — clean (a
            # server / desktop that keeps running is the intended state). For an
            # HTTP profile that never answered, this stays clean-by-survival (F152:
            # a never-served slow build must not false-fail; the >=500 path is what
            # catches the real failure).
            self.rstore.update_session(sid, state="stopped", exit_code=None,
                                       error="probe_window_elapsed", ended_at=_now())
            status, passed, detail = "clean", True, f"survived the {win}s startup window"
        # --- the process EXITED during the window (rc is not None) ---------------
        elif rc is not None and rc < 0:
            # Killed by a signal (segfault / abort) — an unambiguous crash, for any
            # kind. Checked first so the detail names the signal (a long-running
            # kind killed by a signal would otherwise read as a plain "exited").
            self.rstore.update_session(sid, state="crashed", exit_code=rc,
                                       ended_at=_now())
            status, passed = "crashed", False
            detail = f"killed by signal {-rc} on startup; log tail:\n{tail}"
        elif long_running:
            # F153 (G1): a window/server runtime EXITED during the startup window —
            # ANY exit code, including 0, means it failed to stay up. Checked BEFORE
            # the generic `rc == 0` clean branch (a server that binds nothing and
            # sys.exit(0)s on a config error is not a clean launch).
            self.rstore.update_session(sid, state="crashed", exit_code=rc,
                                       ended_at=_now())
            status, passed = "crashed", False
            detail = (f"a {profile.kind} runtime exited during startup (exit {rc}) — "
                      f"it must keep running; log tail:\n{tail}")
        elif rc == 0:
            # A one-shot cli/binary that finished successfully. A zero exit is a
            # success regardless of scary-looking log lines (a script that catches
            # an exception, logs it, and exits 0 ran to completion) — so this is
            # checked BEFORE the crash-signature branch, matching the pre-F153 order.
            self.rstore.update_session(sid, state="stopped", exit_code=0,
                                       ended_at=_now())
            status, passed, detail = "clean", True, "exited cleanly (0)"
        elif has_crash:
            # Non-zero exit WITH a crash signature — the crash catcher's core case
            # (the pygame.font import error; a Node ReferenceError; a Rust panic).
            self.rstore.update_session(sid, state="crashed", exit_code=rc,
                                       ended_at=_now())
            status, passed = "crashed", False
            detail = f"crashed on startup (exit {rc}, {crash_sig}); log tail:\n{tail}"
        else:
            # A one-shot cli/binary that exited non-zero without a crash signature
            # ran to completion with a status (a usage/validation exit) — NOT a
            # startup crash (matches run_cli's non-finding treatment).
            self.rstore.update_session(sid, state="stopped", exit_code=rc,
                                       ended_at=_now())
            status, passed, detail = "clean", True, (
                f"ran and exited (exit {rc}) with no crash")

        # Record launch evidence bound to the delivered head (passed only on a
        # clean launch). A cannot_verify path above never reaches here, so no
        # clean/false verdict is fabricated for an inability to launch.
        try:
            self.rstore.record_runtime_test(
                kind="launch", profile_id=profile_id, session_id=sid,
                passed=passed, head=head, detail=detail)
        except Exception:  # noqa: BLE001 — evidence recording is best-effort
            pass
        return {"status": status, "detail": detail, "session_id": sid}

    # -- spawn one sandboxed child ----------------------------------------- #
    def _spawn(self, argv: list[str], *, backend: str, cwd: Path, home: Path,
               tmp: Path, port: int | None, log_path: Path,
               append: bool, env: dict[str, str] | None = None,
               display: bool = False,
               extra_writable: list[str] | None = None,
               reader_sink: list[Any] | None = None) -> Any:
        from errorta_tools.runner.preview import spawn_sandboxed_child
        # network_allowed=True (the egress default): a dev server must bind its
        # port and installs must fetch. The sandbox's protection here is
        # filesystem write confinement, applied inside spawn_sandboxed_child.
        # display=True (F101-03 T1) additionally grants window-server access
        # without re-opening network / out-of-workspace writes.
        # extra_writable widens the write confinement to the managed venv dir
        # (never the sidecar's own site-packages) for install + .pyc writes.
        if env is None:
            env = _child_env(runner_home=home, runner_tmp=tmp, port=port,
                             include_display=display)
        writable = [str(home), str(tmp), *(extra_writable or [])]
        log_fh = open(log_path, "a" if append else "w", encoding="utf-8", errors="replace")
        proc = spawn_sandboxed_child(
            backend=backend, argv=argv, workspace_root=str(self.workspace_root),
            writable_paths=writable, cwd=str(cwd), env=env,
            display_allowed=display,
        )
        reader = threading.Thread(
            target=self._pump_logs, args=(proc, log_fh, log_path),
            name=f"runtime-log-{log_path.stem}", daemon=True)
        reader.start()
        # F146 Slice C: let a synchronous caller (the launch probe) join the log
        # pump on teardown, so the captured log is fully flushed before it reads
        # the tail for crash classification (avoids a read-before-flush race on
        # the traceback). Other callers pass nothing and are unaffected.
        if reader_sink is not None:
            reader_sink.append(reader)
        return proc

    def _pump_logs(self, proc: Any, log_fh, log_path: Path) -> None:
        try:
            written = log_path.stat().st_size if log_path.exists() else 0
        except OSError:
            written = 0
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if written >= _LOG_CAP_BYTES:
                    continue  # cap reached; drain stdout but stop writing
                line = redact_log_line(raw.rstrip("\n"))
                try:
                    log_fh.write(line + "\n")
                    log_fh.flush()
                    written += len(line) + 1
                except OSError:
                    break
        finally:
            try:
                log_fh.close()
            except OSError:
                pass

    # -- health/liveness monitor for a started runtime --------------------- #
    def _monitor(self, live: _Live, profile: RuntimeProfile) -> None:
        sid = live.session_id
        self._set_state(live, "running")
        health = profile.health or {}
        is_http = str(health.get("type")) == "http"
        timeout = _clamp(float(health.get("timeout_seconds", 20) or 20), 1.0, 120.0)
        url = _sub_port(str(health.get("url", "")), live.port) if is_http else ""
        deadline = time.monotonic() + timeout
        healthy = False
        while not live.stop_event.is_set():
            rc = live.proc.poll() if live.proc else None
            if rc is not None:
                # Exited on its own (not via stop()).
                if not live.stopping:
                    state = "stopped" if rc == 0 else "crashed"
                    self._set_state(live, state, exit_code=rc)
                self._unregister(sid)
                return
            if is_http and not healthy:
                ok, detail = _probe(url)
                if ok:
                    healthy = True
                    self._set_state(live, "healthy",
                                    health_status={"ok": True, "detail": detail})
                elif time.monotonic() > deadline:
                    self._set_state(live, "unhealthy",
                                    health_status={"ok": False, "detail": detail})
                    deadline = float("inf")  # don't re-probe; keep watching liveness
            time.sleep(_POLL_INTERVAL)
        # stop_event was set: teardown owns the terminal state.
        self._unregister(sid)

    # -- stop / teardown (D3: SIGTERM -> grace -> SIGKILL -> reap) ---------- #
    def stop(self, profile_id: str) -> dict[str, Any]:
        stopped_any = False
        for sess in self.rstore.list_sessions():
            if sess.profile_id != profile_id or sess.state in _TERMINAL:
                continue
            with _LIVE_LOCK:
                live = _LIVE.get(sess.session_id)
            if live is not None:
                _teardown_live(live)
                self.rstore.update_session(sess.session_id, state="stopped",
                                           ended_at=_now())
                stopped_any = True
            else:
                # Non-terminal on disk but not live (orphan) -> mark stopped.
                self.rstore.update_session(sess.session_id, state="stopped",
                                           ended_at=_now())
                stopped_any = True
        return {"stopped": True, "any": stopped_any}

    # -- one-shot health check --------------------------------------------- #
    def health_check(self, profile_id: str) -> dict[str, Any]:
        profile = self.rstore.get_profile(profile_id)
        if profile is None:
            raise RuntimeProcessError("profile_not_found")
        health = profile.health or {}
        if str(health.get("type")) != "http":
            return {"ok": False, "detail": "no http health check configured"}
        port = self._active_port(profile_id) or self._preferred_port(profile)
        url = _sub_port(str(health.get("url", "")), port)
        ok, detail = _probe(url)
        return {"ok": ok, "detail": detail}

    def probe_demo(self, profile_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        """Probe the profile's demo URL with the active session's port
        substituted (S4 ``demo_smoke``). Only meaningful for ``demo.type=="url"``."""
        profile = self.rstore.get_profile(profile_id)
        if profile is None:
            raise RuntimeProcessError("profile_not_found")
        demo = profile.demo or {}
        if str(demo.get("type")) != "url":
            return {"ok": False, "detail": "no url demo configured"}
        port = (
            self._session_port(session_id)
            if session_id
            else self._active_port(profile_id)
        ) or self._preferred_port(profile)
        ok, detail = _probe(_sub_port(str(demo.get("url", "")), port))
        return {"ok": ok, "detail": detail}

    # -- reads -------------------------------------------------------------- #
    def get_session(self, session_id: str) -> RuntimeSession | None:
        return self.rstore.get_session(session_id)

    # -- desktop launch evidence: a screenshot of the app's own window ----- #
    def _shots_dir(self) -> Path:
        d = self.work_root / "runtime-shots"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _live_pids(self, session_id: str) -> set[int]:
        """The pid set (leader + best-effort descendants) whose window we may
        capture. Descendants via psutil when present; else just the leader."""
        with _LIVE_LOCK:
            live = _LIVE.get(session_id)
        if live is None or live.proc is None:
            return set()
        pids: set[int] = set()
        try:
            pids.add(int(live.proc.pid))
        except (TypeError, ValueError, AttributeError):
            return set()
        try:
            import psutil  # type: ignore
            for child in psutil.Process(live.proc.pid).children(recursive=True):
                pids.add(int(child.pid))
        except Exception:
            pass  # psutil absent / process gone — leader pid alone is fine
        return pids

    def capture_screenshot(self, session_id: str) -> str | None:
        """Best-effort capture of the live session's app window (never the full
        desktop). Returns a work-root-relative ref, or None — an honest "no
        screenshot" — when window capture isn't available on this host (no
        display / no Quartz / non-macOS). Never raises."""
        pids = self._live_pids(session_id)
        if not pids:
            return None
        from errorta_tools.runner.preview import capture_app_window
        out = self._shots_dir() / f"{session_id}.png"
        if capture_app_window(pids=pids, out_path=out):
            ref = f"runtime-shots/{session_id}.png"
            # F101-03 S7 — surface the demo asset on the session record (F093
            # completion summary / deliverable export read it from here).
            try:
                if self.rstore.get_session(session_id) is not None:
                    self.rstore.update_session(session_id, screenshot_ref=ref)
            except Exception:
                pass  # best-effort; a stamp failure never fails the capture
            return ref
        return None

    def get_logs(self, session_id: str) -> dict[str, Any]:
        sess = self.rstore.get_session(session_id)
        if sess is None or not sess.log_ref:
            return {"lines": [], "truncated": False}
        log_path = self.work_root / sess.log_ref
        if not log_path.exists():
            return {"lines": [], "truncated": False}
        try:
            size = log_path.stat().st_size
            text = log_path.read_text("utf-8", errors="replace")
        except OSError:
            return {"lines": [], "truncated": False}
        lines = text.splitlines()
        tail = lines[-_LOG_TAIL_LINES:]
        truncated = size >= _LOG_CAP_BYTES or len(lines) > _LOG_TAIL_LINES
        return {"lines": tail, "truncated": truncated}

    # -- helpers ------------------------------------------------------------ #
    def _preferred_port(self, profile: RuntimeProfile) -> int | None:
        for p in profile.ports:
            pref = p.get("preferred")
            if isinstance(pref, int):
                return pref
        return None

    def _resolve_listen_port(self, profile: RuntimeProfile) -> int:
        """The loopback port the runtime targets for health/demo. A web port
        marked ``fixed`` (the app hardcodes it and ignores the injected ``PORT``,
        e.g. Flask's ``app.run(port=5000)``) is used EXACTLY — Errorta can't move
        the app off it, so allocating a different ephemeral port would leave
        health/demo pointed where nothing is listening (the AirPlay-squats-:5000
        case: the preferred-port bind test fails, the old code fell back to an
        ephemeral port, but the app still bound its hardcoded port). A non-fixed
        port (env-driven, or a ``{port}`` start argv Errorta substitutes) is
        allocated as usual — Errorta controls it and the app honors it."""
        for p in profile.ports:
            pref = p.get("preferred")
            # Honor a fixed port only in the non-privileged range — a hardcoded
            # port <1024 can't be bound by the unprivileged sandboxed child anyway,
            # so keep allocate_loopback_port's privileged-port guard by falling
            # through to it (the app will still crash on its own bind attempt, but
            # Errorta doesn't target an unbindable privileged port for health/demo).
            if isinstance(pref, int) and p.get("fixed") and 1024 <= pref <= 65535:
                return pref
        return allocate_loopback_port(self._preferred_port(profile))

    def _active_port(self, profile_id: str) -> int | None:
        for sess in reversed(self.rstore.list_sessions()):
            if (sess.profile_id == profile_id and sess.state not in _TERMINAL
                    and sess.allocated_ports):
                return sess.allocated_ports[0]
        return None

    def _session_port(self, session_id: str | None) -> int | None:
        if not session_id:
            return None
        sess = self.rstore.get_session(session_id)
        if sess is not None and sess.allocated_ports:
            return sess.allocated_ports[0]
        return None

    def _record_blocked(self, profile_id: str, *, reason: str) -> RuntimeSession:
        sid = self.rstore.new_session_id()
        session = RuntimeSession(
            session_id=sid, profile_id=profile_id, state="crashed",
            started_at=_now(), ended_at=_now(), sandbox_backend="none",
            error=reason)
        self.rstore.append_session(session)
        return session

    def _set_state(self, live: _Live, state: str, **patch: Any) -> None:
        if live.stopping and state not in _TERMINAL:
            return
        self.rstore.update_session(live.session_id, state=state, **patch)

    def _unregister(self, session_id: str) -> None:
        with _LIVE_LOCK:
            _LIVE.pop(session_id, None)


# --------------------------------------------------------------------------- #
# Teardown — group signal cascade (mirrors the F089 tunnel teardown discipline).
# --------------------------------------------------------------------------- #
def _kill_group(live: _Live, *, grace: float | None = None) -> None:
    """SIGTERM -> grace -> SIGKILL -> reap the live child's process GROUP, plus
    the optional explicit teardown command. Sets ``stopping``/``stop_event`` but
    does NOT join threads or unregister — safe to call from the monitor thread
    itself (the F101-02 timeout-kill path). ``_teardown_live`` wraps this with
    the thread-join + unregister for external callers."""
    grace = _GRACE_SECONDS if grace is None else grace
    live.stopping = True
    live.stop_event.set()
    proc = live.proc
    if proc is not None and proc.poll() is None and live.pgid is not None:
        try:
            os.killpg(live.pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + grace
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                os.killpg(live.pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    # F101 S6: run the explicit teardown command (e.g. `docker compose down`)
    # AFTER killing the foreground process group, so a container runtime leaves
    # no containers behind. Best-effort + bounded; never blocks teardown.
    if live.stop_argv:
        try:
            from errorta_tools.runner.preview import run_teardown_command
            run_teardown_command(live.stop_argv, cwd=live.cwd, env=live.env or {})
        except Exception:
            pass


def _teardown_live(live: _Live, *, grace: float | None = None) -> None:
    _kill_group(live, grace=grace)
    for t in live.threads:
        if t is threading.current_thread():
            continue  # never join our own thread (timeout-kill from the monitor)
        t.join(timeout=2)
    with _LIVE_LOCK:
        _LIVE.pop(live.session_id, None)


def teardown_all() -> int:
    """Tear down every live preview process. Called from the sidecar lifespan
    finally block (D3: app exit leaves no orphaned servers / bound ports)."""
    with _LIVE_LOCK:
        lives = list(_LIVE.values())
    for live in lives:
        _teardown_live(live)
    return len(lives)


def teardown_project(project_id: str) -> int:
    """Tear down every live preview process for one project (project switch)."""
    with _LIVE_LOCK:
        lives = [v for v in _LIVE.values() if v.project_id == project_id]
    for live in lives:
        _teardown_live(live)
    return len(lives)


# --------------------------------------------------------------------------- #
# F157 — persisted-session orphan reaping.
#
# `_LIVE` only tracks processes THIS sidecar spawned, and `teardown_all` runs
# only on a graceful shutdown (server.py lifespan finally). A crash / SIGKILL, or
# a delete against a project whose server outlived a prior sidecar, leaves an
# orphaned process group that nothing reaps. The pgid is already persisted per
# session (`update_session(sid, pgid=...)`), so we can reap by pgid from the store
# even when `_LIVE` is empty. Kept SEPARATE from `_kill_group` (the proven,
# Popen-driven in-memory teardown) so this cross-restart path can't regress it.
# --------------------------------------------------------------------------- #
def _valid_pgid(pgid: Any) -> bool:
    """A pgid we are allowed to signal. Rejects None and anything <= 1 — pgid 0
    means the CALLER's own process group (killpg(0, …) would signal the sidecar
    itself) and pgid 1 is init. Defense-in-depth for every signal path so a
    corrupt/legacy persisted pgid can never turn a reap into self-immolation."""
    return isinstance(pgid, int) and pgid > 1


def _pgid_alive(pgid: int) -> bool:
    """True if the process group still exists (signal 0 probes without killing)."""
    if not _valid_pgid(pgid):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists but not ours to signal — treat as alive
    except OSError:
        return False


def _kill_pgid(pgid: int, *, grace: float | None = None) -> bool:
    """SIGTERM -> grace -> SIGKILL a process GROUP by bare pgid (no Popen handle).

    Returns True if the group is gone afterward. Best-effort: swallows
    ProcessLookupError / PermissionError / OSError. This is the persisted-reap
    counterpart to `_kill_group`; ownership MUST already be confirmed by the
    caller (`_pgid_is_ours`) — this function does not re-check."""
    if not _valid_pgid(pgid):
        return False   # never signal pgid 0 (our own group) / 1 (init)
    grace = _GRACE_SECONDS if grace is None else grace

    def _reap_if_child() -> None:
        # If the group leader is OUR child, reap it so it doesn't linger as a
        # zombie (which killpg(,0) still reports as a live group). In production
        # the orphan was reparented to init after its sidecar died -> ECHILD here,
        # and init reaps it; either way this is harmless best-effort.
        try:
            os.waitpid(pgid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        _reap_if_child()
        return not _pgid_alive(pgid)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap_if_child()
        if not _pgid_alive(pgid):
            break
        time.sleep(0.05)
    if _pgid_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        time.sleep(0.05)
        _reap_if_child()
    return not _pgid_alive(pgid)


def _pgid_is_ours(pgid: int, *, workspace_root: Path) -> bool:
    """PID-reuse guard — the safety-critical gate before any persisted-reap kill.

    Returns True ONLY if the group leader (pid == pgid for our setsid spawns) is
    a live, non-zombie process whose resolved cwd is inside this project's
    workspace_root — the invariant every managed_local spawn enforces (spawn cwd =
    (workspace_root / working_dir).resolve(), guarded to stay within the root).

    Fail-closed: no psutil, an unreadable/zombie process, or a cwd we cannot
    resolve to inside the workspace -> False. We would rather leave an orphan than
    risk SIGKILLing a stranger's process that happens to reuse the pgid after a
    reboot. There is deliberately NO argv fallback: a generic `npm run dev`
    cmdline is not a safe identity signal."""
    if not _valid_pgid(pgid):
        return False
    # The kill acts on the GROUP `pgid`; verify the process we are about to
    # identity-check (pid == pgid) is actually THAT group's leader
    # (getpgid(pid) == pgid) — every setsid spawn is. This ties the ownership
    # check to the exact target killpg() will signal, so PID reuse can't make us
    # validate one process (pid==pgid) while killpg signals a different group.
    try:
        if os.getpgid(pgid) != pgid:
            return False
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        import psutil  # type: ignore
    except Exception:
        return False
    try:
        proc = psutil.Process(pgid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        cwd = Path(proc.cwd()).resolve()
    except Exception:
        return False
    try:
        root = Path(workspace_root).resolve()
    except Exception:
        return False
    return cwd == root or cwd.is_relative_to(root)


def _project_workspace_root(project_id: str) -> Path | None:
    """The apply-workspace root for a project, computed WITHOUT side effects
    (ApplyWorkspace.__init__ only derives paths; it creates nothing)."""
    try:
        from errorta_tools.runner.apply_workspace import ApplyWorkspace
        return ApplyWorkspace(run_id=f"coding-{project_id}").root
    except Exception:
        return None


def reap_persisted_sessions(rstore: RuntimeProfileStore, *, project_id: str,
                            grace: float | None = None) -> int:
    """Reap orphaned managed-local servers recorded in one project's session store.

    For each persisted non-terminal session, decided by the pgid:

    - a pgid the CURRENT sidecar is actively tracking (in ``_LIVE``) is left
      strictly alone — it is a healthy running server, not an orphan;
    - a pgid whose group is already GONE is recorded ``stopped``/``orphan_gone``
      so the store stops advertising a phantom;
    - a pgid confirmed ours (`_pgid_is_ours`) AND whose group we then confirm dead
      after the kill is recorded ``stopped``/``reaped_orphan``;
    - a pgid that is ALIVE but not confirmable as ours (or a kill we could not
      confirm) is left NON-terminal on purpose — we neither kill a stranger that
      may have reused the pgid nor mark a real orphan terminal (which would
      abandon it); a later sweep retries once it is confirmable or gone.

    Returns the number of groups actually killed. Per-session failures are
    isolated so one bad session can't abort the rest of the sweep."""
    workspace_root = _project_workspace_root(project_id)
    if workspace_root is None:
        return 0
    # Never reap a process this sidecar is actively running (defends the public
    # entrypoint: without this a call while a healthy server is live would
    # SIGKILL it, since its pgid IS alive and its cwd IS in the workspace).
    with _LIVE_LOCK:
        live_pgids = {v.pgid for v in _LIVE.values() if v.pgid is not None}
    killed = 0
    for sess in rstore.list_sessions():
        if sess.state in _TERMINAL:
            continue
        pgid = sess.pgid
        try:
            if not _valid_pgid(pgid):
                # None is a mid-spawn session (leave it); a corrupt <=1 pgid is
                # never signalable — record it gone so it stops being advertised.
                if pgid is not None:
                    rstore.update_session(sess.session_id, state="stopped",
                                          error="orphan_gone", ended_at=_now())
                continue
            if pgid in live_pgids:
                continue  # a server THIS sidecar owns — not an orphan
            if not _pgid_alive(pgid):
                rstore.update_session(sess.session_id, state="stopped",
                                      error="orphan_gone", ended_at=_now())
            elif _pgid_is_ours(pgid, workspace_root=workspace_root):
                if _kill_pgid(pgid, grace=grace):
                    rstore.update_session(sess.session_id, state="stopped",
                                          error="reaped_orphan", ended_at=_now())
                    killed += 1
                # else: kill unconfirmed — leave non-terminal, a later sweep retries
            # else: alive but not confirmable as ours — leave non-terminal (never
            # abandon a real orphan, never kill a pgid-reuse stranger)
        except Exception:  # noqa: BLE001 — isolate one session; sweep the rest
            continue
    return killed


def reap_all_persisted_orphans() -> int:
    """Boot sweep: reap managed-local servers orphaned by a NON-graceful prior
    exit, across every project. Best-effort and defensive per project — one
    unreadable store must not abort the sweep or block sidecar startup."""
    from .ledger import LedgerStore, list_projects
    total = 0
    try:
        projects = list_projects()
    except Exception:
        return 0
    for entry in projects:
        pid = entry.get("id") if isinstance(entry, dict) else None
        if not pid:
            continue
        try:
            rstore = RuntimeProfileStore.for_ledger(LedgerStore(pid))
            total += reap_persisted_sessions(rstore, project_id=pid)
        except Exception:
            continue
    return total


# --------------------------------------------------------------------------- #
# Small free functions
# --------------------------------------------------------------------------- #
def _now() -> str:
    from .ledger import _now as ledger_now
    return ledger_now()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _safe_getpgid(proc: Any) -> int:
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return proc.pid


def _probe(url: str) -> tuple[bool, str]:
    from errorta_tools.runner.preview import probe_http
    return probe_http(url, _PROBE_TIMEOUT)


__all__ = [
    "RuntimeProcessManager",
    "RuntimeProcessError",
    "resolve_sandbox_backend",
    "allocate_loopback_port",
    "redact_log_line",
    "teardown_all",
    "teardown_project",
    "reap_persisted_sessions",
    "reap_all_persisted_orphans",
]
