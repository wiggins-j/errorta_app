"""Errorta sidecar HTTP server.

Boots a FastAPI app on a local port. The Tauri frontend connects to this
sidecar; AIAR's pipeline (RAG retrieval, judge, grounding) is reached
through it.

Run standalone (dev):
    python -m errorta_app.server

Or via uvicorn directly:
    uvicorn errorta_app.server:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from errorta_diagnostics.log_buffer import LogBuffer, install_buffer

from . import __version__
from . import settings as app_settings
from .build_info import build_info as _build_info
from .build_info import features as _features
from .health.aiar_pin import check_aiar_pin
from .routes import (
    agent_context as agent_context_routes,
)
from .routes import (
    aiar_connection as aiar_connection_routes,
)
from .routes import (
    alpha as alpha_routes,
)
from .routes import (
    auth as auth_routes,
)
from .routes import (
    briefs as briefs_routes,
)
from .routes import (
    coding as coding_routes,
)
from .routes import (
    corpora as corpora_routes,
)
from .routes import (
    corpus as corpus_routes,
)
from .routes import (
    council as council_routes,
)
from .routes import (
    diagnostics as diagnostics_routes,
)
from .routes import (
    export as export_routes,
)
from .routes import (
    gateway as gateway_routes,
)
from .routes import (
    hardware as hardware_routes,
)
from .routes import (
    judge as judge_routes,
)
from .routes import (
    mobile as mobile_routes,
)
from .routes import (
    model_gateway as model_gateway_routes,
)
from .routes import (
    ollama as ollama_routes,
)
from .routes import (
    onboarding as onboarding_routes,
)
from .routes import (
    residency as residency_routes,
)
from .routes import (
    services as services_routes,
)
from .routes import (
    settings as settings_routes,
)
from .routes import (
    shell as shell_routes,
)
from .routes import (
    tools as tools_routes,
)
from .routes import (
    watch as watch_routes,
)
from .routes import (
    welcome as welcome_routes,
)

# AIAR may or may not be importable yet during scaffold-only development.
# We detect it at startup so the frontend's health endpoint can report it.
try:
    import aiar  # type: ignore  # noqa: F401

    _AIAR_AVAILABLE = True
    _AIAR_VERSION = getattr(aiar, "__version__", "unknown")
except Exception:  # pragma: no cover — AIAR not yet installed in this env
    _AIAR_AVAILABLE = False
    _AIAR_VERSION = None


_TARGET_FD_SOFT_LIMIT = 16384


def _raise_fd_limit() -> None:
    """Raise the open-file (RLIMIT_NOFILE) soft limit for the sidecar process.

    A GUI-launched macOS app inherits a soft limit of just 256, and the frozen
    PyInstaller runtime already holds ~90 FDs for its bundled libraries — leaving
    very little headroom. A concurrent Coding run (multiple members, each spawning
    a CLI subprocess with its own pipes, plus git worktrees, chromadb/AIAR index
    files, grounding, and atomic ledger writes) transiently blows past that and
    fails with ``OSError: [Errno 24] Too many open files`` — which crashes the
    worker thread mid-run and leaves the run wedged "interrupted". Lift the soft
    limit toward the hard limit. Best-effort + cross-platform-guarded: never fail
    startup over it."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = _TARGET_FD_SOFT_LIMIT
        if hard != resource.RLIM_INFINITY:
            target = min(target, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            logging.getLogger(__name__).info(
                "raised RLIMIT_NOFILE soft limit %s -> %s", soft, target)
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logging.getLogger(__name__).warning("could not raise FD limit: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Attach the diagnostic log ring buffer to the root + uvicorn loggers,
    and run Council boot recovery (F031-02 / F031-00 §exit gate).

    Stored on ``app.state.log_buffer`` so the diagnostics route can read
    it without import-time coupling.
    """
    _raise_fd_limit()
    buffer = LogBuffer()
    loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    ]
    original_logger_levels = [(logger, logger.level) for logger in loggers]
    original_handler_levels = [
        (handler, handler.level)
        for logger in loggers
        for handler in logger.handlers
    ]
    log_handlers = [
        (logger, install_buffer(buffer, logger))
        for logger in loggers
    ]
    app.state.log_buffer = buffer

    # Optional full on-disk log sink (F088 traceability): when ERRORTA_LOG_FILE
    # is set, mirror every log line (root + uvicorn + errorta.* incl. the
    # grounding trace) to that file UNREDACTED so an operator can `tail -f` a
    # whole coding/grounding run. Off by default — stdout/the in-memory ring
    # buffer (`/diagnostics/log-tail`) remain the no-config path. Best-effort:
    # an unwritable path must not block startup.
    file_handler = None
    log_file = (os.environ.get("ERRORTA_LOG_FILE") or "").strip()
    if log_file:
        try:
            path = os.path.abspath(os.path.expanduser(log_file))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            for logger in loggers:
                logger.addHandler(file_handler)
            app.state.log_file = path
            logging.getLogger(__name__).info("logging to file: %s", path)
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning(
                "ERRORTA_LOG_FILE could not be opened (%s): %s", log_file, exc)
            file_handler = None

    try:
        settings = app_settings.load()
        app_settings.apply_log_level(settings["log_level"])
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logging.getLogger(__name__).warning("Failed to apply persisted settings: %s", exc)

    # F031 boot recovery: close mid-flight cancels, mark orphan running runs
    # as `interrupted` (invariant 4 — fail closed). Best-effort: a failure here
    # must not block sidecar startup, but it is logged for diagnostics.
    try:
        from errorta_council import paths as _council_paths
        from errorta_council.recovery import scan_and_recover as _scan_and_recover
        from errorta_council.run_store import RunStore as _RunStore

        runs_dir = _council_paths.runs_dir()
        runs_dir.mkdir(parents=True, exist_ok=True)
        summary = _scan_and_recover(_RunStore(runs_dir=runs_dir))
        app.state.council_recovery = summary
        if summary.interrupted_runs or summary.corrupted_runs:
            logging.getLogger("errorta.council").info(
                "council recovery: interrupted=%d corrupted=%d",
                len(summary.interrupted_runs),
                len(summary.corrupted_runs),
            )
    except Exception as exc:  # pragma: no cover — defensive only
        logging.getLogger("errorta.council").warning(
            "council recovery failed at startup: %s", exc
        )
        app.state.council_recovery = None

    # F087-12 Coding Mode boot recovery: an orphaned persisted `running` coding
    # run is marked interrupted and in-flight tasks are requeued. Best-effort and
    # diagnostics-visible, matching Council recovery's fail-closed stance.
    try:
        from errorta_app.routes.coding import _live_project_ids
        from errorta_council.coding.run_recovery import scan_and_recover as _scan_coding

        # F087-13 WS-3: pass real liveness so recovery never reaps a worker that
        # started during/just-before the scan (empty at a clean boot).
        # F147 S9b: ALSO pass an owner-aware peer check so a *second* sidecar's
        # boot never reconciles a run that is live in ANOTHER sidecar (§13.1 —
        # the S9a boot-recovery gap). This reads the CURRENT ${ERRORTA_HOME}/
        # sidecar.json — which, at this point in lifespan, still names the peer
        # (our own advertisement is written LATER, below) — and cross-checks the
        # run's owner_pid against it + a /healthz probe to defeat pid-reuse. It is
        # fail-OPEN toward recovery, so a genuine orphan is always still cleared.
        summary = _scan_coding(
            live_project_ids=_live_project_ids(),
            owner_peer_fn=_coding_owner_peer_fn,
        )
        app.state.coding_recovery = summary
        if summary.interrupted_projects:
            logging.getLogger("errorta.coding").info(
                "coding recovery: interrupted=%d projects=%s",
                len(summary.interrupted_projects),
                ",".join(summary.interrupted_projects),
            )
    except Exception as exc:  # pragma: no cover - defensive only
        logging.getLogger("errorta.coding").warning(
            "coding recovery failed at startup: %s", exc
        )
        app.state.coding_recovery = None

    # F065: bring up the mobile LAN listener if the connector is enabled
    # (off by default — no socket otherwise). Best-effort; a failure here must
    # not block the main sidecar.
    try:
        from errorta_app import mobile_lifecycle

        app.state.mobile_lan = mobile_lifecycle.sync()
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("mobile LAN listener not started: %s", exc)
        app.state.mobile_lan = {"running": False, "reason": "error"}

    # F089: if remote AIAR is in managed-tunnel mode with auto_start, bring the
    # SSH tunnel up at startup (non-blocking — ensure spawns + watches in the
    # background) so the first grounding call finds it ready. Best-effort.
    try:
        from errorta_project_grounding import remote_config as _rc
        from errorta_tunnels import tunnel_manager as _tunnels

        _st = _rc.load_raw()
        if _st.managed and _st.auto_start:
            spec = _rc.tunnel_spec(_st)
            if spec is not None:
                _tunnels.ensure(spec, wait=False)
                logging.getLogger(__name__).info(
                    "remote-AIAR managed tunnel auto-start: host=%s remote=%s:%s",
                    _st.ssh_host, _st.remote_host, _st.remote_port)
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("managed SSH tunnel not started: %s", exc)

    # F063 A3: exit the sidecar if the Tauri shell that spawned us dies without
    # cleaning up (SIGKILL / crash / replaced on disk). No-op when
    # ERRORTA_PARENT_PID is unset (standalone dev runs).
    import threading as _threading

    watchdog_stop = _threading.Event()
    try:
        from .parent_watchdog import start_parent_death_watchdog

        app.state.parent_watchdog = start_parent_death_watchdog(
            stop_event=watchdog_stop
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("parent watchdog not started: %s", exc)
        app.state.parent_watchdog = None

    # F147 S9a: advertise this sidecar on disk (${ERRORTA_HOME}/sidecar.json) so
    # an out-of-process front-end (the headless CLI, app-doctor) can DISCOVER a
    # live sidecar's port+pid and adopt it instead of spawning a competitor.
    # Best-effort; removed on graceful shutdown iff we still own it.
    app.state.sidecar_advert = False
    try:
        from . import sidecar_advert as _advert

        port = _resolve_port()
        app.state.sidecar_port = port
        wrote = _advert.write_advertisement(
            port=port,
            pid=os.getpid(),
            commit=(_build_info() or {}).get("commit"),
        )
        app.state.sidecar_advert = bool(wrote)
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("sidecar advertisement not written: %s", exc)

    # F-DIST-01 — start the alpha check-in loop ONLY when this build ships the
    # gate on. Production keyless builds never start it, so they never phone
    # home. Lazy import so gate-off builds don't even load the module.
    app.state.alpha_sync_stop = None
    try:
        from errorta_alpha import config as _alpha_config

        if _alpha_config.gate_enabled():
            from errorta_alpha import lifecycle as _alpha_lifecycle

            app.state.alpha_sync_stop = _alpha_lifecycle.start_background_sync()
            # F-DIST-01 slice 7 — record a content-free breadcrumb on any uncaught
            # exception (opt-out extra); uploaded on the next check-in.
            from errorta_alpha import feedback as _alpha_feedback

            _alpha_feedback.install_crash_hook()
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning("alpha sync not started: %s", exc)

    try:
        yield
    finally:
        # F-DIST-01 — stop the alpha check-in loop (records a clean session on
        # the way out so crash-free-session accounting stays honest).
        try:
            stop = getattr(app.state, "alpha_sync_stop", None)
            if stop is not None:
                from errorta_alpha import telemetry as _alpha_telemetry

                _alpha_telemetry.record_clean_session()
                stop.set()
        except Exception:  # pragma: no cover - defensive
            pass
        # F065: stop the LAN listener BEFORE the watchdog (so it isn't killed
        # mid-request by a watchdog os._exit during graceful shutdown).
        try:
            from errorta_app import mobile_lifecycle

            mobile_lifecycle.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        # F089: kill every owned SSH tunnel so none leak across a restart.
        try:
            from errorta_tunnels import tunnel_manager as _tunnels

            _tunnels.teardown()
        except Exception:  # pragma: no cover - defensive
            pass
        # F101 D3: tear down every managed-local runtime preview process group
        # so no generated dev server / bound port leaks across a restart.
        try:
            from errorta_council.coding import runtime_process as _runtime

            _runtime.teardown_all()
        except Exception:  # pragma: no cover - defensive
            pass
        # F147 S9a: retract our sidecar advertisement (only if it still points at
        # us — a successor that already overwrote it is left alone). A crash skips
        # this; a reader validates the stale file against a live /healthz.
        try:
            from . import sidecar_advert as _advert

            _advert.remove_advertisement(only_if_pid=os.getpid())
        except Exception:  # pragma: no cover - defensive
            pass
        # Stop the watchdog so it can't os._exit() mid-teardown during a
        # graceful (parent-initiated) shutdown.
        watchdog_stop.set()
        for logger, handler in log_handlers:
            logger.removeHandler(handler)
        if file_handler is not None:
            for logger in loggers:
                logger.removeHandler(file_handler)
            try:
                file_handler.close()
            except Exception:  # pragma: no cover - defensive
                pass
        for logger, level in original_logger_levels:
            logger.setLevel(level)
        for handler, level in original_handler_levels:
            handler.setLevel(level)


app = FastAPI(
    title="Errorta sidecar",
    version=__version__,
    description="Errorta's local Python sidecar — thin layer over AIAR.",
    lifespan=lifespan,
)

# Allowed webview origins:
# - dev: the Vite dev server (`npm run tauri:dev`) at http://localhost:1420.
# - bundled app: Tauri v2 serves the frontend over a custom protocol whose
#   origin is `http://tauri.localhost` on macOS/Linux and `https://tauri.localhost`
#   on Windows. (`tauri://localhost` was the Tauri v1 origin — kept for safety.)
#   Without the `http(s)://tauri.localhost` entries, every non-GET request from
#   the installed .app fails its CORS preflight with 400, surfacing in the UI as
#   a generic "Load failed" on Save / Create / Run. GET works because simple
#   GETs skip the preflight — which is exactly why this only broke in the
#   bundled app and not in dev.
# Both ends are always localhost, so this list is permissive by design.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# Per-feature routers. Each module owns a single APIRouter with the prefix
# baked in (e.g. /hardware, /ollama). Feature agents add endpoints inside
# those modules without touching this file.
app.include_router(hardware_routes.router)
app.include_router(ollama_routes.router)
app.include_router(corpus_routes.router)
app.include_router(corpora_routes.router)
app.include_router(watch_routes.router)
app.include_router(shell_routes.router)
app.include_router(welcome_routes.router)
app.include_router(judge_routes.router)
app.include_router(model_gateway_routes.router)
app.include_router(onboarding_routes.router)
app.include_router(diagnostics_routes.router)
app.include_router(briefs_routes.router)
app.include_router(export_routes.router)
app.include_router(auth_routes.router)
app.include_router(services_routes.router)
app.include_router(residency_routes.router)
app.include_router(settings_routes.router)
app.include_router(council_routes.router)
app.include_router(coding_routes.router)
app.include_router(gateway_routes.router)
app.include_router(tools_routes.router)
app.include_router(agent_context_routes.router)
app.include_router(mobile_routes.router)
app.include_router(aiar_connection_routes.router)
app.include_router(alpha_routes.router)


def _residency_block() -> dict:
    """Build the ``residency`` block surfaced in ``/healthz``.

    F-INFRA-12 Phase B Slice 3: report the active mode, the upstream URL
    (constructed for ssh-remote, taken verbatim for cloud, ``None`` for
    local), and a coarse ``tunnel_state``. Slice 7 will replace the
    Slice-3 stand-in tunnel_state with a value read from the Rust
    shell's live tunnel watcher.

    Never raises — falls back to a local-mode dict if errorta_residency
    isn't importable for any reason.
    """
    try:
        from errorta_residency import config as residency_config
    except Exception:
        return {"mode": "local", "remote_url": None, "tunnel_state": "up"}

    try:
        state = residency_config.load()
    except Exception:
        return {"mode": "local", "remote_url": None, "tunnel_state": "up"}

    if state.mode == "ssh-remote":
        port = state.local_tunnel_port
        remote_url = f"http://127.0.0.1:{port}" if port else None
        return {
            "mode": "ssh-remote",
            "remote_url": remote_url,
            "tunnel_state": "up" if port else "down",
        }

    if state.mode == "cloud":
        remote_url = state.cloud_url
        # Cloud mode has no tunnel — derive ``tunnel_state`` from the last
        # probe outcome. ``up`` if the upstream answered, ``error`` otherwise.
        tunnel_state = "up"
        if remote_url:
            try:
                from errorta_residency import probe as residency_probe

                result = residency_probe.probe_https_url(
                    remote_url, token=state.cloud_token, timeout_s=2.0
                )
                tunnel_state = "up" if result.get("ok") else "error"
            except Exception:
                tunnel_state = "error"
        else:
            tunnel_state = "error"
        return {"mode": "cloud", "remote_url": remote_url, "tunnel_state": tunnel_state}

    # local (and any unknown mode) — always-up local-loopback.
    return {"mode": "local", "remote_url": None, "tunnel_state": "up"}


def _corpus_backend_block() -> dict:
    """Build the ``corpus_backend`` block surfaced in ``/healthz`` (F095).

    Reports which backend the unified corpus catalog reads from (remote AIAR /
    residency-remote / local) and whether Council/judge *retrieval* resolves to
    that same backend. Coordination is FALSE only when the catalog and retrieval
    genuinely point at different backends, so the UI can warn instead of silently
    retrieving against the wrong store. Never raises.
    """
    try:
        from errorta_app.corpus_catalog import resolve_corpus_backend

        backend = resolve_corpus_backend()
    except Exception:
        backend = {"kind": "local", "detail": {}}

    kind = backend.get("kind", "local")
    # F096 B4 / F116: compute coordination from the canonical resolver, which
    # compares the catalog-side backend against the retrieval target the data
    # plane actually uses (``aiar_retrieval_target``). True when both resolve to
    # the same place — including the common case of corpora listed AND retrieved
    # from the same remote AIAR. The ``kind != remote_aiar`` fallback only applies
    # if the resolver itself raises.
    try:
        from errorta_query.backend import resolve_aiar_backend

        coordinated = resolve_aiar_backend().coordinated
    except Exception:
        coordinated = kind != "remote_aiar"
    backend_id = None
    try:
        from errorta_aiar_connection import resolve_aiar_runtime

        backend_id = resolve_aiar_runtime().backend_id
    except Exception:
        backend_id = None
    return {
        "kind": kind,
        "detail": backend.get("detail", {}),
        "retrieval_coordinated": coordinated,
        "backend_id": backend_id,
    }


def _aiar_runtime_block() -> dict:
    try:
        from errorta_aiar_connection import resolve_aiar_runtime

        return resolve_aiar_runtime().to_public_dict()
    except Exception as exc:
        return {
            "kind": "disconnected",
            "runtime_kind": "disconnected",
            "display_name": "AIAR disconnected",
            "connected": False,
            "capabilities": {},
            "error_code": "aiar_runtime_probe_failed",
            "error_message": str(exc)[:200],
        }


@app.get("/healthz")
def healthz() -> dict:
    """Liveness + introspection probe.

    The Errorta React frontend calls this every few seconds to confirm the
    sidecar is up. Also reports whether AIAR is importable in this Python
    environment.
    """
    return {
        "service": "errorta-sidecar",
        "version": __version__,
        "now": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        # F147 S9a: identity + discovery — a front-end reads these to confirm the
        # sidecar it discovered (via ${ERRORTA_HOME}/sidecar.json) is the one it
        # reached, and who spawned it. Additive; existing fields are unchanged.
        "pid": os.getpid(),
        "port": _resolve_port(),
        "started_by": (os.environ.get("ERRORTA_STARTED_BY") or "unknown").strip()
        or "unknown",
        "aiar_available": _AIAR_AVAILABLE,
        "aiar_version": _AIAR_VERSION,
        "aiar_pin": check_aiar_pin(),
        "python": sys.version.split()[0],
        "briefs": True,
        "council": True,
        # Build provenance + capability surface: let the UI and app-doctor see
        # which commit this sidecar was built from, and which features it has,
        # so a stale bundle is detected explicitly instead of failing as a
        # confusing "sidecar unreachable"/404 downstream.
        "build": _build_info(),
        "features": _features(),
        "residency": _residency_block(),
        "corpus_backend": _corpus_backend_block(),
        "aiar_runtime": _aiar_runtime_block(),
    }


@app.get("/version")
def version() -> dict:
    return {"version": __version__}


def _resolve_port() -> int:
    """Pick the bind port.

    Order of precedence:
      1. ERRORTA_SIDECAR_PORT env (set by Tauri at runtime so each instance
         gets a fresh port).
      2. 8765 in dev so devs can hit it from the browser directly.
    """
    raw = os.environ.get("ERRORTA_SIDECAR_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    # 8770 chosen to avoid the common AIAR/Reaper ports (8765, 8766) the
    # user may already have running on the same machine.
    return 8770


def _probe_peer_healthz(port: int) -> dict | None:
    """GET ``/healthz`` on loopback ``port``; return the JSON body or ``None``.

    Used by boot recovery's owner-aware peer check to confirm the sidecar
    advertised in ``sidecar.json`` is really live (defeats a stale advertisement
    + a reused owner_pid). Short timeout, never raises — an unreachable/slow port
    just means "no confirmed peer", which fails OPEN toward recovery."""
    try:
        import httpx

        with httpx.Client(timeout=1.0) as client:
            resp = client.get(f"http://127.0.0.1:{int(port)}/healthz")
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 - best-effort probe
        return None


def _coding_owner_peer_fn(state: dict) -> bool:
    """F147 S9b — boot-recovery owner-aware seam (see ``scan_and_recover``).

    Confirms a ``running`` coding run is owned by a live, *advertised* peer
    sidecar so boot recovery stands down instead of clobbering it. Reads the
    CURRENT ``${ERRORTA_HOME}/sidecar.json`` and cross-checks the run's
    ``owner_pid`` against the advertised pid + a ``/healthz`` probe (defeats
    pid-reuse). Fail-OPEN: on any error, returns ``False`` (recover the orphan)."""
    try:
        from errorta_app import sidecar_advert
        from errorta_app.parent_watchdog import parent_alive
        from errorta_council.coding.locks import owner_is_live_peer_sidecar

        return owner_is_live_peer_sidecar(
            state,
            my_pid=os.getpid(),
            alive_fn=parent_alive,
            advert=sidecar_advert.read_advertisement(),
            healthz_fn=_probe_peer_healthz,
        )
    except Exception:  # noqa: BLE001 - never let the peer check block recovery
        return False


def main() -> None:
    """Entry point for `python -m errorta_app.server` and the PyInstaller bundle."""
    import uvicorn

    uvicorn.run(
        "errorta_app.server:app",
        host="127.0.0.1",
        port=_resolve_port(),
        log_level=os.environ.get("ERRORTA_LOG_LEVEL", "info"),
        reload=False,
    )


if __name__ == "__main__":
    main()
