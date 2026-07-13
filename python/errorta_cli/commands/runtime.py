"""``runtime`` — run the delivered program: read profiles + full control (§8.5).

The bare / ``--session`` read path is unchanged from S2. S7 adds the mutating
sub-actions, grounded against the real ``coding.py`` (file:line inline):

* ``runtime`` (bare)            → ``GET  .../runtime/profiles``            (2966)
* ``runtime --session <sid>``   → ``GET  .../runtime/sessions/{sid}``      (3268)
* ``runtime detect``            → ``POST .../runtime/detect``              (2995)
* ``runtime run [--go --reduced-isolation] [--open|--no-open]`` → ``POST .../runtime/run`` (``confirm``) (3006)
* ``runtime setup <id>``        → ``POST .../runtime/{id}/setup`` (``confirm:true``) (3143)
* ``runtime start <id>``        → ``POST .../runtime/{id}/start``          (3160)
* ``runtime run-cli <id> [--args ...] [--timeout N]`` → ``POST .../runtime/{id}/run-cli`` (3178)
* ``runtime stop <id>``         → ``POST .../runtime/{id}/stop``           (3233)
* ``runtime health <id>``       → ``POST .../runtime/{id}/health-check``   (3285)
* ``runtime test <id> --kind K``→ ``POST .../runtime/{id}/test``          (3311)
* ``runtime repair <id>``       → ``POST .../runtime/{id}/repair``         (3338)
* ``runtime logs <sid> [--watch]`` → ``GET .../runtime/sessions/{sid}/logs`` (3277)
* ``runtime profile set <id> --profile <json>`` → ``PUT .../runtime/profiles/{id}`` (2972)
* ``runtime evidence``          → ``GET  .../projects/{id}`` (runtime_evidence + delivered)

**Residency.** Grounded against the real routes: only ``repair`` is
``refuse_local_dataplane_if_remote`` (it files a coding-ledger dev task);
``run`` / ``run-cli`` are deliberately NOT residency-guarded (running a generated
project locally is a different data plane than AIAR-corpus writes — coding.py:3019
/ 3189). A residency refusal on ``repair`` surfaces as :class:`ResidencyRefused`
(exit 4) via the client's 409 mapping.

**Safety.** The mutating sub-actions take :func:`_mutate.guard_sole_owner` +
the ``--yes``/confirm gate (a launch spawns a real process / runs install
scripts). ``detect`` / ``health`` are probes (no state write, no gate). ``run``
without ``--go`` is a PREVIEW (no execution, no gate). ``--watch`` is refused on
the mutating actions (a watched launch would re-spawn every tick).
"""
from __future__ import annotations

import json as _json
import sys
import time
import webbrowser
from typing import Any, Callable

from ..client import SidecarClient
from ..errors import CliError, NotFound
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render import runtime as _rr
from ..session import Context
from . import _base, _mutate

# Sub-actions that WRITE runtime state / spawn a process — gated + un-watchable.
_MUTATING_ACTIONS = {"setup", "start", "stop", "run-cli", "test", "repair", "profile"}


def _rt(ctx: Context) -> str:
    return f"/coding/projects/{ctx.project_id}/runtime"


def _post(client: SidecarClient, ctx: Context, leaf: str,
          body: dict[str, Any] | None = None) -> dict[str, Any]:
    return client.post_json(f"{_rt(ctx)}/{leaf}", json=body or {}) or {}


def _session_result(resp: dict[str, Any], verb: str) -> dict[str, Any]:
    return {"_kind": "session", "verb": verb, "session": resp.get("session") or {}}


def _profile_id(args: dict[str, Any]) -> str:
    """Resolve the profile/session positional; ``profile set <id>`` skips 'set'."""
    action = str(args.get("action") or "").lower()
    if action == "profile" and str(args.get("p1") or "").lower() == "set":
        return str(args.get("p2") or "").strip()
    return str(args.get("p1") or "").strip()


