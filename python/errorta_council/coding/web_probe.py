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
import re
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
# SPEC-30 (S4 fix): a PER-PR probe runs against a PR BRANCH's tree at the PR head,
# as evidence for the reviewer — it is NOT the integrated acceptance gate. It is
# recorded under this distinct task_id so the gate detectors (`_gate_fingerprint`,
# `_gate_has_failure`) can EXCLUDE it: otherwise every red per-PR probe during
# build-up (a module with no input wired yet is legitimately inert) becomes the
# "latest gate run" with score 0, and `gate_not_improving` trips on branch-scoped
# evidence rather than the integrated gate. The master/delivery probe keeps
# `_PROBE_TASK_ID` and still counts, exactly as before.
PR_PROBE_TASK_ID = "web-probe-pr"
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
    timeout_ms: int = 15000, legacy_sweep: bool = False, whitebox: bool = True,
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
    # SPEC-40 escape hatches. Absent flags = the new behaviour (the defaults);
    # `--legacy-sweep` restores the geometry-anchored powers and `--no-whitebox`
    # removes the phase, each reproducing today's trace for that half.
    if legacy_sweep:
        argv.append("--legacy-sweep")
    if not whitebox:
        argv.append("--no-whitebox")
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
# SPEC-37: a project DECLARES a load-bearing mechanic that FORBIDS straight-line
# solutions when its north-star/DoD names a physics mechanic (gravity/physics/…)
# AND asserts a straight shot must fail. Both are required, matched on WORD/PHRASE
# boundaries (not bare substrings — an earlier draft's "well"/"force"/"must matter"
# false-matched a plain CRUD DoD like "handles validation well ... non-trivial" and
# hard-blocked it). The behavioral assertion is scoped precisely to the
# straight-shot-must-fail claim, so a high-power straight shot that sinks is a REAL
# violation (a straight-line solution the DoD forbids), not a false red; when the
# claim is absent the mechanic verdict is advisory (never gates).
_MECHANIC_TERMS = (r"\bgravity\b", r"\bphysics\b", r"\bmomentum\b", r"\borbit\b",
                   r"\btrajector", r"\bgravity well")
# The DoD must specifically claim STRAIGHT-LINE solutions fail — the precise claim
# the oracle tests. Generic "non-trivial"/"cannot be solved" are deliberately NOT
# here: paired with a physics term they over-gate a non-golf physics project ("a
# non-trivial physics sandbox") into a no-hook hard-fail (re-review). The gravity-golf
# DoD ("none solvable by a straight line") still matches on the straight-specific
# phrases below.
_STRAIGHT_FAIL_TERMS = (
    "straight line", "straight-line", "straight shot", "straight-shot",
    "no straight", "solvable by a straight")


def _declares_load_bearing_mechanic(store: Any) -> bool:
    """SPEC-37 north-star signal: does the project declare a physics mechanic AND
    assert that straight-line solutions must fail? Only then is "a straight shot
    sinks => the mechanic is inert" a sound verdict. Word/phrase-boundary matched;
    fully guarded → False (never invents the gate)."""
    try:
        proj = store.get_project()
        text = (str(getattr(proj, "north_star", "") or "") + " "
                + str(getattr(proj, "definition_of_done", "") or "")).lower()
    except Exception:  # noqa: BLE001
        return False
    has_mechanic = any(re.search(p, text) for p in _MECHANIC_TERMS)
    forbids_straight = any(t in text for t in _STRAIGHT_FAIL_TERMS)
    return has_mechanic and forbids_straight


# The exact hook a declared-mechanic game must expose (named in the fail reason so
# the council can build it — the reviewer sees this and the dev adds it).
_HOOK_CONTRACT = (
    "expose window.__probe = {state:()=>({ball:{x,y},hole:{x,y,r},wells:[...],"
    "moving}) with ball/hole in canvas intrinsic-pixel coordinates, "
    "shoot:(dx,dy,power)=>{} where (dx,dy) is a direction the game normalizes and "
    "power is launch speed, tick:(n)=>{} advancing n FIXED DETERMINISTIC steps, "
    "reset:()=>{} returning the ball to the tee, setMechanic:(on)=>{} actually "
    "enabling/disabling the mechanic. state() must return plain {x,y} number copies "
    "— so the probe can verify the mechanic changes outcomes (fire the same shot "
    "with it on vs off)"
    # SPEC-40 (item D): the white-box contract. Deliberately worded as the FAST,
    # UNAMBIGUOUS path rather than a mandate — a game whose mechanic demonstrably
    # works still clears the gate on the differential alone (item E path 3), and
    # making these mandatory would re-create the instrumentation burden this spec
    # exists to retire.
    "; BEST: also expose won:()=>bool (your own win predicate for the current level — "
    "the same one that draws your win banner) and solution:()=>({dx,dy,power}) "
    "returning a shot that clears the level, in the SAME units shoot() takes. That is "
    "the fastest and least ambiguous way to prove the mechanic: the probe fires your "
    "solution with the mechanic ON (must win) and the identical shot with it OFF (must "
    "NOT win), so no power calibration or endpoint heuristic is involved (SPEC-40)")


