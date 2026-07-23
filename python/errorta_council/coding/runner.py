"""F087 live integration — drive the autonomy loop against real member turns.

Ties the F087-02 brain + F087-03 loop to actual Council members: each turn
builds a role-appropriate prompt (orientation packet + task + skill directive),
calls the member's model through an injected ``member_caller`` (the real model
gateway in production; a fake in tests), parses the member's structured JSON
response, and applies the ledger/worktree mutations + TDD gate.

Structured turn protocol (the member is asked to emit JSON):
* PM (plan)  -> ``{"tasks": [{"title":..,"role":"dev"}], "done": false}``
* dev        -> ``{"files": [{"path":..,"content":..}], "has_passing_test": true,
                  "task_type": "implementation"}``
* reviewer   -> ``{"approved": true}``
* tester     -> F087-10: a ``coding_turn.v1`` ``test_plan`` envelope choosing
                ``command_ids`` from the project registry; it CANNOT self-assert
                a pass — the verdict is derived from the real exit code.

``member_caller`` is ``(member_dict, prompt) -> model_text`` and is INJECTED, so
this module stays free of any direct gateway/HTTP import; the route builds the
real caller over ``LocalGateway``.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
from typing import Any, Callable, NamedTuple, Optional

from . import paths as _paths
from . import task_dedupe
from .autonomy import (
    CodingAutonomyPolicy,
    LoopResult,
    TurnOutcome,
    run_coding_loop,
)
from .completion import pending_completion_work, summarize_open_items
from .ledger import LedgerStore, Task, format_focus_lines
from .orientation import build_orientation_packet
from .schemas import TurnErrorCode, TurnParseError, parse_coding_turn
from .skills import primary_skill, record_turn_skill
from .testing import resolve_commands, run_test_commands
from .topology import (
    DEV,
    PM,
    REVIEWER,
    TESTER,
    Assign,
    GovernanceMaterialize,
    GovernancePlan,
    GovernanceReview,
    Merge,
    Plan,
    PMAssist,
    coding_role_of,
)
from .turn_controller import CodingTurnController, tool_catalog_text
from .workspace import CodingWorkspace

MemberCaller = Callable[[dict[str, Any], str], str]

# F143: per-turn token usage crosses the string-typed MemberCaller seam via a
# thread-local sink. ``gateway_member_caller`` writes the gateway result's token
# fields; the capturing wrapper clears the sink before each call and reads it after,
# on the SAME worker thread (run_coro blocks the caller), then folds the counts into
# that thread's per-turn capture dict (also thread-local — see _cap_of). A fake
# caller never writes the sink, so its turns carry no usage and roll up as
# ``unreported``. Keeping the seam ``-> str`` means the test fakes need no changes.
_usage_sink = threading.local()


# --- F143-01 Slice F: per-member Context Report (segmented prompt builders) ----
#
# The coding-team prompt builders assemble their prompt INLINE (no ContextRouter /
# ContextManifest), so there is no per-section token attribution to read. Slice F
# refactors the highest-value builders (PM + DEV) to emit an ordered list of labeled
# ``PromptSegment``s; ``join_segments`` concatenates their ``text`` verbatim, so the
# prompt string a member receives is BYTE-IDENTICAL to the pre-refactor prompt
# (invariant 7, locked by test_prompt_segments_golden.py). A builder too branchy to
# segment safely wraps its whole output as one coarse ``PromptSegment("prompt", ...)``
# — a correct coarse composition beats a byte-changing fine one.
#
# Composition (per-segment token counts) is computed where the segments are in hand,
# then handed to the gateway caller across the same thread via ``_pending_composition``
# (below): the builder registers ``(prompt_string, composition_dict)`` for the current
# worker thread; the gateway caller, seeing the SAME prompt string it was asked to
# send, adopts ``composition.sent_total`` as this call's ``estimated_input`` (so the
# categorized per-segment sum becomes authoritative for input) and stashes the block
# for ``record_turn``. A corrective-retry re-prompt does NOT match, so it cleanly falls
# back to the whole-string estimate — no stale composition is ever mis-attributed.
_composition_pending = threading.local()

# Category taxonomy (spec §composition). Used as ``PromptSegment.class_`` values and
# tokenized with ``content_kind_for_class`` per segment. A coarse-fallback builder
# uses the single ``"prompt"`` class.
_COMPOSITION_CLASSES = (
    "role_instructions", "work_request", "project_context", "repo_snapshot",
    "prior_outputs", "pr_diff", "tool_guidance", "transcript",
)


class PromptSegment(NamedTuple):
    """One labeled span of an assembled prompt. ``class_`` is a composition category
    (see ``_COMPOSITION_CLASSES``) or the coarse ``"prompt"`` bucket; ``text`` is the
    verbatim span. ``join_segments`` concatenates ``text`` in order with NO added
    separators — segment boundaries carry their own whitespace so the joined string
    equals the pre-refactor prompt byte-for-byte."""

    class_: str
    text: str


def join_segments(segments: list["PromptSegment"]) -> str:
    """Concatenate segment ``text`` in order, verbatim. This is the ONLY way a
    segmented builder's string is produced, so byte-identity is a property of the
    segmentation, not of a re-join step."""
    return "".join(seg.text for seg in segments)


def _composition_from_segments(segments: list["PromptSegment"]) -> dict[str, Any]:
    """Tokenize each segment with the shared estimator (content-kind chosen per
    class) and merge duplicate classes by summing. Returns the compact ``composition``
    block ``{"sent_total", "categories": [{"class", "tokens"}, …], "estimator_method"}``.
    ``sent_total`` is the sum of the per-segment estimates (not the whole-string
    estimate) and becomes the turn's authoritative ``estimated_input``."""
    from errorta_council.context.tokens import content_kind_for_class

    estimator = _get_token_estimator()
    by_class: dict[str, int] = {}
    order: list[str] = []
    for seg in segments:
        if not seg.text:
            continue
        tokens = estimator.estimate(
            seg.text, content_kind=content_kind_for_class(seg.class_))
        if seg.class_ not in by_class:
            order.append(seg.class_)
        by_class[seg.class_] = by_class.get(seg.class_, 0) + int(tokens)
    categories = [{"class": cls, "tokens": by_class[cls]} for cls in order]
    return {
        "sent_total": sum(by_class.values()),
        "categories": categories,
        "estimator_method": getattr(estimator, "method", None),
    }


def _register_pending_composition(segments: list["PromptSegment"]) -> str:
    """Join ``segments`` into the prompt string AND register its composition for the
    current worker thread so the gateway caller can adopt it. Returns the joined
    string, which the call site passes straight to the member caller unchanged."""
    prompt = join_segments(segments)
    try:
        _composition_pending.entry = (prompt, _composition_from_segments(segments))
    except Exception:  # noqa: BLE001 — composition is observability; never break a turn
        _composition_pending.entry = None
        logging.getLogger("errorta.coding").debug(
            "composition computation failed", exc_info=True)
    return prompt


def _take_pending_composition(prompt: str) -> dict[str, Any] | None:
    """Pop the pending composition IFF it was registered for exactly this prompt
    string (guards against a corrective-retry prompt adopting a stale composition).
    Cleared ONLY on a match — an intervening non-matching gateway call on the same
    thread must NOT clear a segmented builder's still-pending entry, so the entry
    survives to be adopted by the real matching call. A new ``_register`` overwrites
    it, so a stale entry never leaks across turns."""
    entry = getattr(_composition_pending, "entry", None)
    if isinstance(entry, tuple) and len(entry) == 2 and entry[0] == prompt:
        _composition_pending.entry = None
        comp = entry[1]
        return comp if isinstance(comp, dict) else None
    return None


def _clean_call_int(value: Any) -> int | None:
    """A per-call token field is a non-negative int or absent. Reject bool
    (an int subclass), negatives, and non-ints so a bad value is dropped."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _merge_call_usage(acc: dict[str, Any] | None,
                      call: dict[str, Any] | None) -> dict[str, Any] | None:
    """F143: fold one model call's token usage into the turn accumulator.

    A single turn can make several gateway calls (e.g. a parse-retry), so token
    counts are SUMMED across them — the recorded per-turn usage reflects true spend,
    not just the last exchange.

    F143-01 Slice D (hybrid-turn fix): the accumulator now tracks a per-call
    EFFECTIVE value so a turn that MIXES a measured call with a dark call keeps both
    calls' spend and reports honest provenance (the old code summed measured fields
    only for measured calls and then collapsed the whole turn to ``measured=True`` if
    ANY call was measured — dropping the dark call's estimated spend and over-claiming
    provenance). Per turn we track:

    * ``measured_input``/``measured_output`` — summed over MEASURED calls only;
    * ``estimated_input``/``estimated_output`` — summed over ALL calls (the fallback
      + cli_overhead basis);
    * ``effective_input``/``effective_output`` — summed over calls of the call's
      MEASURED value if that call was measured, else the call's estimate. This is the
      genuine headline: correct for all-measured, all-dark, AND mixed turns;
    * ``cache_read``/``cache_write`` — summed over measured calls;
    * ``measured_calls``/``total_calls`` — counts driving provenance;
    * ``provider_class``/``model`` — last non-empty call wins.

    A bare/fake call (no measured numbers, no estimate, no meta) leaves the
    accumulator untouched so the turn stays ``unreported``."""
    if not isinstance(call, dict):
        return acc
    measured = bool(call.get("measured"))
    mi = _clean_call_int(call.get("input_tokens")) if measured else None
    mo = _clean_call_int(call.get("output_tokens")) if measured else None
    cr = _clean_call_int(call.get("cache_read_input_tokens")) if measured else None
    cw = _clean_call_int(call.get("cache_write_input_tokens")) if measured else None
    ei = _clean_call_int(call.get("estimated_input"))
    eo = _clean_call_int(call.get("estimated_output"))
    eir = _clean_call_int(call.get("estimated_input_raw"))
    is_measured_call = mi is not None or mo is not None
    has_est = ei is not None or eo is not None
    pc = call.get("provider_class")
    mdl = call.get("model")
    has_meta = bool(pc) or bool(mdl)
    # F143-01 Slice F: the Layer-1 composition of the prompt this call sent (present
    # only for a segmented builder's initial call; None on a corrective retry).
    comp = call.get("composition")
    comp = comp if isinstance(comp, dict) else None
    # Nothing usable on this call at all — leave the accumulator untouched.
    if not is_measured_call and not has_est and not has_meta:
        return acc
    if acc is None:
        # Note: the measured_input/measured_output sums are LEFT ABSENT until a call
        # actually reports one, so the record boundary can tell "never measured on
        # this side" (key absent → None) from "measured and summed to 0". The other
        # sums start at 0 because every real call contributes an estimate.
        acc = {
            "estimated_input": 0, "estimated_output": 0,
            "effective_input": 0, "effective_output": 0,
            # cache_read/cache_write are created LAZILY (like measured_*) so a turn
            # whose provider reported no cache doesn't persist a spurious 0 — cache
            # is a per-provider detail, absent when not reported (D4).
            "measured_calls": 0, "total_calls": 0,
        }
    acc["total_calls"] = int(acc.get("total_calls") or 0) + 1
    if is_measured_call:
        acc["measured_calls"] = int(acc.get("measured_calls") or 0) + 1
    # Measured-only sums — created lazily, so an absent key means "never measured".
    if mi is not None:
        acc["measured_input"] = int(acc.get("measured_input") or 0) + mi
    if mo is not None:
        acc["measured_output"] = int(acc.get("measured_output") or 0) + mo
    if cr is not None:
        acc["cache_read"] = int(acc.get("cache_read") or 0) + cr
    if cw is not None:
        acc["cache_write"] = int(acc.get("cache_write") or 0) + cw
    # Estimated sums (every call that carries an estimate).
    if ei is not None:
        acc["estimated_input"] = int(acc.get("estimated_input") or 0) + ei
    if eo is not None:
        acc["estimated_output"] = int(acc.get("estimated_output") or 0) + eo
    # RAW (uncalibrated) input estimate — the cli_overhead basis. Created lazily so an
    # older accumulator/turn without it stays absent (the record boundary then falls
    # back to the calibrated estimate for overhead, preserving prior behavior).
    if eir is not None:
        acc["estimated_input_raw"] = int(acc.get("estimated_input_raw") or 0) + eir
    # Effective per-call: the call's measured value where measured, else its estimate.
    eff_in = mi if mi is not None else ei
    eff_out = mo if mo is not None else eo
    if eff_in is not None:
        acc["effective_input"] = int(acc.get("effective_input") or 0) + eff_in
    if eff_out is not None:
        acc["effective_output"] = int(acc.get("effective_output") or 0) + eff_out
    if pc:
        acc["provider_class"] = str(pc)
    if mdl:
        acc["model"] = str(mdl)
    cf = call.get("calibration_factor")
    if isinstance(cf, (int, float)) and not isinstance(cf, bool):
        acc["calibration_factor"] = float(cf)
    # Keep the FIRST call's composition — that is the segmented builder's original
    # prompt; a later corrective retry re-prompts an unsegmented string (no comp).
    if comp is not None and "composition" not in acc:
        acc["composition"] = comp
    return acc


# --- F143-01 Slice C: shared token estimator + provenance derivation -----------
#
# The coding team assembles prompts INLINE (no ContextManifest), and model
# responses are capped in turns.jsonl, so estimation MUST be computed at turn time
# from the in-memory prompt string + result.content. We reuse Council's ONE
# estimator (errorta_council/context/tokens.py) and share its calibration-store
# LOCATION (token_calibration_path()) so (provider,model) factors accumulate across
# both Council runs and coding runs.
#
# The estimator is lazily constructed under a module-level lock so the coding
# runner's worker-thread pool can't double-init or race. Estimation reads are pure
# and thread-safe; the calibration UPDATE (write) is guarded by the same lock and is
# strictly best-effort — a calibration write must never break a turn.
_estimator_lock = threading.Lock()
_estimator_singleton: Any = None


def _get_token_estimator() -> Any:
    """Return the process-wide shared ``CalibratedEstimator``, constructing it once
    under ``_estimator_lock``. Backed by the shared Council ``TokenCalibrationStore``
    so factors are shared. Imports are lazy so this module pulls no context/egress
    at import time."""
    global _estimator_singleton
    est = _estimator_singleton
    if est is not None:
        return est
    with _estimator_lock:
        if _estimator_singleton is None:
            from errorta_council.context.tokens import CalibratedEstimator
            _estimator_singleton = CalibratedEstimator()
        return _estimator_singleton


def _calibration_store() -> Any:
    """Construct a ``TokenCalibrationStore`` over the shared Council calibration
    path (under ``${ERRORTA_HOME}``). Cheap; the store itself is stateless (reads +
    atomic-writes the JSON on demand)."""
    from errorta_council.context.tokens import TokenCalibrationStore
    from errorta_council.paths import token_calibration_path
    return TokenCalibrationStore(token_calibration_path())


def _read_calibration_factor(provider_class: str, model: str) -> float:
    """The stored ``(provider,model)`` calibration factor, read FRESH each turn so a
    factor learned earlier in this same run (or a prior run) is applied to later
    turns. Keyed identically to ``_update_calibration`` (``"unknown"`` fallbacks) so a
    write and its readback agree. Any store error → 1.0 (a safe, no-op factor)."""
    try:
        return float(_calibration_store().read_factor(
            provider_class or "unknown", model or "unknown"))
    except Exception:  # noqa: BLE001 — a calibration read must never break a turn
        logging.getLogger("errorta.coding").debug(
            "calibration read failed", exc_info=True)
        return 1.0


def _apply_calibration(base_tokens: int, factor: float) -> int:
    """Scale a RAW (factor-1.0) token estimate by the calibration factor, matching
    ``CalibratedEstimator.estimate`` math (``max(1, ceil(base * factor))``) so the
    composition-derived input estimate and the whole-string calibrated estimate agree."""
    return max(1, int(math.ceil(int(base_tokens) * float(factor))))


def _update_calibration(provider_class: str, model: str,
                        reported_input: int | None,
                        estimated_input: int | None) -> None:
    """F143-01 Slice C: nudge the ``(provider,model)`` calibration factor from a
    measured turn. Best-effort + lock-guarded: any failure (bad path, unwritable
    store) is swallowed so a calibration write never breaks a turn. Only a turn with
    BOTH a reported input and our own estimate contributes a ratio."""
    try:
        from errorta_council.context.tokens import (
            CalibrationSample,
            calibration_ratio,
        )
        ratio = calibration_ratio(
            reported_input_tokens=reported_input,
            estimated_input_tokens=estimated_input,
        )
        if ratio is None:
            return
        with _estimator_lock:
            _calibration_store().record(
                CalibrationSample(provider=provider_class or "unknown",
                                  model=model or "unknown", ratio=ratio))
    except Exception:  # noqa: BLE001 — a calibration write must never break a turn
        logging.getLogger("errorta.coding").debug(
            "token calibration update failed", exc_info=True)


def _derive_provenance(*, measured_input: int | None, measured_output: int | None,
                       estimated_input: int | None, estimated_output: int | None,
                       raw_usage_available: bool,
                       measured_calls: int | None = None,
                       total_calls: int | None = None) -> str:
    """F143-01 Slice C/D — collapse a turn's token slots into an honest provenance.

    * ``measured``          — EVERY call in the turn was measured AND both measured
                              ints (input+output) are present. A turn that mixes a
                              measured call with a dark call is NEVER ``measured``.
    * ``measured_partial``  — some (but not all) calls measured, OR exactly one
                              measured side present; the gap is filled from estimate.
    * ``estimated``         — no measured ints at all, but we have estimates (bytes).
    * ``unreported``        — nothing at all (legacy/no-bytes safety; should not
                              happen for a real turn going forward).

    ``measured_calls``/``total_calls`` (F143-01 Slice D hybrid-turn fix) let a turn
    that mixed a measured + a dark call report ``measured_partial`` instead of
    over-claiming ``measured``. When absent (direct callers/tests), behavior falls
    back to the Slice-C both-sides-present check.
    """
    have_mi = measured_input is not None
    have_mo = measured_output is not None
    # A turn with call counts: full-coverage only when every call was measured.
    if isinstance(total_calls, int) and total_calls > 0 \
            and isinstance(measured_calls, int):
        if measured_calls == 0:
            return "estimated" if (estimated_input is not None
                                   or estimated_output is not None) else "unreported"
        if measured_calls < total_calls:
            return "measured_partial"
        # All calls measured — still require both measured sides for the top grade.
        if have_mi and have_mo:
            return "measured"
        return "measured_partial" if (have_mi or have_mo) else "unreported"
    # No call-count context (direct callers/tests) — Slice-C both-sides logic.
    if raw_usage_available and have_mi and have_mo:
        return "measured"
    if have_mi != have_mo:  # exactly one measured int present
        return "measured_partial"
    if estimated_input is not None or estimated_output is not None:
        return "estimated"
    return "unreported"


class _MemberCallFailed(Exception):
    """F120 control-flow sentinel: a member CALL failed (logged-out CLI, missing
    binary, 401/429, unparseable output). Carries the member identity + classified
    failure so the ``run_turn`` boundary converts it into a ``member_failed``
    TurnOutcome rather than the reason being swallowed into a bare noop."""

    def __init__(self, *, member_id: str, role: str, route: str, failure: Any):
        super().__init__(f"member_call_failed:{member_id}:{failure.status}")
        self.member_id = member_id
        self.role = role
        self.route = route
        self.failure = failure

# Grounding-consumption trace logger — every point where a PM/dev/reviewer/tester
# turn actually pulls grounding emits an INFO line so a run is traceable end to
# end (counts/refs only, never raw corpus content). Set log level to DEBUG/INFO
# and tail the sidecar log (see ERRORTA_LOG_FILE) to follow it.
_grounding_log = logging.getLogger("errorta.grounding")


_CORRECTIVE_PREFIXES = ("fix tests:", "revise:", "resolve conflict:")


def _reason_from_findings(findings: list[dict[str, Any]]) -> str:
    """F141 WS-D: a one-line "why this was sent back" from reviewer findings.
    Prefers blocking findings; names the first + its file + the remaining count."""
    if not findings:
        return ""
    blocking = [f for f in findings if f.get("blocking")]
    primary = blocking or findings
    first = primary[0]
    title = str(first.get("title") or "review finding").strip()
    path = str(first.get("path") or "").strip()
    loc = f" ({path})" if path else ""
    n = len(primary)
    label = "blocking finding" if blocking else "finding"
    if n == 1:
        return f"1 {label}: '{title}'{loc}"
    return f"{n} {label}s — '{title}'{loc} +{n - 1} more"


def _detail_from_findings(findings: list[dict[str, Any]], *, cap: int = 6) -> str:
    """F141 WS-D: a capped "title (path)" list for the rework task's detail view."""
    if not findings:
        return ""
    parts = []
    for f in findings[:cap]:
        title = str(f.get("title") or "finding").strip()
        path = str(f.get("path") or "").strip()
        parts.append(f"{title} ({path})" if path else title)
    more = len(findings) - cap
    tail = f" +{more} more" if more > 0 else ""
    return "; ".join(parts) + tail


# F104 S6 — conflict re-dispatch
_CONFLICT_RESOLVE_RETRY_CAP = 2
# F159: the filename regex + path extraction moved to `paths.py` (so topology can
# share them without a cycle); kept as aliases for the existing call sites here.
_TARGET_PATH_RE = _paths.TARGET_PATH_RE
# F104 S4 — bounded corrective retry on a malformed intent turn
_INTENT_CORRECTIVE_RETRIES = 1
# F127 D3: workers (dev/reviewer/tester) get one extra corrective attempt — the
# strong PM stays at 1. Weaker worker models recover more often with a second,
# blunter re-prompt before the escalation ladder takes over.
_WORKER_CORRECTIVE_RETRIES = 2
_RETRYABLE_TURN_ERRORS = {
    TurnErrorCode.turn_non_json,
    TurnErrorCode.turn_tool_markup_only,  # F127: re-prompt for JSON, not tool calls
    TurnErrorCode.turn_schema_mismatch,
}


def _governance_corrective_prompt(prompt: str, code: str, detail: str, *,
                                  retry: int, max_retries: int) -> str:
    # F100 bugfix (2026-06-22): mirror _corrective_turn_prompt for governance
    # turns. A rejected governance turn gets a bounded re-prompt that restates
    # the exact required schema + the validation detail, so a normalizable-but-
    # imperfect reviewer/PM can self-correct instead of dead-ending the run.
    return (
        f"{prompt}\n\n"
        "Your previous governance_turn.v1 response was rejected "
        f"({retry}/{max_retries} corrective retry): {code}: {detail}\n"
        "Reply with ONLY a valid governance_turn.v1 JSON envelope for the same "
        "role. For an artifact review, \"verdict\" MUST be one of "
        '"approved" | "request_changes" | "blocked"; each finding MUST be an '
        'object {"severity":"low|medium|high|critical","title":"...",'
        '"body":"...","blocking":true|false}; a non-"approved" verdict requires '
        "at least one finding. Drop any unmodeled fields."
    )


def _corrective_turn_prompt(prompt: str, parsed: TurnParseError, *,
                            retry: int, max_retries: int) -> str:
    return (
        f"{prompt}\n\n"
        "Your previous coding_turn.v1 response was rejected "
        f"({retry}/{max_retries} corrective retry): "
        f"{parsed.code.value}: {parsed.detail}\n"
        # F127: weaker CLI-backed models slip into agent mode and emit tool-call
        # markup instead of the envelope — forbid it explicitly and bluntly.
        "Reply with ONLY a single valid coding_turn.v1 JSON object for the same "
        "role and task. Do NOT call tools, do NOT write prose, do NOT emit "
        "<function_calls>/<invoke>/<parameter> markup or a sub-agent. Output the "
        "JSON object and nothing else. If you are implementing, emit at least one "
        "tool_call for implementation/test_only/refactor work. Drop unmodeled "
        "fields such as summary. Reviewer findings must be objects, not bare strings."
    )