# --------------------------------------------------------------------------- #
# Read path (unchanged from S2) — bare profiles + optional --session.
# --------------------------------------------------------------------------- #

def _read_profiles(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    payload = dict(client.get_json(f"{_rt(ctx)}/profiles") or {})
    sid = args.get("session")
    if sid:
        try:
            session = client.get_json(f"{_rt(ctx)}/sessions/{sid}")
            payload["session"] = (session or {}).get("session")
        except NotFound:
            payload["session"] = None
    return payload


# --------------------------------------------------------------------------- #
# Mutating + probe sub-actions.
# --------------------------------------------------------------------------- #

def _detect(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    # A proposal probe — reads the worktree, writes no state; no gate.
    return {"_kind": "detect", **(client.post_json(f"{_rt(ctx)}/detect", json={}) or {})}


def _run(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    execute = bool(args.get("go"))
    body: dict[str, Any] = {
        "confirm": execute,
        "confirm_reduced_isolation": bool(args.get("reduced-isolation")),
    }
    if execute:
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "launch the delivered program",
                               note="spawns a real local process"):
            return {"_kind": "aborted"}
    run = client.post_json(f"{_rt(ctx)}/run", json=body) or {}
    payload: dict[str, Any] = {"_kind": "run", "run": run}
    if execute:
        # A web/API launch serves at a local URL the user needs. Surface it (so a
        # headless caller can read `_url`) and — interactively — open it once the
        # dev server answers.
        _surface_served_url(args, run, payload)
    return payload


def _served_url(run: dict[str, Any]) -> str | None:
    """The ``http://localhost:PORT`` a server-modality launch serves at, or None.

    Only server modality (``web``/``api`` -> ``server``) binds a port; a CLI or
    desktop launch has no URL. The port is the one the sidecar actually allocated
    (``session.allocated_ports``), so it reflects the real bind, not a guess."""
    plan = run.get("plan") or {}
    if str(plan.get("modality") or "") != "server":
        return None
    session = run.get("session") or {}
    ports = session.get("allocated_ports") or []
    port = ports[0] if ports else None
    return f"http://localhost:{port}" if port else None


def _should_open(args: dict[str, Any]) -> bool:
    """Auto-open the browser? ``--no-open`` never, ``--open`` always, else only on
    an interactive TTY (a piped/headless run must not spawn a browser)."""
    if args.get("no-open"):
        return False
    if args.get("open"):
        return True
    try:
        return bool(sys.stdout.isatty())
    except (ValueError, AttributeError):  # pragma: no cover — closed stdout
        return False


def _await_http(url: str, *, attempts: int = 40, interval: float = 0.5,
                sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll ``url`` until it answers (any HTTP status) or ``attempts`` elapse.

    A freshly-spawned dev server (Next.js, Vite, …) takes seconds to compile
    before it binds; opening the browser before then just shows a connection
    error. Best-effort and bounded (~20-30s worst case): returns True once the
    server responds, False on timeout — either way the caller then opens the
    browser. Ctrl-C propagates (``KeyboardInterrupt`` is not an ``Exception``).
    """
    import httpx
    for _ in range(attempts):
        try:
            httpx.get(url, timeout=0.75)
            return True
        except Exception:  # noqa: BLE001 — any transport error means "not up yet"
            sleep(interval)
    return False


def _echo(message: str) -> None:  # pragma: no cover — thin stderr wrapper
    print(message, file=sys.stderr, flush=True)


def _surface_served_url(
    args: dict[str, Any], run: dict[str, Any], payload: dict[str, Any], *,
    opener: Callable[[str], bool] = webbrowser.open,
    waiter: Callable[[str], bool] = _await_http,
    echo: Callable[[str], None] = _echo,
) -> None:
    url = _served_url(run)
    if not url:
        return
    payload["_url"] = url
    if not _should_open(args):
        return
    # Announce the URL up front (to stderr) so the wait isn't a silent hang, then
    # open the browser once the dev server answers.
    echo(f"serving at {url} — waiting for the dev server, then opening your browser…")
    waiter(url)
    payload["_opened"] = bool(opener(url))


def _setup(client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"run setup for '{pid}'",
                           note="runs install commands (e.g. npm/pip postinstall)"):
        return {"_kind": "aborted"}
    return _session_result(_post(client, ctx, f"{pid}/setup", {"confirm": True}), "setup")


def _start(client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"start '{pid}'", note="spawns a real local process"):
        return {"_kind": "aborted"}
    return _session_result(_post(client, ctx, f"{pid}/start"), "start")


def _stop(client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"stop '{pid}'", note="terminates the running process"):
        return {"_kind": "aborted"}
    return {"_kind": "stopped", **_post(client, ctx, f"{pid}/stop")}


def _run_cli(client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"run '{pid}' once", note="runs the CLI/script once"):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {}
    if args.get("args") is not None:
        body["extra_args"] = str(args["args"])
    if args.get("timeout") is not None:
        body["timeout_seconds"] = str(args["timeout"])
    return _session_result(_post(client, ctx, f"{pid}/run-cli", body), "run-cli")


def _health(client: SidecarClient, ctx: Context, pid: str) -> dict[str, Any]:
    # A liveness probe (no state write) — no gate.
    return {"_kind": "health", **_post(client, ctx, f"{pid}/health-check")}


def _test(client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind") or "").strip()
    if not kind:
        return _base.usage("runtime test <id> --kind <smoke|demo_smoke|...>")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"run the '{kind}' runtime test for '{pid}'",
                           note="launches the program to capture evidence"):
        return {"_kind": "aborted"}
    return {"_kind": "test", **_post(client, ctx, f"{pid}/test", {"kind": kind})}


def _repair(client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"file a repair task for '{pid}'",
                           note="creates a Coding Team dev task"):
        return {"_kind": "aborted"}
    # RESID: /repair is refuse_local_dataplane_if_remote — a remote data plane
    # raises ResidencyRefused (exit 4) via the client's 409 mapping.
    return {"_kind": "repair", **_post(client, ctx, f"{pid}/repair")}


def _logs(client: SidecarClient, ctx: Context, sid: str) -> dict[str, Any]:
    if not sid:
        return _base.usage("runtime logs <session-id> [--watch]")
    logs = client.get_json(f"{_rt(ctx)}/sessions/{sid}/logs") or {}
    return {"_kind": "logs", "logs": logs}


def _profile_set(
    client: SidecarClient, ctx: Context, pid: str, args: dict[str, Any]
) -> dict[str, Any]:
    if not pid:
        return _base.usage("runtime profile set <id> --profile '<json>'")
    raw = args.get("profile")
    if raw is None:
        return _base.usage("runtime profile set <id> --profile '<json object>'")
    try:
        parsed = _json.loads(str(raw))
    except (ValueError, TypeError) as exc:
        raise CliError(f"--profile must be a JSON object: {exc}", code="bad_profile")
    if not isinstance(parsed, dict):
        raise CliError("--profile must be a JSON object", code="bad_profile")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"save runtime profile '{pid}'",
                           note="defines the commands that will run"):
        return {"_kind": "aborted"}
    return {"_kind": "profile",
            **(client.put_json(f"{_rt(ctx)}/profiles/{pid}", json={"profile": parsed}) or {})}


def _evidence(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    result = client.get_json(f"/coding/projects/{ctx.project_id}") or {}
    return {"_kind": "evidence", "project": result.get("project") or result}


# --------------------------------------------------------------------------- #
# Dispatch + render.
# --------------------------------------------------------------------------- #

def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    action = str(args.get("action") or "").strip().lower()
    # `run` is watchable as a PREVIEW, but `run --go` performs a real launch
    # (installs deps + starts the program) — a mutation that must not re-fire.
    is_mutation = action in _MUTATING_ACTIONS or (action == "run" and bool(args.get("go")))
    if is_mutation and args.get("watch"):
        label = "run --go" if action == "run" else action
        raise CliError(
            f"--watch is for read views; `runtime {label}` spawns/records and "
            "can't be watched (it would re-fire every tick). Run it once, then "
            "watch progress with: runtime logs <sid> --watch",
            code="watch_on_mutation",
        )
    if action in ("", "profiles"):
        return _read_profiles(client, ctx, args)
    if action == "detect":
        return _detect(client, ctx)
    if action == "run":
        return _run(client, ctx, args)
    if action == "setup":
        return _setup(client, ctx, _profile_id(args), args)
    if action == "start":
        return _start(client, ctx, _profile_id(args), args)
    if action == "stop":
        return _stop(client, ctx, _profile_id(args), args)
    if action == "run-cli":
        return _run_cli(client, ctx, _profile_id(args), args)
    if action == "health":
        return _health(client, ctx, _profile_id(args))
    if action == "test":
        return _test(client, ctx, _profile_id(args), args)
    if action == "repair":
        return _repair(client, ctx, _profile_id(args), args)
    if action == "logs":
        return _logs(client, ctx, _profile_id(args))
    if action == "profile":
        return _profile_set(client, ctx, _profile_id(args), args)
    if action == "evidence":
        return _evidence(client, ctx)
    return _base.usage(
        "runtime [ | --session <sid> | detect | run [--go --reduced-isolation] [--open|--no-open] | "
        "setup <id> | start <id> | stop <id> | run-cli <id> [--args ...] [--timeout N] | "
        "health <id> | test <id> --kind K | repair <id> | logs <sid> [--watch] | "
        "profile set <id> --profile <json> | evidence ]")


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind is None:  # the read path (bare / --session) — same as S2.
        return _rr.render_runtime(payload, verbosity)
    if kind == "aborted":
        return render(muted("aborted — nothing changed."))
    if kind == "detect":
        return _rr.render_detect(payload)
    if kind == "run":
        return _rr.render_run(payload)
    if kind == "session":
        return _rr.render_session_result(payload, verb=str(payload.get("verb") or "session"))
    if kind == "stopped":
        return _rr.render_stopped(payload)
    if kind == "health":
        return _rr.render_health(payload)
    if kind == "test":
        return _rr.render_test(payload)
    if kind == "repair":
        return render("repair task filed — track it with: errorta tasks")
    if kind == "logs":
        return _rr.render_logs(payload)
    if kind == "profile":
        return _rr.render_profile_saved(payload)
    if kind == "evidence":
        return _rr.render_evidence(payload)
    return render(muted("nothing to show"))


register(Command(
    name="runtime",
    help="Run the delivered program: profiles (read) + detect/run/setup/logs/… control.",
    call=_call,
    render=_render,
    params=(
        Param("action", "sub-action (blank = read profiles).", default=""),
        Param("p1", "profile id / session id (or 'set').", default=None),
        Param("p2", "profile id (after 'set').", default=None),
        Param("session", "session id to inspect (read).", is_flag=False),
        Param("kind", "runtime-test kind (test).", is_flag=False),
        Param("args", "run-cli: extra argv appended to start (a single string).",
              is_flag=False),
        Param("timeout", "run-cli: per-run time-box in seconds.", is_flag=False),
        Param("go", "run: actually launch (default is a preview).", is_flag=True),
        Param("reduced-isolation", "run: consent to reduced-isolation launch.",
              is_flag=True),
        Param("open", "run --go: open the served URL in your browser (default on a TTY).",
              is_flag=True),
        Param("no-open", "run --go: never auto-open the browser.", is_flag=True),
        Param("profile", "profile set: the profile as a JSON object.", is_flag=False),
        Param("watch", "re-render on the poll loop (read views only).", is_flag=True),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
))