def _mechanic_verdict(verdict: dict[str, Any], declares_mechanic: bool
                      ) -> tuple[bool, str]:
    """SPEC-37 fold. Returns ``(mechanic_ok, reason)``. Only gates when the project
    DECLARES a load-bearing mechanic that forbids straight-line solutions:
    * mechanic_probe field ABSENT -> advisory (the probe errored / an older script;
      cannot-verify, never a false red — do not append a misleading hook reason);
    * no hook -> fail (the miss must not be a free pass; names the contract);
    * hook present but the phase could not run it (state lacks ball/hole, a no-op
      shoot, a missing reset) -> fail as UNUSABLE (a stub hook must not buy a pass);
    * a straight shot sinks -> fail (the mechanic is inert / a straight-line
      solution the DoD forbids);
    * a straight shot misses at every swept power -> ok."""
    if not declares_mechanic:
        return True, ""
    if "mechanic_probe" not in verdict:
        return True, ""  # phase did not run (probe error) -> advisory, never a red
    mp = verdict.get("mechanic_probe")
    if not isinstance(mp, dict):
        mp = {}
    if not mp.get("has_hook"):
        return False, ("declares a straight-shots-must-fail mechanic but exposes no "
                       "scriptable state hook — " + _HOOK_CONTRACT + " (SPEC-37)")
    if not mp.get("ran"):
        reason = str(mp.get("reason") or "")
        # A transient cannot-verify (timeout / thrown eval) is advisory, not a red —
        # the probe re-runs on the next merge (SPEC-35-style). Only a STRUCTURAL
        # problem (no ball/hole, no-op shoot, non-restoring reset, nondeterminism)
        # is a hard unusable fail.
        # SPEC-40 adds "exhausted its simulation budget" to this set: like a timeout
        # it is a CANNOT-VERIFY, not a structural defect in the hook, so it must stay
        # advisory. Treating a budget exhaustion as an unusable-hook red would blame
        # the council for the probe running out of ticks.
        if ("timed out" in reason or "threw" in reason
                or "cannot verify" in reason):
            return True, ""
        return False, ("exposes a window.__probe hook but it is unusable — "
                       + (reason or "state() lacks ball/hole, shoot() is a no-op, "
                          "or reset() is missing") + "; " + _HOOK_CONTRACT
                       + " (SPEC-37)")
    if mp.get("mechanic_matters") is False:
        return False, ("the mechanic has NO effect — a straight shot at the hole "
                       "behaves identically with the mechanic on vs off. Either it "
                       "is inert, or setMechanic(false) does not actually disable it; "
                       "the DoD's straight-shots-must-fail claim is unmet (SPEC-37)")
    return True, ""


def _whitebox_verdict(verdict: dict[str, Any]) -> tuple[str, str]:
    """SPEC-40 (item D) — classify the white-box phase as
    ``("green" | "red" | "absent", reason)``.

    ``absent`` covers every cannot-verify shape — the ``whitebox`` key is missing (an
    older probe script), the game exposes no ``solution()``/``won()`` contract, or the
    phase could not run (a throwing ``won()``, a malformed ``solution()``). All of them
    fall through to item E path 3/4 rather than becoming a red: the new verbs are the
    FAST path, never a toll gate, so their absence must never fail a project.

    ``green``/``red`` mirror the phase's own verdict, which the probe script composes
    from the three arms. Fully guarded — a malformed payload reads as ``absent``.
    """
    wb = verdict.get("whitebox")
    if not isinstance(wb, dict):
        return "absent", ""
    if not wb.get("has_contract") or not wb.get("ran"):
        return "absent", str(wb.get("reason") or "")
    v = str(wb.get("verdict") or "")
    if v not in ("green", "red"):
        return "absent", str(wb.get("reason") or "")
    return v, str(wb.get("reason") or "")


