"""GL01 (Item 1) — the unconditional default web probe (the black-canvas oracle).

The committed batch (Spec 12/13) grounds the loop in *does-it-run*: it executes
the command registry on the merged tree and catches a crash-on-start. It does NOT
ground the loop in *did-it-render*. A buildless web project that authored no test
has an EMPTY registry, so ``_run_gate`` returns ``None`` and executes nothing; a
runtime that starts cleanly, binds its port, serves HTTP 200, logs nothing, and
renders a **0x0 black canvas** ships ``done``. That is the gravity-golf defect.

This module closes it with a liveness assertion that runs **regardless of the
registry**: stand the served runtime up (the exact ``managed_local`` / ``static``
``python -m http.server`` profile Spec 13 bootstraps for a buildless web target),
resolve its loopback URL, drive a headless Chromium probe (``scripts/web-probe.mjs``
via Playwright), and record a ``web:probe`` runtime-test bound to the merged head —
``passed`` only when the console is clean AND the first canvas (or the viewport) is
non-black after N rendered frames.

Discipline (mirrors ``gate_bootstrap.py`` / ``gate_state.py``): NO ``runner``
import (``runner`` imports ``.topology`` / ``.schemas`` at import time, so importing
it here would be circular). Fully **fail-open**: any spawn / capture /
headless-unavailable / parse failure degrades to **no probe evidence** — never a
red gate, never a failed turn — the same cannot-verify posture ``launch_probe``
uses. The browser invocation is behind an injectable ``node_runner`` seam so the
coding suite (which runs with no Playwright browser installed) can script the
probe result deterministically.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .testing import TestRunResult, TestRunSession

# A stable synthetic task_id + command_id for the probe's recorded runs. The
# command_id doubles as the ANCHOR KEY (see ``anchors.py``) — one anchor per web
# project, promoted on a green frame, broken on a later black one.
_PROBE_TASK_ID = "web-probe"
PROBE_COMMAND_ID = "web:probe"

# The web-profile kinds this liveness oracle applies to. A non-web project (a
# CLI, a library, a native desktop app) has no such profile → the probe is
# skipped, vacuously clean (as the launch arm skips a non-runnable project).
_WEB_KINDS = frozenset({"static", "web"})

# How long to wait for the served runtime to answer before probing, and the hard
# cap on the node probe itself. Bounded so a wedged server never stalls the loop.
_READY_TIMEOUT_S = 20.0
_READY_POLL_S = 0.2
_NODE_TIMEOUT_S = 45.0

# The injectable browser seam. Given a URL + rendered-frame count, return the
# parsed probe JSON, or ``None`` when the probe could not run (the fail-open
# signal). Never raises into the caller.
NodeRunner = Callable[..., Optional[dict[str, Any]]]


# --------------------------------------------------------------------------- #
# Profile selection + URL resolution
# --------------------------------------------------------------------------- #
def _web_profile(rstore: Any) -> Any:
    """The runnable web/static ``managed_local`` runtime profile with an HTTP
    health URL, or ``None``. This is exactly the profile Spec 13's
    ``_detect_static`` registers for a buildless ``index.html`` target: a
    ``managed_local`` runtime whose ``start`` is ``python -m http.server {port}``
    and whose ``health`` is ``{"type":"http","url":"http://127.0.0.1:{port}"}``.
    """
    try:
        profiles = rstore.list_profiles()
    except Exception:  # noqa: BLE001 — can't enumerate -> no web profile
        return None
    for p in profiles:
        if (getattr(p, "kind", "") in _WEB_KINDS
                and getattr(p, "runtime_mode", "") == "managed_local"
                and getattr(p, "start", None)
                and str((getattr(p, "health", {}) or {}).get("type")) == "http"):
            return p
    return None


def _served_url(session: Any, profile: Any) -> str:
    """The concrete loopback URL the started runtime serves — the health URL with
    the session's allocated port substituted. Returns ``""`` when the port can't
    be resolved (the caller then skips the probe, fail-open)."""
    ports = list(getattr(session, "allocated_ports", []) or [])
    if not ports:
        return ""
    port = ports[0]
    raw = str((getattr(profile, "health", {}) or {}).get("url", "")).strip()
    if not raw:
        raw = "http://127.0.0.1:{port}"
    url = raw.replace("{port}", str(port))
    return url if url.endswith("/") else url + "/"


def _wait_reachable(url: str, *, should_cancel: Optional[Callable[[], bool]]) -> bool:
    """Poll the URL until it answers (any HTTP response), bounded. A dev server
    binds its port asynchronously, so the probe must not race the first paint."""
    from errorta_tools.runner.preview import probe_http

    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if should_cancel is not None:
            try:
                if should_cancel():
                    return False
            except Exception:  # noqa: BLE001 — a raising cancel-probe fails closed
                return False
        try:
            ok, detail = probe_http(url, 2.0)
        except Exception:  # noqa: BLE001 — our probe erroring is not the app's fault
            ok, detail = False, ""
        if ok or str(detail).isdigit():
            return True
        time.sleep(_READY_POLL_S)
    return False


# --------------------------------------------------------------------------- #
# The default node-probe runner (the injectable seam's production body)
# --------------------------------------------------------------------------- #
def _probe_script_path() -> Optional[Path]:
    """Locate ``scripts/web-probe.mjs`` at the repo root. This is the errorta
    engine's OWN trusted tool (NOT generated code — the generated app is the
    sandboxed server it points at); it resolves Playwright from errorta's
    ``node_modules``. Absent (a packaged CLI with no repo tree) → ``None`` and the
    probe is skipped, fail-open."""
    try:
        root = Path(__file__).resolve().parents[3]  # coding→council→python→repo
    except Exception:  # noqa: BLE001
        return None
    script = root / "scripts" / "web-probe.mjs"
    return script if script.exists() else None


def _default_node_runner(
    url: str, frames: int, *, screenshot_path: str = "",
    timeout_ms: int = 15000,
) -> Optional[dict[str, Any]]:
    """Shell out to Node + Playwright to run the black-canvas oracle. Returns the
    parsed JSON verdict, or ``None`` when the probe could not run (node missing,
    Playwright/Chromium unavailable, timeout, or unparseable output) — the
    fail-open signal that records NO evidence. Never raises."""
    script = _probe_script_path()
    if script is None or shutil.which("node") is None:
        return None
    argv = ["node", str(script), url, str(int(frames)),
            "--timeout-ms", str(int(timeout_ms))]
    if screenshot_path:
        argv += ["--screenshot", screenshot_path]
    try:
        proc = subprocess.run(  # noqa: S603 — trusted engine script, argv-only
            argv, cwd=str(script.parent.parent), capture_output=True,
            text=True, timeout=_NODE_TIMEOUT_S, check=False)
    except Exception:  # noqa: BLE001 — spawn/timeout failure -> no evidence
        return None
    # The script prints exactly one JSON line to stdout; take the last JSON line
    # so any stray warning above it is ignored.
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            return obj
    return None


# --------------------------------------------------------------------------- #
# The probe verdict -> a recorded runtime-test session
# --------------------------------------------------------------------------- #
def _verdict_to_result(verdict: dict[str, Any]) -> TestRunResult:
    """Fold the node probe's JSON into a synthetic ``web:probe`` ``TestRunResult``
    — ``passed`` only when console-clean AND non-black. The reason + console
    errors go into ``stderr_preview`` VERBATIM so ``gate_state.latest_gate_text``
    surfaces the real "frame is uniformly black" line to the reviewer, not a
    paraphrase of it."""
    non_black = bool(verdict.get("non_black"))
    console_errors = [str(e) for e in (verdict.get("console_errors") or [])]
    passed = bool(verdict.get("ok")) and non_black and not console_errors
    reason = str(verdict.get("reason") or "")
    parts = [f"non_black={non_black}", f"console_errors={len(console_errors)}"]
    if reason:
        parts.append(reason)
    if console_errors:
        parts.append("console:\n" + "\n".join(console_errors[:20]))
    detail = "; ".join(parts[:2]) + ("\n" + "\n".join(parts[2:]) if parts[2:] else "")
    return TestRunResult(
        command_id=PROBE_COMMAND_ID, argv_sha256="",
        status="completed" if passed else "failed",
        exit_code=0 if passed else 1, passed=passed, duration_ms=0,
        stdout_sha256="", stdout_preview="", stderr_preview=detail[:4000],
        reason="" if passed else (reason or "web probe failed"))


def _probe_verdict_fields(verdict: dict[str, Any], *, head: str) -> dict[str, Any]:
    """The additive PR-record fields (Item 3): console-error count, non-black,
    screenshot ref, pass/fail, and the head the probe ran against. Absent →
    falsy, so a probe-less PR is byte-identical to today."""
    console_errors = [str(e) for e in (verdict.get("console_errors") or [])]
    non_black = bool(verdict.get("non_black"))
    passed = bool(verdict.get("ok")) and non_black and not console_errors
    return {
        "probe_passed": passed,
        "probe_non_black": non_black,
        "probe_console_errors": len(console_errors),
        "probe_screenshot": str(verdict.get("screenshot") or ""),
        "probe_reason": str(verdict.get("reason") or "")[:500],
        "probe_head": str(head),
    }


def _attach_verdict_to_prs(store: Any, fields: dict[str, Any], *, head: str) -> None:
    """Item 3: stamp the probe verdict onto every non-terminal PR whose head is
    the probed head, so ``errorta prs`` and the reviewer see execution evidence on
    the record. Best-effort; a PR bookkeeping failure never fails the probe."""
    live = {"open", "changes_requested", "mergeable"}
    try:
        prs = store.list_prs()
    except Exception:  # noqa: BLE001
        return
    for pr in prs:
        if pr.get("status") in live and str(pr.get("head") or "") == str(head):
            try:
                store.update_pr(pr["pr_id"], **fields)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Public entry: run the probe and record it
# --------------------------------------------------------------------------- #
def run_and_record(
    store: Any, workspace: Any, *, head: str, frames: int = 30,
    should_cancel: Optional[Callable[[], bool]] = None,
    node_runner: Optional[NodeRunner] = None,
) -> Optional[dict[str, Any]]:
    """Stand the web runtime up, drive the headless probe, and record a
    ``web:probe`` runtime-test bound to ``head``. Returns the recorded test-run
    dict (for the anchor reconcile), or ``None`` when there is no evidence to
    record:

    * a **non-web project** (no static/web profile) → ``None`` (skipped);
    * the runtime **would not serve** in the ready window → ``None`` (fail-open);
    * the probe **could not run** (node/Playwright/Chromium unavailable, timeout,
      unparseable) → ``None`` (fail-open, the ``cannot_verify`` posture).

    A probe that **did** run records a session whose ``passed`` is the liveness
    verdict — a black canvas or a console error is a RED ``web:probe`` run. Fully
    guarded: any failure returns ``None`` and never raises into the loop."""
    runner = node_runner or _default_node_runner
    try:
        from .runtime import RuntimeProfileStore
        rstore = RuntimeProfileStore.for_ledger(store)
    except Exception:  # noqa: BLE001 — can't enumerate -> skip (non-web posture)
        return None
    profile = _web_profile(rstore)
    if profile is None:
        return None  # non-web project: skipped, vacuously clean

    try:
        from .runtime_process import RuntimeProcessManager
        mgr = RuntimeProcessManager(
            project_id=store.project_id, rstore=rstore,
            workspace_root=workspace.root(), work_root=store.dir)
    except Exception:  # noqa: BLE001 — can't build the launch machinery -> no evidence
        return None

    session = None
    try:
        session = mgr.start(profile.profile_id)
    except Exception:  # noqa: BLE001 — a spawn failure is a verify error, not a crash
        _stop_quiet(mgr, profile.profile_id)
        return None
    if session is None or getattr(session, "state", "") in ("crashed", "stopped"):
        _stop_quiet(mgr, profile.profile_id)
        return None

    try:
        url = _served_url(session, profile)
        if not url or not _wait_reachable(url, should_cancel=should_cancel):
            return None  # never served in the window: fail-open, no evidence
        screenshot = _screenshot_path(store, head)
        try:
            verdict = runner(url, int(frames), screenshot_path=screenshot,
                             timeout_ms=int(_READY_TIMEOUT_S * 1000))
        except Exception:  # noqa: BLE001 — the seam must never raise into the loop
            verdict = None
        if not isinstance(verdict, dict):
            return None  # probe could not run: fail-open (cannot-verify)

        result = _verdict_to_result(verdict)
        probe_session = TestRunSession(
            command_ids=[PROBE_COMMAND_ID], results=[result], unknown_ids=[],
            passed=bool(result.passed), sandbox="")
        try:
            run = store.record_test_run(probe_session, task_id=_PROBE_TASK_ID,
                                        head=head)
        except Exception:  # noqa: BLE001 — recording failed -> no evidence
            return None
        try:
            _attach_verdict_to_prs(
                store, _probe_verdict_fields(verdict, head=head), head=head)
        except Exception:  # noqa: BLE001
            pass
        return run
    finally:
        _stop_quiet(mgr, profile.profile_id)


def _screenshot_path(store: Any, head: str) -> str:
    """A per-head screenshot path under the ledger dir (best-effort; the probe
    writes here for the PR-record ``probe_screenshot`` ref). Empty on failure."""
    try:
        d = Path(store.dir) / "web-probe"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"probe-{str(head)[:12] or 'head'}.png")
    except Exception:  # noqa: BLE001
        return ""


def _stop_quiet(mgr: Any, profile_id: str) -> None:
    try:
        mgr.stop(profile_id)
    except Exception:  # noqa: BLE001 — teardown is best-effort; never raises
        pass


def has_web_profile(store: Any) -> bool:
    """Whether this project has a runnable web/static profile the probe applies
    to. Used by the runner-side arm to skip cleanly on a non-web project."""
    try:
        from .runtime import RuntimeProfileStore
        return _web_profile(RuntimeProfileStore.for_ledger(store)) is not None
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "run_and_record",
    "has_web_profile",
    "PROBE_COMMAND_ID",
    "NodeRunner",
]
