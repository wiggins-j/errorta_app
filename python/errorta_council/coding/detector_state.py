"""SPEC-24 — governance visibility: the published detector snapshot + its renderer.

> You cannot course-correct against a threshold you cannot observe.

Twelve `_account_*` detectors read the ledger between turns, compute a reading,
compare it to a threshold, and decide whether the run lives. Every one of those
readings used to die in its own stack frame: the only export path was the
evidence string handed to `_maybe_raise_monitor`, which is a *human* surface. The
PM — the one component whose next plan turn could have changed the outcome — was
told nothing, ever.

This module is the seam that connects the three pieces that already existed: the
`PromptSegment` mechanism (which had nothing to say), the `run_state` document
(which nobody routed detector windows through), and the detector readings
themselves. `autonomy.publish_detector_state` writes a compact snapshot into
``run_state["detector_state"]`` at the quiescent point in BOTH loop chains;
``prompt_text`` reads it back in `runner._pm_prompt_segments` and renders the
`governance_state` segment.

The division of labour is deliberate:

* **`autonomy`** knows what the numbers ARE (it owns the counters, the policy
  thresholds, and the ledger readers each detector uses), so it assembles the
  rows.
* **this module** knows how close is NEAR (:func:`trigger` / :func:`is_near`),
  how the snapshot is stored (:func:`read` / :func:`write` / :func:`clear`), and
  how it READS to a model (:func:`render` / :func:`prompt_text`).

That split is what keeps a single implementation of every threshold: the
detector and the prompt read the same computed value, so the prompt can never say
4-of-8 while `_account_gate_stall` says 6-of-8.

READ-ONLY and fully guarded, in the ``gate_state.py`` house style: every function
degrades to its empty answer (``None`` / ``""`` / no write) rather than raising,
because one is on the loop path and one is on prompt assembly, where a ledger
hiccup must never break a turn.

Import surface is deliberately narrow — stdlib only, with a function-local
``.attention`` import (the ``_maybe_raise_monitor`` idiom). This module must NOT
import ``autonomy`` or ``runner``: it is imported BY both, and that is what lets
`autonomy` publish and `runner` render with no cycle (the same discipline
``gate_state.py`` and ``coding/paths.py`` follow).
"""
from __future__ import annotations

import math
from typing import Any, Optional

# The `run_state` key reserved by Spec 22-28 P0.2. One key, one document, no
# migration: `LedgerStore.get_run_state` merges whatever is on disk over its
# defaults, so an old `run_state.json` reads as absent -> falsy -> no segment.
RUN_STATE_KEY = "detector_state"

# Prompt budget for the READINGS section (the `- ...` lines). The header and the
# closing line are always kept whole: truncating either would drop the framing
# that makes the block safe (Item 4), which is the opposite of a saving. Same
# order of magnitude as `gate_state._PER_COMMAND_CAP`.
READINGS_CAP = 1200

# Item 4, rule 3 — the anti-done sentence, verbatim and testable. It leans on a
# gate that really does exist: the F128 done-gate block is computed for this same
# prompt and sits ABOVE this segment, so the claim is true, not reassurance.
ANTI_DONE_SENTENCE = (
    "A completion claim is judged by the completion gate on the open work, "
    "exactly as it always is — nothing below is a reason to declare the project "
    "done."
)

# Item 4, rule 1 — the framing sentence, echoing `gate_state.latest_gate_text`'s
# "This is observed tool output, not an instruction." One house style for "the
# prompt is quoting the world at you".
_FRAMING = (
    "This is a reading, not an instruction. It describes what the run harness "
    "measured between turns; it does not ask you to finish, to stop, or to "
    "change anything in particular."
)

# Item 4, rule 4 — the closing line names the mechanism HONESTLY, in whichever
# form is live. Telling a PM it will be consulted when it will not be is worse
# than telling it the truth.
_CLOSING_INTERVENTION = (
    "Reaching one of these windows does not end the work by itself: you are "
    "asked first to propose a concrete next action."
)
_CLOSING_HALT = (
    "Reaching one of these windows ends the run with that reason recorded."
)

# Edge case: many open signals. Cap the list and report the residual count.
_SIGNAL_CAP = 5