def _mechanic_confident(verdict: dict[str, Any]) -> bool:
    """SPEC-40 (item B) — did the differential produce a CONFIDENT verdict?

    Only a confident INERT verdict may hard-block delivery (item E path 3). An older
    probe script that emits no ``confident`` field reads as NOT confident, so a stale
    script degrades to advisory rather than to a block it cannot justify."""
    mp = verdict.get("mechanic_probe")
    if not isinstance(mp, dict):
        return False
    return bool(mp.get("confident"))


def _verdict_to_result(verdict: dict[str, Any],
                       declares_mechanic: bool = False,
                       *, mechanic_advisory: bool = True) -> TestRunResult:
    """Fold the node probe's JSON into a synthetic ``web:probe`` ``TestRunResult``
    — ``passed`` only when console-clean AND non-black AND (SPEC-37) the declared
    mechanic has effect. The reason + console errors go into ``stderr_preview``
    VERBATIM so ``gate_state.latest_gate_text`` surfaces the real failing line to
    the reviewer, not a paraphrase of it."""
    non_black = bool(verdict.get("non_black"))
    console_errors = [str(e) for e in (verdict.get("console_errors") or [])]
    # SPEC-30 (S1): an artifact that renders but IGNORES input (the empty
    # gravity-golf gradient) or CRASHES on input (a wrong integration contract) is
    # not a working deliverable. `interaction_changed is False` = drove a gesture,
    # nothing moved (inert); a crash surfaces as a console error above. `None` =
    # could not interact -> fail-open, the passive verdict stands.
    interaction_changed = verdict.get("interaction_changed")
    mechanic_ok, mechanic_reason = _mechanic_verdict(verdict, declares_mechanic)
    # SPEC-40 (item B): the ANCHORED verdict tracks LIVENESS only — renders non-black,
    # console-clean, responds to input. The mechanic differential is deliberately NOT
    # folded in here, because `anchors.reconcile` keys on this exact boolean: a
    # marginal differential that flips green<->red on a sub-threshold tweak was setting
    # `anchor_regressed`, which fed `revise_livelock` and actively PUNISHED the tuning
    # that flipped it (gravity-golf-4, decisions #197/#231). The mechanic verdict still
    # travels in `stderr_preview` below (so `gate_state.latest_gate_text` shows the
    # reviewer the real line) and on the PR record, and the done-gate composes it
    # there — see `completion.mechanic_gate_status`.
    passed = (bool(verdict.get("ok")) and non_black and not console_errors
              and interaction_changed is not False
              and (mechanic_ok or mechanic_advisory))
    reason = str(verdict.get("reason") or "")
    if not mechanic_ok and mechanic_reason:
        reason = (reason + "; " if reason else "") + mechanic_reason
    wb_status, wb_reason = _whitebox_verdict(verdict)
    if wb_status == "red" and wb_reason:
        reason = (reason + "; " if reason else "") + wb_reason
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


def _probe_verdict_fields(verdict: dict[str, Any], *, head: str,
                          declares_mechanic: bool = False) -> dict[str, Any]:
    """The additive PR-record fields (Item 3): console-error count, non-black,
    screenshot ref, pass/fail, and the head the probe ran against. Absent →
    falsy, so a probe-less PR is byte-identical to today. ``probe_passed`` folds the
    SPEC-37 mechanic verdict so the PR record matches the recorded run."""
    console_errors = [str(e) for e in (verdict.get("console_errors") or [])]
    non_black = bool(verdict.get("non_black"))
    interaction_changed = verdict.get("interaction_changed")
    mechanic_ok, _ = _mechanic_verdict(verdict, declares_mechanic)
    passed = (bool(verdict.get("ok")) and non_black and not console_errors
              and interaction_changed is not False and mechanic_ok)
    mp = verdict.get("mechanic_probe") if isinstance(verdict.get("mechanic_probe"), dict) else {}
    wb_status, wb_reason = _whitebox_verdict(verdict)
    return {
        "probe_passed": passed,
        "probe_non_black": non_black,
        "probe_console_errors": len(console_errors),
        "probe_interacted": interaction_changed is not None,
        "probe_interaction_changed": bool(interaction_changed),
        "probe_mechanic_ok": mechanic_ok,
        "probe_mechanic_has_hook": bool(mp.get("has_hook")),
        "probe_screenshot": str(verdict.get("screenshot") or ""),
        "probe_reason": str(verdict.get("reason") or "")[:500],
        "probe_head": str(head),
        # SPEC-40 (item C): the components that GATE delivery, stamped on the PR record
        # so the reviewer reviews against the real bar. The feedback-locality bug this
        # closes: the per-PR probe was green while the master differential that gates
        # delivery stayed red, so the council optimized the visible-but-wrong signal
        # and merged 22 green PRs that never shipped.
        "probe_whitebox": wb_status,
        "probe_whitebox_reason": wb_reason[:500],
        "probe_mechanic_confident": _mechanic_confident(verdict),
        "probe_mechanic_matters": mp.get("mechanic_matters"),
    }