def _sync_grounding(store: LedgerStore, workspace: Any, *,
                    refresh_corpus: bool = False) -> None:
    """F088-06: project the ledger into the grounding memory store after a merge
    and at run end. Fully guarded + best-effort — a missing
    ``errorta_project_grounding`` package or any sync error degrades to exactly
    today's F087 behavior (the index lives only under ``grounding/`` and never
    touches the ledger or worktree).

    When ``refresh_corpus`` is set (run end — a quiescent point with no worker
    turns touching the worktree) AND a project corpus is bound, the merged
    ``master`` code is also re-ingested into that corpus so the next run's PM/dev
    retrieval reflects what the team built. Per-merge code re-ingest is avoided
    on purpose: it would add latency to the (serial) merge and risk racing live
    git ops."""
    try:
        from errorta_project_grounding.update_pipeline import sync_from_ledger
    except Exception:
        return
    try:
        counts = sync_from_ledger(store, workspace=workspace)
        if isinstance(counts, dict):
            _grounding_log.info(
                "grounding sync: project=%s %s", store.project_id,
                " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    except Exception:
        logging.getLogger("errorta.coding").debug(
            "grounding sync skipped", exc_info=True)
    if not refresh_corpus or workspace is None:
        return
    try:
        from errorta_project_grounding.corpus_binding import load_binding
        from errorta_project_grounding.update_pipeline import rebuild_from_repo

        binding = load_binding(store)
        if binding.mode in ("build_from_repo", "build_from_project") and binding.corpus_id:
            res = rebuild_from_repo(store, workspace)
            _grounding_log.info(
                "grounding corpus refresh: project=%s corpus=%s %s",
                store.project_id, binding.corpus_id,
                " ".join(f"{k}={v}" for k, v in sorted(res.items())))
        from errorta_project_grounding.pm_working_memory import (
            mirror_pm_working_memory_to_aiar,
        )
        mirror = mirror_pm_working_memory_to_aiar(store)
        _grounding_log.info(
            "grounding pm-memory mirror: project=%s status=%s corpus=%s record=%s",
            store.project_id, mirror.status, mirror.corpus_id or "none",
            mirror.record_id or "none")
    except Exception:
        logging.getLogger("errorta.coding").debug(
            "grounding corpus refresh skipped", exc_info=True)


def _reconcile_stale(store: LedgerStore, workspace: Any) -> None:
    """F087-19 #2: drop work whose requirements are already satisfied on master.
    A non-terminal PR whose branch now has an EMPTY diff vs master is superseded
    (master already contains its work) -> abandon it and drop the todo corrective
    tasks (fix tests / revise / resolve) that reference its branch. Deterministic
    and cheap (a git diff per open PR); only runs when there are open PRs."""
    if workspace is None:
        return
    # F091: "superseded" PRs are terminal (their work was redone on a merged
    # sibling) and are intentionally absent from this open-set — never re-reconciled.
    open_prs = [p for p in store.list_prs()
                if p.get("status") in ("open", "changes_requested", "mergeable", "conflict")]
    if not open_prs:
        return
    in_flight_pr_ids = {
        t.pr_id for t in store.list_tasks(state="doing") if getattr(t, "pr_id", None)
    }
    for pr in open_prs:
        if pr.get("pr_id") in in_flight_pr_ids:
            continue
        branch = pr.get("branch", "")
        try:
            superseded = not workspace.pr_diff(branch).strip()
        except Exception:
            continue
        if not superseded:
            continue
        store.update_pr(pr["pr_id"], status="abandoned")
        store.record_decision(
            title=f"superseded PR {branch}", context=f"pr {pr['pr_id']}",
            choice="pr_superseded",
            rationale="master already contains this work; PR abandoned",
            related_task_ids=[pr.get("task_id", "")])
        for t in store.list_tasks(state="todo"):
            if branch in t.title and t.title.lower().startswith(_CORRECTIVE_PREFIXES):
                store.update_task(t.task_id, state="dropped")
        try:
            workspace.delete_branch(branch)
        except Exception:
            pass


def _prune_dead_branches(store: LedgerStore, workspace: Any, *,
                         just_merged: str = "") -> None:
    """F087-18 #6: after a merge, delete the just-merged branch and any other
    task branch whose PR is terminal (merged/abandoned), to save space. Live PRs
    (open/changes_requested/mergeable/conflict — may still be revised) are kept."""
    if workspace is None:
        return
    try:
        in_flight_pr_ids = {
            t.pr_id for t in store.list_tasks(state="doing") if getattr(t, "pr_id", None)
        }
        if just_merged:
            workspace.delete_branch(just_merged)
        terminal = {p["branch"] for p in store.list_prs()
                    if p.get("status") in ("merged", "abandoned", "superseded")
                    and p.get("pr_id") not in in_flight_pr_ids and p.get("branch")}
        existing = set(workspace.list_branches())
        for branch in terminal & existing:
            workspace.delete_branch(branch)
    except Exception:
        pass


def _supersede_ancestors(store: LedgerStore, workspace: Any,
                         merged_pr: dict[str, Any]) -> None:
    """F091: when a revise PR merges, walk the STRICT ancestor chain via each
    revise task's ``pr_id`` back-link and mark every prior rejected PR
    ``superseded`` (with a back-pointer to the merged PR). Follows only the merged
    PR's own lineage — never a shared-key query — so an independent open PR can
    never be swept in. Runs under the store lock inside the merge turn; drops the
    dangling corrective tasks and prunes the branches it retires."""
    if not merged_pr:
        return
    tasks_by_id = {t.task_id: t for t in store.list_tasks()}
    superseding_pr_id = merged_pr.get("pr_id", "")
    cur_task = tasks_by_id.get(merged_pr.get("task_id", ""))
    seen: set[str] = set()
    while cur_task is not None and getattr(cur_task, "pr_id", None):
        prev_pr_id = cur_task.pr_id
        if not prev_pr_id or prev_pr_id in seen:  # cycle / self guard
            break
        seen.add(prev_pr_id)
        prev_pr = store.get_pr(prev_pr_id)
        if prev_pr is None or prev_pr.get("status") in (
                "merged", "abandoned", "superseded"):
            break
        store.update_pr(prev_pr_id, status="superseded",
                        superseded_by_pr_id=superseding_pr_id)
        branch = prev_pr.get("branch", "")
        store.record_decision(
            title=f"superseded PR {branch}", context=f"pr {prev_pr_id}",
            choice="pr_superseded",
            rationale=(f"revise PR {superseding_pr_id} merged; this PR's work was "
                       f"redone and is superseded"),
            related_task_ids=[prev_pr.get("task_id", "")])
        # drop dangling corrective tasks that reference the retired branch
        for t in store.list_tasks(state="todo"):
            if branch and branch in t.title \
                    and t.title.lower().startswith(_CORRECTIVE_PREFIXES):
                store.update_task(t.task_id, state="dropped")
        if workspace is not None and branch:
            try:
                workspace.delete_branch(branch)
            except Exception:
                pass
        # walk up: the retired PR's own task may itself be a revise (have a pr_id)
        cur_task = tasks_by_id.get(prev_pr.get("task_id", ""))


def _revalidate_stale_prs(store: LedgerStore, workspace: Any, *,
                          just_merged_pr_id: str) -> None:
    """F087-3 stale-base revalidation. After a PR lands, ``master`` has moved, so
    every OTHER mergeable PR was validated against an older base. Bring the new
    ``master`` into each such branch and demote it back through re-test BEFORE it
    can merge — so a clean-but-untested integration can never land, and a now-
    conflicting branch is bounced to a resolve task instead of overwriting work.

    A branch that already contains the new master (nothing changed by the merge)
    stays mergeable — it is genuinely still validated. Fully guarded: a workspace
    error on one PR never blocks the merge that triggered this."""
    if workspace is None:
        return
    for p in store.list_prs():
        if p.get("pr_id") == just_merged_pr_id:
            continue
        if p.get("status") != "mergeable":
            continue
        branch = p.get("branch")
        task_id = p.get("task_id")
        if not branch or not task_id:
            continue
        # Per-PR isolation: a ledger/workspace error on ONE stale PR must never
        # leave it silently `mergeable` against a moved master (the hole this
        # feature closes) AND must never abort revalidating the others. On any
        # failure, fail closed with a best-effort demotion.
        try:
            _revalidate_one_pr(store, workspace, p, branch, task_id)
        except Exception as exc:  # noqa: BLE001
            _fail_closed_demote(store, p, branch, task_id, reason=str(exc))


def _revalidate_one_pr(store: LedgerStore, workspace: Any, p: dict[str, Any],
                       branch: str, task_id: str) -> None:
    res = workspace.update_branch_from_base(task_id, branch)
    if res.get("updated") and not res.get("changed"):
        # Branch already contained this master -> still validly mergeable.
        return
    if res.get("updated"):
        # Clean integration with the new master: keep the (unchanged) code
        # review, but the tests are now stale -> re-test the integrated tree.
        store.update_pr(p["pr_id"], status="changes_requested",
                        tests_passed=False, head=res.get("head", p["head"]))
        store.record_decision(
            title=f"stale-base re-test: {branch}", context=f"pr {p['pr_id']}",
            choice="stale_base_revalidation",
            rationale="master advanced after another PR merged; "
                      "re-testing the integrated tree before merge",
            related_task_ids=[task_id])
        store.add_task(title=f"re-test PR: {branch}", role=TESTER,
                       pr_id=p["pr_id"], depends_on=[task_id])
    else:
        # Integration now conflicts -> resolve task (same net as a merge-time
        # conflict; never a silent overwrite).
        store.update_pr(p["pr_id"], status="conflict", tests_passed=False,
                        conflicts=res.get("conflicts", []))
        store.record_decision(
            title=f"stale-base conflict: {branch}", context=f"pr {p['pr_id']}",
            choice="pr_conflict",
            rationale="conflicts integrating latest master: "
                      + ", ".join(res.get("conflicts", [])),
            related_task_ids=[task_id])
        _redispatch_conflict_pr(
            store, workspace, store.get_pr(p["pr_id"]) or p,
            conflicts=res.get("conflicts", []),
        )


def _fail_closed_demote(store: LedgerStore, p: dict[str, Any], branch: str,
                        task_id: str, *, reason: str) -> None:
    """Best-effort fail-closed demotion when revalidation can't run safely: the
    PR must not stay `mergeable` against a moved master. Swallows its own errors
    so cleanup can never crash the merge that triggered revalidation."""
    try:
        store.update_pr(p["pr_id"], status="changes_requested", tests_passed=False)
        store.record_decision(
            title=f"stale-base demote: {branch}", context=f"pr {p['pr_id']}",
            choice="stale_base_revalidation",
            rationale=f"could not revalidate against new master: {reason}",
            related_task_ids=[task_id])
        store.add_task(title=f"re-test PR: {branch}", role=TESTER,
                       pr_id=p["pr_id"], depends_on=[task_id])
    except Exception:  # noqa: BLE001
        logging.getLogger("errorta.coding").warning(
            "coding revalidate: failed to demote stale PR %s (%s)",
            p.get("pr_id"), reason)


_declared_target_paths = _paths.declared_target_paths


def _active_dev_path_owners(store: LedgerStore) -> dict[str, str]:
    """F159: path -> owning DEV task, over tasks that hold the path RIGHT NOW —
    ``todo``/``doing`` (plan-time serialization) OR a task whose PR is open and
    not yet merged (the merge-scoped hold: the conflict surfaces at merge, so the
    file stays owned until the PR lands). Uses declared ``target_files`` when the
    task carries them, else the title/detail prose."""
    owners: dict[str, str] = {}
    live_pr_tasks: set[str] = set()
    list_prs = getattr(store, "list_prs", None)
    if callable(list_prs):
        for pr in list_prs():
            if pr.get("status") not in ("merged", "superseded", "abandoned", "closed"):
                tid = pr.get("task_id")
                if tid:
                    live_pr_tasks.add(str(tid))
    for task in store.list_tasks(role=DEV):
        if task.state not in ("todo", "doing") and task.task_id not in live_pr_tasks:
            continue
        for path in _paths.task_touched_paths(task):
            owners.setdefault(path, task.task_id)
    return owners


def _conflict_resolve_task_exists(store: LedgerStore, pr_id: str) -> bool:
    for task in store.list_tasks(role=DEV):
        if task.state in ("todo", "doing") and task.pr_id == pr_id \
                and task.title.lower().startswith("resolve conflict:"):
            return True
    return False


def _redispatch_conflict_pr(
    store: LedgerStore,
    workspace: Any,
    pr: dict[str, Any],
    *,
    conflicts: list[str] | None = None,
) -> bool:
    """Create one bounded resolve task for a conflicted PR.

    The branch update is delegated to ``CodingWorkspace.update_branch_from_base``
    so the council layer keeps its no-egress boundary. A resolve task carries a
    ``pr_id`` back-link to the conflicted PR; if the new PR later merges,
    _supersede_ancestors retires this conflicted ancestor.
    """
    if workspace is None or pr.get("status") != "conflict":
        return False
    pr_id = str(pr.get("pr_id") or "")
    branch = str(pr.get("branch") or "")
    task_id = str(pr.get("task_id") or "")
    if not pr_id or not branch or not task_id:
        return False
    if _conflict_resolve_task_exists(store, pr_id):
        return False

    attempts = int(pr.get("resolve_attempts") or 0)
    if attempts >= _CONFLICT_RESOLVE_RETRY_CAP:
        store.update_pr(
            pr_id, status="blocked",
            blocked_reason="conflict resolve retry cap reached",
            resolve_attempts=attempts,
            # F104 S6 review (M1): clear stale verdicts so the blocked PR can never
            # be read as reviewed+green for its (now-conflicting) head.
            reviewer_approved=None, tests_passed=None,
        )
        store.record_decision(
            title=f"blocked conflicted PR {branch}", context=f"pr {pr_id}",
            choice="pr_conflict_blocked",
            rationale="resolve retry cap reached; human intervention required",
            related_task_ids=[task_id],
        )
        # F159: a file we failed to auto-rebase `_CONFLICT_RESOLVE_RETRY_CAP` times
        # is hot by definition — hand it to the centralize owner + freeze parallel
        # edits so the churn stops, instead of leaving the PR silently blocked while
        # other writers keep re-colliding on it. Force-escalate regardless of count.
        capped_paths = list(pr.get("conflicts") or [])
        if capped_paths:
            _maybe_escalate_hot_files(store, capped_paths, force=True)
        # And tell a human — the cap is a genuine stuck point, not just a decision row.
        try:
            from . import attention
            from .governance import GovernanceStore
            gstate = GovernanceStore.for_ledger(store).load_state()
            attention.raise_monitor_problem(
                store.project_id,
                stage=(gstate.phase if gstate.mode != "off" else "build"),
                detector="conflict_resolve_capped",
                reason=(f"PR {branch} hit the conflict-resolve retry cap on "
                        f"{', '.join(capped_paths) or 'unknown files'}; centralized "
                        "the file + froze parallel edits — a human may need to look"),
                store=store)
        except Exception:  # noqa: BLE001 — the alert must never break the sweep
            pass
        return True

    update = workspace.update_branch_from_base(task_id, branch)
    attempts += 1
    conflict_paths = list(conflicts or update.get("conflicts") or pr.get("conflicts") or [])
    if update.get("updated") and not update.get("conflicts"):
        store.update_pr(
            pr_id, status="changes_requested", tests_passed=False,
            head=update.get("head", pr.get("head")), conflicts=[],
            resolve_attempts=attempts,
        )
        store.record_decision(
            title=f"rebased conflicted PR {branch}", context=f"pr {pr_id}",
            choice="pr_conflict_rebased",
            rationale="branch updated from master cleanly; re-testing before merge",
            related_task_ids=[task_id],
        )
        store.add_task(title=f"re-test PR: {branch}", role=TESTER,
                       pr_id=pr_id, depends_on=[task_id])
        return True

    store.update_pr(pr_id, status="conflict", conflicts=conflict_paths,
                    resolve_attempts=attempts)
    detail = (
        "Update the work for this conflicted PR on top of latest master and open "
        "a replacement PR. The conflicted ancestor will be superseded when the "
        f"replacement lands. Conflicted files: {', '.join(conflict_paths) or 'unknown'}"
    )
    task = store.add_task(
        title=f"resolve conflict: {branch}", role=DEV, detail=detail,
        pr_id=pr_id, depends_on=[task_id],
    )
    store.record_decision(
        title=f"redispatched conflicted PR {branch}", context=f"pr {pr_id}",
        choice="pr_conflict_redispatched",
        rationale=f"resolve attempt {attempts}/{_CONFLICT_RESOLVE_RETRY_CAP}",
        related_task_ids=[task_id, task.task_id],
        extra={"conflicts": conflict_paths, "resolve_task_id": task.task_id},
    )
    # F159: a file that keeps conflicting gets escalated to a centralize+freeze.
    _maybe_escalate_hot_files(store, conflict_paths)
    return True


def _redispatch_conflicted_prs(store: LedgerStore, workspace: Any) -> int:
    if workspace is None:
        return 0
    count = 0
    for pr in store.list_prs():
        if pr.get("status") == "conflict":
            count += int(_redispatch_conflict_pr(store, workspace, pr))
    return count


def _orientation_text(store: LedgerStore) -> str:
    pkt = build_orientation_packet(store, token_budget=2000)
    return json.dumps(pkt.to_dict(), ensure_ascii=False)


def _grounding_packet_text(role: str, store: LedgerStore, *,
                           task: Any = None, pr: Any = None) -> str:
    """F088-07: a role-scoped grounding context packet appended to a member
    prompt. Fully guarded — if the grounding package is absent or the project has
    no memory index, returns '' so the prompt is byte-identical to today's."""
    try:
        from errorta_project_grounding.context_packets import (
            build_role_context_packet,
            format_packet,
            role_token_budget,
        )
    except Exception:
        return ""
    try:
        packet = build_role_context_packet(
            store=store, role=role, task=task, pr=pr,
            token_budget=role_token_budget(role))
        text = format_packet(packet)
        corpus_count = len((packet or {}).get("corpus_evidence") or [])
        if text and packet:
            # F104 S7: log corpus_evidence_count so a dev/reviewer turn with ZERO
            # corpus hits on a corpus-bound project is visible in run-log.txt (the
            # regression this fixes — the implementer coded the spec values blind).
            _grounding_log.info(
                "grounding packet: project=%s role=%s items=%d "
                "corpus_evidence_count=%d budget=%d claims_excluded=%d truncated=%s",
                store.project_id, role, len(packet.get("items") or []),
                corpus_count,
                (packet.get("budget") or {}).get("max_tokens", 0),
                (packet.get("omitted") or {}).get("claims_excluded", 0),
                (packet.get("budget") or {}).get("truncated", False))
        # F104 S5: record the implementer-grounding signal for the merge gate —
        # did THIS task's implementer turn carry corpus evidence?
        if role == "dev" and corpus_count > 0:
            tid = getattr(task, "task_id", None)
            if tid:
                try:
                    store.record_implementer_grounding(
                        task_id=tid, corpus_evidence_count=corpus_count)
                except Exception:
                    pass
        return text
    except Exception:
        return ""


def _answer_dev_context_request(store: LedgerStore, task: Task, intent: Any) -> dict:
    """F088-09: answer a dev's read-only context request from corpus retrieval +
    project memory, capped. Records a ``context_request`` decision (auditable
    ledger metadata) — it writes NO files and mutates NO durable truth (memory is
    only queried). Returns the typed ``context_response.v1``."""
    sources = set(getattr(intent.scope, "sources", None) or ["memory", "corpus"])
    max_items = max(1, min(int(getattr(intent, "max_items", 6) or 6), 20))
    corpus_evidence: list[dict[str, Any]] = []
    if "corpus" in sources:
        try:
            from errorta_project_grounding.pm_working_memory import _is_pm_memory_hit
            from errorta_project_grounding.retrieval import retrieve_project_corpus
            q = (getattr(intent.scope, "corpus_query", "") or intent.question)
            # F099: the PM working-memory document is mirrored into the SAME bound
            # corpus, so an unfiltered dev retrieval can surface it. The PM memory
            # is PM-only by default (spec non-goal: no developer memory pollution),
            # so post-filter PM-memory chunks out of a non-PM (dev) context answer.
            for h in retrieve_project_corpus(store, query=q, top_k=max_items):
                if _is_pm_memory_hit(h, store.project_id):
                    continue
                corpus_evidence.append({"ref": f"hit:{h.corpus_id}:{h.chunk_id}",
                                        "summary": (h.content or "")[:240]})
                if len(corpus_evidence) >= max_items:
                    break
        except Exception:
            corpus_evidence = []
    memory: list[dict[str, Any]] = []
    if "memory" in sources:
        try:
            from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
            mem = ProjectMemoryStore(store.project_id, root=store.dir.parent)
            for it in mem.query(MemoryQuery(authorities=("durable_truth", "wip"),
                                            role="dev", limit=max_items))[:max_items]:
                memory.append({"ref": f"mem:{it.memory_id}", "authority": it.authority,
                               "summary": (it.summary or it.content or "")[:240]})
        except Exception:
            memory = []
    answer = {
        "schema_version": "context_response.v1",
        "question": intent.question,
        "reason": getattr(intent, "reason", "other"),
        "corpus_evidence": corpus_evidence,
        "memory": memory,
    }
    store.record_decision(
        title=f"context request: {task.title}", context=f"task {task.task_id}",
        choice="context_request", rationale=intent.question,
        extra={"context_response": answer}, related_task_ids=[task.task_id])
    _grounding_log.info(
        "grounding context-request: project=%s task=%s corpus_hits=%d memory_hits=%d "
        "sources=%s", store.project_id, task.task_id, len(corpus_evidence),
        len(memory), ",".join(sorted(sources)))
    # Surface it as a WIP memory row (operational, NOT durable) so the PM boot
    # briefing's context_requests + role packets can see it. Best-effort.
    try:
        from errorta_project_grounding.memory_store import MemorySourceRef, ProjectMemoryStore
        mem = ProjectMemoryStore(store.project_id, root=store.dir.parent)
        mem.admit_wip(
            source_type="context_request",
            source_ref=MemorySourceRef(task_id=task.task_id),
            content=(f"dev asked: {intent.question[:200]} "
                     f"({len(corpus_evidence)} corpus + {len(memory)} memory items)"),
            metadata={"status": "answered", "lower_authority": True})
    except Exception:
        pass
    return answer


def _latest_context_response_text(store: LedgerStore, task_id: str) -> str:
    """F088-09: deliver the most recent context response for THIS task back to the
    dev in a dedicated typed channel (the dev asked; it must receive the answer).
    Returns '' if there is none."""
    try:
        responses = [d["context_response"] for d in store.list_decisions()
                     if d.get("choice") == "context_request"
                     and task_id in (d.get("related_task_ids") or [])
                     and isinstance(d.get("context_response"), dict)]
    except Exception:
        return ""
    if not responses:
        return ""
    body = json.dumps(responses[-1], ensure_ascii=False, sort_keys=True)
    return ("\nContext response to YOUR earlier request (use this answer; cite "
            "refs):\n```json\n" + body + "\n```\n")


def _pm_boot_text(store: LedgerStore) -> str:
    """F088-08: on the FIRST PM turn only, a grounded boot briefing. First turn =
    no tasks have been created yet. Returns '' on later turns or with no grounding
    (then the PM gets the F088-07 role packet instead)."""
    try:
        if store.list_tasks():  # tasks already exist -> not the first PM turn
            return ""
    except Exception:
        return ""
    try:
        from errorta_project_grounding.context_packets import (
            build_pm_boot_briefing,
            format_pm_boot_briefing,
        )
        briefing = build_pm_boot_briefing(store=store)
        text = format_pm_boot_briefing(briefing)
        if text and briefing:
            fr = briefing.get("freshness") or {}
            _grounding_log.info(
                "grounding pm-boot: project=%s durable=%d corpus_evidence=%d "
                "corpus_status=%s open_wip=%d blockers=%d context_requests=%d "
                "warnings=%s", store.project_id,
                len(briefing.get("durable_truth") or []),
                len(briefing.get("corpus_evidence") or []),
                fr.get("corpus_retrieval"),
                len(briefing.get("open_wip") or []),
                len(briefing.get("blockers") or []),
                len(briefing.get("context_requests") or []),
                ",".join(briefing.get("warnings") or []) or "none")
        return text
    except Exception:
        return ""


def _skill_line(role: str) -> str:
    sk = primary_skill(role)
    return f"Operate under the '{sk}' discipline." if sk else ""


def _model_assignment_prompt(store: LedgerStore) -> str:
    """Bounded F129 catalog/pool evidence for the PM; metadata only."""
    try:
        from .model_catalog import load_catalog
        from .performance_corpus import digest

        members = [
            member for member in (store.get_run_config().get("members") or [])
            if isinstance(member, dict) and member.get("enabled", True)
        ]
        route_ids: list[str] = []
        team: list[dict[str, Any]] = []
        for member in members:
            mode = str(member.get("model_mode") or "single")
            pool = (
                [str(route) for route in member.get("model_pool", [])][:12]
                if mode == "multi"
                else [str(member.get("gateway_route_id") or "")]
            )
            pool = [route for route in pool if route]
            route_ids.extend(pool)
            team.append({
                "member_id": member.get("id"),
                "role": coding_role_of(member),
                "model_mode": mode,
                "routes": pool,
            })
        catalog = load_catalog(sorted(set(route_ids)))
        catalog_view = {
            route: {
                "capability": entry.capability_tier,
                "cost": entry.cost_tier,
            }
            for route, entry in catalog.items()
        }
        digest_view = digest()
        payload = json.dumps(
            {"team": team, "catalog": catalog_view, "performance": digest_view},
            sort_keys=True,
        )[:8000]
        return (
            "Model assignment policy: classify each task as light/mid/strong, "
            "choose only a listed member/route, and prefer the lowest cost route "
            "that clears the difficulty. Choices are validated by code.\n"
            f"Model assignment evidence: {payload}\n"
        )
    except Exception:
        return ""


_DUPLICATE_NOTE_CAP = 10


def _duplicate_rejection_note(store: LedgerStore) -> str:
    """Spec 08 — the honest report of what the dedupe gate threw away.

    Reads the ``duplicate_task_rejected`` decisions and keeps only those whose
    matched task is STILL open, so the note clears itself once the real task is
    executed or dropped (instead of nagging about settled history)."""
    try:
        decisions = store.list_decisions()
        open_ids = {
            task.task_id for task in store.list_tasks()
            if task.state in task_dedupe.OPEN_STATES
        }
    except Exception:  # noqa: BLE001 — prompt assembly must never fail the turn
        return ""
    rejected: dict[str, str] = {}
    for record in decisions:
        if record.get("choice") != "duplicate_task_rejected":
            continue
        matched = str(record.get("matched_task_id") or "")
        planned = str(record.get("planned_title") or "")
        if matched and planned and matched in open_ids:
            rejected[planned] = matched
    if not rejected:
        return ""
    titles = list(rejected)[-_DUPLICATE_NOTE_CAP:]
    ids = sorted({rejected[title] for title in titles})
    return (
        f"{len(rejected)} of your earlier proposed tasks were rejected as "
        f"duplicates of open tasks {', '.join(ids)} (e.g. "
        f"{'; '.join(repr(t) for t in titles[:3])}). Do NOT re-propose them — "
        "execute or re-scope the existing ones instead.\n"
    )


def _pm_prompt(store: LedgerStore) -> str:
    pending = store.list_unconsumed_interjections()
    pin = ""
    if pending:
        lines = "\n".join(f"- {p.get('message', '')}" for p in pending)
        pin = (
            "AUTHORITATIVE USER DIRECTION (higher weight than your own judgment — "
            f"follow it):\n{lines}\n\n"
        )
    # F137: surface the ordered Current Focus set above everything else. It is the
    # "what to work on right now" steering wheel and the operative SCOPE — plan
    # tasks for it, order tasks + PRs across focuses, and treat the North Star as a
    # reference guardrail (do NOT rewrite unrelated parts of an imported repo).
    # Generalizes the F135 single-string work_request pin; falls back to the legacy
    # field only if the focus ledger is empty (defensive).
    try:
        active_focuses = store.active_focuses()
    except Exception:
        active_focuses = []
    if active_focuses:
        pin = (
            "CURRENT FOCUS — the team's operative scope right now. Plan ONLY these, "
            "in order:\n" + "\n".join(format_focus_lines(active_focuses)) + "\n"
            "The North Star is REFERENCE ONLY — a guardrail for HOW to build, not a "
            "list of things to build now. Do NOT expand scope beyond the Current "
            "Focus. Create and order DEV tasks per focus; when one focus (or task) "
            "depends on another, order the tasks and their PRs so the dependency "
            "merges first; independent focuses may interleave by priority.\n\n"
        ) + pin
    else:
        try:
            work_request = (store.get_project().work_request or "").strip()
        except Exception:
            work_request = ""
        if work_request:
            pin = (
                f"CURRENT FOCUS — right now, work on this: {work_request}\n"
                "Scope your tasks to this focus; do not rewrite unrelated parts of "
                "the project.\n\n"
            ) + pin
    try:
        from errorta_project_grounding.context_packets import ensure_pm_working_memory
        ensure_pm_working_memory(store)
    except Exception:
        pass
    # F128: if the backlog still has open work, the PM may NOT declare done — tell
    # it exactly what's open so it finishes or explicitly cancels obsolete items.
    done_gate = ""
    open_items = pending_completion_work(store)
    if open_items:
        done_gate = (
            "You may NOT declare the project done — these items are still open: "
            f"{summarize_open_items(open_items)}. Finish them. If an item is "
            "obsolete, identify it in a decision for the operator to drop; the "
            "current PM plan schema has no cancel intent. An item marked "
            "(human-required) — a "
            "blocked task or a conflicted PR — cannot be auto-closed; leave it and "
            "the run will surface it for the human.\n"
        )
    # Spec 08: tell the PM its proposals were rejected as duplicates. Without
    # this it re-proposes the same job forever — it cannot see the gate.
    done_gate = f"{done_gate}{_duplicate_rejection_note(store)}"
    return _register_pending_composition(
        _pm_prompt_segments(store, pin=pin, done_gate=done_gate))


def _pm_prompt_segments(store: LedgerStore, *, pin: str,
                        done_gate: str) -> list[PromptSegment]:
    """F143-01 Slice F: the PM prompt as ordered labeled segments. Joined verbatim
    (``join_segments``) this equals the pre-refactor ``_pm_prompt`` string byte-for-
    byte (golden-locked). ``pin``/``done_gate`` are computed by ``_pm_prompt`` (their
    logic is unchanged) and passed in so both callers share one code path."""
    role_head = (
        f"{done_gate}{_skill_line(PM)} You are the PM of an autonomous coding team.\n"
    )
    instructions = (
        "Plan the next batch of DEV tasks only — each task is a unit of code a "
        "developer implements. Review, testing, and merge happen AUTOMATICALLY "
        "for every task (each opened PR is reviewed, tested, and merged into "
        "master), so do NOT create reviewer/tester/merge tasks. Keep tasks small "
        "and ordered (use depends_on by title when one builds on another). "
        # F142 WS-B: the foundation slice should ship a dependency manifest when the
        # stack pulls in third-party packages (e.g. requirements.txt for a Python
        # project that imports pygame) so real deps are captured — belt-and-suspenders
        # alongside the ecosystem-aware foundation gate.
        "If any task uses third-party packages, have the foundation task also add "
        "the matching dependency manifest (e.g. requirements.txt / package.json). "
        # F127: the PM carries the team's intelligence so a WEAKER worker model can
        # one-shot each task — this instruction lives IN the prompt (not a comment).
        "Make each task easy for a weaker model: one self-contained responsibility "
        "per task, with the acceptance criteria and the exact files/interfaces in "
        "scope written in its detail — the more you specify, the less the worker "
        "has to guess. Reply "
        "with ONLY a coding_turn.v1 envelope: "
        '{"schema_version": "coding_turn.v1", "role": "pm", "intent": '
        '{"kind": "plan", "done": false, "tasks": [{"title": "...", '
        '"role": "dev", "detail": "Acceptance criteria... In-scope files...", '
        '"depends_on": [], "task_type": "implementation", '
        '"difficulty_tier": "mid", "preferred_member_id": "m-dev", '
        '"preferred_route_id": "provider.model", '
        '"assignment_rationale": "why this is the cheapest capable route"}]}}. '
        'Set done=true ONLY when the North Star is fully met and nothing remains '
        "(then include a non-empty \"completion_summary\" and omit tasks)."
    )
    return [
        # CURRENT FOCUS / authoritative user direction — the operative scope.
        PromptSegment("work_request", pin),
        # Role identity + the done-gate instruction block.
        PromptSegment("role_instructions", role_head),
        # F129 model-assignment catalog/pool evidence (metadata for the PM).
        PromptSegment("tool_guidance", _model_assignment_prompt(store)),
        # Project orientation state.
        PromptSegment("project_context",
                      f"Project state: {_orientation_text(store)}\n"),
        # F088-08 boot briefing on the first PM turn; otherwise the F088-07 packet.
        PromptSegment("project_context",
                      _pm_boot_text(store) or _grounding_packet_text("pm", store)),
        # The standing PM planning instructions + envelope schema.
        PromptSegment("role_instructions", instructions),
    ]


def _pm_assist_prompt(store: LedgerStore, task: Task) -> str:
    extras = getattr(task, "_extras", {}) or {}
    excluded = ", ".join(extras.get("excluded_member_ids") or []) or "worker team"
    return (
        f"{_skill_line(PM)} You are the PM backstop for a coding task that the "
        "worker team could not execute.\n"
        f"Project state: {_orientation_text(store)}\n"
        f"Stuck task: {task.title}\nDetail: {task.detail}\n"
        f"Workers already tried: {excluded}.\n"
        "Split or re-scope this task into smaller self-contained DEV tasks. Each "
        "replacement must include explicit acceptance criteria and exact files or "
        "interfaces in scope. Do not declare the project done and do not retry the "
        "same task unchanged. Reply with ONLY a coding_turn.v1 PM plan envelope: "
        '{"schema_version":"coding_turn.v1","role":"pm","intent":'
        '{"kind":"plan","done":false,"tasks":[{"title":"...",'
        '"role":"dev","detail":"Acceptance criteria... Files...",'
        '"depends_on":[]}]}}.'
    )


def _materialize_pm_tasks(
    store: LedgerStore, intent: Any, *, parent_task: Task | None = None
) -> list[Task]:
    """Create a PM plan batch and resolve title/path dependencies."""
    all_tasks = store.list_tasks()
    existing_title_to_id = {task.title: task.task_id for task in all_tasks}
    created: list[tuple[Task, list[str]]] = []
    title_to_id = dict(existing_title_to_id)
    # Spec 08 — the dedupe gate. `existing_title_to_id` above maps EVERY task
    # (it must: `depends_on` may name a finished prerequisite by title), but the
    # duplicate test may only consider OPEN work — re-doing a `done`/`dropped`
    # task is legitimate. Hence a second, open-only index.
    #
    # The re-scope (PMAssist) path excludes its own parent, exactly as
    # `path_owners` does below: the parent is still open right now but is
    # dropped moments later, and its replacements are *supposed* to restate its
    # job in smaller pieces. Deduping against it would reject the re-scope and
    # wedge the stuck task permanently.
    open_index = [
        entry for entry in task_dedupe.build_open_index(all_tasks)
        if parent_task is None or entry.task_id != parent_task.task_id
    ]
    path_owners = _active_dev_path_owners(store)
    if parent_task is not None:
        path_owners = {
            path: owner
            for path, owner in path_owners.items()
            if owner != parent_task.task_id
        }
    inherited_deps = list(parent_task.depends_on) if parent_task is not None else []
    for planned in intent.tasks:
        paths = _declared_target_paths(planned.title, planned.detail)
        path_deps = [
            owner for _path, owner in sorted(
                (path, path_owners[path]) for path in paths if path in path_owners
            )
        ]
        # Spec 08: reject a planned task that is materially the same job as an
        # already-open one. The rejection is recorded as a decision (auditable +
        # renderable), the title still resolves — onto the MATCHED id — so a
        # sibling's `depends_on` keeps working, and crucially the task never
        # enters `created`, so `made_progress` goes False when the whole batch
        # was duplicates. That re-arms the pm_idle / NO_PROGRESS detector
        # instead of letting a churning PM look productive.
        duplicate = task_dedupe.find_duplicate(
            open_index, title=planned.title, role=DEV, paths=paths)
        if duplicate is not None:
            title_to_id[planned.title] = duplicate.task_id
            store.record_decision(
                title=f"duplicate task rejected: {planned.title}",
                context="task_dedupe",
                choice="duplicate_task_rejected",
                rationale=duplicate.rationale(planned.title),
                related_task_ids=[duplicate.task_id],
                extra={
                    "planned_title": planned.title,
                    "matched_task_id": duplicate.task_id,
                    "matched_title": duplicate.title,
                    "rule": duplicate.rule,
                    "similarity": round(duplicate.similarity, 3),
                },
            )
            continue
        task = store.add_task(
            title=planned.title,
            # F087-18: PM plans DEV work only. Review/test/merge are auto-driven
            # by the coding team topology (reviewer/tester members pull PRs off
            # the queue). A PM that names a reviewer/tester role for a planned
            # task is proposing work the topology already handles — coerce it
            # to dev so the review/test loop still runs. Kept independent of
            # the F129 model_assignment surface: proposals in ``planned`` are
            # validated separately by ``model_assignment.build_assignment``.
            role="dev",
            detail=planned.detail,
            parent_task_id=parent_task.task_id if parent_task is not None else None,
            source_spec_artifact_id=(
                parent_task.source_spec_artifact_id if parent_task is not None else None
            ),
            source_plan_artifact_id=(
                parent_task.source_plan_artifact_id if parent_task is not None else None
            ),
            source_slice_id=(
                parent_task.source_slice_id if parent_task is not None else None
            ),
            governance_required=(
                parent_task.governance_required if parent_task is not None else False
            ),
            task_type=planned.task_type,
            difficulty_tier=planned.difficulty_tier,
            preferred_member_id=planned.preferred_member_id,
            preferred_route_id=planned.preferred_route_id,
            assignment_rationale=planned.assignment_rationale,
        )
        title_to_id[planned.title] = task.task_id
        # A second identical proposal in the SAME batch must be caught too.
        open_index.append(task_dedupe.index_entry(
            task_id=task.task_id, title=planned.title, role=DEV, paths=paths))
        # Spec 09 §4 — bound the path-owner chaining amplifier. This used to be
        # `path_owners[path] = task.task_id`, which OVERWRITES the owner, so
        # sibling 2 inherited from sibling 1, sibling 3 from sibling 2, ... —
        # a serial LINE through the whole batch. One wedged head then held the
        # entire backlog hostage (the observed 130-task deadlock). `setdefault`
        # caps the inherited path-dep depth at 1: every toucher of a path hangs
        # off the SINGLE oldest live owner (a pre-existing task when there is
        # one, else the first sibling in this batch to claim it), never off
        # another inheritor. Same rule `_active_dev_path_owners` already applies
        # across batches, so within-batch and cross-batch now agree.
        for path in paths:
            path_owners.setdefault(path, task.task_id)
        created.append(
            (task, inherited_deps + list(planned.depends_on) + path_deps)
        )
    for task, dependencies in created:
        resolved: list[str] = []
        for dependency in dependencies:
            dependency_id = title_to_id.get(dependency, dependency)
            if dependency_id and dependency_id not in resolved:
                resolved.append(dependency_id)
        if resolved:
            store.update_task(task.task_id, depends_on=resolved)
    return [task for task, _dependencies in created]


def _dep_graph(store: LedgerStore) -> dict[str, list[str]]:
    return {t.task_id: list(t.depends_on or []) for t in store.list_tasks()}


def _reaches(graph: dict[str, list[str]], start: str, target: str) -> bool:
    """True if ``target`` is reachable from ``start`` along ``depends_on`` edges."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def _repoint_dropped_dependents(
    store: LedgerStore, dropped_task_id: str, replacement_ids: list[str],
) -> list[str]:
    """Spec 09 §2 — rewrite dependents when ``dropped_task_id`` is dropped.

    Every task whose ``depends_on`` names the dropped id has that id replaced by
    the superseding ids (or simply removed when the drop created no
    replacements). Without this the dependents point at a task that can never
    reach ``done`` — the dependency deadlock. (`_DEP_SATISFIED_STATES` already
    unblocks dispatch; this keeps the recorded graph honest so the ordering the
    PM intended still holds against the replacement work.)

    Never introduces a cycle: a replacement is skipped when the dependent is
    already reachable FROM it (self-dependency included) — the replacement can
    inherit a path/parent dep on the very task being re-pointed. Returns the ids
    of the tasks that were rewritten."""
    graph = _dep_graph(store)
    touched: list[str] = []
    for task in store.list_tasks():
        if task.task_id == dropped_task_id:
            continue
        deps = list(task.depends_on or [])
        if dropped_task_id not in deps:
            continue
        rewritten: list[str] = []
        for dep in deps:
            if dep != dropped_task_id:
                if dep not in rewritten:
                    rewritten.append(dep)
                continue
            for replacement in replacement_ids:
                if not replacement or replacement in rewritten:
                    continue
                if _reaches(graph, replacement, task.task_id):
                    continue  # would close a cycle — drop the edge instead
                rewritten.append(replacement)
        if rewritten == deps:
            continue
        store.update_task(task.task_id, depends_on=rewritten)
        graph[task.task_id] = rewritten
        touched.append(task.task_id)
    return touched


def _dev_prompt(task: Task, store: LedgerStore, readback: str = "") -> str:
    # F087-17: the dev works on its own branch off master. The current contents
    # of the worktree (everything merged so far) are inlined so the dev EXTENDS
    # the project instead of regenerating a file from scratch and clobbering
    # prior work. code_write replaces the WHOLE file, so it must include
    # everything that should remain.
    return _register_pending_composition(_dev_prompt_segments(task, store, readback))


def _dev_prompt_segments(task: Task, store: LedgerStore,
                         readback: str = "") -> list[PromptSegment]:
    """F143-01 Slice F: the DEV prompt as ordered labeled segments. Joined verbatim
    this equals the pre-refactor ``_dev_prompt`` string byte-for-byte (golden-locked)."""
    existing = (f"Current files in the worktree (EXTEND these — do not drop "
                f"existing code; code_write replaces the whole file so include "
                f"all of it):\n{readback}\n" if readback
                else "The worktree is empty; create the files from scratch.\n")
    envelope = (
        "Implement the task via tool-backed writes; preserve all prior functions. "
        # F101-03: the runtime injects a free PORT env var and expects the server
        # to bind it; a hardcoded port collides (e.g. macOS AirPlay owns :5000) and
        # the demo/health probe then points where nothing is listening.
        "If you write a web server, read its listen port from the PORT environment "
        "variable (with a sensible default) instead of hardcoding one, so the "
        "runtime can bind a free port. "
        # Binary assets: the code_write channel is UTF-8 text by default, so a
        # binary file (image/font/audio) written as text is corrupt and crashes
        # the engine at load. Emit real bytes via content_base64 instead.
        "A binary asset (an image, font, audio clip, or any non-text file — e.g. a "
        "PNG sprite or tileset) MUST be written as REAL bytes: emit code_write with "
        '{"path": "...", "content_base64": "<base64 of the actual file bytes>"} '
        "(never a text description or placeholder in a binary file body — an "
        "undecodable .png is not a valid image). "
        "Reply with ONLY a coding_turn.v1 envelope: "
        '{"schema_version": "coding_turn.v1", "role": "dev", "task_id": '
        f'"{task.task_id}", "intent": {{"kind": "tool_plan", "task_type": '
        '"implementation", "tool_calls": [{"tool": "code_write", "args": '
        '{"path": "rel/path", "content": "..."}}]}}.'
    )
    return [
        # Role identity + THIS task's title/detail (the work request).
        PromptSegment(
            "work_request",
            f"{_skill_line(DEV)} You are a developer for task id {task.task_id!r}: "
            f"{task.title}. {task.detail}\n"),
        # Project orientation state.
        PromptSegment("project_context",
                      f"Context: {_orientation_text(store)}\n"),
        # Retrieved project grounding for the dev.
        PromptSegment("project_context",
                      _grounding_packet_text("dev", store, task=task)),
        # Prior PM/reviewer context response threaded to this task.
        PromptSegment("prior_outputs",
                      _latest_context_response_text(store, task.task_id)),
        # The current worktree snapshot the dev extends.
        PromptSegment("repo_snapshot", existing),
        # Tool catalog / how-to-emit-tool-calls guidance.
        PromptSegment("tool_guidance",
                      f"{tool_catalog_text(DEV)} Do not request merge-back.\n"),
        # Standing implement instructions + envelope schema.
        PromptSegment("role_instructions", envelope),
    ]


# The reviewer must see enough of a PR to judge it. A single complete source
# file (e.g. a game.py) routinely exceeds a small cap, and a reviewer shown a
# diff cut off mid-file correctly REFUSES to approve ("cannot verify the rest")
# — which permanently wedges the PR in changes_requested and (for a `new`
# project) never lets the foundation merge, pinning worker concurrency at 1.
# Size the cap to hold a normal multi-file slice; when a diff still overflows,
# the prompt tells the reviewer truncation is a tooling limit, not a defect.
_REVIEW_DIFF_CAP = 48000

# F087-18 #5: generated/build files that must never enter the review context.
_GENERATED_RE = re.compile(
    r"(^|/)(__pycache__/|\.pytest_cache/|\.mypy_cache/|\.ruff_cache/|"
    r"node_modules/|dist/|build/|.*\.egg-info/)|\.(pyc|pyo)$|(^|/)\.DS_Store$")


def _filter_generated_from_diff(diff: str) -> str:
    """Drop per-file sections for generated/build artifacts so the reviewer never
    sees __pycache__/*.pyc etc. (belt-and-suspenders over the worktree .gitignore;
    also covers existing-repo diffs)."""
    out: list[str] = []
    keep = True
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            path = parts[2][2:] if len(parts) >= 3 and parts[2].startswith("a/") else ""
            keep = not _GENERATED_RE.search(path)
        if keep:
            out.append(line)
    return "\n".join(out)


def _task_is_governance_sourced(task: Task) -> bool:
    """Whether ``task`` was materialized from F100 governance planning (has an
    approved slice / plan / spec provenance). Single source of truth for the
    "governance-sourced" test (also used by ``_governance_review_context``)."""
    return bool(
        task.source_plan_artifact_id
        or task.source_spec_artifact_id
        or task.source_slice_id
    )


def _review_pr_prompt(task: Task, pr: dict[str, Any], diff: str,
                      project_context: str, scope_task: Task | None = None) -> str:
    diff = _filter_generated_from_diff(diff)
    cap = diff[:_REVIEW_DIFF_CAP]
    truncated = len(diff) > _REVIEW_DIFF_CAP
    trunc = " [diff truncated]" if truncated else ""
    trunc_note = (
        "The diff above was truncated to fit — code beyond the cut is NOT shown. "
        "This is a tooling limit, not evidence of a source-code defect, but review "
        "coverage is incomplete and unseen code cannot be approved. Set approved "
        "to false and include one finding asking the author to split or reduce the "
        "change so its complete diff can be reviewed. Do not speculate about defects "
        "in code that is not shown.\n"
        if truncated else ""
    )
    # F142 WS-A: the reviewer judges THIS PR against its own task's scope, not
    # the whole North Star. `task` is the reviewer's OWN task ("review PR: ...");
    # the scope the PR must satisfy belongs to the DEV task under review, passed
    # as `scope_task` (fetched from `pr["task_id"]` at the call site). The task_id
    # echoed in the envelope stays the reviewer task. For legacy/simple tasks the
    # scope is the dev task's title/detail; for governance-sourced tasks the slice
    # `done_when` / `review_focus` (already injected via `project_context` /
    # `_governance_review_context`) is the acceptance bar — point the instruction
    # at it. The North Star / Definition of Done in `project_context` are
    # directional context only, never the completion bar (this is what stops the
    # false "the product isn't done yet" rejection of a correct foundation slice).
    st = scope_task or task
    task_scope = f"{st.title}. {st.detail}".strip()
    if _task_is_governance_sourced(st):
        bar = ("This task is governance-sourced: its acceptance bar is the plan "
               "slice's done_when / review_focus in the Governance planning "
               "context above (fall back to the task scope if that is absent).")
    else:
        bar = ("Its acceptance bar is the task scope stated above.")
    example_findings = ([{
        "severity": "major",
        "title": "Diff exceeds review context",
        "body": "Split or reduce this change so the complete diff can be reviewed.",
    }] if truncated else [])
    verdict_example = json.dumps({
        "schema_version": "coding_turn.v1",
        "role": "reviewer",
        "task_id": task.task_id,
        "intent": {
            "kind": "review_verdict",
            "reviewed_head": pr.get("head"),
            "approved": not truncated,
            "findings": example_findings,
        },
    })
    return _register_pending_composition(_review_pr_prompt_segments(
        task, pr, project_context, task_scope=task_scope, bar=bar, cap=cap,
        trunc=trunc, trunc_note=trunc_note, verdict_example=verdict_example))


def _review_pr_prompt_segments(
        task: Task, pr: dict[str, Any], project_context: str, *,
        task_scope: str, bar: str, cap: str, trunc: str, trunc_note: str,
        verdict_example: str) -> list[PromptSegment]:
    """F143-01 Slice F: the reviewer prompt as ordered labeled segments. Joined
    verbatim this equals the pre-refactor ``_review_pr_prompt`` string byte-for-byte
    (golden-locked). The branchy truncation/scope logic stays in ``_review_pr_prompt``;
    the derived strings are passed in so both callers share one assembly path."""
    review_rules = (
        "This PR implements ONE scoped task, not the whole product. "
        f"{bar}\n"
        "REQUEST CHANGES (blocking) if EITHER holds:\n"
        "(a) the change does not correctly AND fully implement THIS task's own "
        "stated scope — a partial or incorrect implementation of THIS task (e.g. "
        "the task names three classes and only two are present) IS a defect and "
        "must be sent back; or\n"
        "(b) the change breaks or drops any code already on master, or introduces "
        "a contract mismatch — a type/signature/import inconsistent with the "
        "merged surface OR with an incompatible shared type another in-flight PR "
        "defines. When you see such a mismatch you MUST write a finding naming it "
        "(that is how the shared contract gets centralized).\n"
        "NOT a reason to request changes: the overall product being incomplete, "
        "or functionality that belongs to OTHER tasks being absent. Sibling tasks "
        "listed as in-flight or todo/backlog will deliver the rest — that is "
        "out-of-scope future work, not a defect in this PR. Distinguish 'missing "
        "part of THIS task's scope' (block) from 'missing another task's work' "
        "(fine). The North Star / Definition of Done are directional context only.\n"
    )
    envelope = (
        f"The PR head you are reviewing is {pr.get('head')!r}; echo it verbatim as "
        '"reviewed_head".\n'
        "Reply with ONLY a coding_turn.v1 envelope: "
        f"{verdict_example}. "
        "If approved=false you MUST include at least one finding."
    )
    return [
        # Role identity + which PR/branch is under review.
        PromptSegment(
            "role_instructions",
            f"{_skill_line(REVIEWER)} You are a reviewer for task id {task.task_id!r}. "
            f"Review this PR (branch {pr.get('branch')}) before it merges to master.\n"),
        # The scoped task the PR must satisfy (the review's work request).
        PromptSegment("work_request",
                      f"The scope of THIS PR is ONE task: {task_scope}\n"),
        # North Star / merged surface / grounding — the project context.
        PromptSegment("project_context", project_context),
        # Standing review rules + acceptance bar.
        PromptSegment("role_instructions", review_rules),
        # The PR diff under review (+ optional truncation flag).
        PromptSegment("pr_diff", f"PR diff vs master{trunc}:\n```diff\n{cap}\n```\n"),
        # Truncation caveat (empty when the diff fit).
        PromptSegment("role_instructions", trunc_note),
        # reviewed_head echo instruction + verdict envelope schema.
        PromptSegment("role_instructions", envelope),
    ]


# --- F146 Slice B: delivery review of the integrated head --------------------

# A stable synthetic task_id for delivery-review turns (the reviewer echoes it;
# parse_coding_turn requires envelope.task_id == this for a non-PM role).
_DELIVERY_TASK_ID = "delivery-review"


class DeliveryReviewResult(NamedTuple):
    """Outcome of verifying the INTEGRATED delivered head as a unit.

    ``passed`` gates whether ``project_done`` is allowed to stick. ``filed_findings``
    tells the loop whether real rework was queued — a fail that queued dev tasks is
    *progress* (the run re-opens to work them), a fail that could not verify and
    queued nothing counts toward the no-progress stop. ``reason`` is diagnostic."""
    passed: bool
    filed_findings: bool = False
    reason: str = ""


def _delivery_review_prompt(store: LedgerStore, head: str, diff: str) -> str:
    """Ask a reviewer to judge the COMPLETE delivered diff as one integrated unit
    (integration correctness the per-PR reviews cannot see), bound to the delivered
    head. Emits the SAME coding_turn.v1 reviewer envelope as ``_review_pr_prompt``
    so ``parse_coding_turn(REVIEWER, ...)`` validates it; only the framing differs
    (delivery-wide vs one scoped task)."""
    diff = _filter_generated_from_diff(diff)
    cap = diff[:_REVIEW_DIFF_CAP]
    truncated = len(diff) > _REVIEW_DIFF_CAP
    trunc = " [diff truncated]" if truncated else ""
    trunc_note = (
        "The diff above was truncated to fit — code beyond the cut is NOT shown. "
        "Coverage is incomplete, so set approved=false with a finding asking to "
        "reduce/split the delivered change so its full diff can be reviewed.\n"
        if truncated else "")
    try:
        project = store.get_project()
        north_star = str(getattr(project, "north_star", "") or "")
        dod = str(getattr(project, "definition_of_done", "") or "")
    except Exception:  # noqa: BLE001 — prompt context is best-effort
        north_star = dod = ""
    verdict_example = json.dumps({
        "schema_version": "coding_turn.v1",
        "role": "reviewer",
        "task_id": _DELIVERY_TASK_ID,
        "intent": {
            "kind": "review_verdict",
            "reviewed_head": head,
            "approved": not truncated,
            "findings": ([{
                "severity": "major",
                "title": "Delivered change exceeds review context",
                "body": "Reduce the delivered change so its complete diff can be "
                        "reviewed.",
            }] if truncated else []),
        },
    })
    return (
        f"{_skill_line(REVIEWER)} You are the DELIVERY reviewer. The team believes "
        "the project is complete; review the ENTIRE delivered change as a single "
        f"integrated unit for task id {_DELIVERY_TASK_ID!r} before it is marked "
        "done.\n"
        f"North Star: {north_star}\n"
        f"Definition of Done: {dod}\n"
        "REQUEST CHANGES (approved=false, at least one finding) if the delivered "
        "code, taken as a whole, does not meet the Definition of Done, OR has an "
        "INTEGRATION defect the per-PR reviews could miss: a contract mismatch "
        "between merged parts (a type/signature/import inconsistent across the "
        "integrated code), a missing import, or code that cannot run as assembled. "
        "Approve only if the whole delivered result is correct, consistent, and "
        "complete. Do NOT request changes merely because more features could be "
        "added — judge against the Definition of Done.\n"
        f"Delivered diff vs the project base{trunc}:\n```diff\n{cap}\n```\n"
        f"{trunc_note}"
        f"The delivered head you are reviewing is {head!r}; echo it verbatim as "
        '"reviewed_head".\n'
        "Reply with ONLY a coding_turn.v1 envelope: "
        f"{verdict_example}. If approved=false you MUST include at least one "
        'finding. Each finding is an object {"severity":"minor|major|blocking",'
        '"title":"...","body":"...","path":"..."}.\n'
    )


# --- F146 Slice C: runtime launch evidence for the delivered head ------------

def _delivery_launch_evidence(
    store: LedgerStore, workspace: Any, head: str,
    *, should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[bool, bool, str]:
    """F146 Slice C: LAUNCH the delivered runnable program headless + bounded and
    classify it, as part of the delivery review. Catches runtime-only crashes the
    per-PR reviews + unit tests miss (the ``pygame.font`` case) — a runnable
    project is not truly ``done`` until its delivered head launches without
    crashing on startup.

    Returns ``(launched_clean, cannot_verify, detail)``:

    * a **non-runnable** project (no launchable ``managed_local`` runtime profile)
      -> ``(True, False, "")`` — the launch probe is skipped, vacuously clean
      (exactly the pre-Slice-C behavior);
    * a **clean launch** -> ``(True, False, detail)``;
    * a **startup crash / non-zero exit** -> ``(False, False, detail)`` — a real
      code finding (the caller files a dev task and blocks ``done``);
    * an **inability to launch** (setup/sandbox/spawn failure, cancel) ->
      ``(False, True, detail)`` — a verify error (the caller blocks ``done`` and
      records no clean evidence, so the next completion claim retries).

    Never rubber-stamps: a clean verdict comes only from a real launch of the exact
    delivered ``head`` under the F039 sandbox. Fully guarded — a failure to even
    build the launch machinery is fail-closed (``cannot_verify``), except that a
    failure to *enumerate* profiles relaxes to "no runtime" (mirroring Slice D's
    ``_has_runnable_runtime`` guard) so the reviewer + tests still gate."""
    try:
        from .runtime import RuntimeProfileStore
        rstore = RuntimeProfileStore.for_ledger(store)
        profiles = rstore.list_profiles()
    except Exception:  # noqa: BLE001 — can't enumerate -> treat as no runtime
        return True, False, ""
    runnable = [
        p for p in profiles
        if getattr(p, "runtime_mode", "") == "managed_local"
        and getattr(p, "start", None)
    ]
    if not runnable:
        return True, False, ""  # non-runnable: launch probe skipped (vacuously clean)
    try:
        from .runtime_process import RuntimeProcessManager
        mgr = RuntimeProcessManager(
            project_id=store.project_id, rstore=rstore,
            workspace_root=workspace.root(), work_root=store.dir)
    except Exception as exc:  # noqa: BLE001 — can't build the probe -> blocks done
        return False, True, f"launch probe error: {exc}"
    # Probe EVERY runnable managed_local profile (a hand-edited project may have
    # more than one — e.g. a backend + a frontend). Aggregate fail-closed: any
    # crash -> crashed; any inability -> cannot_verify; only all-clean is clean.
    # (Slice D counts any runnable profile toward tests_required, so a single
    # unprobed runnable profile must not slip a startup crash past `done`.)
    crashed_detail = ""
    cannot_verify_detail = ""
    for profile in runnable:
        try:
            result = mgr.launch_probe(profile.profile_id, head=head,
                                      should_cancel=should_cancel)
        except Exception as exc:  # noqa: BLE001 — any probe failure blocks done
            cannot_verify_detail = f"launch probe error: {exc}"
            continue
        status = str(result.get("status"))
        detail = f"{profile.profile_id}: {result.get('detail', '')}"
        if status == "crashed":
            crashed_detail = detail if not crashed_detail else f"{crashed_detail}; {detail}"
        elif status == "cannot_verify":
            cannot_verify_detail = detail
        # "clean" / "skipped" contribute nothing to a block.
    if crashed_detail:
        return False, False, crashed_detail   # a real crash: a code finding
    if cannot_verify_detail:
        return False, True, cannot_verify_detail  # inability to launch: verify error
    return True, False, ""  # all runnable profiles launched clean (or were skipped)


def _governance_review_context(store: LedgerStore, task: Task) -> str:
    """Approved planning context for reviewer prompts when F100 governance made
    the task. Empty for legacy/off-mode tasks."""
    if not _task_is_governance_sourced(task):
        return ""
    try:
        from .governance import GovernanceStore
        from .governance_materialize import plan_slice_for_task
        governance = GovernanceStore.for_ledger(store)
        parts: list[str] = []
        if task.source_spec_artifact_id:
            spec = governance.get_artifact(task.source_spec_artifact_id)
            if spec is not None:
                parts.append(
                    "Approved spec artifact "
                    f"{spec.artifact_id} ({spec.title}):\n"
                    f"{spec.body_markdown[:3000]}"
                )
        if task.source_plan_artifact_id:
            plan = governance.get_artifact(task.source_plan_artifact_id)
            if plan is not None:
                parts.append(
                    "Approved implementation plan artifact "
                    f"{plan.artifact_id} ({plan.title})."
                )
        plan_slice = plan_slice_for_task(store, task.task_id)
        if plan_slice is not None:
            parts.append(
                "Plan slice under review:\n"
                f"- id: {plan_slice.slice_id}\n"
                f"- title: {plan_slice.title}\n"
                f"- done_when: {'; '.join(plan_slice.done_when) or 'none'}\n"
                f"- tests: {'; '.join(plan_slice.tests) or 'none'}\n"
                f"- review_focus: {'; '.join(plan_slice.review_focus) or 'none'}"
            )
        if not parts:
            return ""
        return "Governance planning context:\n" + "\n\n".join(parts) + "\n"
    except Exception:
        return ""


def _strict_governance(store: LedgerStore) -> bool:
    """Whether F100 governance is in ``strict`` mode (the reviewer AND the PM
    must both approve). Fully guarded — defaults to False (today's behavior)."""
    try:
        from .governance import GovernanceStore
        return GovernanceStore.for_ledger(store).load_state().mode == "strict"
    except Exception:
        return False


def _open_pm_review_task(store: LedgerStore, pr_id: str) -> Task | None:
    """An un-finished PM PR-review task for ``pr_id`` (F100 PR-B), or None.
    Used to avoid spawning a duplicate PM review when the reviewer re-approves."""
    for t in store.list_tasks():
        if (t.role == PM and t.pr_id == pr_id and t.state not in ("done", "dropped")
                and str(t.title or "").lower().startswith("review pr:")):
            return t
    return None


def _strict_governance_merge_blocker(store: LedgerStore, task: Task | None) -> str:
    """Return a human-readable blocker when a strict-governance task has lost
    its approved planning provenance before merge."""
    if task is None:
        return ""
    try:
        from .governance import GovernanceStore
        governance = GovernanceStore.for_ledger(store)
        state = governance.load_state()
        if state.mode != "strict" and not task.governance_required:
            return ""
        if not task.source_plan_artifact_id or not task.source_slice_id:
            return "strict governance task has no source plan slice"
        plan = governance.get_artifact(task.source_plan_artifact_id)
        if plan is None or plan.state != "approved":
            return "source implementation plan is not approved"
        if task.source_spec_artifact_id:
            spec = governance.get_artifact(task.source_spec_artifact_id)
            if spec is None or spec.state != "approved":
                return "source spec is not approved"
        if not any(s.slice_id == task.source_slice_id for s in governance.plan_slices(plan)):
            return "source plan slice no longer exists"
    except Exception as exc:
        return f"strict governance check failed: {exc}"
    return ""


def _review_project_context(store: LedgerStore, workspace: Any,
                            pr: dict[str, Any]) -> str:
    """North Star + Definition of Done + open blockers + the post-merge file set,
    so the reviewer is NOT task-local (F087-18 #3)."""
    try:
        proj = store.get_project()
        north, dod = proj.north_star, proj.definition_of_done
    except Exception:
        north, dod = "", ""
    blockers = [t.title for t in store.list_tasks(state="blocked")]
    # F139 WS-B: give the reviewer the TRUE merged surface (git truth from
    # `master`) plus the exact set of files THIS PR changes (adds + modifies +
    # deletes, from the PR branch's own diff vs master). The old code showed the
    # PR branch's whole file list — a branch cut from a stale master omitted
    # siblings' just-merged files and produced false "imports absent from master"
    # contract-mismatch rejections in the reddit-look-a-like run. The two reads
    # are independent (a transient failure of one never mislabels the other), and
    # `changed_paths` is derived from git so it reflects modifications, not just
    # additions.
    merged_files: list[str] = []
    changed_files: list[str] = []
    if workspace is not None:
        try:
            merged_files = [f for f in workspace.list_files(scope="master")
                            if f != ".gitignore"]
        except Exception:
            merged_files = []
        try:
            changed_files = [f for f in workspace.changed_paths(str(pr.get("branch", "")))
                             if f != ".gitignore"]
        except Exception:
            changed_files = []
    task = _fetch_task(store, str(pr.get("task_id") or ""))
    governance_context = _governance_review_context(store, task) if task else ""
    return (
        f"North Star: {north}\n"
        f"Definition of done: {dod}\n"
        f"Open blockers: {', '.join(blockers) if blockers else 'none'}\n"
        f"Project files currently on master (the merged API surface to keep "
        f"consistent): {', '.join(merged_files) if merged_files else '(none yet)'}\n"
        f"This PR changes: "
        f"{', '.join(changed_files) if changed_files else '(none)'}\n"
        f"{governance_context}"
        f"{_grounding_packet_text('reviewer', store, pr=pr)}"
    )


def _test_prompt(task: Task, store: LedgerStore) -> str:
    registry = store.get_test_commands()
    if registry:
        ids = ", ".join(sorted(registry.keys()))
        avail = (f"Available test command_ids (you MUST choose from these): {ids}.")
    else:
        avail = ("No test commands are configured for this project, so there is "
                 "nothing to run — reply with empty \"command_ids\": [] and "
                 "\"not_applicable\": true (the test gate is non-blocking).")
    return _register_pending_composition(_test_prompt_segments(task, store, avail=avail))


def _test_prompt_segments(task: Task, store: LedgerStore, *,
                          avail: str) -> list[PromptSegment]:
    """F143-01 Slice F: the tester prompt as ordered labeled segments. Joined verbatim
    this equals the pre-refactor ``_test_prompt`` string byte-for-byte (golden-locked).
    ``avail`` (the registered-command availability line) is computed by ``_test_prompt``
    and passed in so both callers share one assembly path."""
    instructions = (
        f"{avail} You CANNOT declare pass or fail — the verdict comes from the "
        "REAL exit code of the commands actually run.\n"
        "This PR implements ONE scoped task, not the whole product. If NO "
        "registered command meaningfully exercises THIS task's slice (e.g. the "
        "project is not yet runnable end-to-end and the full suite would only "
        "fail on not-yet-built modules), you MAY reply with an empty "
        '"command_ids": [] and "not_applicable": true plus a rationale; the '
        "test gate is then non-blocking for this slice. This is NOT a way to "
        "dodge a real failure: a command that runs and returns non-zero for a "
        "genuine in-scope defect still blocks — so if any registered command "
        "does exercise this slice, run it and let its exit code govern.\n"
        'Reply with ONLY a coding_turn.v1 envelope: {"schema_version": '
        f'"coding_turn.v1", "role": "tester", "task_id": "{task.task_id}", '
        '"intent": {"kind": "test_plan", "command_ids": ["<id>", ...], '
        '"scope": "full_project", "not_applicable": false, "rationale": '
        '"..."}}.'
    )
    return [
        # Role identity + which task is under test (the work request).
        PromptSegment(
            "work_request",
            f"{_skill_line(TESTER)} You are a tester for task id {task.task_id!r}: "
            f"{task.title}.\n"),
        # Project orientation state.
        PromptSegment("project_context",
                      f"Context: {_orientation_text(store)}\n"),
        # Retrieved project grounding for the tester.
        PromptSegment("project_context",
                      _grounding_packet_text("tester", store, task=task)),
        # Standing test instructions (available commands + envelope schema).
        PromptSegment("role_instructions", instructions),
    ]


def _fetch_task(store: LedgerStore, task_id: str) -> Optional[Task]:
    for t in store.list_tasks():
        if t.task_id == task_id:
            return t
    return None


def _governance_artifact_payload(
    intent: Any,
) -> tuple[str, str, str, dict[str, Any], list[str], str | None]:
    from .governance_schemas import (
        PMBrainstormDraftIntent,
        PMPlanDraftIntent,
        PMSpecDraftIntent,
    )

    if isinstance(intent, PMBrainstormDraftIntent):
        return (
            "brainstorm",
            intent.title,
            intent.markdown(),
            intent.artifact_body(),
            list(intent.source_refs),
            None,
        )
    if isinstance(intent, PMSpecDraftIntent):
        # F100 robustness: never persist a blank spec body. The schema requires a
        # non-empty body_markdown for a clean parse, but if it is somehow blank we
        # render the structured fields (title + acceptance criteria) so the human
        # can still read the spec instead of an empty box.
        return (
            "spec",
            intent.title,
            intent.body_markdown.strip() or intent.markdown(),
            intent.artifact_body(),
            list(intent.source_refs),
            intent.supersedes_artifact_id,
        )
    if isinstance(intent, PMPlanDraftIntent):
        return (
            "implementation_plan",
            intent.title,
            intent.markdown(),
            intent.artifact_body(),
            list(intent.source_refs),
            intent.supersedes_artifact_id,
        )
    raise TypeError(f"unsupported governance intent: {type(intent).__name__}")


# --- F139 WS-A: foundation gate (code-derived) ------------------------------- #

# A greenfield project's "foundation" = a recognized build manifest plus at least
# one source entrypoint, MERGED to master. Until that lands, worker concurrency is
# clamped to 1 (autonomy.runtime_cap) so the team scaffolds one coherent base
# before fanning out — the reddit-look-a-like run fanned out 3 devs onto a
# near-empty master and never integrated a foundation.
_BUILD_MANIFESTS = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "CMakeLists.txt", "Makefile",
)
_SOURCE_EXT = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".go", ".rs", ".java", ".kt", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift",
)
# F142 WS-B: ecosystems where a build manifest is genuinely load-bearing — node/web
# needs package.json to resolve imports/run; compiled ecosystems (go/rust/java/…)
# need a build file to produce an artifact. If master carries any of these, keep
# requiring a matching manifest (the reddit-look-a-like protection stays intact).
_MANIFEST_BOUND_EXT = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".swift",
)
# Interpreted "script-style" languages that run directly from a source file with no
# build/manifest step. A project whose only source is these (no node/web/compiled
# source) is a legitimate script deliverable (e.g. the pokemon `game.py` North Star)
# and is foundation-ready on an entrypoint alone.
_SCRIPT_EXT = (".py", ".rb", ".php", ".pl", ".lua", ".sh")
# Predicate (F142 WS-B, spec Risks — decided in review): script-style = NO
# manifest-bound source on master AND >=1 script entrypoint. The gate's job is
# "is there a coherent base to fan out onto"; for a script ecosystem an importable
# entrypoint IS that base, so we deliberately do NOT cap file count or require a
# flat tree. An earlier draft did (`<=3` flat files); review found it both too
# loose (a 2-file package lifted the clamp with no manifest) and too tight (a 4th
# helper file, or a README/LICENSE/asset subdir, re-clamped to 1 forever and
# re-fired `foundation_not_converging` — the exact bug WS-B exists to kill). Deps
# aren't load-bearing for THIS gate (the PM is nudged to add a manifest when
# third-party packages are used). A project that later grows node/web/compiled
# source flips back to requiring a manifest via `refresh_foundation_status`.


def foundation_ready(store: LedgerStore, workspace: Any) -> bool:
    """True iff a `new` project's foundation has merged to master, read from git (so
    only MERGED work counts). An `existing` target imports a real repo, so its
    foundation is always present. Fails closed (keeps the clamp on) when the
    workspace/git can't be read.

    Ecosystem-aware (F142 WS-B): what counts as a "foundation" depends on the stack.
    - Manifest-load-bearing ecosystems (node/web + compiled): require a matching
      build manifest AND >=1 source entrypoint on master — unchanged, so the
      reddit-look-a-like protection (3 devs fanned onto a near-empty master) holds.
    - Script-style projects (no node/web/compiled source on master): a script
      North Star (e.g. the pokemon `game.py`) produces no manifest and never
      should, so >=1 script entrypoint alone makes it foundation-ready — file
      count and directory nesting are irrelevant (non-source files and asset
      subdirs must not defeat the gate). Without this the clamp stays at 1 forever
      and `foundation_not_converging` false-fires.

    DoD note (F139 WS-A): this is the entrypoint/manifest-existence half of the
    spec's DoD (git-derived, deterministic, F106-independent). The optional
    "typecheck is green on master" half needs the F087-10 execution path and is a
    documented follow-on; existence already makes "clamp lifted" imply
    "runnable-shaped"."""
    if workspace is None:
        return True
    try:
        if str(getattr(store.get_project(), "target", "new")) != "new":
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        files = [f for f in workspace.list_files(scope="master") if f != ".gitignore"]
    except Exception:  # noqa: BLE001
        return False
    has_manifest_bound = any(f.endswith(_MANIFEST_BOUND_EXT) for f in files)
    if not has_manifest_bound and any(f.endswith(_SCRIPT_EXT) for f in files):
        # Pure script-style tree (no node/web/compiled source that a manifest is
        # load-bearing for): a script project's coherent base IS its entrypoint,
        # so >=1 script entrypoint on master makes it foundation-ready — file
        # count and directory nesting are deliberately ignored so README/LICENSE
        # or an assets/ subdir cannot re-clamp the run (see the _SCRIPT_EXT note).
        return True
    has_manifest = any(f.rsplit("/", 1)[-1] in _BUILD_MANIFESTS for f in files)
    has_entry = any(f.endswith(_SOURCE_EXT) for f in files)
    return has_manifest and has_entry


def refresh_foundation_status(store: LedgerStore, workspace: Any) -> str:
    """Persist WS-A foundation status (`pending`/`merged`) on run_state so the loop
    reads it cheaply. Derived from git each call — no persisted-flag drift, and it
    self-heals (flips back to `pending` if a merge ever removed the foundation).

    The gate (concurrency clamp + ramp) applies ONLY to greenfield (`new`) projects.
    An imported (`existing`) repo already has a foundation, so we leave
    foundation_status UNSET — `runtime_cap` then runs it at full parallelism from
    the start (no clamp, no ramp)."""
    try:
        if workspace is None or str(getattr(store.get_project(), "target", "new")) != "new":
            return "n/a"
    except Exception:  # noqa: BLE001
        pass
    status = "merged" if foundation_ready(store, workspace) else "pending"
    try:
        store.set_run_state(foundation_status=status)
    except Exception:  # noqa: BLE001
        pass
    # F141 WS-I: a `new` project crosses into the steering phase (Current Focus
    # becomes relevant) only when its initial North Star is MET — i.e. the project
    # reaches `done` (stamped in set_project_status), NOT at the first
    # foundation-merge. A foundation landing means the build is underway, not that
    # the North Star is complete, so the Current Focus panel stays hidden until
    # completion ("Building toward <North Star>" shows instead).
    return status


# --- F139 WS-D2: reactive contract centralization ---------------------------- #

# The reddit-look-a-like run's rejections clustered on cross-cutting CONTRACT
# mismatches ("does not match the merged Post type", "imports absent from master")
# because parallel devs each re-invented the shared types / mock data / component
# APIs. When a reviewer flags such a mismatch, centralize the contract reactively:
# a single owner task the dependent revise waits on — so the contract stops being
# re-invented even if the PM did not foresee it at plan time (mechanism, not
# prompt). Conservative signal set to avoid false positives.
_CONTRACT_MISMATCH_SIGNS = (
    "does not match", "do not match", "doesn't match", "mismatch",
    "absent from master", "not on master", "incompatible with",
    "import/export", "is not merged", "not yet merged",
)
# A mismatch phrase alone ("assertion does not match expected output") is a local
# bug, not a cross-cutting contract problem. Require a shared-contract NOUN to
# co-occur so WS-D2 only fires on genuine cross-cutting contracts.
_CONTRACT_NOUNS = (
    "type", "interface", "import", "export", "schema", "contract", "api",
    "signature", "prop", "component", "module", "field", "shape", "mock",
)
_CONTRACT_OWNER_TITLE = (
    "Define + centralize the shared contract (types / mock data / component APIs)")


def _findings_show_contract_mismatch(findings: list[dict[str, Any]]) -> bool:
    for f in findings or []:
        text = f"{f.get('title', '')} {f.get('body', '')}".lower()
        if (any(sign in text for sign in _CONTRACT_MISMATCH_SIGNS)
                and any(noun in text for noun in _CONTRACT_NOUNS)):
            return True
    return False


def _ensure_contract_owner(store: LedgerStore, *, detail: str) -> Optional[str]:
    """Return the single (deduped, created-on-demand) contract-owner task_id.

    Dedup is keyed on a stable ``run_state.contract_owner_task_id``, NOT on the
    task's state: a dev task flips to ``done`` the instant it opens a PR (even a
    later-rejected one), so a state-based dedup would spawn a duplicate owner on the
    next trigger. We reuse the recorded owner unless it was dropped. Shared by the
    reviewer-finding path (WS-D2) and the F159 conflict-count path so they never
    spawn two competing owners."""
    owner = None
    try:
        existing_id = str(store.get_run_state().get("contract_owner_task_id", "") or "")
    except Exception:  # noqa: BLE001
        existing_id = ""
    if existing_id:
        owner = next((t for t in store.list_tasks()
                      if t.task_id == existing_id and t.state != "dropped"), None)
    if owner is None:
        owner = store.add_task(title=_CONTRACT_OWNER_TITLE, role=DEV, detail=detail)
        try:
            store.set_run_state(contract_owner_task_id=owner.task_id)
        except Exception:  # noqa: BLE001
            pass
    return owner.task_id


def _contract_owner_for(store: LedgerStore, pr: dict[str, Any],
                        findings: list[dict[str, Any]]) -> Optional[str]:
    """F139 WS-D2: if ``findings`` show a cross-cutting contract mismatch, return
    the task_id of a single (deduped, created-on-demand) contract-owner task the
    caller should make the revise depend on; else None. Best-effort — never raises
    into the turn."""
    try:
        if not _findings_show_contract_mismatch(findings):
            return None
        owner_id = _ensure_contract_owner(store, detail=(
            "Reviewers rejected work for a cross-cutting contract mismatch. Define "
            "the shared types / mock data / component APIs in ONE place and merge "
            "them, so dependent slices conform instead of re-inventing them."))
        store.record_decision(
            title="contract mismatch -> centralize",
            context=f"pr {pr.get('pr_id')}",
            choice="contract_centralized",
            rationale=("reviewer flagged a cross-cutting contract mismatch; the "
                       "revise now depends on a single shared-contract owner task"),
            related_task_ids=[owner_id, pr.get("task_id", "")])
        return owner_id
    except Exception:  # noqa: BLE001
        return None


def _maybe_escalate_hot_files(store: LedgerStore, conflict_paths: list[str],
                              *, force: bool = False) -> None:
    """F159: when a file crosses the conflict-count escalation threshold, centralize
    it (reuse the WS-D2 contract owner) and FREEZE parallel edits to it — only the
    owner task may touch it until it merges. Best-effort; never raises into a turn.

    ``force=True`` escalates the given paths regardless of the count — used when a
    conflict has exhausted the resolve-retry cap (`_CONFLICT_RESOLVE_RETRY_CAP`): a
    file we failed to auto-rebase that many times IS hot, so hand it to the
    centralize owner + freeze it instead of leaving the PR silently blocked."""
    try:
        try:
            from .autonomy import load_policy
            esc = max(1, int(load_policy(store).hot_file_escalation_threshold))
        except Exception:  # noqa: BLE001
            esc = 4
        counts: dict[str, int] = {}
        for pr in store.list_prs():
            for raw in (pr.get("conflicts") or []):
                p = _paths.normalize_path(str(raw))
                if p:
                    counts[p] = counts.get(p, 0) + 1
        rs = store.get_run_state()
        frozen = {_paths.normalize_path(str(p)) for p in (rs.get("frozen_paths") or []) if p}
        newly = sorted(
            p for cp in conflict_paths
            if (p := _paths.normalize_path(str(cp))) and p not in frozen
            and (force or counts.get(p, 0) >= esc)
        )
        if not newly:
            return
        owner_id = _ensure_contract_owner(store, detail=(
            "Parallel edits keep colliding on: " + ", ".join(newly) + ". Define the "
            "canonical module (shared types / mock data / component APIs) in ONE "
            "place and merge it; every other task must import from it, not edit "
            "these files directly."))
        store.set_run_state(frozen_paths=sorted(frozen | set(newly)))
        store.record_decision(
            title="hot file escalated -> centralize + freeze",
            context="hot_file", choice="hot_file_escalated",
            rationale=("files kept conflicting under parallel edits (" + ", ".join(newly)
                       + "); centralizing them and freezing parallel edits until the "
                       "shared owner merges"),
            related_task_ids=[owner_id])
    except Exception:  # noqa: BLE001
        pass


def build_run_turn(
    store: LedgerStore,
    workspace: Optional[CodingWorkspace],
    members_by_role: dict[str, list[dict[str, Any]]],
    caller: MemberCaller,
    *,
    guardrail_enabled: bool,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Callable[[Any, Any], TurnOutcome]:
    """Construct the ``run_turn`` the autonomy loop drives."""
    import logging
    import time

    controller = CodingTurnController(store, workspace)
    _log = logging.getLogger("errorta.coding")

    # F087-16: capture the verbatim prompt + RAW model response of every member
    # call so each turn can be persisted to the run transcript. The branches call
    # ``caller`` (shadowed here), so wrapping it captures every model exchange
    # without touching each call site.
    _raw_caller = caller
    # F143/concurrency: the capture scratch is PER-THREAD. The concurrent loop
    # (autonomy._run_concurrent_loop) shares this one run_turn closure across a
    # ThreadPoolExecutor, so a single shared dict would let overlapping turns clobber
    # each other's captured prompt/response/member_id/usage (turn A's _cap.clear()
    # wiping turn B's in-flight capture). A thread-local gives each worker its own
    # turn scratch. Cleared at the top of each run_turn; every accessor aliases it.
    _cap_tls = threading.local()

    def _cap_of() -> dict[str, Any]:
        d = getattr(_cap_tls, "d", None)
        if d is None:
            d = _cap_tls.d = {}
        return d

    def caller(member: dict[str, Any], prompt: str) -> str:  # noqa: F811 (intentional shadow)
        _cap = _cap_of()
        # F120: a member CALL that fails (logged-out CLI, missing binary, 401/429,
        # unparseable output) raises a gateway FatalError/RetryableError here. We
        # classify it into a typed MemberFailure and re-raise a control-flow
        # sentinel carrying the member identity, so the run_turn boundary turns it
        # into a TurnOutcome instead of letting the exception be swallowed into a
        # bare noop (the bug: the reason was dropped and the loop re-ran forever).
        t0 = time.perf_counter()
        member_id = str(member.get("id", ""))
        member_role = str(member.get("coding_role") or member.get("role") or "")
        member_route = str(member.get("gateway_route_id") or member.get("provider_kind") or "")
        assignment_raw = member.get("model_assignment")
        if isinstance(assignment_raw, dict):
            _cap["model_assignment"] = dict(assignment_raw)
        _cap["model_calls"] = int(_cap.get("model_calls", 0)) + 1
        _usage_sink.last = None  # F143: clear before the call so nothing leaks in
        try:
            resp = _raw_caller(member, prompt)
        except _MemberCallFailed:
            raise
        except Exception as exc:  # noqa: BLE001 — classify, never swallow silently
            from .member_health import classify_member_failure
            failure = classify_member_failure(exc)
            _cap.update(
                member_id=member_id, prompt=prompt, response="",
                member_role=member_role, member_route=member_route,
                duration_ms=int((time.perf_counter() - t0) * 1000))
            raise _MemberCallFailed(
                member_id=member_id,
                role=member_role,
                route=member_route,
                failure=failure,
            ) from exc
        _cap.update(member_id=member_id, prompt=prompt,
                    member_role=member_role, member_route=member_route,
                    response=resp or "",
                    duration_ms=int((time.perf_counter() - t0) * 1000))
        # F143: accumulate this call's usage into the turn (a turn may make several
        # calls; tokens sum across them). Cleared per turn via _cap.clear().
        _cap["usage"] = _merge_call_usage(_cap.get("usage"),
                                          getattr(_usage_sink, "last", None))
        return resp

    def _member(role: str, member_id: str | None = None) -> dict[str, Any]:
        # F087-3 fix: honor the scheduler's chosen member so same-role work
        # actually spreads across the team (e.g. dev1 AND dev2), instead of
        # funnelling every turn to members[0]. Falls back to the first member of
        # the role when the id is unknown (or for single-member roles).
        members = members_by_role.get(role) or [{"id": f"m-{role}"}]
        if member_id:
            for m in members:
                if str(m.get("id")) == str(member_id):
                    return m
            # An unknown id funnels to member 0 — log it so a scheduler that
            # emits a wrong member_id is visible, not silently masked.
            logging.getLogger("errorta.coding").debug(
                "coding member fallback: role=%s unknown member_id=%s -> %s",
                role, member_id, members[0].get("id"))
        return members[0]

    def _parse_member_turn(
        role: str,
        task_id: str | None,
        member: dict[str, Any],
        prompt: str,
        *,
        context: str,
        related_task_ids: list[str] | None = None,
    ) -> Any:
        _cap = _cap_of()
        parsed = parse_coding_turn(role, task_id, caller(member, prompt))
        retries = 0
        # F127 D3: workers get the extra corrective attempt; the PM stays at 1.
        max_retries = (
            _INTENT_CORRECTIVE_RETRIES
            if role == PM or coding_role_of(member) == PM
            else _WORKER_CORRECTIVE_RETRIES
        )
        while (
            isinstance(parsed, TurnParseError)
            and parsed.code in _RETRYABLE_TURN_ERRORS
            and retries < max_retries
        ):
            retries += 1
            store.record_decision(
                title=f"{role} turn corrective retry",
                context=context,
                choice=f"{role}_turn_correction_retry",
                rationale=f"{parsed.code.value}: {parsed.detail}",
                related_task_ids=list(related_task_ids or []),
                extra={"retry": retries, "max_retries": max_retries},
            )
            prompt = _corrective_turn_prompt(
                prompt, parsed, retry=retries, max_retries=max_retries)
            parsed = parse_coding_turn(role, task_id, caller(member, prompt))
        _cap["parse_ok"] = not isinstance(parsed, TurnParseError)
        _cap["parse_retries"] = retries
        if not isinstance(parsed, TurnParseError) and parsed.repairs:
            repair_text = ", ".join(parsed.repairs)
            store.record_decision(
                title=f"{role} turn repaired",
                context=context,
                choice="turn_repaired",
                rationale=repair_text,
                related_task_ids=list(related_task_ids or []),
            )
            logging.getLogger("errorta.coding").info(
                "turn repaired: role=%s context=%s repairs=%s",
                role,
                context,
                repair_text,
            )
            _cap["repairs"] = int(_cap.get("repairs", 0)) + len(parsed.repairs)
        return parsed

    def _set_mergeable_if_ready(pr_id: str) -> None:
        # A PR is mergeable only when reviewer-approved AND tests-green for its
        # head — so a blind reviewer can never land a regression (F087-17).
        #
        # If the project has NO registered test commands, there is nothing for a
        # tester to run, so the tests-green gate is vacuously satisfied — review
        # approval alone governs the merge. Without this, a greenfield project
        # (which starts with an empty test-command registry) could never advance a
        # PR past `tests_passed`, so NOTHING ever merged and the team churned
        # forever in a revise loop. When test commands ARE configured the strict
        # reviewer-AND-tests gate is unchanged.
        p = store.get_pr(pr_id)
        tests_ok = p is not None and (
            p.get("tests_passed") is True or not store.get_test_commands())
        # F100 PR-B: in strict governance mode a code PR needs the PM's review
        # too (reviewer AND PM). In off/light, PM review is not required, so this
        # gate is exactly today's reviewer-AND-tests behavior.
        pm_ok = p is not None and (
            not _strict_governance(store) or p.get("pm_reviewer_approved") is True)
        # F104 S6 review (M1): a PR `blocked` at the conflict-resolve retry cap is
        # terminal — its stale reviewer_approved/tests_passed must NOT resurrect it
        # to mergeable without the conflict being resolved (defense-in-depth on the
        # exact trust boundary this feature protects).
        if (p and p.get("reviewer_approved") is True and tests_ok and pm_ok
                and p.get("status") not in ("merged", "conflict", "abandoned", "blocked")):
            store.update_pr(pr_id, status="mergeable")

    def _execute(action: Any, ledger: Any) -> TurnOutcome:
        if isinstance(action, GovernancePlan):
            from .governance import GovernanceStore
            from .governance_prompts import build_pm_governance_prompt
            from .governance_schemas import (
                GovernanceTurnParseError,
                PMSliceAcceptanceIntent,
                parse_governance_turn,
            )

            member = _member(PM, action.member_id)
            governance = GovernanceStore.for_ledger(store)
            record_turn_skill(
                store,
                member_id=member.get("id", "m-pm"),
                task_id="governance",
                role=PM,
                phase=action.phase,
            )
            pm_prompt = build_pm_governance_prompt(
                store=store, governance=governance, phase=action.phase,
            )
            parsed = parse_governance_turn(PM, caller(member, pm_prompt))
            pm_gov_retries = 0
            while (
                isinstance(parsed, GovernanceTurnParseError)
                and pm_gov_retries < _INTENT_CORRECTIVE_RETRIES
            ):
                pm_gov_retries += 1
                store.record_decision(
                    title="pm governance turn rejected",
                    context=f"governance:{action.phase}",
                    choice="pm_governance_turn_rejected",
                    rationale=f"{parsed.code.value}: {parsed.detail}",
                    extra={"retry": pm_gov_retries,
                           "max_retries": _INTENT_CORRECTIVE_RETRIES},
                )
                pm_prompt = _governance_corrective_prompt(
                    pm_prompt, parsed.code.value, parsed.detail,
                    retry=pm_gov_retries, max_retries=_INTENT_CORRECTIVE_RETRIES)
                parsed = parse_governance_turn(PM, caller(member, pm_prompt))
            if isinstance(parsed, GovernanceTurnParseError):
                store.record_decision(
                    title="pm governance turn rejected",
                    context=f"governance:{action.phase}",
                    choice="pm_governance_turn_rejected",
                    rationale=f"{parsed.code.value}: {parsed.detail}",
                )
                # F100 bugfix: an unparseable PM governance turn (incl. the strict
                # PM dual-review) after the bounded retries is a clear blocker, not
                # a silent no_progress dead-end.
                return TurnOutcome(
                    kind="governance_progress",
                    made_progress=False,
                    hard_blocker=True,
                    reason="governance_pm_turn_unparseable",
                )
            store.mark_interjections_consumed()
            intent = parsed.intent
            if isinstance(intent, PMSliceAcceptanceIntent):
                artifact = governance.append_artifact(
                    kind="slice_acceptance",
                    title=f"slice acceptance: {intent.source_slice_id or 'project'}",
                    body_markdown=intent.rationale,
                    body_json={
                        "source_slice_id": intent.source_slice_id,
                        "accepted": intent.accepted,
                        "rationale": intent.rationale,
                    },
                    state="approved" if intent.accepted else "changes_requested",
                    author={"role": PM, "member_id": str(member.get("id", "m-pm"))},
                )
                if not intent.accepted:
                    store.add_task(
                        title=f"revise accepted slice: {intent.source_slice_id}",
                        role=DEV,
                        detail=intent.rationale,
                    )
                else:
                    artifact = governance.set_artifact_state(artifact.artifact_id, "approved")
                return TurnOutcome(kind="governance_progress")

            try:
                kind, title, markdown, body_json, source_refs, supersedes = (
                    _governance_artifact_payload(intent)
                )
            except Exception as exc:
                store.record_decision(
                    title="pm governance payload rejected",
                    context=f"governance:{action.phase}",
                    choice="pm_governance_payload_rejected",
                    rationale=str(exc),
                )
                return TurnOutcome(kind="governance_progress", made_progress=False)

            # F100 PR-A: artifact governance never creates a human approval gate.
            # light skips brainstorm review (auto-approve); every other artifact
            # goes to under_review and is settled by reviewer (+ PM, in strict)
            # reviews. off never reaches here (scheduler returns None for off).
            from .governance import reviewing_phase_for_kind

            mode = governance.load_state().mode
            if mode == "light" and kind == "brainstorm":
                artifact = governance.append_artifact(
                    kind=kind, title=title, body_markdown=markdown,
                    body_json=body_json, state="approved", source_refs=source_refs,
                    supersedes_artifact_id=supersedes,
                    author={"role": PM, "member_id": str(member.get("id", "m-pm"))},
                )
                governance.update_state(phase="drafting_spec")
            else:
                artifact = governance.append_artifact(
                    kind=kind, title=title, body_markdown=markdown,
                    body_json=body_json, state="under_review", source_refs=source_refs,
                    supersedes_artifact_id=supersedes,
                    author={"role": PM, "member_id": str(member.get("id", "m-pm"))},
                )
                governance.update_state(phase=reviewing_phase_for_kind(kind))
            return TurnOutcome(kind="governance_progress")

        if isinstance(action, GovernanceReview):
            from .governance import (
                GovernanceFinding,
                GovernanceStore,
            )
            from .governance_prompts import build_governance_review_prompt
            from .governance_schemas import (
                GovernanceTurnParseError,
                parse_governance_turn,
            )

            review_role = getattr(action, "reviewer_role", REVIEWER) or REVIEWER
            review_role = PM if review_role == "pm" else REVIEWER
            member = _member(review_role, action.member_id)
            governance = GovernanceStore.for_ledger(store)
            artifact = governance.get_artifact(action.artifact_id)
            if artifact is None:
                return TurnOutcome(kind="governance_progress", made_progress=False)
            record_turn_skill(
                store,
                member_id=member.get("id", f"m-{review_role}"),
                task_id=artifact.artifact_id,
                role=review_role,
                phase="artifact_review",
            )
            review_prompt = build_governance_review_prompt(
                store=store, governance=governance, artifact=artifact,
                reviewer_role=review_role,
            )
            parsed = parse_governance_turn(review_role, caller(member, review_prompt))
            gov_retries = 0
            while (
                isinstance(parsed, GovernanceTurnParseError)
                and gov_retries < _INTENT_CORRECTIVE_RETRIES
            ):
                gov_retries += 1
                store.record_decision(
                    title="governance review rejected",
                    context=f"governance:{artifact.artifact_id}",
                    choice="governance_review_turn_rejected",
                    rationale=f"{parsed.code.value}: {parsed.detail}",
                    extra={"retry": gov_retries,
                           "max_retries": _INTENT_CORRECTIVE_RETRIES},
                )
                review_prompt = _governance_corrective_prompt(
                    review_prompt, parsed.code.value, parsed.detail,
                    retry=gov_retries, max_retries=_INTENT_CORRECTIVE_RETRIES)
                parsed = parse_governance_turn(
                    review_role, caller(member, review_prompt))
            if isinstance(parsed, GovernanceTurnParseError):
                store.record_decision(
                    title="governance review rejected",
                    context=f"governance:{artifact.artifact_id}",
                    choice="governance_review_turn_rejected",
                    rationale=f"{parsed.code.value}: {parsed.detail}",
                )
                # F100 bugfix: a review that stays unparseable after the bounded
                # corrective retries is a CLEAR blocker, not a vague no_progress
                # dead-end. autonomy.py maps hard_blocker -> a HARD_BLOCKER stop
                # with this reason.
                return TurnOutcome(
                    kind="governance_progress",
                    made_progress=False,
                    hard_blocker=True,
                    reason="governance_review_unparseable",
                )
            intent = parsed.intent
            if getattr(intent, "artifact_id", "") != artifact.artifact_id:
                store.record_decision(
                    title="governance review artifact mismatch",
                    context=f"governance:{artifact.artifact_id}",
                    choice="governance_review_artifact_mismatch",
                    rationale=f"{getattr(intent, 'artifact_id', '')} != {artifact.artifact_id}",
                )
                return TurnOutcome(kind="governance_progress", made_progress=False)
            findings = [
                GovernanceFinding(
                    severity=f.severity,
                    title=f.title,
                    body=f.body,
                    blocking=f.blocking,
                )
                for f in getattr(intent, "findings", [])
            ]
            governance.append_review(
                artifact_id=artifact.artifact_id,
                reviewer_member_id=str(member.get("id", f"m-{review_role}")),
                verdict=intent.verdict,
                findings=findings,
                reviewer_role=review_role,
            )
            # F100 PR-A: a single settle call decides whether the artifact is now
            # rejected (-> revision), fully approved (every required reviewer
            # approved -> next phase), or still under review (more reviewers
            # pending). No human approval gate is ever created.
            gov_state = governance.load_state()
            mode = gov_state.mode
            # F117-04: surface non-blocking reviewer findings as advisory Alerts
            # (the "button vs autosave" class). Best-effort — an alert-store
            # hiccup must never break the review turn. Blocking findings already
            # drive the governance reject/settle path below.
            try:
                from . import attention
                for _f in findings:
                    if not _f.blocking:
                        attention.raise_review_alert(
                            store.project_id, stage=gov_state.phase,
                            title=_f.title or "Reviewer note",
                            summary=_f.body or _f.title or "",
                            store=store,
                        )
            except Exception:  # noqa: BLE001 - alert producer never breaks the run
                pass
            resolved = governance.settle_artifact_after_review(
                artifact.artifact_id, mode)
            # F100-02 A1 (RC2): stuck stop. If the artifact was just rejected and
            # the loop isn't converging — the cap is hit (counting only
            # rejections, so cap=3 means the PM revised twice) OR two consecutive
            # no-progress rounds (byte-identical resubmission) — stop and ask the
            # human instead of looping silently to the iteration budget. autonomy
            # maps hard_blocker -> a HARD_BLOCKER "needs you" stop.
            if resolved == "changes_requested":
                kind = artifact.artifact_kind
                cap = gov_state.max_review_rounds
                rounds = governance.review_round_count(kind)
                streak = governance.no_progress_streak(kind)
                if rounds >= cap or streak >= 2:
                    # Convergence cap hit. Who is the final authority?
                    #   strict -> the human (stop and ask, the "needs you" path)
                    #   light/off -> the PM (it finalizes its best version and the
                    #     run proceeds; the human is NOT pulled in). The reviewer's
                    #     findings are already recorded as alerts above.
                    if mode != "strict":
                        try:
                            governance.force_accept_artifact(
                                artifact.artifact_id, by="pm")
                            return TurnOutcome(kind="governance_progress")
                        except Exception:  # noqa: BLE001 - fall back to asking
                            pass
                    store.record_decision(
                        title="governance review not converging",
                        context=f"governance:{artifact.artifact_id}",
                        choice="governance_review_not_converging",
                        rationale=(
                            f"{kind}: {rounds} changes_requested rounds "
                            f"(cap={cap}), no_progress_streak={streak}"
                        ),
                        extra={"rounds": rounds, "cap": cap,
                               "no_progress_streak": streak, "kind": kind},
                    )
                    return TurnOutcome(
                        kind="governance_progress",
                        made_progress=False,
                        hard_blocker=True,
                        reason="governance_review_not_converging",
                    )
            return TurnOutcome(kind="governance_progress")

        if isinstance(action, GovernanceMaterialize):
            from .governance import GovernanceStore
            from .governance_materialize import materialize_approved_plan

            governance = GovernanceStore.for_ledger(store)
            result = materialize_approved_plan(store, governance)
            store.record_decision(
                title="materialized governance plan",
                context="governance:development",
                choice="governance_plan_materialized",
                rationale=f"created={result['created']} existing={result['existing']}",
            )
            return TurnOutcome(
                kind="governance_progress",
                made_progress=bool(result["created"]),
                model_calls=0,
            )

        if isinstance(action, PMAssist):
            task = _fetch_task(store, action.task_id)
            if task is None:
                return TurnOutcome(kind="noop", model_calls=0)
            extras = getattr(task, "_extras", {}) or {}
            if not extras.get("pm_assist_pending"):
                return TurnOutcome(kind="noop", model_calls=0)
            member = _member(PM, action.member_id)
            record_turn_skill(
                store,
                member_id=member.get("id", "m-pm"),
                task_id=task.task_id,
                role=PM,
            )
            parsed = _parse_member_turn(
                PM,
                None,
                member,
                _pm_assist_prompt(store, task),
                context=f"pm assist {task.task_id}",
                related_task_ids=[task.task_id],
            )
            attempts = int(extras.get("pm_assist_attempts") or 0) + 1
            limit = max(1, int(extras.get("pm_assist_limit") or 1))
            invalid_reason = ""
            if isinstance(parsed, TurnParseError):
                invalid_reason = f"{parsed.code.value}: {parsed.detail}"
            elif parsed.intent.done:
                invalid_reason = "PM assist cannot declare the project done"
            if invalid_reason:
                store.update_task(task.task_id, pm_assist_attempts=attempts)
                store.record_decision(
                    title=f"PM assist rejected: {task.title}",
                    context=f"task {task.task_id}",
                    choice="pm_assist_rejected",
                    rationale=invalid_reason,
                    related_task_ids=[task.task_id],
                    extra={"attempt": attempts, "limit": limit},
                )
                if attempts < limit:
                    return TurnOutcome(kind="planned", made_progress=True)
                from . import attention

                excluded = set(extras.get("excluded_member_ids") or [])
                failed_routes = dict(extras.get("excluded_member_routes") or {})
                last_member = sorted(excluded)[-1] if excluded else ""
                attention.raise_worker_unproductive_problem(
                    store.project_id,
                    task_id=task.task_id,
                    task_title=task.title,
                    members_tried=sorted(excluded),
                    last_member=last_member,
                    last_route=str(failed_routes.get(last_member, "")),
                    last_error=invalid_reason,
                    store=store,
                )
                return TurnOutcome(
                    kind="pm_assist_exhausted",
                    made_progress=False,
                    hard_blocker=True,
                    reason="worker_unproductive",
                )
            replacements = _materialize_pm_tasks(
                store, parsed.intent, parent_task=task
            )
            replacement_ids = [replacement.task_id for replacement in replacements]
            if replacement_ids:
                # FIX 3 (race): re-point dependents onto the replacements BEFORE
                # dropping the parent. `dropped` counts as a satisfied dep (Spec 09),
                # so there is a window where a dependent reads as ready between the
                # drop and the repoint — the concurrent loop re-enters dispatch
                # whenever any in-flight future completes. While the parent is still
                # non-satisfied its dependents keep waiting; repointing first
                # transfers that wait to the replacements with no ready-window.
                _repoint_dropped_dependents(store, task.task_id, replacement_ids)
                store.update_task(
                    task.task_id,
                    state="dropped",
                    assignee_member_id=None,
                    pm_assist_pending=False,
                    pm_assist_attempts=attempts,
                    superseded_by_task_ids=replacement_ids,
                )
                store.record_decision(
                    title=f"PM re-scoped task: {task.title}",
                    context=f"task {task.task_id}",
                    choice="pm_assist_completed",
                    rationale=f"Created {len(replacements)} smaller replacement task(s).",
                    related_task_ids=[task.task_id] + replacement_ids,
                )
                return TurnOutcome(kind="planned", made_progress=True)
            # FIX 4 (edge): the re-scope produced NO replacement tasks (all deduped
            # against the open backlog, or an empty intent). Dropping the parent
            # here would strip its dependents' only dependency edge — since
            # `dropped` is satisfied and `_repoint_dropped_dependents(..., [])`
            # removes the edge entirely, the dependents would dispatch prematurely
            # against work that was never actually re-scoped. Keep the parent
            # (non-satisfied) so its dependents keep waiting; only clear the
            # pm_assist flag so the ladder does not spin on it.
            store.update_task(
                task.task_id,
                pm_assist_pending=False,
                pm_assist_attempts=attempts,
            )
            store.record_decision(
                title=f"PM re-scope produced no new tasks: {task.title}",
                context=f"task {task.task_id}",
                choice="pm_assist_no_replacements",
                rationale=(
                    "Re-scope yielded no replacement tasks (all deduped or empty); "
                    "kept the parent so its dependents keep waiting."
                ),
                related_task_ids=[task.task_id],
            )
            return TurnOutcome(kind="planned", made_progress=False)

        if isinstance(action, Plan):
            if _redispatch_conflicted_prs(store, workspace):
                return TurnOutcome(kind="planned", model_calls=0)
            member = _member(PM)
            record_turn_skill(store, member_id=member.get("id", "m-pm"),
                              task_id="plan", role=PM)
            # F087-13 WS-2: the PM turn is schema-validated (coding_turn.v1
            # PMPlanIntent). A malformed turn, or done=true without a completion
            # summary / done=false with no tasks, fails closed — it does NOT end
            # the run or silently no-op-succeed.
            parsed = _parse_member_turn(
                PM, None, member, _pm_prompt(store), context="plan")
            if isinstance(parsed, TurnParseError):
                # F087-15 L1: do NOT consume interjections on a rejected turn —
                # an authoritative user instruction must survive a malformed PM
                # response and be re-delivered next turn.
                store.record_decision(
                    title="pm turn rejected", context="plan",
                    choice="pm_turn_rejected",
                    rationale=f"{parsed.code.value}: {parsed.detail}")
                return TurnOutcome(kind="planned", made_progress=False)
            # F087-07-E: the interjections were delivered to (and accepted by) the
            # PM this turn — mark them consumed (read-once) only now.
            store.mark_interjections_consumed()
            intent = parsed.intent
            # F088-04: PM decisions are durable project truth — persist them so
            # the grounding layer can promote them (previously dropped on the
            # floor). The ledger remains the source; grounding derives from it.
            for dec in intent.decisions:
                store.record_decision(
                    title=dec.title, context="pm_decision",
                    choice="pm_decision", rationale=dec.rationale)
            if intent.done:
                # F128: a done=true claim is verified against the backlog before
                # it becomes project truth. The PM's word alone is not enough — a
                # run must never report "done" while non-terminal tasks or open
                # PRs remain (e.g. a blocked merge conflict awaiting a human).
                open_items = pending_completion_work(store)
                if open_items:
                    store.record_decision(
                        title="completion refused: open work remains",
                        context="plan", choice="pm_completion_refused",
                        rationale=summarize_open_items(open_items),
                        related_task_ids=[
                            i.id for i in open_items if i.kind == "task" and i.id
                        ][:20],
                    )
                    # Not project_done: the loop re-prompts the PM with the open
                    # items (finish or cancel them) and, if they never resolve,
                    # escalates to a blocking completion_blocked Problem.
                    return TurnOutcome(
                        kind="completion_refused", made_progress=False,
                        reason="open_work_remains")
                # F093: persist the PM's completion justification so the UI can
                # show "✓ Complete — here's why". (intent.completion_summary is
                # validated non-empty when done=true, schemas.py PMPlanIntent.)
                store.set_completion(intent.completion_summary)
                return TurnOutcome(kind="project_done")
            created = _materialize_pm_tasks(store, intent)
            return TurnOutcome(kind="planned", made_progress=len(created) > 0)

        if isinstance(action, Merge):
            # F087-17: the PM integrates a reviewer-approved + tests-green PR into
            # master (conflict-aware). master accumulates; conflicts bounce back
            # to a dev resolve task (never a silent overwrite).
            pr = store.get_pr(action.pr_id)
            if pr is None or pr.get("status") != "mergeable" or workspace is None:
                return TurnOutcome(kind="noop", model_calls=0)
            source_task = _fetch_task(store, str(pr.get("task_id") or ""))
            governance_blocker = _strict_governance_merge_blocker(store, source_task)
            if governance_blocker:
                store.update_pr(action.pr_id, status="changes_requested")
                store.record_decision(
                    title=f"governance blocked merge {pr['branch']}",
                    context=f"pr {action.pr_id}",
                    choice="governance_merge_blocked",
                    rationale=governance_blocker,
                    related_task_ids=[pr["task_id"]],
                )
                store.add_task(
                    title=f"refresh governed slice: {pr['branch']}",
                    role=DEV,
                    detail=governance_blocker,
                    pr_id=action.pr_id,
                    source_spec_artifact_id=getattr(source_task, "source_spec_artifact_id", None),
                    source_plan_artifact_id=getattr(source_task, "source_plan_artifact_id", None),
                    source_slice_id=getattr(source_task, "source_slice_id", None),
                    governance_required=True,
                )
                return TurnOutcome(kind="pr_skipped", model_calls=0)
            # F159: capture the branch's changed files BEFORE the merge (after it,
            # the branch no longer diffs against master) so the PR record carries
            # ground-truth of what this task touched — used to weight hot files.
            try:
                _changed = workspace.changed_paths(pr["branch"]) if workspace else []
            except Exception:  # noqa: BLE001 — never fail a merge over bookkeeping
                _changed = []
            res = workspace.merge_pr(pr["branch"])
            if res.get("merged"):
                store.update_pr(action.pr_id, status="merged",
                                head=res.get("head", pr["head"]),
                                changed_paths=list(_changed))
                store.record_decision(
                    title=f"merged PR {pr['branch']}", context=f"pr {action.pr_id}",
                    choice="pr_merged", rationale="PM merged into master",
                    related_task_ids=[pr["task_id"]])
                # F087-19 #5: durable merge-level memory so old reasoning doesn't
                # fall out of context as the turn log caps.
                # F139 WS-B: report the MERGED tree (git truth) here, not the
                # artifact ledger. The ledger accumulates every write to every
                # branch — including abandoned ones — which made this episode
                # claim a phantom "complete" project (the reddit-look-a-like bug).
                # This runs right after a successful merge, so master is current.
                files = (workspace.list_files(scope="master")
                         if workspace is not None else [])
                store.record_episode(
                    title=f"merged {pr['branch']}",
                    summary=(f"PR {pr['branch']} (task {pr['task_id']}) merged into "
                             f"master; project files now: {', '.join(str(f) for f in files)}"),
                    head=res.get("head", pr["head"]),
                    related_task_ids=[pr["task_id"]])
                # F139 WS-A: master advanced — re-derive whether the foundation
                # (build manifest + source entrypoint) is now on master so the loop
                # lifts the concurrency clamp exactly when the scaffold lands.
                refresh_foundation_status(store, workspace)
                # F159: the shared-contract owner landed → lift the hot-file freeze
                # (the canonical module is now on master; parallel edits are safe).
                try:
                    _rs = store.get_run_state()
                    if _rs.get("frozen_paths") and str(
                            _rs.get("contract_owner_task_id", "") or "") == str(pr["task_id"]):
                        store.set_run_state(frozen_paths=[])
                except Exception:  # noqa: BLE001
                    pass
                # F087-18 #6: reclaim space — delete the merged branch and prune any
                # other branches whose PR is now terminal (merged/abandoned).
                _prune_dead_branches(store, workspace, just_merged=pr["branch"])
                # F088-04/06: promote merged truth + refresh WIP/supersession.
                _sync_grounding(store, workspace)
                # F087-3: master moved -> revalidate any other mergeable PR
                # against the new base before it can land (parallel-merge safety).
                _revalidate_stale_prs(store, workspace, just_merged_pr_id=action.pr_id)
                # F091: if THIS merged PR was a revise (its task carries a pr_id
                # back-link), mark the superseded ancestor chain terminal so the PM
                # stops seeing those stale PRs as outstanding work.
                _supersede_ancestors(store, workspace,
                                     store.get_pr(action.pr_id) or pr)
                _log.info("coding merge: project=%s merged %s into master",
                          store.project_id, pr["branch"])
                return TurnOutcome(kind="pr_merged", model_calls=0)
            store.update_pr(action.pr_id, status="conflict",
                            conflicts=res.get("conflicts", []))
            store.record_decision(
                title=f"merge conflict {pr['branch']}", context=f"pr {action.pr_id}",
                choice="pr_conflict",
                rationale="conflicts: " + ", ".join(res.get("conflicts", [])),
                related_task_ids=[pr["task_id"]])
            _redispatch_conflict_pr(
                store, workspace, store.get_pr(action.pr_id) or pr,
                conflicts=res.get("conflicts", []),
            )
            return TurnOutcome(kind="pr_conflict", model_calls=0)

        if isinstance(action, Assign):
            task = _fetch_task(store, action.task_id)
            if task is None:
                return TurnOutcome(kind="noop")
            member = _member(action.role, getattr(action, "member_id", None))
            # F129: resolve and revalidate the concrete route before any prompt,
            # health classification, transcript capture, or gateway call. The
            # room/run snapshot is immutable; execution receives a bound copy.
            from .model_assignment import bind_member_route, resolve_task_assignment

            assignment, override_reason = resolve_task_assignment(task, member)
            if assignment is None:
                store.update_task(
                    task.task_id, state="blocked",
                    model_assignment_failure=override_reason or "no_capable_model",
                )
                store.record_decision(
                    title=f"model assignment failed: {task.title}",
                    context=f"task {task.task_id}", choice="model_assignment_failed",
                    rationale=override_reason or "no capable available model",
                    related_task_ids=[task.task_id],
                )
                return TurnOutcome(
                    kind="model_assignment_failed", made_progress=False,
                    hard_blocker=True, reason=override_reason or "no_capable_model",
                    member_id=str(member.get("id") or ""), member_role=action.role,
                )
            prior_assignment = dict(task.model_assignment or {})
            if assignment.to_dict() != prior_assignment:
                pool_snapshot = (
                    list(member.get("model_pool") or [])
                    if str(member.get("model_mode") or "single") == "multi"
                    else [assignment.route_id]
                )
                task = store.update_task(
                    task.task_id,
                    model_assignment=assignment.to_dict(),
                    model_pool_snapshot=pool_snapshot,
                    model_assignment_failure=None,
                )
                store.record_decision(
                    title=f"model assigned: {task.title}",
                    context=f"task {task.task_id}",
                    choice=("model_assignment_overridden" if override_reason else "model_assigned"),
                    rationale=assignment.rationale,
                    related_task_ids=[task.task_id],
                    extra={
                        "assignment_id": assignment.assignment_id,
                        "member_id": assignment.member_id,
                        "route_id": assignment.route_id,
                        "difficulty_tier": assignment.difficulty_tier,
                        "task_type": assignment.task_type,
                        "source": assignment.source,
                        "override_reason": override_reason,
                    },
                )
            member = bind_member_route(member, assignment)
            record_turn_skill(store, member_id=member.get("id", f"m-{action.role}"),
                              task_id=task.task_id, role=action.role)

            if action.role == DEV:
                # F087-17: the dev works on its OWN branch off master with the
                # current tree inlined (read-back), so it extends accumulated work
                # and opens a PR instead of committing to a shared branch.
                readback = ""
                branch = None
                if workspace is not None:
                    branch = workspace.start_task_branch(task.task_id)
                    readback = workspace.read_back(task_id=task.task_id)
                parsed = _parse_member_turn(
                    DEV, task.task_id, member, _dev_prompt(task, store, readback),
                    context=f"task {task.task_id}", related_task_ids=[task.task_id])
                if isinstance(parsed, TurnParseError):
                    store.record_decision(
                        title=f"dev turn rejected: {task.title}",
                        context=f"task {task.task_id}", choice="dev_turn_rejected",
                        rationale=f"{parsed.code.value}: {parsed.detail}",
                        related_task_ids=[task.task_id])
                    store.update_task(task.task_id, state="todo")
                    # F127: signal the escalate-up ladder — a dev that can't
                    # produce a usable turn must route around itself, not loop.
                    return TurnOutcome(
                        kind="noop", unproductive=True,
                        member_id=str(member.get("id", "")),
                        member_role=DEV, member_route=str(member.get("gateway_route_id", "")),
                        reason=parsed.code.value)
                intent = parsed.intent
                # F088-09: a read-only context request — answer from grounding,
                # record it, and re-queue the task so the dev acts on the answer.
                # No file writes, no durable mutation.
                from .schemas import DeveloperContextRequestIntent
                if isinstance(intent, DeveloperContextRequestIntent):
                    _answer_dev_context_request(store, task, intent)
                    store.update_task(task.task_id, state="todo")
                    return TurnOutcome(kind="noop")
                data = {"task_type": intent.task_type,
                        "tool_calls": [{"tool": tc.tool, "args": tc.args}
                                       for tc in intent.tool_calls]}
                writes = controller.execute_dev_turn(task=task, member=member, data=data)
                if writes.failures:
                    for path, reason in writes.failures:
                        choice = "tool_failed" if reason == "tool_not_allowed" else "write_failed"
                        title = "tool failed" if choice == "tool_failed" else "write failed"
                        store.record_decision(
                            title=f"{title}: {task.title}",
                            context=f"task {task.task_id}", choice=choice,
                            rationale=f"{path}: {reason}",
                            related_task_ids=[task.task_id])
                    store.update_task(task.task_id, state="todo")
                    # F136: a turn that produced NO usable write (every tool
                    # failed / was disallowed) is unproductive — feed the F127
                    # escalate-up ladder so a dev that keeps emitting a
                    # rejected/disallowed tool call routes around itself instead
                    # of looping `todo<->doing` forever (live: 352 identical
                    # `MemRead: tool_not_allowed` failures on one task). Partial
                    # progress (some writes landed) requeues without penalty.
                    if writes.success_count == 0:
                        return TurnOutcome(
                            kind="noop", unproductive=True,
                            member_id=str(member.get("id", "")), member_role=DEV,
                            member_route=str(member.get("gateway_route_id", "")),
                            reason=writes.failures[0][1] if writes.failures else "tool_failed")
                    return TurnOutcome(kind="noop")
                if intent.task_type == "implementation" and writes.success_count == 0:
                    store.record_decision(
                        title=f"write missing: {task.title}",
                        context=f"task {task.task_id}", choice="write_missing",
                        rationale="implementation task completed no code_write tool event",
                        related_task_ids=[task.task_id])
                    store.update_task(task.task_id, state="todo")
                    # F136: an implementation turn that wrote nothing usable is
                    # unproductive — same escalate-up path as a parse rejection.
                    return TurnOutcome(
                        kind="noop", unproductive=True,
                        member_id=str(member.get("id", "")), member_role=DEV,
                        member_route=str(member.get("gateway_route_id", "")),
                        reason="write_missing")
                if workspace is None or branch is None:
                    # No worktree -> can't open a PR; mark done (degenerate path).
                    store.update_task(task.task_id, state="done")
                    return TurnOutcome(kind="task_done", task=task)
                # F139 WS-C (supersedes F087-19 #3's auto-close): a dev turn whose
                # branch has NO net change vs master must NOT be counted as
                # progress. The old behaviour marked the task `done` "already
                # satisfied" — which let a stuck dev that keeps re-emitting an
                # existing file (the reddit Navigation-rewritten-100× loop) close
                # its task without producing anything, and F136's escalate-up
                # ladder never engaged because success_count > 0.
                #
                # A write-intent turn that changed nothing is now unproductive:
                # re-queue + feed the F127 ladder (escalate the model, then a
                # blocking attention Problem), and record a `superseded_on_master`
                # decision so the PM can confirm the requirement is genuinely
                # already met (PM authority) rather than a dev deciding so silently.
                # A read/context-intent turn already returned above (it never
                # reaches here), so this path is write-intent only. The gate is the
                # branch's diff vs master (the authoritative git signal, also used
                # for the real PR below); `writes.net_changed_files` is the same
                # signal surfaced on the summary but is only informational here.
                if not workspace.pr_diff(branch).strip():
                    store.update_task(task.task_id, state="todo")
                    store.record_decision(
                        title=f"no net change vs master: {task.title}",
                        context=f"task {task.task_id}", choice="superseded_on_master",
                        rationale=("dev branch has no changes vs master — the "
                                   "requirement may already be satisfied, or the "
                                   "dev re-emitted existing files; escalating for a "
                                   "stronger attempt and PM confirmation"),
                        related_task_ids=[task.task_id])
                    workspace.delete_branch(branch)
                    return TurnOutcome(
                        kind="noop", unproductive=True,
                        member_id=str(member.get("id", "")), member_role=DEV,
                        member_route=str(member.get("gateway_route_id", "")),
                        reason="no_net_change")
                pr = store.record_pr(task_id=task.task_id, branch=branch,
                                     head=workspace.branch_head(branch),
                                     dev_member=str(member.get("id", "")))
                # F159: persist the OBSERVED touched-files at PR-open (not only at
                # merge), so hot-file ownership can be detected while the PR is still
                # open — the merge-scoped hold needs the owner's real paths, and dev
                # tasks rarely declare `target_files` or name the file in prose.
                try:
                    _opened_changed = [f for f in workspace.changed_paths(branch)
                                       if f != ".gitignore"]
                    if _opened_changed:
                        store.update_pr(pr["pr_id"], changed_paths=_opened_changed)
                except Exception:  # noqa: BLE001 — best-effort observability signal
                    pass
                store.update_task(task.task_id, state="done")
                store.add_task(title=f"review PR: {task.title}", role=REVIEWER,
                               pr_id=pr["pr_id"], depends_on=[task.task_id])
                store.record_decision(
                    title=f"opened PR: {task.title}", context=f"task {task.task_id}",
                    choice="pr_opened", rationale=f"branch {branch}",
                    related_task_ids=[task.task_id], extra={"pr_id": pr["pr_id"]})
                return TurnOutcome(kind="pr_opened", task=task)

            if action.role == REVIEWER:
                pr = store.get_pr(task.pr_id) if task.pr_id else None
                if pr is None or workspace is None:
                    store.update_task(task.task_id, state="done")
                    return TurnOutcome(kind="noop")
                diff = workspace.pr_diff(pr["branch"])
                ctx = _review_project_context(store, workspace, pr)
                parsed = _parse_member_turn(
                    REVIEWER, task.task_id, member,
                    _review_pr_prompt(
                        task, pr, diff, ctx,
                        scope_task=_fetch_task(store, str(pr.get("task_id") or ""))),
                    context=f"task {task.task_id}", related_task_ids=[task.task_id])
                # F126: persist the reviewer's findings on the PR so the task
                # detail can show WHY a PR got "changes requested", not just that
                # it did. (Parse-error / stale-head rejections have no structured
                # findings — the reason is in the decision log.)
                review_findings: list[dict[str, Any]] = []
                if isinstance(parsed, TurnParseError):
                    store.record_decision(
                        title=f"reviewer turn rejected: {task.title}",
                        context=f"task {task.task_id}", choice="reviewer_turn_rejected",
                        rationale=f"{parsed.code.value}: {parsed.detail}",
                        related_task_ids=[task.task_id])
                    approved = False
                elif parsed.intent.reviewed_head != pr["head"]:
                    store.record_decision(
                        title=f"stale review: {pr['branch']}",
                        context=f"task {task.task_id}", choice="stale_review_head",
                        rationale=(f"reviewed_head {parsed.intent.reviewed_head!r} != "
                                   f"pr head {pr['head']!r}"),
                        related_task_ids=[task.task_id])
                    approved = False
                else:
                    approved = bool(parsed.intent.approved)
                    review_findings = [
                        {"severity": f.severity, "title": f.title, "body": f.body,
                         "path": f.path, "blocking": f.severity == "blocking"}
                        for f in parsed.intent.findings
                    ]
                store.update_pr(pr["pr_id"], reviewer_approved=approved,
                                reviewed_head=pr["head"],
                                review_findings=review_findings)
                store.record_decision(
                    title=f"review verdict: {pr['branch']}",
                    context=f"pr {pr['pr_id']}",
                    choice="review_approved" if approved else "review_rejected",
                    rationale=f"reviewer verdict for {pr['branch']}",
                    related_task_ids=[task.task_id, pr["task_id"]],
                    extra={"reviewed_head": pr["head"], "pr_id": pr["pr_id"]})
                store.update_task(task.task_id, state="done")
                if approved:
                    # Only queue a tester when there's something to run. With no
                    # registered test commands the PR is already mergeable on
                    # approval (see _set_mergeable_if_ready) — spawning a tester
                    # task would just starve in the backlog forever.
                    if store.get_test_commands():
                        store.add_task(title=f"test PR: {pr['branch']}", role=TESTER,
                                       pr_id=pr["pr_id"], depends_on=[task.task_id])
                    # F100 PR-B: strict mode is a DUAL review — the PM must review
                    # the PR too. Spawn the PM PR-review task in parallel with the
                    # tester (both run before merge; the gate clears only when
                    # reviewer + PM + tests are all green). Guard against a dup if
                    # the reviewer re-approves the same head.
                    if _strict_governance(store) and not _open_pm_review_task(store, pr["pr_id"]):
                        store.add_task(title=f"review PR: {task.title}", role=PM,
                                       pr_id=pr["pr_id"], depends_on=[task.task_id])
                else:
                    store.update_pr(pr["pr_id"], status="changes_requested")
                    # F139 WS-D2: a contract-mismatch rejection reactively spawns a
                    # single shared-contract owner task; the revise waits on it so
                    # the contract is centralized instead of re-invented per branch.
                    owner_id = _contract_owner_for(store, pr, review_findings)
                    revise_depends = [task.task_id]
                    if owner_id:
                        revise_depends.append(owner_id)
                    # F091: thread a back-link onto the revise task. pr_id on a
                    # DEV revise task means "the PR this revise supersedes" (vs a
                    # TESTER task's pr_id = "the PR under test"); depends_on chains
                    # it after the review; detail names the branch so the dev can
                    # read back the prior work. When the revise PR merges,
                    # _supersede_ancestors walks this back-link.
                    findings_detail = _detail_from_findings(review_findings)
                    store.add_task(
                        title=f"revise: {pr['branch']}", role=DEV,
                        pr_id=pr["pr_id"], depends_on=revise_depends,
                        reason_summary=_reason_from_findings(review_findings),
                        detail=(f"Address reviewer findings on branch "
                                f"{pr['branch']} and open a new PR. The prior PR "
                                f"({pr['pr_id']}) is superseded when this lands."
                                + (f" Findings: {findings_detail}."
                                   if findings_detail else "")))
                _set_mergeable_if_ready(pr["pr_id"])
                return TurnOutcome(kind="pr_reviewed", task=task)

            if action.role == TESTER:
                pr = store.get_pr(task.pr_id) if task.pr_id else None
                if pr is None or workspace is None:
                    store.update_task(task.task_id, state="done")
                    return TurnOutcome(kind="noop")
                parsed = _parse_member_turn(
                    TESTER,
                    task.task_id,
                    member,
                    _test_prompt(task, store),
                    context=f"task {task.task_id}",
                    related_task_ids=[task.task_id],
                )
                registry = store.get_test_commands()

                def _changes_requested(reason: str, choice: str) -> TurnOutcome:
                    store.record_decision(
                        title=f"tests not green: {pr['branch']}",
                        context=f"pr {pr['pr_id']}", choice=choice, rationale=reason,
                        related_task_ids=[task.task_id, pr["task_id"]])
                    store.update_pr(pr["pr_id"], tests_passed=False,
                                    tested_head=pr["head"], status="changes_requested")
                    store.update_task(task.task_id, state="done")
                    store.add_task(title=f"fix tests: {pr['branch']}", role=DEV,
                                   detail=f"Make the tests pass: {reason}")
                    return TurnOutcome(kind="pr_tested", task=task)

                if isinstance(parsed, TurnParseError):
                    return _changes_requested(parsed.code.value, "tester_turn_rejected")
                command_ids = list(parsed.intent.command_ids)
                # F142 WS-C: applicability gate. The tester may declare that no
                # registered command exercises this slice (project not yet
                # runnable end-to-end) -> the test gate is non-blocking for this
                # slice. GUARDRAIL: honored ONLY when command_ids is empty. If
                # the tester set not_applicable but ALSO named commands, we
                # ignore the flag and fall through to run them — real exit codes
                # govern, so a command that ran and failed can never be masked.
                if getattr(parsed.intent, "not_applicable", False) and not command_ids:
                    store.record_decision(
                        title=f"tests not applicable: {pr['branch']}",
                        context=f"pr {pr['pr_id']}", choice="tests_not_applicable",
                        rationale=(parsed.intent.rationale
                                   or "no registered command exercises this slice"),
                        related_task_ids=[task.task_id, pr["task_id"]])
                    # Non-blocking: mark tests satisfied so _set_mergeable_if_ready
                    # can proceed. This is NOT a false pass of a suite that ran and
                    # failed — no command ran.
                    store.update_pr(pr["pr_id"], tests_passed=True,
                                    tested_head=pr["head"])
                    store.update_task(task.task_id, state="done")
                    # F142 WS-C observability: surface a non-blocking Alert (deduped
                    # to one per run) so a human sees that a slice merged without any
                    # test running — otherwise a run could merge to done with tests
                    # never executed and nothing telling the operator.
                    try:
                        from . import attention
                        attention.raise_tests_skipped_alert(
                            store.project_id, stage="build",
                            summary=(f"PR on branch {pr['branch']} merged without "
                                     "running tests (tester declared the slice "
                                     "not-applicable). Verify test coverage."),
                            store=store)
                    except Exception:  # noqa: BLE001 — observability is best-effort
                        pass
                    _set_mergeable_if_ready(pr["pr_id"])
                    return TurnOutcome(kind="pr_tested", task=task)
                _resolved, unknown = resolve_commands(registry, command_ids)
                if unknown:
                    return _changes_requested(
                        "unknown command_ids: " + ", ".join(unknown),
                        "invalid_test_command")
                task_root = getattr(workspace, "task_root", None)
                test_root = (
                    task_root(pr["task_id"], branch=pr["branch"])
                    if callable(task_root) else workspace.root()
                )
                session = run_test_commands(test_root, registry, command_ids,
                                            should_cancel=should_cancel,
                                            require_sandbox=store.get_require_sandbox())
                store.record_test_run(session, task_id=task.task_id, head=pr["head"])
                exits = "; ".join(f"{r.command_id}={r.status}/{r.exit_code}"
                                  for r in session.results)
                store.record_decision(
                    title=f"tested PR {pr['branch']}", context=f"pr {pr['pr_id']}",
                    choice="tested_pass" if session.passed else "tested_fail",
                    rationale=f"command_ids={command_ids}; {exits}",
                    related_task_ids=[task.task_id, pr["task_id"]])
                store.update_pr(pr["pr_id"], tests_passed=bool(session.passed),
                                tested_head=pr["head"])
                store.update_task(task.task_id, state="done")
                if not session.passed:
                    store.update_pr(pr["pr_id"], status="changes_requested")
                    store.add_task(title=f"fix tests: {pr['branch']}", role=DEV,
                                   detail=f"Tests failed: {exits}")
                _set_mergeable_if_ready(pr["pr_id"])
                return TurnOutcome(kind="pr_tested", task=task)

            if action.role == PM and task.pr_id:
                # F100 PR-B: strict-mode PM PR-review (the second of the dual
                # review). The PM plays a reviewer role on the code PR, mirroring
                # PR-A's PM-as-artifact-reviewer pattern. Reuses the reviewer PR
                # prompt + parse path; records pm_review_approved/_rejected with
                # the reviewed head so the merge gate can require it.
                pr = store.get_pr(task.pr_id)
                if pr is None or workspace is None:
                    store.update_task(task.task_id, state="done")
                    return TurnOutcome(kind="noop")
                diff = workspace.pr_diff(pr["branch"])
                ctx = _review_project_context(store, workspace, pr)
                parsed = _parse_member_turn(
                    REVIEWER, task.task_id, member,
                    _review_pr_prompt(
                        task, pr, diff, ctx,
                        scope_task=_fetch_task(store, str(pr.get("task_id") or ""))),
                    context=f"task {task.task_id}", related_task_ids=[task.task_id])
                pm_findings: list[dict[str, Any]] = []
                if isinstance(parsed, TurnParseError):
                    store.record_decision(
                        title=f"pm review turn rejected: {task.title}",
                        context=f"task {task.task_id}", choice="pm_review_turn_rejected",
                        rationale=f"{parsed.code.value}: {parsed.detail}",
                        related_task_ids=[task.task_id])
                    approved = False
                elif parsed.intent.reviewed_head != pr["head"]:
                    store.record_decision(
                        title=f"stale pm review: {pr['branch']}",
                        context=f"task {task.task_id}", choice="stale_review_head",
                        rationale=(f"reviewed_head {parsed.intent.reviewed_head!r} != "
                                   f"pr head {pr['head']!r}"),
                        related_task_ids=[task.task_id])
                    approved = False
                else:
                    approved = bool(parsed.intent.approved)
                    pm_findings = [
                        {"severity": f.severity, "title": f.title, "body": f.body,
                         "path": f.path, "blocking": f.severity == "blocking"}
                        for f in parsed.intent.findings
                    ]
                store.update_pr(pr["pr_id"], pm_reviewer_approved=approved,
                                pm_reviewed_head=pr["head"])
                store.record_decision(
                    title=f"pm review verdict: {pr['branch']}",
                    context=f"pr {pr['pr_id']}",
                    choice="pm_review_approved" if approved else "pm_review_rejected",
                    rationale=f"PM verdict for {pr['branch']}",
                    related_task_ids=[task.task_id, pr["task_id"]],
                    extra={"reviewed_head": pr["head"], "pr_id": pr["pr_id"]})
                store.update_task(task.task_id, state="done")
                if not approved:
                    store.update_pr(pr["pr_id"], status="changes_requested")
                    pm_reason = _reason_from_findings(pm_findings) or "PM requested changes"
                    pm_detail = _detail_from_findings(pm_findings)
                    store.add_task(
                        title=f"revise: {pr['branch']}", role=DEV,
                        pr_id=pr["pr_id"], depends_on=[task.task_id],
                        reason_summary=pm_reason,
                        detail=(f"Address PM review findings on branch "
                                f"{pr['branch']} and open a new PR. The prior PR "
                                f"({pr['pr_id']}) is superseded when this lands."
                                + (f" Findings: {pm_detail}." if pm_detail else "")))
                _set_mergeable_if_ready(pr["pr_id"])
                return TurnOutcome(kind="pr_reviewed", task=task)

        return TurnOutcome(kind="noop")

    def run_turn(action: Any, ledger: Any) -> TurnOutcome:
        # F087-19 #2: clean up stale/superseded PRs + corrective tasks before each
        # turn so the backlog/context reflects what master actually still needs.
        _reconcile_stale(store, workspace)
        # F087-16: record a verbatim transcript entry for every member turn
        # (the captured prompt + raw response + the resulting outcome), and emit
        # a one-line log so a live run is reviewable end to end.
        _cap = _cap_of()
        _cap.clear()
        if isinstance(action, Plan):
            role, task_id = PM, "plan"
        elif isinstance(action, PMAssist):
            role, task_id = PM, action.task_id
        elif isinstance(action, GovernancePlan):
            role, task_id = PM, f"governance:{action.phase}"
        elif isinstance(action, GovernanceReview):
            role = PM if getattr(action, "reviewer_role", REVIEWER) == PM else REVIEWER
            task_id = action.artifact_id
        elif isinstance(action, GovernanceMaterialize):
            role, task_id = PM, "governance:materialize"
        elif isinstance(action, Assign):
            role, task_id = action.role, action.task_id
        else:
            role, task_id = "", ""
        try:
            outcome = _execute(action, ledger)
        except _MemberCallFailed as failed:
            # F120: surface the classified member-call failure as a TurnOutcome.
            # made_progress=False so the loop's per-member counter increments;
            # the loop (not the runner) owns raising the attention Problem.
            outcome = TurnOutcome(
                kind="member_failed", made_progress=False,
                reason=f"{failed.failure.status}: {failed.failure.detail}",
                member_id=failed.member_id, member_failure=failed.failure,
                member_role=failed.role, member_route=failed.route)
        if _cap.get("member_id") and not outcome.member_id:
            # F120: successful member turns must carry their identity too; the
            # loop uses that to reset consecutive failure streaks for the member.
            outcome.member_id = str(_cap.get("member_id", ""))
            outcome.member_role = str(_cap.get("member_role", ""))
            outcome.member_route = str(_cap.get("member_route", ""))
        if _cap.get("model_calls"):
            outcome.model_calls = int(_cap["model_calls"])
        if _cap.get("repairs"):
            outcome.repairs = int(_cap["repairs"])
        if _cap.get("prompt") is not None:
            parse_ok = _cap.get(
                "parse_ok",
                outcome.kind not in ("noop",) or not _cap.get("response"),
            )
            _u = _cap.get("usage") or {}  # F143: gateway token usage for this turn
            # F143-01 Slice A: stamp the resolved route the gateway dispatched to.
            # ``member_route`` is captured in ``caller`` from the member's resolved
            # ``gateway_route_id`` on EVERY member turn (PM/review/test included),
            # independent of the F129 assignment gate — so this is the authoritative
            # resolved-route value here. Fall back to the F129 assignment's
            # ``route_id`` only when the caller didn't capture one.
            _assignment = _cap.get("model_assignment")
            _resolved_route = str(_cap.get("member_route") or "")
            if not _resolved_route and isinstance(_assignment, dict):
                _resolved_route = str(_assignment.get("route_id") or "")
            # F143-01 Slice C/D: derive provenance + EFFECTIVE ints + cli_overhead
            # from the merged per-turn accumulator (see _merge_call_usage). The
            # accumulator sums a per-call EFFECTIVE value (a call's measured value if
            # measured, else its estimate), so a turn that MIXES a measured call with a
            # dark call keeps BOTH calls' spend and reports honest provenance —
            # measured_partial, never over-claimed measured (the Slice-D hybrid fix).
            # cli_overhead is the CLI's vendor-managed inner context we can't see,
            # inferred as clamp>=0(measured_input - RAW estimated_input) — only for a CLI
            # provider that actually reported input (Layer-1 composition, spec D6/inv 6).
            # It is measured against the RAW (uncalibrated) input estimate: the CLI's
            # calibration factor learns to absorb this very overhead, so measuring it
            # against the calibrated estimate would collapse the Layer-2 band toward 0.
            _total_calls = int(_u.get("total_calls") or 0)
            _measured_calls = int(_u.get("measured_calls") or 0)
            _measured = _measured_calls > 0
            _measured_input = _u.get("measured_input") if _measured_calls else None
            _measured_output = _u.get("measured_output") if _measured_calls else None
            _estimated_input = _u.get("estimated_input")
            _estimated_output = _u.get("estimated_output")
            # Effective ints = the per-call effective sums (correct for all-measured,
            # all-dark, AND mixed turns). Fall back to the estimated/measured-only sums
            # for a legacy accumulator shape that lacks the effective keys.
            _effective_input = _u.get("effective_input")
            if _effective_input is None:
                _effective_input = (_measured_input if _measured_input is not None
                                    else _estimated_input)
            _effective_output = _u.get("effective_output")
            if _effective_output is None:
                _effective_output = (_measured_output if _measured_output is not None
                                     else _estimated_output)
            _provenance = _derive_provenance(
                measured_input=_measured_input, measured_output=_measured_output,
                estimated_input=_estimated_input, estimated_output=_estimated_output,
                raw_usage_available=_measured,
                measured_calls=_measured_calls, total_calls=_total_calls)
            _provider_class = str(_u.get("provider_class") or "")
            # Overhead basis: the RAW (uncalibrated) input estimate; fall back to the
            # calibrated estimate only for an older accumulator that never carried a raw
            # sum (preserves prior behavior for legacy shapes).
            _raw_estimated_input = _u.get("estimated_input_raw")
            if not isinstance(_raw_estimated_input, int):
                _raw_estimated_input = _estimated_input
            _cli_overhead = None
            if (_provider_class.endswith("_cli")
                    and isinstance(_measured_input, int)
                    and isinstance(_raw_estimated_input, int)):
                _cli_overhead = max(0, _measured_input - _raw_estimated_input)
            _estimator = _get_token_estimator()
            # F143-01 Slice F: the Layer-1 per-segment composition of the prompt this
            # turn sent (only present for a segmented builder's initial call).
            _composition = _u.get("composition")
            if not isinstance(_composition, dict):
                _composition = None
            store.record_turn(
                role=role, member_id=_cap.get("member_id", ""), task_id=task_id,
                prompt=_cap.get("prompt", ""), response=_cap.get("response", ""),
                outcome=outcome.kind, reason=outcome.reason or "",
                parse_ok=parse_ok, duration_ms=_cap.get("duration_ms", 0),
                model_assignment=_assignment,
                route_id=_resolved_route or None,
                input_tokens=_effective_input,
                output_tokens=_effective_output,
                cache_read_input_tokens=_u.get("cache_read"),
                cache_write_input_tokens=_u.get("cache_write"),
                measured=_measured,
                provenance=_provenance,
                composition=_composition,
                measured_input=_measured_input,
                measured_output=_measured_output,
                estimated_input=_estimated_input,
                estimated_output=_estimated_output,
                estimated_input_raw=(_raw_estimated_input
                                     if isinstance(_raw_estimated_input, int)
                                     else None),
                cli_overhead_tokens=_cli_overhead,
                estimator_method=getattr(_estimator, "method", None),
                # The live (provider,model) factor actually applied to this turn's
                # estimates (last call wins), not the base estimator's constant 1.0.
                # Falls back to the base factor when no call carried one (unreported).
                calibration_factor=_u.get(
                    "calibration_factor",
                    getattr(_estimator, "calibration_factor", None)))
            assignment_raw = _cap.get("model_assignment")
            if isinstance(assignment_raw, dict) and assignment_raw.get("route_id"):
                try:
                    from .model_catalog import load_catalog
                    from .performance_corpus import (
                        append,
                        make_attempt,
                    )

                    route_id = str(assignment_raw["route_id"])
                    entry = load_catalog([route_id])[route_id]
                    run_state = store.get_run_state()
                    payload = dict(
                        assignment_id=str(assignment_raw.get("assignment_id") or ""),
                        project_id=store.project_id,
                        run_id=str(run_state.get("started_at") or store.project_id),
                        task_id=task_id,
                        member_id=str(_cap.get("member_id") or ""),
                        route_id=entry.route_id,
                        task_type=str(assignment_raw.get("task_type") or "implementation"),
                        difficulty_tier=str(assignment_raw.get("difficulty_tier") or "mid"),
                        capability_tier=entry.capability_tier,
                        cost_tier=entry.cost_tier,
                        latency_ms=int(_cap.get("duration_ms") or 0),
                        reason_code=str(outcome.reason or "")[:120],
                        triggered_escalation=bool(outcome.unproductive),
                        task_had_prior_escalation=int(
                            assignment_raw.get("escalation_count") or 0
                        ) > 0,
                    )
                    if outcome.kind == "member_failed":
                        # Gateway failure is final immediately (Slice 5).
                        append(make_attempt(outcome="gateway_failed", **payload))
                    elif outcome.unproductive or not bool(parse_ok):
                        # Unproductive/unparseable turn is final immediately (Slice 5).
                        append(make_attempt(outcome="rejected", **payload))
                    else:
                        # Productive turn is PENDING until task-boundary review
                        # closes or escalates it (Slice 5, Contract #7). Buffer
                        # on the task's _extras so it survives restarts and gets
                        # attributed correctly at task-done or task-escalated.
                        task_row = next(
                            (t for t in store.list_tasks() if t.task_id == task_id),
                            None,
                        )
                        if task_row is not None:
                            pending = list((task_row._extras or {}).get(
                                "_f129_pending") or [])
                            pending.append(dict(payload))
                            store.update_task(task_id, _f129_pending=pending)
                except Exception:
                    _log.exception("failed to record F129 performance attempt")
            _log.info("coding turn: project=%s role=%s task=%s -> %s%s (%dms)",
                      store.project_id, role, task_id, outcome.kind,
                      f" [{outcome.reason}]" if outcome.reason else "",
                      _cap.get("duration_ms", 0))
        return outcome

    def delivery_review(ledger: Any) -> DeliveryReviewResult:
        """F146 Slice B: verify the INTEGRATED delivered head as a unit before a
        ``project_done`` is allowed to stick — a real reviewer over the WHOLE
        delivered diff plus the registered test suite, both bound to
        ``workspace.head()``. Never rubber-stamps: every recorded verdict comes
        from a real reviewer turn / real test run against the exact head. Bounded:
        cached once per unchanged delivered head. Fail-closed: reject / test
        failure / verify error does NOT mark done (findings are filed as dev tasks
        so Slice E's ``_has_open_work`` re-opens the run)."""
        if workspace is None:
            # No workspace to verify against (unit-test / no-workspace runs):
            # preserve the pre-F146 done behavior.
            return DeliveryReviewResult(passed=True, reason="no_workspace")

        def _cannot_verify(reason: str) -> DeliveryReviewResult:
            # An INABILITY to verify (git index-lock contention, a corrupt/missing
            # worktree, an unreadable registry) is a verify error — it must BLOCK
            # done, never pass it through (mirrors gather_merge_evidence's M1
            # preview_ok=False blocker; fail-closed per the golden constraint).
            # Record no verdict (the accept gate honestly stays unreviewed) and
            # file nothing (there is no code finding to fix — the next completion
            # claim retries; a persistent error stops via no_progress /
            # max_iterations, never a false `done`).
            try:
                store.record_decision(
                    title="delivery review could not run",
                    context="delivery_review", choice="delivery_review_error",
                    rationale=reason)
            except Exception:  # noqa: BLE001
                pass
            return DeliveryReviewResult(passed=False, filed_findings=False,
                                        reason=reason)

        try:
            head = workspace.head()
        except Exception:  # noqa: BLE001
            head = ""
        if not head:
            # An empty head on a REAL workspace is a verify error (git error / lock
            # contention) -> block done. Only a genuinely absent workspace (no
            # commits / does-not-exist, i.e. a degenerate/unit-test case) preserves
            # the pre-F146 done behavior.
            try:
                real_workspace = bool(workspace.exists())
            except Exception:  # noqa: BLE001
                real_workspace = True  # probe failed -> assume real -> fail-closed
            if real_workspace:
                return _cannot_verify("workspace head unavailable")
            return DeliveryReviewResult(passed=True, reason="no_head")
        # Bounded cost: one delivery review per unchanged delivered head.
        try:
            rs = store.get_run_state()
        except Exception:  # noqa: BLE001
            rs = {}
        if rs.get("delivery_reviewed_head") == head:
            return DeliveryReviewResult(
                passed=bool(rs.get("delivery_review_passed")), reason="cached")
        # A reviewer (falling back to the PM) is required for a real review. With
        # neither configured we cannot verify — record NOTHING (the accept gate
        # honestly stays unreviewed) and preserve prior done behavior for such
        # minimal teams; this is not a rubber-stamp (no verdict is fabricated).
        reviewer_members = members_by_role.get(REVIEWER) or members_by_role.get(PM)
        if not reviewer_members:
            return DeliveryReviewResult(passed=True, reason="no_reviewer")
        reviewer_member = reviewer_members[0]

        # 1) Reviewer over the WHOLE delivered diff, bound to `head`. A preview
        #    failure means a corrupt/missing worktree (F087-15 M1) — do NOT review
        #    a blank diff and pass; block done as an unverifiable delivery.
        try:
            diff = str((workspace.preview() or {}).get("diff") or "")
        except Exception:  # noqa: BLE001
            return _cannot_verify("delivered diff unavailable (preview failed)")
        approved = False
        findings: list[dict[str, Any]] = []
        try:
            parsed = _parse_member_turn(
                REVIEWER, _DELIVERY_TASK_ID, reviewer_member,
                _delivery_review_prompt(store, head, diff),
                context="delivery_review", related_task_ids=[])
        except _MemberCallFailed as exc:
            # Could not run the reviewer -> do NOT mark done and record NO verdict
            # (the gate stays unreviewed). A genuine inability to verify, not a
            # rubber-stamp; the loop retries on the next completion claim.
            store.record_decision(
                title="delivery review could not run",
                context="delivery_review", choice="delivery_review_error",
                rationale=f"reviewer call failed: {exc.failure.status}")
            return DeliveryReviewResult(passed=False, filed_findings=False,
                                        reason="reviewer_call_failed")
        if isinstance(parsed, TurnParseError):
            store.record_decision(
                title="delivery review rejected (unparseable)",
                context="delivery_review", choice="review_rejected",
                rationale=f"{parsed.code.value}: {parsed.detail}",
                extra={"reviewed_head": head})
            approved = False
        elif parsed.intent.reviewed_head != head:
            # Reviewed a different head than delivered -> stale, does not count.
            # Recorded as a NON-verdict so the gate stays unreviewed (fail-closed).
            store.record_decision(
                title="delivery review stale head",
                context="delivery_review", choice="stale_review_head",
                rationale=(f"reviewed_head {parsed.intent.reviewed_head!r} != "
                           f"delivered head {head!r}"))
            approved = False
        else:
            approved = bool(parsed.intent.approved)
            findings = [
                {"severity": f.severity, "title": f.title, "body": f.body,
                 "path": f.path, "blocking": f.severity == "blocking"}
                for f in parsed.intent.findings
            ]
            store.record_decision(
                title="delivery review verdict",
                context="delivery_review",
                choice="review_approved" if approved else "review_rejected",
                rationale=f"delivery reviewer verdict (approved={approved})",
                extra={"reviewed_head": head})

        # 2) Tests: run ALL registered commands for real against the delivered
        #    master root, bound to `head`. Deterministic (no model command
        #    selection) so the test verdict cannot be gamed — strongest possible
        #    anti-rubber-stamp. No registered commands -> nothing to run here
        #    (Slice D handles the vacuous-tests gate side).
        try:
            registry = store.get_test_commands()
        except Exception:  # noqa: BLE001 — a corrupt registry is a verify error
            return _cannot_verify("test registry unavailable")
        tests_passed = True
        tests_failed_detail = ""
        if registry:
            command_ids = list(registry.keys())
            try:
                session = run_test_commands(
                    workspace.root(), registry, command_ids,
                    should_cancel=should_cancel,
                    require_sandbox=store.get_require_sandbox())
                store.record_test_run(session, task_id=_DELIVERY_TASK_ID, head=head)
                tests_passed = bool(session.passed)
                if not tests_passed:
                    tests_failed_detail = "; ".join(
                        f"{r.command_id}={r.status}/{r.exit_code}"
                        for r in session.results)
                store.record_decision(
                    title="delivery tests", context="delivery_review",
                    choice="tested_pass" if tests_passed else "tested_fail",
                    rationale=f"command_ids={command_ids}; {tests_failed_detail}")
            except Exception as exc:  # noqa: BLE001
                store.record_decision(
                    title="delivery tests could not run",
                    context="delivery_review", choice="delivery_test_error",
                    rationale=str(exc))
                tests_passed = False

        # 3) Runtime launch evidence (F146 Slice C): for a runnable managed_local
        #    profile, LAUNCH the delivered program headless + bounded and require
        #    it to get past startup without a traceback — catching runtime-only
        #    crashes (the `pygame.font` case) that per-PR review + unit tests miss.
        #    Deterministic launch of the exact `head`; recorded against it.
        #    Non-runnable projects skip the probe (launched_clean vacuously True).
        launched_clean, launch_cannot_verify, launch_detail = \
            _delivery_launch_evidence(store, workspace, head,
                                      should_cancel=should_cancel)

        # `passed` requires a clean launch too. A launch cannot_verify leaves
        # launched_clean=False so it also fails `passed`.
        passed = approved and tests_passed and launched_clean
        # Cache once-per-head ONLY for a real verdict. A cannot_verify (inability
        # to launch) is NOT cached, so the next completion claim retries the launch
        # instead of resting on a false negative (matches _cannot_verify above; a
        # persistent failure stops via no_progress, never a false `done`).
        if not launch_cannot_verify:
            try:
                store.set_run_state(delivery_reviewed_head=head,
                                    delivery_review_passed=passed)
            except Exception:  # noqa: BLE001
                pass
        if passed:
            return DeliveryReviewResult(passed=True, reason="reviewed")

        # Fail-closed: file the failure as dev work so Slice E's `_has_open_work`
        # re-opens the run. The team fixes it, the head changes, and the next
        # completion claim re-reviews the new head (the cache is keyed by head).
        filed = False
        if not approved and findings:
            store.add_task(
                title="fix delivery review findings", role=DEV,
                reason_summary=_reason_from_findings(findings),
                detail=("The delivery review of the integrated result requested "
                        "changes. Address these findings and re-deliver: "
                        f"{_detail_from_findings(findings)}."))
            filed = True
        elif not approved:
            store.add_task(
                title="fix delivery review", role=DEV,
                reason_summary="Delivery review requested changes",
                detail=("The delivery review of the integrated result did not "
                        "approve; see the decision log and re-deliver."))
            filed = True
        if not tests_passed:
            store.add_task(
                title="fix delivery tests", role=DEV,
                reason_summary="Delivery tests failed",
                detail=("The registered test suite failed against the delivered "
                        f"head. Make the tests pass: {tests_failed_detail}."))
            filed = True
        if launch_cannot_verify:
            # An INABILITY to launch the runnable delivered program (setup/sandbox/
            # spawn failure, cancel) is a verify error — record a decision but file
            # NO finding (there is no code defect to fix; the run retries and stops
            # via no_progress on a persistent failure). Never marks `done`.
            store.record_decision(
                title="delivery launch could not run",
                context="delivery_review", choice="delivery_launch_error",
                rationale=launch_detail[:1000])
        elif not launched_clean:
            # A real startup crash IS a delivered-code defect (the pygame.font
            # case). File it as dev work with the traceback so the team fixes the
            # crash and re-delivers; blocks `done` until it launches cleanly.
            store.add_task(
                title="fix runtime launch crash", role=DEV,
                reason_summary="The delivered program crashed on launch",
                detail=("The delivered program crashed on startup when launched "
                        "headless for delivery verification. Fix the crash so it "
                        f"launches without error: {launch_detail}"))
            filed = True
        reason = "launch_cannot_verify" if launch_cannot_verify else "rejected"
        return DeliveryReviewResult(passed=False, filed_findings=filed,
                                    reason=reason)

    # Expose the delivery-review verifier as an attribute so the ~50 existing
    # callers that treat the return as a single ``run_turn`` callable are
    # unaffected; the production caller reads ``run_turn.delivery_review``.
    run_turn.delivery_review = delivery_review  # type: ignore[attr-defined]
    return run_turn


def gateway_member_caller(gateway: Any) -> MemberCaller:
    """Wrap an async LocalGateway into the sync ``(member, prompt) -> text``
    caller the runner needs. Runs each call on the process-wide shared event
    loop (F087 Slice 0) instead of a fresh ``asyncio.run`` loop per call, so the
    provider concurrency semaphores bind to one loop and bound concurrency
    correctly when many worker threads call at once (the old per-thread loops
    deadlocked on the shared semaphore). Gateway/request imports are lazy so this
    module pulls no egress at import time."""

    def caller(member: dict[str, Any], prompt: str) -> str:
        from errorta_council.gateway_local import LocalCouncilModelRequest
        tl = member.get("turn_limits") or {}
        gen = member.get("generation") or {}
        req = LocalCouncilModelRequest(
            role=str(member.get("role", "answerer")),
            route_id=str(member.get("gateway_route_id", "")),
            provider=str(member.get("provider_kind", "local")),
            model=str(member.get("model") or member.get("model_display") or ""),
            messages=[{"role": "user", "content": prompt}],
            max_output_tokens=int(tl.get("max_output_tokens", 2048) or 2048),
            temperature=float(gen.get("temperature", 0.3) or 0.3),
            # CLI-backed members (claude_cli/codex_cli/cursor_cli) run a full agentic loop per
            # turn and routinely need minutes — a 180s cap timed turns out
            # constantly (each crash requeues the task, so the team spun without
            # landing anything). Default to 10 min; per-room override via
            # turn_limits.timeout_seconds.
            timeout_seconds=int(tl.get("timeout_seconds", 600) or 600),
        )
        from errorta_model_gateway.loop_bridge import run_coro
        result = run_coro(gateway.call(req))
        # F143-01 Slice C: compute an estimate from OUR OWN bytes on every real
        # gateway call — this is the always-available meter. We have both halves
        # here: the full assembled ``prompt`` we sent and ``result.content`` we got
        # back (before the ledger caps the response text). content_kind="mixed"
        # because a coding prompt/response interleaves prose, code, and JSON.
        raw_usage_available = bool(getattr(result, "raw_usage_available", False))
        measured_input = getattr(result, "input_tokens", None)
        provider_class = str(getattr(result, "provider_class", "") or "")
        model = str(getattr(result, "model", "") or "")
        estimator = _get_token_estimator()  # RAW base (factor 1.0)
        # F143-01 calibration: the stored (provider,model) factor corrects the base
        # heuristic's systematic bias vs THIS provider's real tokenizer. Read fresh so
        # a factor learned on an earlier turn steers later turns' estimates. Applied to
        # both input and output (same base heuristic, same tokenizer bias).
        factor = _read_calibration_factor(provider_class, model)
        raw_output = estimator.estimate(
            getattr(result, "content", "") or "", content_kind="mixed")
        estimated_output = _apply_calibration(raw_output, factor)
        # F143-01 Slice F: if the prompt this call sent was built by a segmented
        # builder, adopt the per-segment categorized sum as the RAW input estimate (it
        # UPGRADES Slice C's whole-string estimate into the attributed sum — same
        # ballpark, now itemized) and carry the ``composition`` block through the sink
        # to ``record_turn``. Matched on exact prompt equality, so a corrective-retry
        # re-prompt (unsegmented) cleanly falls back to the whole-string estimate. The
        # composition block stays RAW (provider-agnostic Layer-1 bytes); only the
        # top-line ``estimated_input`` is calibrated.
        composition = _take_pending_composition(prompt)
        if isinstance(composition, dict) and isinstance(
                composition.get("sent_total"), int):
            raw_input = int(composition["sent_total"])
        else:
            raw_input = estimator.estimate(prompt, content_kind="mixed")
        estimated_input = _apply_calibration(raw_input, factor)
        # F143: stash the result's token usage + our estimate for the run_turn
        # capture wrapper to thread into record_turn. ``raw_usage_available``
        # distinguishes real provider counts from absent ones; the estimate is
        # always present so a dark turn rolls up as ``estimated``, not ``unreported``.
        _usage_sink.last = {
            "input_tokens": measured_input,
            "output_tokens": getattr(result, "output_tokens", None),
            "cache_read_input_tokens": getattr(result, "cache_read_input_tokens", None),
            "cache_write_input_tokens": getattr(result, "cache_write_input_tokens", None),
            "estimated_input": estimated_input,
            "estimated_output": estimated_output,
            # The RAW (uncalibrated) Layer-1 input estimate — "what Errorta actually
            # sent" in our own tokenizer. Kept alongside the calibrated top-line so
            # cli_overhead stays honest: a CLI factor absorbs the vendor's hidden inner
            # context, so measuring overhead against the CALIBRATED estimate would
            # collapse it toward 0. Overhead = measured − RAW, not measured − calibrated.
            "estimated_input_raw": raw_input,
            "provider_class": provider_class,
            "model": model,
            "measured": raw_usage_available,
            # The calibration factor actually applied to this call's estimates, so the
            # persisted turn reports the live factor (not a hardcoded 1.0).
            "calibration_factor": factor,
            # F143-01 Slice F: the Layer-1 composition (only present for a segmented
            # builder's first call — corrective retries carry none).
            "composition": composition,
        }
        # When the provider actually reported input, feed the calibrator so this
        # (provider,model)'s factor tracks reality over time. Best-effort + lock-
        # guarded (see _update_calibration) — never breaks the turn. Fed the RAW
        # (factor-1.0) estimate, NOT the calibrated one: the factor must converge to
        # reported/raw so ``calibrated = raw * factor`` tracks reported — feeding it the
        # calibrated value would create a drifting feedback loop.
        if raw_usage_available and isinstance(measured_input, int):
            _update_calibration(provider_class, model, measured_input, raw_input)
        return getattr(result, "content", "") or ""

    return caller


def members_by_coding_role(members: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        if m.get("enabled", True):
            out.setdefault(coding_role_of(m), []).append(m)
    return out


class CodingRunner:
    """Drive one autonomous coding run end to end against real member calls."""

    def __init__(self, project_id: str, members: list[dict[str, Any]],
                 caller: MemberCaller, *, root: Any = None,
                 guardrail_enabled: bool = True,
                 setup_workspace: bool = True) -> None:
        self.store = LedgerStore(project_id, root=root)
        self.members = members
        self.caller = caller
        self.guardrail_enabled = guardrail_enabled
        self.workspace: Optional[CodingWorkspace] = None
        if setup_workspace:
            proj = self.store.get_project()
            self.workspace = CodingWorkspace(project_id, self.store)
            self.workspace.setup(target=proj.target, repo_path=proj.repo_path)

    def run(self, policy: CodingAutonomyPolicy, *, counters: Any = None,
            should_cancel: Optional[Callable[[], bool]] = None,
            manage_lifecycle: bool = True) -> LoopResult:
        # F087-19 #4: own the run lifecycle so a DIRECT CodingRunner.run() (e.g. a
        # script) leaves run_state.json correct (running -> stopped/failed), not
        # stuck at "idle" while the process is alive. The product route manages
        # its own lifecycle (concurrency lock, cancel, recovery flags) and passes
        # manage_lifecycle=False to avoid double-writes.
        from .ledger import _now
        if manage_lifecycle:
            self.store.set_run_state(status="running", started_at=_now(),
                                     ended_at=None, stop_reason=None, last_error=None)
        # F124-followup: reclaim tasks left wedged in 'doing' by a prior run that
        # ended terminally (e.g. a member_unhealthy stop). recover_orphaned_run
        # only requeues on an orphaned 'running' status, so a clean terminal stop
        # would otherwise strand its in-flight tasks forever (the scheduler only
        # dispatches 'todo'). At run start nothing is in flight in THIS process, so
        # every 'doing' task is a safe-to-requeue orphan.
        from .run_recovery import reclaim_stranded_inflight
        reclaim_stranded_inflight(self.store, reason="run_start")
        # F087-15 M2: persist a worktree fingerprint so resume can verify the
        # worktree wasn't deleted/reset between interruption and resume.
        if self.workspace is not None:
            try:
                self.store.set_run_state(
                    workspace_fingerprint=self.workspace.workspace_fingerprint())
            except Exception:
                pass
        # F139 WS-A: seed the foundation gate BEFORE the loop starts so a fresh
        # `new` project (empty master) is clamped to 1 worker from iteration 0 —
        # the team must scaffold a buildable base before fanning out.
        refresh_foundation_status(self.store, self.workspace)
        by_role = members_by_coding_role(self.members)
        member_pairs = [(m["id"], coding_role_of(m)) for m in self.members
                        if m.get("enabled", True)]
        # F127: member tier ranks so the escalate-up ladder reassigns a task a
        # weak member can't do to a stronger one.
        from .model_tier import member_rank
        member_tiers = {
            m["id"]: member_rank(m) for m in self.members if m.get("enabled", True)
        }
        run_turn = build_run_turn(
            self.store, self.workspace, by_role, self.caller,
            guardrail_enabled=self.guardrail_enabled,
            should_cancel=should_cancel)
        try:
            res = run_coding_loop(self.store, member_pairs, policy,
                                  run_turn=run_turn, counters=counters,
                                  should_cancel=should_cancel,
                                  member_tiers=member_tiers,
                                  delivery_review=getattr(
                                      run_turn, "delivery_review", None))
        except Exception as exc:
            if manage_lifecycle:
                self.store.set_run_state(status="failed", last_error=str(exc),
                                         ended_at=_now())
            raise
        # F088-06: final projection so the index reflects end-of-run state even
        # if no merge happened this run. At run end the worktree is quiescent, so
        # this also re-ingests the merged master code into a bound project corpus
        # (so the next run's PM/dev retrieval sees what the team built). Guarded —
        # never affects the run result.
        _sync_grounding(self.store, self.workspace, refresh_corpus=True)
        if manage_lifecycle:
            self.store.set_run_state(
                status="stopped", stop_reason=res.stop_reason, ended_at=_now(),
                counters={
                    "iterations": res.counters.iterations,
                    "turns_repaired": res.counters.turns_repaired,
                    "model_escalations": res.counters.model_escalations,
                    "task_reassignments": res.counters.task_reassignments,
                    "pm_assists": res.counters.pm_assists,
                })
        return res