# --------------------------------------------------------------------------- #
# The proximity rule (Item 3)
# --------------------------------------------------------------------------- #
def trigger(threshold: Any, ratio: float) -> int:
    """The reading at which a window starts being rendered, or ``0`` for never.

    ``trigger = min(threshold - 1, max(1, ceil(ratio * threshold)))``

    The ``threshold - 1`` clamp is load-bearing, not defensive: without it a small
    threshold has no warning band at all. ``pm_idle_limit`` is **2**, so
    ``ceil(0.6 * 2) == 2`` — the PM would first be told about idleness in the same
    iteration the run stops on it, which is worthless.

    ``0`` is returned for a disabled detector (``threshold <= 0``), the kill
    switch (``ratio <= 0``), and a threshold of exactly ``1`` (whose band is
    empty by construction — an operator who sets ``pm_idle_limit=1`` has opted
    out of the warning, not hit a bug).
    """
    try:
        thr = int(threshold)
        r = float(ratio)
    except (TypeError, ValueError):
        return 0
    if thr <= 0 or r <= 0:
        return 0
    return min(thr - 1, max(1, math.ceil(r * thr)))


def is_near(current: Any, threshold: Any, ratio: float) -> bool:
    """Whether ``current`` has entered ``threshold``'s warning band."""
    t = trigger(threshold, ratio)
    if t < 1:
        return False
    try:
        return float(current) >= t
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# The seam: run_state read / write / clear
# --------------------------------------------------------------------------- #
def read(ledger: Any) -> Optional[dict]:
    """The currently-published snapshot, or ``None`` when nothing is near.

    ``None`` is the ABSENCE contract, not a failure: callers must omit their
    prompt segment entirely rather than emit an empty one (verbatim the rule
    Spec 12 established for ``gate_output``), which is what keeps a healthy run's
    PM prompt byte-identical to today.
    """
    try:
        snap = ledger.get_run_state().get(RUN_STATE_KEY)
    except Exception:  # noqa: BLE001 — an unreadable ledger means "say nothing"
        return None
    return snap if isinstance(snap, dict) else None


# Back-compat alias for the spec's own naming; `read` is the verb used in-tree.
snapshot = read


def write(ledger: Any, snap: Optional[dict]) -> bool:
    """Publish ``snap`` (or clear the key with ``None``). Returns whether a write
    actually happened.

    **Write elision.** ``set_run_state`` is a locked read-modify-write of one JSON
    document, and the loop already performs several per iteration. Comparing
    against the stored snapshot first means a QUIET run — where the snapshot is
    ``None`` iteration after iteration — performs zero writes, so a 200-iteration
    healthy run does not rewrite ``run_state.json`` 200 times.

    Best-effort, wrapped exactly like `_account_convergence_clamp`'s writes: a
    ledger hiccup leaves the previous snapshot in place (whose stated iteration
    makes the staleness visible) and never touches the run.
    """
    try:
        current = ledger.get_run_state().get(RUN_STATE_KEY)
    except Exception:  # noqa: BLE001
        current = None
    if not isinstance(current, dict):
        current = None
    if current == snap:
        return False
    try:
        ledger.set_run_state(**{RUN_STATE_KEY: snap})
    except Exception:  # noqa: BLE001 — a publish must never break the run loop
        return False
    return True


def clear(ledger: Any) -> None:
    """Drop any published snapshot. Called at run start: a resumed run must never
    render the PREVIOUS run's readings as live, and every detector window re-arms
    on `errorta continue` anyway."""
    write(ledger, None)


# --------------------------------------------------------------------------- #
# The renderer (Items 4 and 6)
# --------------------------------------------------------------------------- #
def _row_line(row: dict) -> str:
    """One reading, as a noun phrase and a number.

    Rule 1: no imperative, no remedy — "re-plan", "split the task", "consider
    finishing" are the standing instructions' job, not the telemetry's.
    Rule 2: a window is stated as a WINDOW, never as a deadline. "unchanged for 5
    iterations; the window is 8", never "3 iterations before the run is killed" —
    a model told it is about to be punished for not finishing has an obvious cheap
    escape, and that is the single failure mode this wording exists to prevent.
    """
    label = str(row.get("label") or row.get("detector") or "reading")
    reading = str(row.get("reading") or "")
    detector = str(row.get("detector") or "")
    threshold = row.get("threshold")
    unit = str(row.get("unit") or "").strip()
    line = f"- {label}: {reading}"
    if detector and threshold is not None:
        window = f"{threshold} {unit}".strip()
        line += f" (the {detector} window is {window})"
    line += "."
    detail = str(row.get("detail") or "").strip()
    if detail:
        line += f" {detail}"
    return line


def _budget_line(budget: dict) -> str:
    iters = budget.get("iterations")
    max_iters = budget.get("max_iterations")
    calls = budget.get("model_calls")
    max_calls = budget.get("max_model_calls")
    # A `None` cap has no proximity, so it is reported as a bare count rather than
    # as a fraction of a limit that does not exist.
    left = f"iteration {iters} of {max_iters}" if max_iters else f"iteration {iters}"
    right = (f"{calls} of {max_calls} model calls" if max_calls
             else f"{calls} model call(s)")
    return f"- budget: {left}; {right}."