# The run_state key holding the newest MASTER-arm mechanic evidence. Additive, no
# migration — the same discipline `anchors.py` uses for `test_anchors`.
_EVIDENCE_KEY = "probe_mechanic_evidence"


def record_mechanic_evidence(store: Any, *, head: str,
                             verdict: dict[str, Any],
                             declares: bool = False) -> None:
    """SPEC-40 (item E) — persist the structured mechanic evidence the done-gate reads.

    Written only for the MASTER arm, bound to the head it was measured at, so
    ``completion.mechanic_gate_status`` can refuse evidence that describes a different
    tree. Best-effort and never raises: a write failure degrades to "no evidence",
    which the gate treats as advisory (it must never INVENT a block).

    ``declares`` is the project's own straight-shots-must-fail signal and MUST be
    persisted. An earlier revision hardcoded ``True`` here, which made every ordinary
    web project — a CRUD app, a dashboard, anything with no ``window.__probe`` — look
    like a declared-mechanic game with a missing hook. That is item E path 3b, so the
    done-gate refused those projects and told them to expose a golf hook. The gate now
    requires this flag, and the recorder no longer speaks for a project that never
    made the claim."""
    wb_status, wb_reason = _whitebox_verdict(verdict)
    mp = verdict.get("mechanic_probe") if isinstance(verdict.get("mechanic_probe"), dict) else {}
    mechanic_ok, mechanic_reason = _mechanic_verdict(verdict, bool(declares))
    payload = {
        "head": str(head or ""),
        "declares": bool(declares),
        "whitebox": wb_status,
        "whitebox_reason": wb_reason[:500],
        "mechanic_matters": mp.get("mechanic_matters"),
        "confident": _mechanic_confident(verdict),
        # SPEC-37's verdict, carried through so the gate can distinguish the two very
        # different ways it can be False. "The differential says inert" is a MEASURED
        # claim that may be marginal, so it needs `confident` to block (the golf-4
        # protection). "There is no usable hook at all" is not a measurement — it is an
        # absence of evidence about a project that DECLARED the claim, and SPEC-37
        # blocked it. Collapsing the two would let gravity-golf-2, which exposes no
        # __probe at all, ship on `advisory`.
        "has_hook": bool(mp.get("has_hook")),
        "mechanic_ok": mechanic_ok,
        "mechanic_reason": mechanic_reason[:500],
        "reason": str(verdict.get("reason") or "")[:500],
    }
    try:
        store.set_run_state(**{_EVIDENCE_KEY: payload})
    except Exception:  # noqa: BLE001 — evidence is best-effort, never fails a turn
        pass


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
    serve_root: Optional[Any] = None,
    pr_scoped: bool = False,
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
    guarded: any failure returns ``None`` and never raises into the loop.

    SPEC-30 (S4): ``serve_root`` overrides the tree the runtime serves. Default
    ``None`` serves ``workspace.root()`` (master, the post-merge / delivery arm).
    A PER-PR pre-merge probe passes the PR branch's worktree here so the reviewer
    gets execution evidence on the code it is about to approve, bound to the PR's
    OWN head — which is why ``_attach_verdict_to_prs`` can stamp that PR (the head
    matches). The post-merge arm never matched a PR head, so evidence never reached
    the reviewer; this is the seam that closes that gap."""
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
            workspace_root=(serve_root if serve_root is not None
                            else workspace.root()), work_root=store.dir)
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
        # SPEC-40: read the four escape hatches off the policy. Fully guarded — an
        # unreadable policy means "all on" (the defaults), never a crash.
        adaptive, advisory, whitebox_on, pr_gating = _probe_knobs(store)
        try:
            verdict = runner(url, int(frames), screenshot_path=screenshot,
                             timeout_ms=int(_READY_TIMEOUT_S * 1000),
                             legacy_sweep=not adaptive, whitebox=whitebox_on)
        except TypeError:
            # An injected test seam predating SPEC-40 accepts neither new keyword.
            # Fall back to the old call shape rather than losing the probe entirely.
            try:
                verdict = runner(url, int(frames), screenshot_path=screenshot,
                                 timeout_ms=int(_READY_TIMEOUT_S * 1000))
            except Exception:  # noqa: BLE001
                verdict = None
        except Exception:  # noqa: BLE001 — the seam must never raise into the loop
            verdict = None
        if not isinstance(verdict, dict):
            return None  # probe could not run: fail-open (cannot-verify)

        # SPEC-37/SPEC-40 (item C). Two things the original code conflated:
        #
        # `declares`     — does the project DECLARE a straight-shots-must-fail
        #                  mechanic? Drives what we COMPUTE and STAMP, on BOTH arms.
        # `gates_hard`   — may that verdict FAIL this probe? Still arm-scoped, because
        #                  a partial-module PR mid-build legitimately has no
        #                  whole-game hook yet and hard-redding it would red every
        #                  in-progress PR (the SPEC-37 guard, preserved).
        #
        # Splitting them closes the feedback-locality bug: the per-PR probe the
        # reviewer saw was green while the master differential that gates delivery
        # stayed red, so the council optimized the visible-but-wrong signal, merged 22
        # green PRs, and delivery never cleared. Now the per-PR arm surfaces the same
        # components, and hard-gates once the contract is actually present on that head.
        declares = _declares_load_bearing_mechanic(store)
        contract_present = _whitebox_verdict(verdict)[0] != "absent"
        if pr_gating:
            gates_hard = declares and (not pr_scoped or contract_present)
        else:
            gates_hard = declares and not pr_scoped
        result = _verdict_to_result(verdict, gates_hard,
                                    mechanic_advisory=advisory)
        probe_session = TestRunSession(
            command_ids=[PROBE_COMMAND_ID], results=[result], unknown_ids=[],
            passed=bool(result.passed), sandbox="")
        try:
            run = store.record_test_run(
                probe_session,
                task_id=(PR_PROBE_TASK_ID if pr_scoped else _PROBE_TASK_ID),
                head=head)
        except Exception:  # noqa: BLE001 — recording failed -> no evidence
            return None
        try:
            _attach_verdict_to_prs(
                store,
                # SPEC-40 (item C): stamp with `declares`, not `gates_hard`, so BOTH
                # arms carry the full verdict on the record even when the per-PR arm
                # declines to fail on it.
                _probe_verdict_fields(verdict, head=head,
                                      declares_mechanic=declares),
                head=head)
        except Exception:  # noqa: BLE001
            pass
        # SPEC-40 (item E): persist the structured evidence the done-gate reads —
        # MASTER arm only. A per-PR head is a branch tip, not the tree we would call
        # done, so letting it write here would let a branch decide delivery.
        if not pr_scoped:
            record_mechanic_evidence(store, head=head, verdict=verdict,
                                     declares=declares)
        return run
    finally:
        _stop_quiet(mgr, profile.profile_id)


def _probe_knobs(store: Any) -> tuple[bool, bool, bool, bool]:
    """SPEC-40 — read the four escape hatches as
    ``(adaptive_sweep, mechanic_advisory, whitebox, pr_gating)``.

    Fully guarded: an unreadable policy returns all-ON (the dataclass defaults) rather
    than raising, matching this module's never-fail-the-loop discipline."""
    try:
        from .autonomy import load_policy
        p = load_policy(store)
    except Exception:  # noqa: BLE001
        return True, True, True, True
    return (bool(getattr(p, "probe_adaptive_sweep", True)),
            bool(getattr(p, "probe_mechanic_advisory", True)),
            bool(getattr(p, "probe_whitebox", True)),
            bool(getattr(p, "probe_pr_gating", True)))


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
    "record_mechanic_evidence",
    "PROBE_COMMAND_ID",
    "NodeRunner",
]