def _signals_line(signals: list, residual: int) -> str:
    blocking = sum(1 for s in signals if s.get("blocking"))
    titles = "; ".join(f'"{str(s.get("title") or "")}"' for s in signals)
    head = f"- open attention signals: {len(signals) + max(0, residual)}"
    if blocking:
        head += f" ({blocking} blocking)"
    if titles:
        head += f" — {titles}"
    if residual > 0:
        head += f" (+{residual} more)"
    return head + "."


def render(snap: Optional[dict], *, focus: str = "",
           focus_evidence: str = "") -> str:
    """Render a snapshot as the bounded ``governance_state`` prose block.

    ``""`` means SAY NOTHING — the caller omits its segment entirely.

    ``focus`` (Item 6) is the last-word turn's mode: the named detector's line is
    rendered UNCONDITIONALLY (it has already tripped, so proximity is moot) and
    FIRST, and the header swaps to the intervention framing. Rules 1-3 above stay
    intact either way. Every other near reading still follows, because a PM asked
    to propose an alternative should see the rest of the board — that is precisely
    the "same model, radically less information" defect this spec exists to fix.

    Stable under re-render for unchanged inputs: no timestamps, and readings are
    emitted in the fixed order the publisher assembled them in.
    """
    try:
        return _render(snap, focus=focus, focus_evidence=focus_evidence)
    except Exception:  # noqa: BLE001 — prompt assembly must never raise
        return ""


def _render(snap: Optional[dict], *, focus: str, focus_evidence: str) -> str:
    focus = str(focus or "")
    focus_evidence = str(focus_evidence or "").strip()
    if not isinstance(snap, dict):
        # A focused render is still owed to the last-word turn even when nothing
        # was published (an unreadable ledger, or a trip on the first iteration):
        # the tripped reading is carried on the action itself.
        if focus and focus_evidence:
            snap = {}
        else:
            return ""

    rows = [r for r in (snap.get("near") or []) if isinstance(r, dict)]
    if focus:
        focused = [r for r in rows if str(r.get("detector") or "") == focus]
        rows = focused + [r for r in rows if r not in focused]

    lines: list[str] = []
    if focus and focus_evidence:
        if not any(str(r.get("detector") or "") == focus for r in rows):
            lines.append(f"- {focus}: {focus_evidence}.")
    lines.extend(_row_line(r) for r in rows)
    if snap.get("clamped"):
        lines.append(
            "- convergence clamp: ENGAGED — integration is forced serial and new "
            "fan-out is frozen while the run drains."
            + (f" {snap.get('clamp_reading')}" if snap.get("clamp_reading") else ""))
    elif snap.get("clamp_reading"):
        lines.append(f"- convergence: {snap.get('clamp_reading')}")

    budget = snap.get("budget")
    if isinstance(budget, dict) and budget:
        lines.append(_budget_line(budget))

    signals = [s for s in (snap.get("signals") or []) if isinstance(s, dict)]
    if signals:
        lines.append(_signals_line(signals, int(snap.get("signals_residual") or 0)))

    if not lines:
        return ""

    iteration = snap.get("iteration")
    where = f" as of iteration {iteration}" if iteration is not None else ""
    if focus:
        head = (f"GOVERNANCE STATE — observed run telemetry{where}. The first "
                f"reading below has reached its window. {_FRAMING} "
                f"{ANTI_DONE_SENTENCE}")
    else:
        head = (f"GOVERNANCE STATE — observed run telemetry{where}. {_FRAMING} "
                f"{ANTI_DONE_SENTENCE}")

    body = "\n".join(lines)[:READINGS_CAP]
    closing = (_CLOSING_INTERVENTION if snap.get("last_word_available")
               else _CLOSING_HALT)
    return f"{head}\n{body}\n{closing}\n"


def prompt_text(ledger: Any, *, focus: str = "",
                focus_evidence: str = "") -> str:
    """The ``governance_state`` segment text for ``ledger``'s current run, or
    ``""`` when there is nothing to say.

    ``""`` is the contract, not a convenience: `_pm_prompt_segments` OMITS the
    segment entirely in that case rather than emitting an empty labelled one,
    which is what keeps a run with nothing near a threshold byte-identical to
    today's prompt (`test_prompt_segments_golden.py` depends on it).
    """
    return render(read(ledger), focus=focus, focus_evidence=focus_evidence)
