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

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter
from typing import Any, Callable, NamedTuple, Optional

from . import capabilities as _capabilities
from . import detector_state as _detector_state
from . import drop_ledger as _drop_ledger
from . import drop_reasons as _drop_reasons
from . import gate_state as _gate_state
from . import paths as _paths
from . import task_dedupe
from .autonomy import (
    CodingAutonomyPolicy,
    LoopResult,
    TurnOutcome,
    run_coding_loop,
    window_counters_to_dict,
)
from .completion import (
    acceptance_gate_status,
    mechanic_gate_reason,
    mechanic_gate_status,
    pending_completion_work,
    summarize_open_items,
)
from .ledger import LedgerStore, Task, format_focus_lines
from .orientation import build_orientation_packet
from .schemas import (
    BlockedIntent,
    TurnErrorCode,
    TurnParseError,
    blocked_example,
    minimal_valid_example,
    parse_coding_turn,
)
from .skills import primary_skill, record_turn_skill
from .testing import (
    TestRunResult,
    TestRunSession,
    resolve_commands,
    run_test_commands,
)
from .topology import (
    DESIGNER,
    DEV,
    PM,
    REVIEWER,
    TESTER,
    Assign,
    DesignPlan,
    GateRun,
    GovernanceMaterialize,
    GovernancePlan,
    GovernanceReview,
    LastWord,
    Merge,
    Plan,
    PMAssist,
    coding_role_of,
)
from .turn_controller import CodingTurnController, tool_catalog_text
from .workspace import CodingWorkspace

MemberCaller = Callable[[dict[str, Any], str], str]

# Spec 17 (Item 1): the gateway vendors that actually HONOR the read-only cwd
# invocation ``repo_read`` promises. Only the ``claude_cli`` provider runs the
# turn with cwd=worktree and a Read/Grep/Glob allowlist and consumes the
# ``repo_read_root`` metadata (see errorta_model_gateway/providers/
# async_claude_cli.py — it is the single consumer today). Every other vendor
# (codex_cli, cursor_cli, remote APIs) silently ignores the metadata, so naming
# Read/Grep/Glob in its tool catalog would be a lie and forwarding the key a
# no-op. A future vendor that grows a real read surface is a one-line add here.
_REPO_READ_HONORING_VENDORS = frozenset({"claude_cli"})


def _member_vendor(member: dict[str, Any]) -> str:
    """The gateway vendor (provider class) for a member — the prefix of its
    ``gateway_route_id`` before the first '.' (``claude_cli.opus`` ->
    ``claude_cli``), falling back to ``provider_kind`` when the route carries no
    prefix."""
    route_id = str(member.get("gateway_route_id") or "").strip()
    if "." in route_id:
        return route_id.split(".", 1)[0]
    return route_id or str(member.get("provider_kind") or "")


from errorta_council.reasoning_budget import (  # noqa: E402
    resolve_turn_budget as _resolve_turn_budget,
)


def _member_model_id(member: dict[str, Any]) -> str:
    """The provider-side model id for a member's gateway request.

    Prefers an explicit ``model`` / ``model_display``, then falls back to the
    ``gateway_route_id`` suffix — the SAME derivation ``bind_member_route`` uses
    (``model_assignment.py:54-56``), so an unbound member behaves like a bound one.

    Why the fallback is needed: a PERSISTED coding member has neither key.
    ``recipes.resolve_team`` writes only id/role/enabled/metadata/model_mode/
    gateway_route_id, and ``bind_member_route`` — the only thing that sets ``model``
    — has one call site, ``runner.py``'s **Assign** (worker task) path. PM governance
    and governance review turns pass the raw member from ``_member(...)``, so before
    this fallback they sent ``model=""``.

    That was not harmless on a local route. ``LocalGateway.call`` does not divert a
    ``local.*`` route to ``_registry_dispatch`` (which derives the model from the
    route id at ``gateway_local.py:285``) because ``local`` is an allowed legacy
    provider; it falls through to ``_ollama_dispatch``, which posts the empty string.
    Ollama answers ``HTTP 400 {"error":"model is required"}``, which
    ``_ollama_dispatch`` maps to ``FatalError("gateway_4xx: 400")`` — a hard failure
    on every PM-governance and governance-review turn of an all-local team.

    Returns ``""`` when there is nothing to derive from: this resolves an id, it
    never invents one.
    """
    explicit = member.get("model") or member.get("model_display")
    if explicit:
        return str(explicit)
    from .model_catalog import model_id_from_route
    return model_id_from_route(member.get("gateway_route_id") or "")


def _vendor_honors_repo_read(member: dict[str, Any]) -> bool:
    """True when the member's vendor actually runs the read-only cwd invocation
    ``repo_read`` promises (i.e. consumes ``repo_read_root`` metadata)."""
    return _member_vendor(member) in _REPO_READ_HONORING_VENDORS


def _member_honors_repo_read(member: dict[str, Any], policy_flag: bool) -> bool:
    """Spec 17 (Item 1): ``repo_read`` is effectively ON for a member only when
    BOTH the policy flag is on AND the member's vendor honors the read-only cwd
    invocation. Gating the per-turn ``repo_read_root`` tag on this (rather than
    the raw policy flag) keeps the prompt catalog, the forwarded metadata, and
    the grounding-reflex check all consistent: a codex/cursor member never
    receives Read/Grep/Glob, so its catalog shows the context_request/off
    variant and no no-op key is forwarded."""
    return bool(policy_flag) and _vendor_honors_repo_read(member)


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
    # Spec 12 added `gate_output` and Spec 22-28 P0.4 reserves `governance_state`;
    # both were already (or are about to be) used as `PromptSegment.class_` values.
    # This tuple is DOCUMENTATION — nothing validates against it, and
    # `content_kind_for_class` falls through to "prose" for an unlisted class — so
    # naming them here is a tidy-up, not a behaviour change. It lives in the prep PR
    # so two feature branches don't both edit this line.
    "gate_output", "governance_state",
)

# --- Spec 22-28 batch (prep PR P0.4) — the prompt segment ORDER contract ----- #
#
# Three specs add or edit prompt content in these same builders (SPEC-24's
# `governance_state` segment, SPEC-25's `_corrective_turn_prompt`, SPEC-23's
# `_last_word_prompt`). Fixing the ORDER here, before any of them lands, is what
# keeps their diffs from racing: each spec inserts at an already-reserved site
# rather than choosing (and defending) a position of its own.
#
# The tail of every member prompt runs:
#
#     gate_output  ->  governance_state  ->  tool_guidance  ->  standing rules
#
# Read as: OBSERVED WORLD first (what the acceptance gate reported, then how close
# the run is to its own limits), then GUIDANCE (what this role may actually do),
# then the standing role instructions + envelope schema. The instructions stay
# LAST so they are the most recent thing the model reads.
#
# `gate_output` is already placed in the dev / reviewer / tester builders (Spec 12)
# and `tool_guidance` in all four (Spec 15 / Spec 17). `governance_state` does not
# exist yet: its insertion point is reserved by comment at the exact site in
# `_pm_prompt_segments`, and `tests/coding/test_prompt_segments_golden.py`'s
# reference builder calls a stub at the same position, so SPEC-24 lands as a
# one-line diff on each side and the goldens stay byte-identical until it does.
#
# NOTE for SPEC-24: this order supersedes that spec's own "Item 5 — Where the
# segment goes", which places `governance_state` AFTER the capability
# `tool_guidance` segment. The batch plan's order is the de-conflict authority and
# both readings keep the standing instructions last.
PROMPT_TAIL_SEGMENT_ORDER = (
    "gate_output", "governance_state", "tool_guidance", "role_instructions",
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


_FIX_TASK_STDERR_CAP = 2000


def _failed_stderr_appendix(results: list[Any]) -> str:
    """Spec 11 (P1b): the failing commands' verbatim ``stderr_preview``, capped.

    The fix-task detail historically carried only ``cmd=status/exit_code`` — the
    raw error (``TestRunResult.stderr_preview``, already captured by the tester)
    was dropped, so the fixing dev never saw WHY the test failed. Reassemble the
    non-passing results' stderr previews into a bounded block to append to the
    fix-task detail. Empty when nothing failed or no stderr was captured."""
    parts: list[str] = []
    for r in results or []:
        if getattr(r, "passed", True):
            continue
        preview = str(getattr(r, "stderr_preview", "") or "").strip()
        if preview:
            parts.append(f"[{getattr(r, 'command_id', '?')}] {preview}")
    if not parts:
        return ""
    return "\n".join(parts)[:_FIX_TASK_STDERR_CAP]


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


def _finding_class(findings: list[dict[str, Any]]) -> frozenset[str]:
    """Spec 16 (Item 1): the normalized token set over ALL of a rejection's
    findings — not just the first blocking one, because Specs 14/15 flag and
    suppress findings, so keying on "the first" would read an absent value and let
    the livelock survive. Two restatements of the same demand ("no evidence tests
    were run" / "no evidence that the tests were actually run") collapse to one
    class. An empty finding set yields the empty class, and empty compares EQUAL to
    empty — a run of contentless rejections should break the chain (the observed
    pathology)."""
    parts: list[str] = []
    for f in findings:
        parts.append(str(f.get("title") or ""))
        parts.append(str(f.get("body") or ""))
    return task_dedupe.normalized_tokens(*parts)


# GL02 (Item 1) — the machine-lane predicate. A finding is MACHINE-LANE when its
# reason turns on runtime/execution SEMANTICS — does it load, run, race, crash;
# were the tests run — rather than on the text of the diff. This is deliberately
# BROADER than ``capabilities.classify_task_text`` (which needs a run-verb AND an
# evidence-demand, and so misses "renders black at runtime", a claim with neither):
# a diff cannot evidence an executable question at ANY reviewer false-rejection
# rate, which is the invariant this predicate fences. It is CONSERVATIVE by
# construction — only the high-signal runtime phrases below trip it; every
# ambiguous reason falls to the judgment lane (Spec 14's existing behaviour), which
# is the documented fail-toward default. Bare "gate"/"probe"/"anchor" are NOT
# triggers: they name the machine lane's own evidence (a red such run BACKS a
# claim, §2 below) and appear verbatim in ordinary judgment findings ("stale gate")
# — including them would route work a DEV can act on.
# Deliberately anchored to runtime-OUTCOME context, not bare defect verbs. A real,
# diff-evidenced defect ("null deref in init — src/mod.js:1 crashes") is NOT a
# machine-lane claim just because it says "crashes"; only a claim tied to running/
# starting/rendering ("crashes on start", "renders black at runtime", "tests were
# not run") is. That is the line between a defect a DEV can fix from the diff and an
# executable question only the executor can decide.
_RUNTIME_CLAIM_PATTERNS = tuple(re.compile(p) for p in (
    r"\bat runtime\b", r"\bat start(?:up)?\b", r"\bon (?:start|startup|launch|boot)\b",
    r"\bcrash\w*\s+(?:on|at|during|when|the)\b",
    r"\brace condition\b", r"\bdead ?lock\w*", r"\blive ?lock\w*", r"\binfinite loop\b",
    r"\bblack (?:canvas|screen)\b", r"\bblank (?:canvas|screen)\b",
    r"\brenders?\s+(?:it\s+)?(?:black|blank|nothing)\b", r"\bnothing renders?\b",
    r"\bwon'?t\s+(?:load|run|start|render|boot|launch|init\w*)\b",
    r"\b(?:will|does|do)\s+not\s+(?:load|run|start|render|boot|launch|init\w*)\b",
    r"\bdoes\s?n'?t\s+(?:load|run|start|render|boot|launch|init\w*)\b",
    r"\bfails?\s+to\s+(?:load|run|start|render|boot|launch|init\w*)\b",
    r"\bno evidence\b", r"\buntested\b",
    r"\bnever\s+(?:been\s+)?(?:run|ran|executed|tested)\b",
    r"\b(?:were|was|are|is|been)\s+not\s+(?:run|ran|executed|tested)\b",
    r"\bnot\s+(?:actually\s+)?(?:been\s+)?(?:run|ran|executed)\b",
))


def is_execution_claim(finding: dict[str, Any]) -> bool:
    """GL02 (Item 1): does this finding reason about runtime/execution behaviour a
    DIFF cannot evidence (machine lane), rather than design/clarity/spec conformance
    (judgment lane)? Conservative: only the curated high-signal phrases in
    ``_RUNTIME_CLAIM_PATTERNS`` trip it; anything ambiguous stays judgment."""
    text = f"{finding.get('title') or ''} {finding.get('body') or ''}".lower()
    return any(p.search(text) for p in _RUNTIME_CLAIM_PATTERNS)


def _red_runtime_evidence(store: LedgerStore, head: str) -> bool:
    """GL02 (Item 2): is there EXECUTOR evidence that BACKS a runtime claim — a
    failing (red) acceptance-gate or ``web:probe`` run (GL01) at this head? A red
    run documents a real runtime failure a DEV can fix, so a machine-lane finding
    riding on it is a normal cited defect (actionable). A green run CONTRADICTS the
    claim (Spec 14 Item 5) and does not back it; no run at all leaves it
    unverifiable — both route rather than bounce to a DEV. Fully guarded."""
    head = str(head or "")
    try:
        runs = store.list_test_runs()
    except Exception:  # noqa: BLE001 — a read failure means "no evidence"
        return False
    for r in reversed(runs):
        if not isinstance(r, dict):
            continue
        if head and str(r.get("head") or "") != head:
            continue
        if r.get("passed") is False:
            return True
    return False


def _all_unactionable_by_dev(blocking: list[dict[str, Any]], *,
                             has_backing_evidence: bool = False) -> bool:
    """Spec 15 (Item 3) + GL02 (Items 1-2): are ALL blocking findings ones a
    write-only DEV cannot act on? A finding is unactionable when it is an execution
    demand (Spec 15), or uncited (Spec 14's ``cited: false``), OR — GL02 — a
    machine-lane runtime claim (``is_execution_claim``) with NO executor evidence to
    back it (``has_backing_evidence`` False): the LLM may not bounce an unverifiable
    executable question to a DEV who also cannot run anything. A single real, citable
    code finding — or a machine-lane finding BACKED by a red gate/probe — makes this
    False so the revise still fires."""
    for f in blocking:
        is_exec = _capabilities.classify_task_text(
            str(f.get("title") or ""), str(f.get("body") or "")) == "execution"
        is_uncited = f.get("cited") is False   # explicit False only; absent != uncited
        is_unbacked_runtime = is_execution_claim(f) and not has_backing_evidence
        if not (is_exec or is_uncited or is_unbacked_runtime):
            return False
    return True


def _already_requeued_for_head(store: LedgerStore, head: str) -> bool:
    """Spec 15 (Item 3): has a gate-output re-review already been queued for this
    PR head? Bounds the re-review to at most one per head so it cannot loop."""
    try:
        return any(d.get("choice") == "review_requeued_for_gate"
                   and str(d.get("head") or "") == head
                   for d in store.list_decisions())
    except Exception:  # noqa: BLE001
        return False


def _route_unactionable_rejection(
    store: LedgerStore, *, pr: dict[str, Any], task: Task,
    findings: list[dict[str, Any]],
) -> None:
    """Spec 15 (Item 3): a rejection whose blocking findings are all execution
    demands / uncited spawns no DEV revise. Instead:

    * with a gate, re-queue exactly ONE re-review per head — the review prompt
      already carries the acceptance-gate output (Spec 12 Item 3), so the demand is
      SATISFIED, not forwarded: a green gate lets the reviewer approve and the PR
      merges (this is what keeps a genuinely-fine PR from being stranded). A second
      unactionable rejection on the same head means the re-review did not resolve
      it, so it escalates to the PM;
    * without a gate, escalate to the PM once per lineage to register a gate or
      re-scope.

    The finding is preserved verbatim in the decision — routed, not dropped."""
    reason = _reason_from_findings(findings)
    gate = _gate_state.gate_available(store)
    head = str(pr.get("head") or "")
    if gate and not _already_requeued_for_head(store, head):
        store.record_decision(
            title=f"re-review queued with gate output: {pr['branch']}",
            context=f"pr {pr['pr_id']}", choice="review_requeued_for_gate",
            rationale=("blocking findings were all execution demands / uncited; "
                       "re-queued one re-review with the gate output attached rather "
                       "than spawning a DEV revise nobody could satisfy"),
            related_task_ids=[task.task_id], extra={"head": head})
        store.add_task(
            title=f"review PR: {pr['branch']} (re-review, gate output attached)",
            role=REVIEWER, pr_id=pr["pr_id"], depends_on=[task.task_id],
            reason_summary="re-review with acceptance-gate output attached",
            detail=("The prior review demanded execution evidence. The acceptance-"
                    "gate output is in your prompt now — decide on THAT evidence. Do "
                    "NOT reject again for missing execution evidence: if the gate is "
                    "green and the diff is sound, approve."))
        return
    choice = "finding_routed_to_gate" if gate else "finding_requires_absent_capability"
    store.record_decision(
        title=f"unactionable review rejection: {pr['branch']}",
        context=f"pr {pr['pr_id']}", choice=choice,
        rationale=(f"every blocking finding on {pr['branch']} is an execution demand "
                   "or uncited; no DEV revise spawned — "
                   + ("re-review with gate output did not resolve it, escalated to PM"
                      if gate else "no gate exists, escalated to PM to re-plan")),
        related_task_ids=[task.task_id])
    esc_title = f"unexecutable rejection: {pr['branch']}"
    if not any(t.role == PM and str(t.title or "") == esc_title
               and t.state not in ("done", "dropped") for t in store.list_tasks()):
        store.add_task(
            title=esc_title, role=PM,
            reason_summary=(reason or "review demanded evidence no role can produce"),
            detail=(
                f"PR {pr['pr_id']} on branch {pr['branch']} was rejected only on "
                "grounds a DEV cannot act on (execution evidence and/or uncited "
                "findings), and a gate-output re-review did not clear it. No revise "
                "was spawned. "
                + ("Decide: is the PR actually fine (re-plan to let it land), or does "
                   "a real, citable defect remain?" if gate else
                   "There is no acceptance gate to produce the demanded evidence — "
                   "plan a task that registers a test command, or re-scope so the "
                   "demand is satisfiable.")))


# GL02 (Item 3) — the per-head reviewer veto cap. The report (§3 Pathology 2 rec 4)
# escalates a reviewer's disagreement to the PM after 2 rejections; committed Spec 16
# caps the revise LINEAGE at depth 3. They count different things and COMPOSE: the
# per-head cap is reviewer-scoped and task-agnostic (the SAME PR head rejected
# twice), fires at the finer grain and FIRST — usually short-circuiting the lineage
# before it reaches depth 3 — while Spec 16's depth cap stays the hard structural
# backstop (three distinct heads each rejected once, or escalation that never
# converges). Both enter the ONE PM-replan escalation path; neither flips the verdict.
_REVIEWER_VETO_CAP = 2


def _head_veto_count(store: LedgerStore, head: str) -> int:
    """GL02 (Item 3): how many times this PR head has ALREADY been vetoed by a
    reviewer (an actionable rejection that reached the revise seam). Counts the
    ``reviewer_veto`` markers ``_handle_review_rejection`` records per head — mirrors
    Spec 15's ``_already_requeued_for_head`` decision-log bookkeeping."""
    head = str(head or "")
    try:
        return sum(1 for d in store.list_decisions()
                   if d.get("choice") == "reviewer_veto"
                   and str(d.get("head") or "") == head)
    except Exception:  # noqa: BLE001 — a read failure counts as no prior veto
        return 0


def _escalate_reviewer_veto(
    store: LedgerStore, *, pr: dict[str, Any], task: Task,
    findings: list[dict[str, Any]], count: int,
) -> None:
    """GL02 (Item 3): the same reviewer head vetoed this PR head ``count`` times
    (>= the cap). Surface the DISAGREEMENT to the PM instead of spawning yet another
    revise — the reviewer's finding + the diff are already on the PR record. Fail-
    closed: the PR stays ``changes_requested`` (set by the caller); the verdict is
    NOT flipped (Spec 16 non-goal, inherited). Deduped per head."""
    reason = _reason_from_findings(findings)
    store.record_decision(
        title=f"reviewer veto cap: {pr['branch']}",
        context=f"pr {pr['pr_id']}", choice="reviewer_veto_escalated",
        rationale=(f"the same PR head was rejected {count} times "
                   f"(cap {_REVIEWER_VETO_CAP}); escalated the disagreement to the PM "
                   "instead of spawning another revise — an ungrounded reviewer with "
                   "an absolute veto is a randomized rejection machine"),
        related_task_ids=[task.task_id, pr.get("task_id", "")])
    esc_title = f"reviewer disagreement: {pr['branch']}"
    if not any(t.role == PM and str(t.title or "") == esc_title
               and t.state not in ("done", "dropped") for t in store.list_tasks()):
        store.add_task(
            title=esc_title, role=PM,
            reason_summary=(reason or "reviewer rejected this head twice"),
            detail=(f"PR {pr['pr_id']} on branch {pr['branch']} has now been rejected "
                    f"{count} times by the reviewer on the SAME head, with no dev "
                    "revise spawned this round. Adjudicate the disagreement: is the "
                    "reviewer's objection real (re-scope / decompose), or is the PR "
                    "actually sound (re-plan to let it land)? Do not simply re-queue "
                    "the same review."
                    + (f" Reviewer's objection: {reason}." if reason else "")))


# GL04 (GAP-4) — the diff-level breaker. A revise lineage can spin in two shapes
# Spec 16's finding-CLASS breaker is structurally blind to: (a) DIFF-STASIS — the
# dev keeps resubmitting the essentially-same change (non-progressive iteration);
# (b) OSCILLATION/REVERT — a revision undoes an earlier one (A->B->A), which reads
# as progress to a class-only breaker because each hop presents a distinct class.
# We fingerprint each PR diff once at revise-task creation and, at the NEXT
# rejection, compare the live diff against the lineage — tripping Spec 16's ONE
# breaker as a THIRD condition, BEFORE its depth+class cap so we break at the
# reverting round, not after wasting rounds to depth 3.

_DIFF_HUNK_RE = re.compile(r"^@@ ")


def _norm_diff_line(line: str) -> str:
    """Whitespace-normalize a diff content line for near-identity comparison: drop
    the leading +/-, strip, and collapse internal whitespace runs. So an
    indentation-only reshuffle does not read as a different change."""
    return re.sub(r"\s+", " ", line[1:].strip())


def _diff_fingerprint(diff: str) -> dict[str, Any]:
    """GL04 (GAP-4): a cheap structural summary of a unified PR diff, JSON-round-
    tripping so it can ride on the revise task's ``_extras`` (like Spec 16's
    ``finding_class``). Two components:

    * ``shape`` — the sorted set of ``(path, hunk-header)`` tuples, plus ``digest``
      (a whitespace-normalized content hash), for NEAR-IDENTITY (stasis) comparison;
    * ``hunks`` — the SIGNED hunk multiset: ``[path, '+'|'-', normalized-line]``
      entries (repeats preserved), so a REVERT is detectable as an ancestor's
      multiset with the signs flipped.

    Empty/absent diff -> an empty fingerprint (compares as no-signal, never trips)."""
    shape: list[list[str]] = []
    hunks: list[list[str]] = []
    content: list[str] = []
    path = ""
    for raw in (diff or "").splitlines():
        if raw.startswith("diff --git "):
            # `diff --git a/<p> b/<p>` — take the b/ path as the current file.
            parts = raw.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            path = p[2:] if p.startswith("b/") else p
            continue
        if raw.startswith("--- "):
            continue
        if _DIFF_HUNK_RE.match(raw):
            shape.append([path, raw.strip()])
            continue
        if raw.startswith("+") or raw.startswith("-"):
            sign = raw[0]
            norm = _norm_diff_line(raw)
            if not norm:
                continue  # a pure-whitespace add/remove carries no signal
            hunks.append([path, sign, norm])
            content.append(f"{sign}{path}\x00{norm}")
    digest = hashlib.sha1("\n".join(sorted(content)).encode("utf-8")).hexdigest() \
        if content else ""
    return {"shape": sorted(shape), "digest": digest, "hunks": sorted(hunks)}


def _fp_hunk_counter(fp: dict[str, Any]) -> Counter:
    """The signed hunk multiset of a fingerprint as a ``Counter`` keyed on
    ``(path, sign, line)`` — the comparison currency for both signals."""
    return Counter(tuple(h) for h in (fp or {}).get("hunks") or [])


def _fp_is_empty(fp: dict[str, Any]) -> bool:
    return not (fp or {}).get("hunks")


def _fp_stasis_distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Jaccard DISTANCE between two fingerprints' signed hunk multisets (0.0 ==
    identical change, 1.0 == disjoint). A distance within ``diff_stasis_epsilon`` is
    diff-stasis — the same change resubmitted. Two empty fingerprints are treated as
    disjoint (1.0) so an empty/unreadable diff never reads as stasis."""
    ca, cb = _fp_hunk_counter(a), _fp_hunk_counter(b)
    if not ca or not cb:
        return 1.0
    inter = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return 1.0 - (inter / union) if union else 1.0


def _fp_revert_overlap(current: dict[str, Any], ancestor: dict[str, Any]) -> float:
    """The fraction of ``ancestor``'s change that ``current`` UNDOES: how much of the
    SIGN-FLIP of the ancestor's signed multiset the current diff reproduces. 1.0 ==
    a full revert of the ancestor; a mostly-new diff grazing one old hunk stays low
    (the real-progress lock). 0.0 when either side is empty."""
    cur = _fp_hunk_counter(current)
    anc = _fp_hunk_counter(ancestor)
    if not cur or not anc:
        return 0.0
    flipped = Counter()
    for (path, sign, line), n in anc.items():
        flipped[(path, "-" if sign == "+" else "+", line)] += n
    undone = sum((cur & flipped).values())
    total = sum(flipped.values())
    return (undone / total) if total else 0.0


def _pr_lineage_fingerprints(store: LedgerStore, pr: dict[str, Any]) -> list[dict[str, Any]]:
    """GL04 (GAP-4): the ANCESTOR diff fingerprints of ``pr``, nearest-first — the
    same ``pr_id`` back-link walk and cycle/self/terminal guard as
    ``_revise_lineage_depth``, but rooted on the CURRENT PR (unambiguous) rather than
    the rejection ``task`` (which is the reviewer task in production, the dev task in
    the unit-test proxy — walking from the PR is correct for both).

    The dev task that opened ``pr`` is itself a revise carrying the fingerprint of the
    PR it superseded, so ``fps[0]`` is the immediately preceding lineage member's diff
    and later entries are older ancestors. Stops at a terminal (retired) ancestor and
    skips any ancestor with no stored fingerprint."""
    tasks_by_id = {t.task_id: t for t in store.list_tasks()}
    cur_task = tasks_by_id.get(pr.get("task_id", ""))
    seen: set[str] = set()
    fps: list[dict[str, Any]] = []
    while cur_task is not None and getattr(cur_task, "pr_id", None):
        fp = cur_task._extras.get("diff_fingerprint")   # fp of the PR this task supersedes
        prev_pr_id = cur_task.pr_id
        if not prev_pr_id or prev_pr_id in seen:  # cycle / self guard
            break
        seen.add(prev_pr_id)
        prev_pr = store.get_pr(prev_pr_id)
        if prev_pr is None or prev_pr.get("status") in (
                "merged", "abandoned", "superseded", "blocked"):
            break
        if fp:
            fps.append(fp)
        cur_task = tasks_by_id.get(prev_pr.get("task_id", ""))
    return fps


def _account_diff_deadlock(
    store: LedgerStore, *, pr: dict[str, Any], task: Task,
    findings: list[dict[str, Any]], diff: str,
) -> bool:
    """GL04 (GAP-4): the diff-level trip, run at the rejection seam BEFORE Spec 16's
    depth+class check. Fingerprint the live PR diff and compare it to the lineage:

    * DIFF-STASIS — within ``diff_stasis_epsilon`` of the immediately preceding
      member (a resubmitted-essentially-the-same change); or
    * OSCILLATION/REVERT — reproduces >= ``revert_overlap`` of the sign-flip of ANY
      ancestor (A->B->A, even with a distinct class per hop); or
    * an ``anchor_regressed`` decision on this head (GL01 — a green check flipped
      red is oscillation at the artifact level).

    On a trip, routes into Spec 16's EXISTING escalation (``_break_revise_chain``)
    with a sub-detail — one blocked PR, one PM re-plan, one deduped alert, feeding
    Item 3 like any other break. No new stop reason, no second breaker. Returns True
    iff it broke the chain (the caller then returns without spawning a revise).

    A distinct-diff-each-round lineage (a hard defect needing a fresh fix each hop)
    trips NONE of these — the regression lock, mirroring Spec 16 Item 2's
    distinct-class escape hatch."""
    from .autonomy import load_policy
    policy = load_policy(store)
    if not getattr(policy, "diff_deadlock", True):
        return False
    current = _diff_fingerprint(diff)
    depth = _revise_lineage_depth(store, task)
    ancestors = _pr_lineage_fingerprints(store, pr)

    trigger = ""
    from . import anchors as _anchors
    # A revision that left a previously-green anchor CURRENTLY red is oscillation
    # (GL01→GL04). Gate on being in a real revise lineage (depth >= 1) — a first
    # attempt failing an anchor is not oscillation — and match on anchor STATE
    # (head-agnostic), since the break is recorded at the integrated master head,
    # not this PR's branch tip. Satisfiable: re-green clears it.
    if depth >= 1 and _anchors.has_unresolved_regression(store):
        trigger = "a revision regressed a green test anchor (oscillation)"
    elif not _fp_is_empty(current):
        eps = float(getattr(policy, "diff_stasis_epsilon", 0.12))
        overlap = float(getattr(policy, "revert_overlap", 0.7))
        if ancestors and _fp_stasis_distance(current, ancestors[0]) <= eps:
            trigger = ("successive revise diffs are near-identical (diff-stasis) — "
                       "the same change resubmitted")
        elif any(_fp_revert_overlap(current, anc) >= overlap for anc in ancestors):
            trigger = ("this revision reverts an earlier one in the lineage "
                       "(oscillation) — a distinct finding class each hop hid it")
    if not trigger:
        return False
    _break_revise_chain(store, pr=pr, task=task, findings=findings, depth=depth,
                        trigger=trigger)
    return True


def _break_revise_chain(
    store: LedgerStore, *, pr: dict[str, Any], task: Task,
    findings: list[dict[str, Any]], depth: int, trigger: str = "",
) -> None:
    """Spec 16 (Item 2): the revise lineage is non-progressive — spawn NO further
    revise. Block the PR — terminal, and ``_set_mergeable_if_ready`` already refuses
    to resurrect a ``blocked`` PR, so this can never become a merge path — hand the PM
    ONE re-plan task (deduped per lineage), record ``revise_chain_broken``, and raise
    one deduped alert.

    Spec 16's own trip is a repeated finding CLASS at the depth cap; GL04 (GAP-4)
    reuses this SAME breaker for its diff-level trips (stasis / revert), passing a
    ``trigger`` that names the diff-level reason. One breaker, one escalation — GAP-4
    is a third trip condition, not a second breaker."""
    why = trigger or "the same finding class kept repeating"
    store.update_pr(pr["pr_id"], status="blocked")
    reason = _reason_from_findings(findings)
    esc_title = f"revise chain broken: {pr['branch']}"
    if not any(t.role == PM and str(t.title or "") == esc_title
               and t.state not in ("done", "dropped") for t in store.list_tasks()):
        store.add_task(
            title=esc_title, role=PM,
            reason_summary=(reason or f"revise chain broken — {why}"),
            detail=(f"The revise chain for the PR on branch {pr['branch']} is non-"
                    f"progressive at depth {depth}: {why}. The PR is blocked. Re-scope, "
                    "decompose, or abandon this work — do not hand the same rejection "
                    "back to a dev again."
                    + (f" Finding: {reason}." if reason else "")))
    store.record_decision(
        title=f"revise chain broken: {pr['branch']}",
        context=f"pr {pr['pr_id']}", choice="revise_chain_broken",
        rationale=(f"revise lineage broke at depth {depth} — {why}; blocked the PR "
                   "and escalated to the PM instead of spawning another revise"),
        related_task_ids=[task.task_id],
        extra={"trigger": ("diff_deadlock" if trigger else "finding_class")})
    try:
        from . import attention
        attention.raise_review_alert(
            store.project_id, stage="review",
            title=f"revise chain broken: {pr['branch']}",
            summary=(f"revise chain on branch {pr['branch']} broke at depth {depth} — "
                     f"{why}; escalated to the PM."),
            store=store)
    except Exception:  # noqa: BLE001 — observability is best-effort
        pass


def _handle_review_rejection(
    store: LedgerStore, workspace: Any, *, pr: dict[str, Any], task: Task,
    findings: list[dict[str, Any]], source: str, diff: str | None = None,
) -> None:
    """Spec 12-18 prep (P0.1): the single seam every "a review said no" path runs
    through — mark the PR ``changes_requested`` and queue the DEV rework.

    Behaviour is byte-identical to the two inlined copies this replaces (the
    reviewer arm and the strict-mode PM-review arm); ``source`` selects the two
    strings that actually differed. It exists because FOUR specs in the
    gravity-golf batch edit this logic across two parallel branches:

    * Spec 13 + Spec 14 change what comes IN (foundation-scope classification of
      the rejection, per-finding ``cited`` flags);
    * Spec 15 + Spec 16 change what comes OUT (whether a ``revise:`` task is
      spawned at all, and what replaces it when it is not).

    Keeping one function with one writer per side is what lets those land
    independently instead of conflicting on ~30 shared lines.

    ``workspace`` is unused today and is threaded deliberately: the downstream
    specs need it (re-queueing a review, resolving changed paths) and a stable
    signature is the point of the seam.
    """
    store.update_pr(pr["pr_id"], status="changes_requested")
    # Spec 13 (S2): if this rejection is of a foundation-UNLOCKING PR but is OFF-SCOPE
    # for the foundation (no finding names a foundation file it adds), the clamp is
    # being held at 1 for an unrelated reason — surface it so the run isn't silently
    # serialized forever. Accounted FIRST, before any route/veto/break return below,
    # so this advisory foundation-scope signal is independent of which downstream path
    # the rejection takes (GL02's per-head veto cap can short-circuit before the revise
    # spawn). Best-effort so escalation can never break the seam.
    try:
        _account_offscope_foundation_rejection(store, pr=pr, findings=findings)
    except Exception:  # noqa: BLE001 — escalation is advisory, never load-bearing
        pass
    # Spec 15 (Item 3): if EVERY blocking finding is unactionable by a DEV — an
    # execution demand ("no evidence the tests were run") or uncited (the Spec 14
    # flag) — do NOT spawn a DEV revise task. Forwarding such a rejection to a dev
    # who also cannot run anything is exactly what drove the gravity-golf spiral.
    # Route it to the PM instead; the PR stays changes_requested (never merges),
    # only the DEV rework is withheld. A rejection that also names a real citable
    # defect keeps today's behaviour — the revise addresses that defect.
    # GL02 (Items 1-2): the two-lane invariant. A blocking finding that reasons about
    # runtime/execution behaviour (machine lane) is decided by the executor's evidence,
    # never by the LLM verdict: with a RED gate/probe backing it (GL01) it is a real
    # cited defect a DEV fixes; WITHOUT that evidence it is unverifiable-by-diff and
    # must route (to the gate re-review, or the PM), never bounce to a DEV who also
    # cannot run it. This feeds the runtime-claim signal into Spec 15's ONE suppression
    # seam below (no second writer) — exactly as Spec 14 Item 3 feeds it the ``cited``
    # flag. ``approved`` is never touched; the PR stays ``changes_requested``.
    blocking = [f for f in findings if f.get("blocking")]
    _backed = _red_runtime_evidence(store, str(pr.get("head") or ""))
    if blocking and _all_unactionable_by_dev(blocking, has_backing_evidence=_backed):
        _route_unactionable_rejection(store, pr=pr, task=task, findings=findings)
        return
    # GL02 (Item 3): the per-head reviewer veto cap, at a FINER grain than Spec 16's
    # depth cap and fired FIRST. Record this actionable veto against the PR head; once
    # the same head has been vetoed _REVIEWER_VETO_CAP times, escalate the disagreement
    # to the PM rather than spawn another revise. Because each revise makes a NEW head,
    # this only trips when the SAME head is rejected repeatedly (e.g. after a Spec 15
    # re-review), which is precisely the single-head disagreement the report escalates —
    # orthogonal to the lineage depth Spec 16 walks, so the two never double-fire.
    _head = str(pr.get("head") or "")
    _prior_vetoes = _head_veto_count(store, _head)
    store.record_decision(
        title=f"reviewer veto: {pr['branch']}", context=f"pr {pr['pr_id']}",
        choice="reviewer_veto",
        rationale=f"reviewer rejected head {_head[:12]} (veto #{_prior_vetoes + 1})",
        related_task_ids=[task.task_id], extra={"head": _head, "pr_id": pr["pr_id"]})
    if _REVIEWER_VETO_CAP and _prior_vetoes + 1 >= _REVIEWER_VETO_CAP:
        _escalate_reviewer_veto(store, pr=pr, task=task, findings=findings,
                                count=_prior_vetoes + 1)
        return
    # GL04 (GAP-4): the diff-level breaker, checked BEFORE Spec 16's depth+class cap.
    # An A->B->A oscillation (a distinct finding class per hop) or a near-identical
    # resubmission slips past the class-scoped cap entirely; break it at the reverting
    # round — the round where the signal is — rather than wasting rounds to depth 3.
    # Only runs when the diff is available (the reviewer/PM arms thread it); a caller
    # without it (a direct unit-test path) keeps Spec 16's class-only behaviour.
    # Runs on BOTH the reviewer and strict-mode PM-review arms (both route here).
    _current_fp: dict[str, Any] | None = None
    if diff is not None:
        try:
            if _account_diff_deadlock(store, pr=pr, task=task, findings=findings,
                                      diff=diff):
                return  # broke via Spec 16's escalation; no revise spawned
            _current_fp = _diff_fingerprint(diff)
        except Exception:  # noqa: BLE001 — the diff signal must never break the seam
            _current_fp = None
    # Spec 16 (Item 2): bound the revise chain. If this lineage has already been
    # revised revise_chain_limit times AND this rejection is the SAME finding class
    # the current revise was created to address, the loop is non-progressive — break
    # it (block the PR, hand it to the PM) instead of spawning an N+1th identical
    # revise. A DIFFERENT class resets the streak (real progress through distinct
    # defects), so only a genuinely stuck lineage is broken. Covers both the reviewer
    # and strict-mode PM-review arms — both route through this one seam.
    from .autonomy import load_policy
    _limit = max(0, int(getattr(load_policy(store), "revise_chain_limit", 3)))
    _depth = _revise_lineage_depth(store, task)
    _new_class = _finding_class(findings)
    _prev_class = task._extras.get("finding_class")
    if (_limit and _prev_class is not None and _depth >= _limit
            and frozenset(_prev_class) == _new_class):
        _break_revise_chain(store, pr=pr, task=task, findings=findings, depth=_depth)
        return
    depends = [task.task_id]
    if source == "reviewer":
        # F139 WS-D2: a contract-mismatch rejection reactively spawns a single
        # shared-contract owner task; the revise waits on it so the contract is
        # centralized instead of re-invented per branch.
        owner_id = _contract_owner_for(store, pr, findings)
        if owner_id:
            depends.append(owner_id)
        reason = _reason_from_findings(findings)
        whose = "reviewer findings"
    else:
        reason = _reason_from_findings(findings) or "PM requested changes"
        whose = "PM review findings"
    # F091: thread a back-link onto the revise task. pr_id on a DEV revise task
    # means "the PR this revise supersedes" (vs a TESTER task's pr_id = "the PR
    # under test"); depends_on chains it after the review; detail names the
    # branch so the dev can read back the prior work. When the revise PR merges,
    # _supersede_ancestors walks this back-link.
    findings_detail = _detail_from_findings(findings)
    store.add_task(
        title=f"revise: {pr['branch']}", role=DEV,
        pr_id=pr["pr_id"], depends_on=depends,
        reason_summary=reason,
        # Spec 16 (Item 2): this revise is one hop deeper, and carries the finding
        # class it must address — so the NEXT rejection can tell a repeated class
        # (non-progressive) from a new one (real progress). GL04 (GAP-4): it also
        # carries the diff fingerprint of the PR it supersedes, so the next rejection
        # can compare diffs for stasis/revert (nearest ancestor == this PR's diff).
        revise_depth=_depth + 1, finding_class=list(_new_class),
        diff_fingerprint=_current_fp,
        detail=(f"Address {whose} on branch "
                f"{pr['branch']} and open a new PR. The prior PR "
                f"({pr['pr_id']}) is superseded when this lands."
                + (f" Findings: {findings_detail}." if findings_detail else "")))


def _account_offscope_foundation_rejection(
    store: LedgerStore, *, pr: dict[str, Any], findings: list[dict[str, Any]],
) -> None:
    """Spec 13 (S2): classify a foundation-unlocking PR's rejection scope and, when
    it is off-scope for the blocker, record it + escalate to the PM (deduped per PR
    lineage) + raise a deduped alert at the second consecutive occurrence.

    Scope is UNKNOWN unless at least one finding carries a ``path`` — reviewers
    routinely emit path-less findings, and treating "no paths" as off-scope would
    fire an escalation on every ordinary rejection. Only a rejection whose paths
    are all present AND none intersects the foundation files the PR adds counts as
    off-scope."""
    if not pr.get("unlocks_foundation"):
        return
    finding_paths = [str(f.get("path") or "").strip()
                     for f in findings if str(f.get("path") or "").strip()]
    if not finding_paths:
        return  # no path signal -> scope unknown, not off-scope
    added_foundation = set(_foundation_files_in(
        [str(p) for p in (pr.get("changed_paths") or [])]))
    if not added_foundation:
        return  # nothing to be off-scope OF
    # Off-scope iff no finding path is one of the foundation files this PR adds.
    if any(p in added_foundation for p in finding_paths):
        return  # a finding targets the foundation itself -> genuinely in scope
    store.record_decision(
        title=f"off-scope rejection of foundation PR {pr['branch']}",
        context=f"pr {pr['pr_id']}", choice="foundation_pr_rejected_offscope",
        rationale=("a PR adding the foundation (" + ", ".join(sorted(added_foundation))
                   + ") was rejected only on unrelated files (" +
                   ", ".join(finding_paths) + "); the concurrency clamp stays at 1 "
                   "until the foundation lands"),
        related_task_ids=[pr.get("task_id", "")])
    # Escalate to the PM once per PR lineage (dedup on an open PM task naming it).
    esc_title = f"foundation blocked: {pr['branch']}"
    if not any(t.role == PM and str(t.title or "") == esc_title
               and t.state not in ("done", "dropped") for t in store.list_tasks()):
        store.add_task(
            title=esc_title, role=PM,
            reason_summary="foundation PR rejected off-scope — clamp held at 1",
            detail=(f"PR {pr['pr_id']} on branch {pr['branch']} adds the project "
                    f"foundation but was rejected for reasons unrelated to it, so "
                    f"worker concurrency stays clamped at 1. Re-scope or re-plan so "
                    f"the foundation can land."))
    # At the 2nd consecutive off-scope rejection, raise one deduped alert.
    n_offscope = sum(
        1 for d in store.list_decisions()
        if d.get("choice") == "foundation_pr_rejected_offscope"
        and d.get("context") == f"pr {pr['pr_id']}")
    if n_offscope >= 2:
        try:
            from . import attention
            attention.raise_foundation_deadlock_alert(
                store.project_id,
                summary=(f"PR on branch {pr['branch']} would lift the concurrency "
                         "clamp but keeps being rejected off-scope."),
                store=store)
        except Exception:  # noqa: BLE001 — observability is best-effort
            pass


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


# --- Spec 25 — the blocked turn ---------------------------------------------- #
#
# How much of the agent's own words ride on the ledger row / the TurnOutcome
# reason. Bounded for the same reason `_CONTEXT_QUESTION_CAP` is: the reason
# string is rendered into the PM's backlog view, and an essay there crowds out
# everything else the PM needs to read.
_BLOCKED_DETAIL_CAP = 400


def _blocked_reason_text(intent: Any) -> str:
    """Render a ``BlockedIntent`` as the one-line reason recorded on the task.

    The agent's own words, verbatim (bounded) — never a paraphrase. A block is a
    QUESTION addressed to the PM, and the PM can only answer the question that
    was actually asked."""
    reason = str(getattr(intent, "reason", "") or "other")
    detail = " ".join(str(getattr(intent, "detail", "") or "").split())
    text = f"{reason}: {detail[:_BLOCKED_DETAIL_CAP]}"
    needs = getattr(intent, "needs", None)
    if needs is not None:
        what = " ".join(str(getattr(needs, "what", "") or "").split())
        text += (f" [needs {getattr(needs, 'capability', 'other')}"
                 + (f": {what[:_BLOCKED_DETAIL_CAP]}" if what else "") + "]")
    return text


def _record_capability_ask(store: LedgerStore, intent: Any, *, role: str,
                           task: Task | None, context: str) -> None:
    """Spec 25 (Item 2): a `needs` block on a `blocked` turn is recorded as its
    own `capability_ask` decision, beside the `blocked` one.

    Two records, not one, because they answer different questions: the block
    says *this task cannot move*, the ask says *this ROLE lacks this
    capability* — the second outlives the task and is the input a human (or
    SPEC-26's role-closure pass) reads. Nothing here grants anything: enforcement
    stays in `allowed_tools_for_role` / `execute_dev_turn`, exactly as the
    spec's non-goal requires. Best-effort — a telemetry write must never fail a
    turn that was otherwise legal."""
    needs = getattr(intent, "needs", None)
    if needs is None:
        return
    try:
        store.record_decision(
            title=f"capability ask ({role}): {getattr(needs, 'capability', 'other')}",
            context=context, choice="capability_ask",
            rationale=(f"{str(getattr(needs, 'what', '') or '')[:_BLOCKED_DETAIL_CAP]}"
                       + (f" — {str(getattr(needs, 'why', '') or '')[:_BLOCKED_DETAIL_CAP]}"
                          if str(getattr(needs, "why", "") or "").strip() else "")),
            related_task_ids=[task.task_id] if task is not None else [],
            extra={"role": role,
                   "capability": str(getattr(needs, "capability", "other")),
                   "blocked_reason": str(getattr(intent, "reason", "other"))},
        )
    except Exception:  # noqa: BLE001 — a recorded ask is telemetry, not control
        pass


def _governance_corrective_prompt(prompt: str, code: str, detail: str, *,
                                  retry: int, max_retries: int) -> str:
    # F100 bugfix (2026-06-22): mirror _corrective_turn_prompt for governance
    # turns. A rejected governance turn gets a bounded re-prompt that restates
    # the exact required schema + the validation detail, so a normalizable-but-
    # imperfect reviewer/PM can self-correct instead of dead-ending the run.
    # Spec 25 (Item 4): the same treatment as `_corrective_turn_prompt` — this
    # function already hand-rolls half of it (it restates the verdict schema
    # inline), which is evidence the idea is right AND evidence that
    # hand-rolling it per call site drifts. The validator dump is humanised
    # here too; the raw one still lands on the recorded decision.
    return (
        f"{prompt}\n\n"
        "Your previous governance_turn.v1 response was rejected "
        f"({retry}/{max_retries} corrective retry): "
        f"{_humanize_parse_detail(code, detail)}\n"
        "Reply with ONLY a valid governance_turn.v1 JSON envelope for the same "
        "role. For an artifact review, \"verdict\" MUST be one of "
        '"approved" | "request_changes" | "blocked"; each finding MUST be an '
        'object {"severity":"low|medium|high|critical","title":"...",'
        '"body":"...","blocking":true|false}; a non-"approved" verdict requires '
        "at least one finding. Drop any unmodeled fields."
    )


_PYDANTIC_MSG_RE = re.compile(r"'msg': '((?:\\.|[^'\\])*)'")
_PYDANTIC_LOC_RE = re.compile(r"'loc': \(([^)]*)\)")
_CODE_PLAIN_REASON = {
    TurnErrorCode.turn_non_json.value:
        "your response contained no JSON envelope",
    TurnErrorCode.turn_tool_markup_only.value:
        "your response was tool-call markup instead of a JSON envelope",
    TurnErrorCode.turn_schema_mismatch.value:
        "your JSON envelope did not match the turn schema",
    TurnErrorCode.role_mismatch.value:
        "the envelope named a different role than the one you are seated in",
    TurnErrorCode.task_mismatch.value:
        "the envelope named a different task_id than the one assigned to you",
}


def _humanize_parse_detail(code: str, detail: str) -> str:
    """Spec 25 (Item 4): turn a validator dump into a sentence a MODEL can act on.

    ``parse_coding_turn`` returns ``f"invalid {role} intent: {exc.errors()[:3]}"``
    — a repr of Pydantic's error dicts, complete with ``'loc'``, ``'input'``, and
    an ``errors.pydantic.dev`` URL — and that string used to be spliced verbatim
    into the retry prompt. It names what is FORBIDDEN and never what is ACCEPTED,
    and with one corrective retry for the PM there is exactly one attempt to
    guess the difference. Extract the human-readable ``msg`` (paired with its
    field path, which is what makes "Field required" actionable) and drop
    everything else; the raw dump is still recorded on the
    ``{role} turn corrective retry`` decision, where an operator — who CAN read
    it — will find it."""
    msgs: list[str] = []
    locs = [(m.start(), m.group(1)) for m in _PYDANTIC_LOC_RE.finditer(detail or "")]
    for match in _PYDANTIC_MSG_RE.finditer(detail or ""):
        text = match.group(1).replace("\\'", "'").replace('\\"', '"')
        text = text.replace("\\n", " ").strip()
        if text.startswith("Value error, "):
            text = text[len("Value error, "):]
        prior = [raw for pos, raw in locs if pos < match.start()]
        field = ""
        if prior:
            parts = [p.strip().strip("'\"") for p in prior[-1].split(",")
                     if p.strip()]
            for part in parts:
                field += f"[{part}]" if part.isdigit() else (
                    f".{part}" if field else part)
        msgs.append(f"{field}: {text}" if field else text)
        if len(msgs) >= 3:
            break
    plain = _CODE_PLAIN_REASON.get(code, "your response was not a valid turn")
    if not msgs:
        return plain
    return f"{plain} — " + "; ".join(" ".join(m.split()) for m in msgs)


def _corrective_turn_prompt(prompt: str, parsed: TurnParseError, *,
                            retry: int, max_retries: int,
                            role: str = "", task_id: str | None = None) -> str:
    """Spec 25 (Item 4): a rejection must TEACH THE ACCEPTED SHAPE.

    Order, deliberately: (1) what was wrong, in plain language; (2) the minimal
    valid envelope for the seat being re-prompted; (3) the escape shape, with the
    standing promise that it is always accepted. (3) is not decoration — the turn
    being corrected may be one the schema genuinely cannot express, and without a
    legal way to SAY that, the only remaining moves are to guess again or to go
    silent, both of which are scored as failure."""
    example = minimal_valid_example(role, task_id=task_id) if role else ""
    escape = blocked_example(role, task_id=task_id) if role else ""
    teach = ""
    if example:
        teach = (
            f"A minimal VALID turn for your role looks exactly like this:\n{example}\n"
            "If you genuinely cannot proceed — a capability you do not have, a "
            "contradiction, or something this schema cannot express — this shape "
            f"is ALWAYS accepted, from any role, and is never counted against you:\n"
            f"{escape}\n")
    return (
        f"{prompt}\n\n"
        "Your previous coding_turn.v1 response was rejected "
        f"({retry}/{max_retries} corrective retry): "
        f"{_humanize_parse_detail(parsed.code.value, parsed.detail)}\n"
        f"{teach}"
        # F127: weaker CLI-backed models slip into agent mode and emit tool-call
        # markup instead of the envelope — forbid it explicitly and bluntly.
        "Reply with ONLY a single valid coding_turn.v1 JSON object for the same "
        "role and task. Do NOT call tools, do NOT write prose, do NOT emit "
        "<function_calls>/<invoke>/<parameter> markup or a sub-agent. Output the "
        "JSON object and nothing else. If you are implementing, emit at least one "
        "tool_call for implementation/test_only/refactor work. Drop unmodeled "
        "fields. Reviewer findings must be objects, not bare strings."
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


# SPEC-30 (Fix B): title markers for the ENGINE-FILED execution-failure fix tasks
# — the delivery review, the acceptance gate, the runtime launch, and the web
# probe/artifact each file a "fix ..." DEV task when they observe a failure at some
# head. These can go stale (the failure is resolved on master before the task is
# worked), and a DEV that blocks a stale one wedges the completion gate. Matched by
# a conservative title signature so a genuine human-authored task is never touched.
_DELIVERY_FIX_MARKERS = (
    "delivery review", "delivery test", "acceptance gate", "web artifact",
    "runtime launch", "canvas rendering", "web probe",
)


def _is_engine_filed_fix_title(title: str) -> bool:
    t = str(title or "").lower()
    return t.startswith("fix ") and any(m in t for m in _DELIVERY_FIX_MARKERS)


def _reconcile_moot_gate_fixes(store: LedgerStore, workspace: Any) -> None:
    """SPEC-30 (Fix B): drop an engine-filed execution-failure fix task that is now
    MOOT. The delivery review / gate / launch / probe file a "fix ..." task when
    they see a failure at some head; by the time it is worked the failure can be
    resolved on master (run 8: the acceptance gate was green — "nothing to fix").
    A DEV that blocks such a stale task turns it ``blocked`` (human-required), which
    PERMANENTLY wedges the completion gate — a blocked human-required task cannot be
    auto-closed, so `done` is refused forever and the run dies planning_churn even
    though nothing is actually wrong.

    If there is NO failing runtime evidence at the delivered head, the fix is moot:
    drop it (terminal, unblocks `done`). Only engine-filed fix tasks (title markers)
    that are NOT in flight; a real remaining failure (`_red_runtime_evidence` True)
    is left untouched so a genuine defect still blocks. Cheap and deterministic."""
    if workspace is None:
        return
    try:
        head = workspace.head()
    except Exception:  # noqa: BLE001
        return
    # A real failure at the delivered head means the fix tasks are NOT moot.
    if not head or _red_runtime_evidence(store, head):
        return
    try:
        in_flight = {t.task_id for t in store.list_tasks(state="doing")}
        candidates = [
            t for t in store.list_tasks()
            if str(getattr(t, "state", "")) in ("todo", "blocked")
            and t.task_id not in in_flight
            and _is_engine_filed_fix_title(getattr(t, "title", ""))
        ]
    except Exception:  # noqa: BLE001
        return
    for t in candidates:
        try:
            store.update_task(t.task_id, state="dropped")
            store.record_decision(
                title=f"stale fix task dropped: {t.title}",
                context=f"task {t.task_id}", choice="stale_fix_resolved",
                rationale=("no failing gate / probe / launch evidence remains at the "
                           f"delivered head {str(head)[:12]}; this engine-filed fix "
                           "task is moot — dropping it so a satisfied blocked task "
                           "cannot wedge the completion gate as human-required"),
                related_task_ids=[t.task_id])
        except Exception:  # noqa: BLE001 — reconcile is best-effort
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
                "merged", "abandoned", "superseded", "blocked"):  # Spec 16: +blocked
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


def _revise_lineage_depth(store: LedgerStore, task: Task) -> int:
    """Spec 16 (Item 1): count revise-hops from ``task`` back to the original dev
    task, following each revise task's ``pr_id`` back-link — the same traversal and
    cycle/self guard as ``_supersede_ancestors``. 0 for an original task, 1 for a
    first revise, 2 for a revise-of-a-revise, … . Stops at a terminal ancestor PR
    (``merged``/``abandoned``/``superseded``/``blocked``) so a retired lineage is
    not walked."""
    tasks_by_id = {t.task_id: t for t in store.list_tasks()}
    cur: Task | None = task
    seen: set[str] = set()
    depth = 0
    while cur is not None and getattr(cur, "pr_id", None):
        prev_pr_id = cur.pr_id
        if not prev_pr_id or prev_pr_id in seen:  # cycle / self guard
            break
        seen.add(prev_pr_id)
        prev_pr = store.get_pr(prev_pr_id)
        if prev_pr is None or prev_pr.get("status") in (
                "merged", "abandoned", "superseded", "blocked"):
            break
        depth += 1
        cur = tasks_by_id.get(prev_pr.get("task_id", ""))
    return depth


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


def _tester_unseated_by_closure(store: LedgerStore) -> bool:
    """SPEC-26 (S4) — the module-level read of the tester seat check, for the call
    sites that have no ``RoleClosure`` in scope. The stale-base and conflict
    revalidators are plain module functions, so they read the snapshot
    ``_apply_role_closure`` / ``_reevaluate_role_closure`` publish on
    ``run_state.role_closure`` instead of the live object.

    Absent (every direct test caller, and every pre-SPEC-26 run state) -> ``False``,
    which is today's behaviour exactly. Guarded: a run-state hiccup can only relax
    back to today's behaviour, never invent an unseat."""
    try:
        state = store.get_run_state().get("role_closure") or {}
        return TESTER in (state.get("unseated") or [])
    except Exception:  # noqa: BLE001
        return False


def _apply_merge_gate(store: LedgerStore, pr_id: str, *,
                      tester_seated: bool = True) -> None:
    """THE merge gate, and the only writer of ``status="mergeable"`` in the tree.

    A PR is mergeable only when reviewer-approved AND tests-green for its head — so
    a blind reviewer can never land a regression (F087-17).

    If the project has NO registered test commands, there is nothing for a tester to
    run, so the tests-green gate is vacuously satisfied — review approval alone
    governs the merge. Without this, a greenfield project (which starts with an empty
    test-command registry) could never advance a PR past ``tests_passed``, so NOTHING
    ever merged and the team churned forever in a revise loop. When test commands ARE
    configured the strict reviewer-AND-tests gate is unchanged.

    SPEC-26 (S4): ``tester_seated=False`` is the same situation reached by a
    different route — the commands exist but capability closure took the TESTER off
    the board, so holding the PR for a green tester verdict would hold it forever.
    Default ``True`` means "nothing was unseated", i.e. today's behaviour exactly.
    """
    p = store.get_pr(pr_id)
    # Spec 12 (S1): only UNIT-scoped commands gate a merge. An acceptance command
    # (in-loop gate / delivery) never blocks a per-PR merge, else bootstrapping one
    # would wedge every merge on a partial branch.
    tests_ok = p is not None and (
        p.get("tests_passed") is True or not store.get_unit_test_commands()
        or not tester_seated)
    # F100 PR-B: in strict governance mode a code PR needs the PM's review too
    # (reviewer AND PM). In off/light, PM review is not required, so this gate is
    # exactly today's reviewer-AND-tests behavior.
    pm_ok = p is not None and (
        not _strict_governance(store) or p.get("pm_reviewer_approved") is True)
    # F104 S6 review (M1): a PR `blocked` at the conflict-resolve retry cap is
    # terminal — its stale reviewer_approved/tests_passed must NOT resurrect it to
    # mergeable without the conflict being resolved (defense-in-depth on the exact
    # trust boundary this feature protects).
    if (p and p.get("reviewer_approved") is True and tests_ok and pm_ok
            and p.get("status") not in ("merged", "conflict", "abandoned", "blocked")):
        store.update_pr(pr_id, status="mergeable")


def _revalidate_one_pr(store: LedgerStore, workspace: Any, p: dict[str, Any],
                       branch: str, task_id: str) -> None:
    res = workspace.update_branch_from_base(task_id, branch)
    if res.get("updated") and not res.get("changed"):
        # Branch already contained this master -> still validly mergeable.
        return
    if res.get("updated") and _tester_unseated_by_closure(store):
        # SPEC-26 (S4): no seated TESTER, so there is nothing to demote INTO — the
        # `re-test PR:` task below would sit in the backlog forever, and a
        # non-terminal task blocks the completion claim (`pending_completion_work`),
        # so a clean integration would end the run `completion_blocked`. The
        # tests-green gate is already vacuously satisfied for an unseated tester
        # (`_set_mergeable_if_ready`), so advance the PR to the new integrated head
        # and leave it mergeable — the same net state the not-applicable tester turn
        # produces today, minus the turn nobody can take.
        store.update_pr(p["pr_id"], head=res.get("head", p["head"]))
        store.record_decision(
            title=f"stale-base re-test skipped: {branch}", context=f"pr {p['pr_id']}",
            choice="stale_base_revalidation",
            rationale="master advanced after another PR merged; no TESTER is seated "
                      "this run (SPEC-26), so the tests-green gate is vacuous and the "
                      "PR stays mergeable against the newly integrated head",
            related_task_ids=[task_id])
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
        # SPEC-26 (S4): the demotion stands (this is the fail-closed path — the PR
        # must NOT stay mergeable against a moved master it could not be checked
        # against), but a `re-test PR:` task for an unseated TESTER is a phantom: it
        # can never be dispatched and it blocks the completion claim. Skip it.
        if not _tester_unseated_by_closure(store):
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
        # SPEC-26 (S4): with no seated TESTER the `re-test PR:` task can never be
        # dispatched, and a non-terminal task blocks the completion claim
        # (`pending_completion_work`) — so spawning it would turn a cleanly rebased
        # PR into a permanently open item. Re-apply the merge gate instead: the
        # tests-green half is vacuous for an unseated tester, so a still-approved PR
        # goes straight back to `mergeable` through the ONE writer, with the strict-
        # governance and terminal-status checks intact.
        if _tester_unseated_by_closure(store):
            _apply_merge_gate(store, pr_id, tester_seated=False)
        else:
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


# Spec 20: the dev context-request channel is BOUNDED. Before this it was the one
# dev dead-end that requeued the task without an `unproductive` signal, so the
# F127 escalate-up ladder never engaged — and because only the MOST RECENT answer
# was threaded back, a follow-up ask saw a prompt byte-identical to the one that
# produced it: same prompt -> same output -> a deterministic fixed point that
# re-dispatched forever. Three caps close it: how many asks a task gets, how many
# answers are threaded back (so consecutive prompts actually differ), and how much
# of a question is quoted into the exhaustion ledger row.
_CONTEXT_REQUEST_LIMIT = 3
_CONTEXT_RESPONSE_THREAD_N = 3
_CONTEXT_QUESTION_CAP = 400


def _context_question_key(question: Any) -> str:
    """Spec 20: fold a context question to its repeat-detection key — whitespace
    collapsed, case-folded, then HASHED. The hash (not the text) is what gets
    persisted on the task, for two reasons: the ledger row stays a fixed 64 bytes
    however long the question is, and the comparison sees the WHOLE question.
    Truncating the text before comparing would let two genuinely different asks
    that share a long preamble ("I am implementing task t-…<350 chars>… so: <the
    actual question>") collide and trip the verbatim-repeat guard on a legitimate,
    progressing follow-up. The readable question survives in the recorded
    `context_request` / `context_request_exhausted` decisions, so nothing is lost
    for debugging."""
    folded = " ".join(str(question or "").split()).lower()
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()


def _context_attempts_of(extras: Any, default: int = 0) -> int:
    """Spec 20: read the persisted per-task ask counter DEFENSIVELY. `update_task`
    takes `**patch` and routes unknown keys straight through `_split_unknown` into
    `_extras` with no validation, so any present or future caller — or a migrated
    or hand-edited row — can leave a non-numeric value under this key. A bare
    `int(...)` would then raise out of DEV prompt composition, which runs on EVERY
    dev turn: `_latest_context_response_text` was total before Spec 20 (it could
    only return "" or a string) and must stay that way. A junk value degrades to
    `default` instead, which at worst re-arms a bounded 3-ask budget."""
    raw = (extras or {}).get("context_request_attempts")
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _latest_context_response_text(store: LedgerStore, task_id: str, *,
                                  task: Task | None = None) -> str:
    """F088-09: deliver the recorded context responses for THIS task back to the
    dev in a dedicated typed channel (the dev asked; it must receive the answer).
    Returns '' if there is none.

    Spec 20: thread the last ``_CONTEXT_RESPONSE_THREAD_N`` answers oldest-first
    instead of only the latest, and state the remaining ask budget plainly. The
    single-answer version made every follow-up turn compose the SAME prompt (the
    same question retrieves the same corpus/memory hits), which is why the loop
    never converged; carrying the whole recent thread guarantees turn N+1 differs
    from turn N, and the budget line tells the dev when it must stop asking and
    implement with what it has. Pass ``task`` so the budget reflects the persisted
    counter (authoritative — a short-circuited repeat records no answer)."""
    try:
        responses = [d["context_response"] for d in store.list_decisions()
                     if d.get("choice") == "context_request"
                     and task_id in (d.get("related_task_ids") or [])
                     and isinstance(d.get("context_response"), dict)]
    except Exception:
        return ""
    if not responses:
        return ""
    body = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                     for r in responses[-_CONTEXT_RESPONSE_THREAD_N:])
    # The persisted counter REPLACES the answer count when it is present — it is
    # not a floor. The recorded `context_request` decisions are append-only, so
    # `max(...)` would pin the rendered budget to asks that may have been
    # deliberately forgiven: `attention.resolve_stale_worker_unproductive` zeroes
    # the counter when a room change rescues an exhausted task, precisely to
    # re-arm the channel for the new route, and a floor would still tell that
    # dev "0 remain — do NOT ask again" while the runner would happily serve 3.
    # Fall back to the answer count only for a task that predates the counter.
    ctx_extras = getattr(task, "_extras", {}) or {} if task is not None else {}
    # Clamp the DISPLAYED spend at the limit. The dispatch branch already writes
    # at most `_CONTEXT_REQUEST_LIMIT`, but the key is an unvalidated passthrough
    # and an exhausted task is re-dispatched several times while the F127 ladder
    # walks its rungs — rendering "You have used 5 of 3 context requests" would
    # feed the model a self-contradictory instruction on exactly the turns where
    # it most needs a clear one. Gating is unaffected: it reads the raw counter.
    asked = min(_context_attempts_of(ctx_extras, default=len(responses)),
                _CONTEXT_REQUEST_LIMIT)
    remaining = max(0, _CONTEXT_REQUEST_LIMIT - asked)
    budget = (
        f"You have used {asked} of {_CONTEXT_REQUEST_LIMIT} context requests on "
        f"this task; {remaining} remain. "
        # Δ review: this must OVERRIDE the capability text, not merely contradict
        # it. The tool catalog (Spec 17) unconditionally tells a DEV without repo
        # read to "emit a context_request intent to see a file", and Spec 17's
        # `tool_not_allowed` carry-forward repeats that string in prior_outputs —
        # so an exhausted dev was being pointed straight back at the one channel
        # it is forbidden from, which is how it kept walking the ladder.
        # Spec 22-28 P0.5 (bug 2): the escape hatch used to read "say so in your
        # summary" — an action the DEV cannot take. `DeveloperToolPlanIntent` has
        # no `summary` field and `extra="ignore"`, so the field is silently
        # dropped, and `_corrective_turn_prompt` separately instructed "Drop
        # unmodeled fields such as summary". Two correct strings assembling a dead
        # end: an exhausted dev was told to do the one thing the schema erases.
        # Spec 25 finishes the repair BY CONSTRUCTION: the exhausted dev is now
        # pointed at the `blocked` intent — a real, typed, always-accepted shape
        # that lands on the `task_blocked` transition and is NOT counted as an
        # unproductive turn — instead of at an empty investigation, which parses
        # but then scores `no_net_change` and feeds the escalation ladder anyway.
        # Locked by `test_spec25_expressibility.py` (the text must name the
        # blocked intent and must never name an unmodeled field).
        + ("You have NO context requests left — do NOT ask again; implement the "
           "task with the evidence above. This OVERRIDES any earlier instruction "
           "in this prompt to emit a context_request intent: that channel is "
           "closed for this task. If you truly cannot proceed, say so with the "
           "blocked intent — it is always accepted, it is not held against you, "
           "and it hands the problem to the PM with your own words:\n"
           + blocked_example(DEV, task_id=task_id) + "\n" if remaining <= 0 else
           "Ask again ONLY if the answers above genuinely do not contain what you "
           "need; re-asking a question you already asked is treated as a dead end "
           "and spends the whole budget at once.\n"))
    return ("\nContext response to YOUR earlier request (use these answers, "
            "oldest first; cite refs):\n```json\n" + body + "\n```\n" + budget)


# Spec 17 (Item 3): a bounded cap on the carried tool-failure line, so a chatty
# rejected-tool reason cannot crowd out the rest of the prompt.
_TOOL_FAILURE_CAP = 400


def _last_tool_failure_text(task: Task) -> str:
    """Spec 17 (Item 3): render the task's carried tool failure as a bounded
    corrective line, or '' when there is none. This is the ONLY channel by which
    the model sees a `tool_not_allowed` rejection — the failure happens after a
    successful parse, so no corrective-retry path reaches it; it is persisted on
    the task and replayed here on the next dispatch, then cleared on the next
    successful write."""
    failure = str(getattr(task, "last_tool_failure", "") or "").strip()
    if not failure:
        return ""
    return ("\nYour last turn on this task had a tool call rejected — do NOT repeat "
            f"it; use the real read path named below:\n{failure[:_TOOL_FAILURE_CAP]}\n")


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


def _capability_refusal_note(store: LedgerStore) -> str:
    """Spec 15 (Item 2): tell the PM which of its proposed tasks were refused
    because no role could execute them and no gate exists to produce the demanded
    evidence. Mirrors ``_duplicate_rejection_note`` — reads the
    ``task_requires_absent_capability`` decisions so the next plan turn sees WHY,
    instead of re-proposing the same impossible 'run X and report' task forever."""
    try:
        decisions = store.list_decisions()
    except Exception:  # noqa: BLE001 — prompt assembly must never fail the turn
        return ""
    refused: list[str] = []
    for record in decisions:
        if record.get("choice") != "task_requires_absent_capability":
            continue
        planned = str(record.get("planned_title") or "")
        if planned and planned not in refused:
            refused.append(planned)
    if not refused:
        return ""
    titles = refused[-_DUPLICATE_NOTE_CAP:]
    return (
        f"{len(refused)} of your earlier proposed tasks were refused because no "
        "role can run a command and there is no acceptance gate to produce "
        f"execution evidence (e.g. {'; '.join(repr(t) for t in titles[:3])}). Do "
        "NOT re-propose 'run X and report' tasks — plan work the gate can verify "
        "(e.g. 'add a test that fails on trivial levels'), or register a test "
        "command so a gate exists.\n"
    )


# How much of the worker's own words ride into the PM note. Bounded for the same
# reason `_BLOCKED_DETAIL_CAP` is: the note is prepended to the PM prompt, and an
# essay there crowds out the backlog it has to read.
_CAPABILITY_ASK_NOTE_DETAIL_CAP = 160


def _capability_ask_note(store: LedgerStore) -> str:
    """Spec 25 (Item 2): DELIVER a recorded capability ask to the PM.

    ``_record_capability_ask`` writes one ``capability_ask`` decision per ``needs``
    block on a blocked turn, and until this note existed its only readers were that
    writer, the team-log renderer, and a test — so a worker asked for a capability
    and the PM never saw it. An ask nobody reads is unanswerable by construction.

    Exact sibling of ``_capability_refusal_note`` and ``_duplicate_rejection_note``:
    read the decisions, keep only asks whose task is still live (so the note clears
    itself once the task lands or is dropped, instead of nagging about settled
    history), dedupe per ``(role, capability)`` so one systematic gap is one line,
    render a bounded note, and never fail the turn.

    Surfacing an ask is NOT granting it — enforcement stays in
    ``allowed_tools_for_role`` / ``execute_dev_turn``, exactly as the spec's
    non-goal requires, and the note tells the PM so in as many words."""
    from .completion import _TERMINAL_TASK_STATES
    try:
        decisions = store.list_decisions()
        settled = {
            task.task_id for task in store.list_tasks()
            if str(getattr(task, "state", "") or "") in _TERMINAL_TASK_STATES
        }
    except Exception:  # noqa: BLE001 — prompt assembly must never fail the turn
        return ""
    asks: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in decisions:
        if record.get("choice") != "capability_ask":
            continue
        related = [str(t) for t in (record.get("related_task_ids") or [])]
        # A task-bound ask dies with its task; a run-level ask (the PM's own, which
        # carries no task) has nothing to settle it, so it always stands.
        if related and all(task_id in settled for task_id in related):
            continue
        role = str(record.get("role") or "")
        capability = str(record.get("capability") or "other")
        key = (role, capability)
        if key in seen:
            continue
        seen.add(key)
        what = " ".join(str(record.get("rationale") or "").split())
        asks.append((role, capability, what[:_CAPABILITY_ASK_NOTE_DETAIL_CAP]))
    if not asks:
        return ""
    items = "; ".join(
        f"{role or 'a worker'} asked for {capability}"
        + (f" ({what})" if what else "")
        for role, capability, what in asks[-_DUPLICATE_NOTE_CAP:])
    return (
        f"{len(asks)} capability ask(s) from the team are open and unanswered "
        f"({items}). You CANNOT grant a tool — answer by RE-PLANNING: re-scope the "
        "task to work a granted role can already do, register a test command so an "
        "execution gate exists, split the work, or drop the task via "
        "`cancel_task_ids`. Ignoring the ask leaves the asker with no legal move.\n"
    )


def _count_confabulated_tool_failures(
        store: LedgerStore, task_id: str, tool_name: str) -> int:
    """GL03: how many times THIS ungranted tool has failed on THIS task — the tally
    the threshold reads. A systematic capability gap repeats (the live 352-event
    storm was one tool, one task); a one-off is a typo. Counts the `tool_failed`
    decisions already recorded verbatim (rationale is ``<tool>: <reason>``), so no
    second per-event ledger write is needed."""
    n = 0
    for record in store.list_decisions():
        if record.get("choice") != "tool_failed":
            continue
        if task_id not in (record.get("related_task_ids") or []):
            continue
        if str(record.get("rationale") or "").startswith(f"{tool_name}:"):
            n += 1
    return n


def _capability_gap_already_recorded(
        store: LedgerStore, role: str, capability: str) -> bool:
    """True iff a `tool_confabulation` decision for this (role, capability) already
    exists — the dedupe lock so the 352-storm records ONE decision, not 352."""
    for record in store.list_decisions():
        if (record.get("choice") == "tool_confabulation"
                and record.get("role") == role
                and record.get("capability") == capability):
            return True
    return False


def _detect_tool_confabulation(
        store: LedgerStore, task: Task, role: str,
        tool_name: str, reason: str) -> None:
    """GL03 (Item 1): recognize a repeated ungranted-tool-call attempt as a
    capability-gap confabulation, raise ONE deduped alarm, and feed the
    capability-aware PM so the next plan turn re-plans/routes instead of the loop
    re-spawning the impossible task. Pure detection lives in ``capabilities``; this
    owns the threshold, the dedupe, and the ledger writes."""
    from . import attention
    try:
        from .autonomy import load_policy
        policy = load_policy(store)
    except Exception:  # noqa: BLE001 — a manifest must never fail a turn
        policy = None
    manifest = _capabilities.capability_manifest(store, policy)
    signal = _capabilities.confabulation_from_failure(
        role, tool_name, reason, manifest)
    if not signal.is_gap:
        return
    # Threshold guard (spec §7): a single stray confabulation is a fat-finger typo,
    # not a systematic gap — only escalate once the same ungranted tool repeats.
    count = _count_confabulated_tool_failures(store, task.task_id, tool_name)
    if count < _capabilities.GAP_ESCALATION_THRESHOLD:
        return
    capability = signal.capability or "unknown"
    # ONE deduped, non-blocking alarm per (role, capability).
    attention.raise_capability_gap_alert(
        store.project_id, role=role, capability=capability, tool_name=tool_name,
        summary=signal.why, store=store)
    # Record the decision ONCE and feed the PM's planner-feedback channel.
    if not _capability_gap_already_recorded(store, role, capability):
        store.record_decision(
            title=f"tool confabulation: {role} kept trying {tool_name}",
            context=f"task {task.task_id}", choice="tool_confabulation",
            rationale=(
                f"{role} attempted the ungranted tool {tool_name!r} {count}× on "
                f"task {task.task_id} — its manifest grants no {capability} "
                "capability, so the task needs an interface it lacks; re-plan/route."),
            related_task_ids=[task.task_id],
            extra={"role": role, "capability": capability, "tool_name": tool_name})


class RoleClosure:
    """SPEC-26 — the run-scoped seating consequence of the closure verdicts.

    GL05's topology audit has always been able to SAY that a seated role cannot
    discharge its duty. It has never been able to DO anything about it, so the same
    advisory fired on every run and never resolved. This object is the doing half.

    It owns the two roster structures the loop actually reads and keeps them in
    lockstep — ``member_pairs`` (what ``run_coding_loop`` schedules from) and
    ``by_role`` (what ``build_run_turn`` resolves a member from). Filtering one and
    not the other would seat a ghost: a role the scheduler skips but a turn can
    still resolve a member for, or the reverse.

    Both are held BY REFERENCE and mutated in place, which is what makes mid-run
    re-seating a one-liner: the sequential and concurrent loops hand the same
    ``member_pairs`` list object back and forth and re-read it every iteration, and
    ``build_run_turn`` closes over the same ``by_role`` dict. Nothing in the
    ``RunTurn`` seam changes.

    The consequence is UNSEAT, never REFUSE. On the shipped defaults a fresh
    project flags BOTH the TESTER (no unit-scoped command) and the REVIEWER
    (``reviewer_repo_read`` defaults False), so a binding refusal would refuse the
    product's own default configuration on every new project. Unseating is free and
    already correct: ``decide_next``/``plan_next_batch`` skip a role with no seated
    members and ``_has_open_work`` only counts roles in the seated set."""

    __slots__ = ("store", "policy", "member_pairs", "by_role", "full_pairs",
                 "full_by_role", "verdicts", "unseated", "indeterminate")

    def __init__(self, store: LedgerStore, policy: Any,
                 member_pairs: list[tuple[str, str]],
                 by_role: dict[str, list[dict[str, Any]]]) -> None:
        self.store = store
        self.policy = policy
        self.member_pairs = member_pairs
        self.by_role = by_role
        # Pre-closure snapshots: what re-seating restores from. Copies, so a later
        # unseat can never destroy the roster it would need to re-seat.
        self.full_pairs: list[tuple[str, str]] = list(member_pairs)
        self.full_by_role: dict[str, list[dict[str, Any]]] = {
            role: list(members) for role, members in by_role.items()}
        self.verdicts: dict[str, _capabilities.ClosureVerdict] = {}
        self.unseated: dict[str, _capabilities.ClosureVerdict] = {}
        self.indeterminate = False

    def seated(self, role: str) -> bool:
        """Is ``role`` present in the run's current seated roster?"""
        return any(seated_role == role for _, seated_role in self.member_pairs)

    def unseat(self, verdict: "_capabilities.ClosureVerdict") -> None:
        role = verdict.role
        if role in self.unseated:
            return
        self.unseated[role] = verdict
        self.member_pairs[:] = [(mid, r) for mid, r in self.member_pairs if r != role]
        self.by_role.pop(role, None)

    def reseat(self, role: str) -> bool:
        """Put a previously-unseated role back on the board. Takes effect on the
        NEXT loop iteration (both loops re-read ``member_pairs`` every pass)."""
        if role not in self.unseated:
            return False
        self.unseated.pop(role, None)
        members = self.full_by_role.get(role)
        if members:
            self.by_role[role] = list(members)
        have = set(self.member_pairs)
        for pair in self.full_pairs:
            if pair[1] == role and pair not in have:
                self.member_pairs.append(pair)
        return True


def _closure_verdicts(store: LedgerStore, member_pairs: list[tuple[str, str]],
                      policy: Any) -> list[_capabilities.ClosureVerdict]:
    """The pure evaluation half of SPEC-26. Deliberately NOT guarded — the caller
    owns the fail-open, because a swallowed exception here would silently seat an
    un-capable role while claiming the check ran."""
    manifest = _capabilities.capability_manifest(store, policy)
    seated = tuple(sorted({role for _mid, role in member_pairs}))
    overrides = _capabilities.capability_override_roles(policy)
    return _capabilities.role_closure(
        manifest, seated_roles=seated, overrides=overrides)


def _publish_role_closure(closure: RoleClosure) -> None:
    """Publish the verdicts on ``run_state.role_closure`` (the reserved key). The
    operator surfaces and SPEC-24's snapshot row read this; nothing gates on it."""
    try:
        closure.store.set_run_state(role_closure={
            "verdicts": [v.to_dict() for v in closure.verdicts.values()],
            "seated": sorted({role for _mid, role in closure.member_pairs}),
            "unseated": sorted(closure.unseated),
            "indeterminate": closure.indeterminate,
        })
    except Exception:  # noqa: BLE001 — publication is a view, never a gate
        pass


# SPEC-26 Item 2, and the ONE place its central premise has to be checked against
# the code rather than assumed. The spec justifies unseating with: *"Unseating is
# free and already correct — an unseated role costs zero dispatches and zero model
# calls, with no new machinery."* That is true for the TESTER once S4 couples the
# task spawn and the merge gate to `_tester_seated()`. It is NOT true for every role,
# and the difference is decidable from the code:
#
#   DEV       — `decide_next` falls through to `Plan` forever with no producer
#               (`topology.py`), so an empty-DEV council cannot generate work at all.
#               The spec names this one itself as the refusal-shaped case.
#   REVIEWER  — `_set_mergeable_if_ready` requires `reviewer_approved is True`
#               UNCONDITIONALLY, and it is the ONLY writer of `status="mergeable"`
#               in the tree. There is no reviewer-less merge path: a `review PR:`
#               task is spawned on every PR open, and with nobody to take it every PR
#               sits at `open` forever and the run ends `completion_blocked` with an
#               empty master. Unseating the reviewer does not cost "zero dispatches";
#               it removes the only producer of the merge gate's own precondition.
#
# So those two roles are SEATED UNDER PROTEST: the verdict is still computed, still
# recorded, still paged, and carries its remedy — but the consequence is a loud
# `role_capability_unclosed` decision rather than an unseat, because an unseat here
# would trade an unread advisory for a wedged run. Making the ungrounded reviewer
# genuinely unseatable needs a reviewer-less merge path (auto-approve, or a PM-review
# fallback) — a product-level trust-boundary decision that belongs in its own change,
# not smuggled in as a side effect of a capability audit. Recorded as the top
# follow-up out of this spec.
_UNSEAT_BREAKS_THE_PIPELINE: dict[str, str] = {
    DEV: ("a council with no producer never advances — decide_next falls through to "
          "Plan forever"),
    REVIEWER: ("_set_mergeable_if_ready requires reviewer_approved and is the only "
               "writer of `mergeable`, so with no seated reviewer every PR sits at "
               "`open` forever and the run ends completion_blocked"),
}


def _report_role_closure(closure: RoleClosure) -> None:
    """Record + page the verdicts. Guarded end to end: a ledger hiccup must not
    fail a run, and must not undo the seating decision already applied either."""
    try:
        from . import attention
        already = {
            str(d.get("title") or "")
            for d in closure.store.list_decisions()
            if d.get("choice") in ("topology_advisory", "role_capability_seated",
                                   "role_capability_unclosed")
        }
        for verdict in closure.verdicts.values():
            if verdict.outcome == _capabilities.CAPABLE:
                continue
            msg = verdict.reason
            # The advisory title/choice are kept VERBATIM from GL05 so the existing
            # dedupe and any operator tooling keep working; what is new is that it
            # now names a consequence and carries a resolvable context.
            title = f"topology advisory: {msg[:80]}"
            if title not in already:
                closure.store.record_decision(
                    title=title,
                    context="run-setup role-capability closure (SPEC-26)",
                    choice="topology_advisory",
                    rationale=f"{msg} [outcome={verdict.outcome}; "
                              f"{'seated by override' if verdict.overridden else 'role not seated'}"
                              f"] remedy: {verdict.remedy}")
                already.add(title)
            if verdict.overridden:
                seat_title = f"capability override: {verdict.role} seated anyway"
                if seat_title not in already:
                    closure.store.record_decision(
                        title=seat_title,
                        context="run-setup role-capability closure (SPEC-26)",
                        choice="role_capability_seated",
                        rationale=f"capability_overrides names {verdict.role}; the "
                                  f"{verdict.capability} gap is recorded and unchanged "
                                  f"({verdict.outcome}) — the override suppresses the "
                                  "consequence, never the finding")
                    already.add(seat_title)
            elif verdict.role in _UNSEAT_BREAKS_THE_PIPELINE:
                # Seated under protest — see `_UNSEAT_BREAKS_THE_PIPELINE`. The
                # finding is louder here, not quieter: the operator gets a second,
                # differently-keyed decision naming why the role kept its seat and
                # what would actually close the gap.
                unclosed_title = f"role capability unclosed: {verdict.role}"
                if unclosed_title not in already:
                    closure.store.record_decision(
                        title=unclosed_title,
                        context="run-setup role-capability closure (SPEC-26)",
                        choice="role_capability_unclosed",
                        rationale=f"{msg} — {verdict.role} is seated anyway because "
                                  f"unseating it would wedge the run "
                                  f"({_UNSEAT_BREAKS_THE_PIPELINE[verdict.role]}); "
                                  f"remedy: {verdict.remedy}")
                    already.add(unclosed_title)
            try:
                for s in attention.list_open(closure.store.project_id,
                                             store=closure.store):
                    if (s.kind == "alert" and s.source == "topology_audit"
                            and s.title == title):
                        break
                else:
                    attention.raise_signal(
                        closure.store.project_id, kind="alert",
                        source="topology_audit", stage="development",
                        title=title, summary=msg,
                        # SPEC-26 Item 3: the context must key a RESOLUTION. The old
                        # `{"advisory": msg}` could not — a title prefix is not a key.
                        context={"role": verdict.role,
                                 "capability": verdict.capability,
                                 "outcome": verdict.outcome,
                                 "remedy": verdict.remedy,
                                 "advisory": msg},
                        store=closure.store)
            except Exception:  # noqa: BLE001 — the signal is advisory, never fatal
                pass
    except Exception:  # noqa: BLE001 — reporting must never fail the run
        pass


def _apply_role_closure(
        store: LedgerStore, member_pairs: list[tuple[str, str]],
        by_role: dict[str, list[dict[str, Any]]],
        policy: CodingAutonomyPolicy) -> RoleClosure:
    """SPEC-26 Item 2 — score the seated council and give the verdict a consequence.

    Replaces GL05's `_audit_topology_advisory`, which computed the same verdict and
    then let the run proceed exactly as if the audit had not run. For every seated
    role: ``duty ⊆ capability``, or the role is NOT SEATED, or ``capability_overrides``
    names it. There is no fourth state.

    Mutates ``member_pairs`` and ``by_role`` in place and returns the live
    ``RoleClosure`` so the caller can hand it to ``build_run_turn`` (which needs the
    seated set for the tester-spawn / merge-gate coupling) and so a deferred role can
    be re-seated mid-run.

    Fail-open, never silent: if the EVALUATION raises, the full roster is seated —
    today's behaviour — and a ``role_capability_indeterminate`` decision records that
    the check did not run. The temptation on a check whose purpose is to refuse
    things is to fail closed; that would let a ledger hiccup empty a council."""
    closure = RoleClosure(store, policy, member_pairs, by_role)
    try:
        verdicts = _closure_verdicts(store, member_pairs, policy)
    except Exception as exc:  # noqa: BLE001 — fail OPEN on the roster, loudly
        closure.indeterminate = True
        try:
            store.record_decision(
                title="role capability closure indeterminate",
                context="run-setup role-capability closure (SPEC-26)",
                choice="role_capability_indeterminate",
                rationale=f"could not evaluate role capability closure ({exc}); "
                          "the full roster is seated, exactly as before SPEC-26")
        except Exception:  # noqa: BLE001
            pass
        _publish_role_closure(closure)
        return closure
    closure.verdicts = {v.role: v for v in verdicts}
    for verdict in verdicts:
        # PM is category (a) and always capable. DEV and REVIEWER are seated under
        # protest even when un-capable, because unseating THEM is not free — it
        # removes a structural precondition of the pipeline itself
        # (`_UNSEAT_BREAKS_THE_PIPELINE` states each one against the code). Every
        # other role that cannot discharge its duty, and is not overridden, loses its
        # seat for this run.
        if verdict.seatable or verdict.role in _UNSEAT_BREAKS_THE_PIPELINE:
            continue
        closure.unseat(verdict)
    _report_role_closure(closure)
    _publish_role_closure(closure)
    return closure


def _reevaluate_role_closure(closure: Optional[RoleClosure]) -> None:
    """SPEC-26 Item 3 — a ``deferred`` verdict is a claim about NOW, so re-check it
    at the one quiescent moment the runner already re-derives gate state: after a
    merge advances master (``_arm_gate_after_merge``, right after the bootstrap
    re-attempt).

    When a deferred role has become capable: re-seat it, dismiss the open advisory
    (the half that has never happened in this codebase), and record one
    ``role_capability_closed`` decision. The original ``topology_advisory`` decision
    is left verbatim — the ledger is append-only and the pair *(advisory at
    iteration 0, closed at iteration N)* is the trace that proves the loop works.

    Bound, stated rather than hidden: a mid-run ``PUT /test-commands`` on a LIVE run
    is picked up at the next merge, not instantly. The loop has no config-watch seam
    and a second poll for a rare operator action is not worth an iteration hook."""
    if closure is None or not closure.unseated:
        return
    try:
        verdicts = {
            v.role: v for v in _closure_verdicts(
                closure.store, closure.full_pairs, closure.policy)
        }
    except Exception:  # noqa: BLE001 — re-evaluation must never fail a merge
        return
    for role in list(closure.unseated):
        verdict = verdicts.get(role)
        if verdict is None or verdict.outcome != _capabilities.CAPABLE:
            continue
        closure.verdicts[role] = verdict
        if not closure.reseat(role):
            continue
        try:
            from . import attention
            attention.resolve_closed_capability(
                closure.store.project_id, role, verdict.capability,
                store=closure.store)
        except Exception:  # noqa: BLE001 — resolution is a view, never a gate
            pass
        try:
            closure.store.record_decision(
                title=f"role capability closed: {role}",
                context="mid-run role-capability closure (SPEC-26)",
                choice="role_capability_closed",
                rationale=f"{role} now has the {verdict.capability} capability its "
                          f"duty demands ({_closure_evidence(closure.store, role)}); "
                          "re-seated for the remainder of the run")
        except Exception:  # noqa: BLE001
            pass
    _publish_role_closure(closure)


def _closure_evidence(store: LedgerStore, role: str) -> str:
    """One legible phrase naming WHAT closed the gap, for the decision row."""
    if role == TESTER:
        try:
            ids = sorted(store.get_unit_test_commands())
        except Exception:  # noqa: BLE001
            ids = []
        return ("unit-scoped test command(s) registered: " + ", ".join(ids)
                if ids else "unit-scoped test command registered")
    return "capability granted"


def _capability_gap_note(store: LedgerStore) -> str:
    """GL03 (Item 1): tell the capability-aware PM which roles kept reaching for a
    capability their manifest lacks — the confabulation→gap→re-plan seam. Sibling of
    ``_capability_refusal_note``; reads the deduped ``tool_confabulation`` decisions.
    SPEC-15's manifest tells the PM what each role CAN do; this tells it what a role
    kept TRYING to do and couldn't, so re-planning is grounded."""
    try:
        decisions = store.list_decisions()
    except Exception:  # noqa: BLE001 — prompt assembly must never fail the turn
        return ""
    gaps: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in decisions:
        if record.get("choice") != "tool_confabulation":
            continue
        role = str(record.get("role") or "")
        capability = str(record.get("capability") or "")
        tool_name = str(record.get("tool_name") or "")
        key = (role, capability)
        if key in seen:
            continue
        seen.add(key)
        gaps.append((role, capability, tool_name))
    if not gaps:
        return ""
    items = "; ".join(
        f"{role} kept trying ungranted {capability} tool {tool_name!r}"
        for role, capability, tool_name in gaps[-_DUPLICATE_NOTE_CAP:])
    return (
        f"{len(gaps)} role/capability gap(s) surfaced from repeated ungranted tool "
        f"calls ({items}). A role that keeps inventing a tool is telling you the "
        "task needs an interface it lacks — re-plan or route that work to a "
        "role/gate that HAS the capability (grant it, or drop the task). Do NOT "
        "re-dispatch the same work to a role whose manifest cannot discharge it.\n")


def _design_contract_text(store: LedgerStore, task: Task | None = None) -> str:
    """Slice 1 §4 — the ``design_contract`` prompt segment for DEV / REVIEWER turns.

    Rendered from the APPROVED design_spec artifact: a token summary, the current
    task's relevant screen layout intent (matched by screen name when possible),
    and the do/don'ts, plus the standing "consume tokens, never invent raw values"
    instruction. Returns "" when there is no approved design_spec — so a project
    without a design contract (every non-UI project, and any UI project before its
    contract is approved) renders a prompt byte-identical to before, which is what
    keeps the golden prompt-segment lock intact (spec §4: the golden update is the
    deliberate one-time insertion of this renderer call at a reserved position)."""
    try:
        from .governance import GovernanceStore
        artifact = GovernanceStore.for_ledger(store).latest_approved_artifact(
            "design_spec")
    except Exception:  # noqa: BLE001 — prompt context is best-effort
        artifact = None
    if artifact is None:
        return ""
    body = artifact.body_json if isinstance(artifact.body_json, dict) else {}
    lines = ["DESIGN CONTRACT (the approved design_spec — build to it; consume its "
             "tokens, never invent raw colors/sizes/spacing):"]
    tokens = body.get("tokens")
    if isinstance(tokens, dict) and tokens:
        lines.append("Tokens: " + ", ".join(sorted(tokens.keys())) + ".")
    matrix = body.get("direction_matrix")
    if isinstance(matrix, dict) and matrix:
        from .design_spec import DIRECTION_AXES
        # Canonical axis order (not alphabetical) so the contract reads naturally.
        ordered = [a for a in DIRECTION_AXES if a in matrix] + [
            a for a in matrix if a not in DIRECTION_AXES]
        lines.append("Direction: " + "; ".join(
            f"{axis}={matrix[axis]}" for axis in ordered
            if isinstance(matrix.get(axis), str)) + ".")
    screens = body.get("screens")
    if isinstance(screens, list) and screens:
        title = str(getattr(task, "title", "") or "").lower()
        detail = str(getattr(task, "detail", "") or "").lower()
        relevant = None
        for screen in screens:
            if isinstance(screen, dict):
                name = str(screen.get("screen", "")).lower()
                if name and (name in title or name in detail):
                    relevant = screen
                    break
        if isinstance(relevant, dict):
            lines.append(
                f"This task's screen '{relevant.get('screen')}': "
                f"layout={relevant.get('layout')}; hierarchy="
                f"{relevant.get('hierarchy')}; purpose={relevant.get('purpose')}.")
        else:
            names = ", ".join(str(s.get("screen")) for s in screens
                              if isinstance(s, dict) and s.get("screen"))
            if names:
                lines.append(f"Screens in the contract: {names}.")
    markdown = str(getattr(artifact, "body_markdown", "") or "").strip()
    if markdown:
        lines.append("Do/don'ts & rationale: " + markdown.splitlines()[0][:400])
    return "\n".join(lines) + "\n"


def _designer_prompt(store: LedgerStore) -> str:
    """Slice 1 §2 — the Designer's design_spec authoring prompt.

    Shows the North Star / Definition of Done, the host asset-library manifest to
    pick from, the six direction-matrix axes the Designer must commit, and asks for
    a ``design_spec`` coding_turn.v1 envelope. Not bound to a task (like the PM)."""
    from . import design_library
    from .design_spec import DIRECTION_AXES

    try:
        project = store.get_project()
        north_star = str(getattr(project, "north_star", "") or "")
        dod = str(getattr(project, "definition_of_done", "") or "")
    except Exception:  # noqa: BLE001 — prompt context is best-effort
        north_star = dod = ""
    try:
        manifest_summary = design_library.manifest_summary_for_prompt()
    except Exception:  # noqa: BLE001 — a missing library must not crash the turn
        manifest_summary = "(asset library unavailable)"
    axes = ", ".join(DIRECTION_AXES)
    envelope_example = json.dumps({
        "schema_version": "coding_turn.v1",
        "role": "designer",
        "intent": {
            "kind": "design_spec",
            "title": "Design contract",
            "body_markdown": "Aesthetic direction + rationale, do/don'ts, per-screen "
                             "layout intent in prose.",
            "body_json": {
                "direction_matrix": {axis: "<your pick>" for axis in DIRECTION_AXES},
                "tokens": {"palette": {}, "type_scale": {}, "spacing": {},
                           "radii": {}, "shadows": {}},
                "assets": {"font_family_ids": ["<id from the manifest>"],
                           "icon_set_id": "<id from the manifest>"},
                "screens": [{"screen": "...", "purpose": "...", "layout": "...",
                             "hierarchy": "...", "key_states": ["..."]}],
                "components": [{"name": "...", "usage": "..."}],
            },
        },
    })
    return (
        f"{_skill_line(DESIGNER)} You are the DESIGNER of an autonomous coding team. "
        "Author the DESIGN CONTRACT (a design_spec) that every UI dev and reviewer "
        "will build to. The contract is the source of truth: the app is brought to "
        "the contract, never the reverse. You read the repo and author this "
        "artifact; you do NOT write code — code changes happen via DEV tasks.\n"
        f"North Star: {north_star}\n"
        f"Definition of Done: {dod}\n"
        f"{manifest_summary}\n"
        f"Commit an explicit pick for EACH direction-matrix axis ({axes}) so the "
        "design does not collapse to a generic default. Choose font families and "
        "an icon set by id from the asset library above (only ids that appear "
        "there). Give tokens (palette, type scale, spacing, radii, shadows), a "
        "per-screen inventory ({screen, purpose, layout, hierarchy, key_states}), "
        "and a component inventory with usage rules.\n"
        "Reply with ONLY a coding_turn.v1 envelope: "
        f"{envelope_example}."
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
    # it exactly what's open so it finishes or prunes obsolete items.
    done_gate = ""
    open_items = pending_completion_work(store)
    if open_items:
        done_gate = (
            "You may NOT declare the project done — these items are still open: "
            f"{summarize_open_items(open_items)}. Finish the ones that the "
            "Definition of Done genuinely needs. If an item is OBSOLETE or beyond "
            "the DoD (scope you over-planned), DROP it: put its task_id in "
            "`cancel_task_ids` on your plan turn — this is how you converge, do NOT "
            "keep adding scope. (A todo/blocked task with no live PR is dropped; "
            "in-flight work is not.) An item marked (human-required) — a conflicted "
            "PR — cannot be auto-closed; leave it and the run will surface it for "
            "the human.\n"
        )
    # Spec 08: tell the PM its proposals were rejected as duplicates. Without
    # this it re-proposes the same job forever — it cannot see the gate.
    done_gate = f"{done_gate}{_duplicate_rejection_note(store)}"
    # Spec 15 (Item 2): and tell it which were refused as unexecutable.
    done_gate = f"{done_gate}{_capability_refusal_note(store)}"
    # GL03 (Item 1): and which roles kept confabulating a capability they lack, so
    # the next plan turn re-plans/routes instead of re-spawning the impossible task.
    done_gate = f"{done_gate}{_capability_gap_note(store)}"
    # Spec 25 (Item 2): and which workers ASKED for a capability. The honest form
    # of the same signal the line above infers from confabulation — it belongs in
    # the same place, or the ask is recorded and never answered.
    done_gate = f"{done_gate}{_capability_ask_note(store)}"
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
    # SPEC-24 (Item 5): computed once — the segment is included only when it has
    # something to say, and `""` is the ABSENCE contract, not a convenience.
    _governance_text = _detector_state.prompt_text(store)
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
        # --- Spec 22-28 P0.4: the reserved tail order starts HERE ------------- #
        # `PROMPT_TAIL_SEGMENT_ORDER` (top of this module) fixes:
        #     gate_output -> governance_state -> tool_guidance -> standing rules
        #
        # The PM prompt has no `gate_output` segment today; if one is ever added it
        # goes immediately above this comment.
        #
        # SPEC-24 (Items 3 + 5) — `governance_state`, at the reserved position.
        # The live detector/budget readings, rendered ONLY when something is near a
        # limit; OMITTED entirely (never an empty labelled block) otherwise, so a
        # run nowhere near a threshold keeps today's prompt bytes and
        # `test_prompt_segments_golden.py` stays byte-locked. `_governance_text` is
        # computed ONCE above and is `""` for every quiet run.
        *([PromptSegment("governance_state", _governance_text)]
          if _governance_text else []),
        #
        # Spec 15 (Item 1): what each role can actually do, and the rule that no
        # role can run a command from inside a turn — so the PM stops planning
        # "run X and report" tasks no DEV can discharge (the gravity-golf wedge).
        PromptSegment("tool_guidance",
                      _capabilities.pm_capability_segment(store) + "\n"),
        # The standing PM planning instructions + envelope schema — LAST, always:
        # the instructions must be the most recent thing the model reads.
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


def _last_word_prompt(store: LedgerStore, action: Any) -> str:
    """SPEC-23 (Item 2) — the intervention prompt: the run is about to stop on a
    HEURISTIC detector; propose a concrete next action, or confirm the halt.

    Deliberately mirrors ``_pm_assist_prompt`` above, and just as deliberately asks
    a DIFFERENT question. PM assist asks a TASK question ("split or re-scope this
    task") and is structurally forbidden from doing anything else. This asks a RUN
    question, and its answer may legitimately be to abandon a task entirely and
    attack the North Star another way — a move rung 4 cannot make. That is why the
    two compose instead of duplicating each other.

    Carries three things and nothing else: the detector and its threshold, the
    evidence the detector actually computed (the same string its attention Problem
    was raised with — the last word is that string's first real consumer), and the
    demand, stated as a binary. Plus the standing orientation so the proposal is
    grounded.

    SPEC-24 (Item 6) — the governance block below is the SHARED renderer, focused
    on the tripped detector, NOT a second copy of it. `_detector_state.render`
    owns every threshold phrasing in the tree, so the numbers in the standing PM
    prompt and the numbers in this intervention prompt cannot drift apart; a
    second evidence renderer living beside that one is the exact duplication this
    batch keeps paying for. `focus` renders the tripped reading first and
    unconditionally (proximity is moot once it has tripped) and swaps the header,
    and every OTHER near reading still follows — a PM asked to propose an
    alternative should see the rest of the board, which is precisely the "same
    model, radically less information" defect this spec exists to close."""
    detector = str(getattr(action, "detector", "") or "the run")
    evidence = str(getattr(action, "evidence", "") or detector)
    governance = _detector_state.prompt_text(
        store, focus=detector, focus_evidence=evidence)
    if not governance:
        # Degradation only (the renderer is fully guarded and returns "" solely on
        # an internal failure): never send a last-word turn that does not name what
        # tripped. NOT a second rendering — no threshold, no window phrasing.
        governance = f"A guard called `{detector}` has tripped: {evidence}.\n"
    return (
        f"{_skill_line(PM)} You are the PM of an autonomous coding team, and this "
        "run is about to STOP.\n"
        f"Project state: {_orientation_text(store)}\n"
        f"{governance}"
        # SPEC-24 (S4): the antecedent moved into the block above, which now names
        # the guard, so it is named again here rather than referred to as "that".
        f"The `{detector}` guard is a heuristic computed between turns from the "
        "ledger alone — "
        "it can be wrong, and it cannot see what you know. This is your last word "
        "before the run ends.\n"
        "Answer ONE of two ways.\n"
        "(1) PROPOSE A CONCRETE NEXT ACTION — a different route to the North Star, "
        "not a restatement of work already queued. Reply with a coding_turn.v1 PM "
        "plan envelope carrying at least one NEW dev task; each task needs explicit "
        "acceptance criteria and the exact files/interfaces in scope. A duplicate of "
        "an already-open task is rejected and reads as (2), so change the approach "
        "rather than repeating it. If the work really is finished, set done=true "
        "with a non-empty completion_summary — it is checked against the open "
        "backlog like any other completion claim.\n"
        "(2) CONFIRM THE HALT — if stopping is genuinely right, say so in a decision "
        "and add no tasks. The run will end with the reason above and your rationale "
        "on the record.\n"
        "Reply with ONLY a coding_turn.v1 PM envelope: "
        '{"schema_version":"coding_turn.v1","role":"pm","intent":'
        '{"kind":"plan","done":false,"tasks":[{"title":"...",'
        '"role":"dev","detail":"Acceptance criteria... Files...",'
        '"depends_on":[]}],"decisions":[{"title":"...","rationale":"..."}]}}.'
    )


def _pm_turn_made_progress(
    intent: Any, created: list[Task],
    prior_decision_titles: Optional[set[str]] = None,
    dropped: Optional[list[str]] = None,
) -> bool:
    """Spec 22-28 P0.5 (bug 1) — did this PM plan turn DO something?

    ``made_progress`` feeds ``pm_idle``, and ``pm_idle_limit`` stops the run. Until
    now the answer was ``len(created) > 0``, which contradicted Spec 21: that spec
    legalised the decision-only PM turn ("drop these duplicate HUD tasks, add
    nothing, not done") precisely because the schema kept rejecting it — but such a
    turn creates no task, so it still scored no-progress and still fed the idle
    detector. The 2026-07-26 run stopped `no_progress` with a PM that was answering
    correctly four turns in a row. A legal turn that did something must count.

    THE TENSION THIS MUST PRESERVE. Spec 08's dedupe (see ``_materialize_pm_tasks``)
    deliberately keeps a rejected duplicate OUT of ``created`` so that a batch which
    was ALL duplicates scores ``made_progress=False`` and re-arms the idle detector
    on a churning PM. That is a real pathology (the PM re-proposing the same job
    forever) and it must keep tripping.

    THE RULE, and why this one:

        A turn that PROPOSED TASKS is judged ONLY on whether any task was created.
        A turn that proposed NO tasks is judged on whether it recorded a decision.

    The discriminator is ``intent.tasks`` — what the PM TRIED to do — not
    ``created``, which is what survived dedupe. So:

    * proposed tasks, all duplicates  -> False. Spec 08 is untouched, and crucially
      it stays untouched even when the PM attaches a decision to the batch —
      otherwise "explain yourself" would become a licence to churn forever.
    * proposed nothing, recorded a decision -> True. This is Spec 21's turn: the PM
      pruning, deferring, or recording what it is waiting on. It wrote durable
      project truth to the ledger (``record_decision`` ran above), so it is not
      idle.
    * proposed nothing, recorded nothing -> False, and unreachable: ``PMPlanIntent``
      already refuses an empty not-done turn. Kept explicit so the invariant does
      not depend on a validator two modules away.

    Regression lock 6 of the batch plan still holds: ``pm_idle_limit`` continues to
    bound genuinely empty turns, because a turn with neither a created task nor a
    decision is still no-progress.

    SPEC-25 (Item 3b) adds the NOVELTY gate this rule needs to be safe. Counting
    any decision as progress makes "explain yourself" a licence to idle: a PM that
    re-emits the same decision every turn would reset ``pm_idle`` forever. So a
    decision-only turn counts only when it recorded something NOT already on the
    ledger. ``prior_decision_titles`` is the caller's snapshot of the PM decisions
    already recorded for this project, taken BEFORE this turn's are written;
    ``None`` (the default) keeps the pre-Spec-25 behaviour for callers that cannot
    take one, so no existing call site changes meaning by accident."""
    if created:
        return True
    # SPEC-30 convergence: pruning obsolete tasks is real progress — it drains the
    # backlog toward a completion claim and writes durable ledger truth (dropped
    # tasks + a pm_cancel decision). A prune-only turn must NOT read as idle, or the
    # convergence path this enables would itself re-arm the no-progress detector.
    if dropped:
        return True
    if getattr(intent, "tasks", None):
        # Every proposed task was rejected (duplicate / uncreatable): Spec 08 says
        # this is churn, decisions or not.
        return False
    decisions = list(getattr(intent, "decisions", None) or [])
    if not decisions:
        return False
    if prior_decision_titles is None:
        return True
    known = {str(t).strip().lower() for t in prior_decision_titles}
    return any(str(getattr(d, "title", "") or "").strip().lower() not in known
               for d in decisions)


def _materialize_pm_tasks(
    store: LedgerStore, intent: Any, *, parent_task: Task | None = None
) -> list[Task]:
    """Create a PM plan batch and resolve title/path dependencies."""
    all_tasks = store.list_tasks()
    existing_title_to_id = {task.title: task.task_id for task in all_tasks}
    created: list[tuple[Task, list[str]]] = []
    blocked_for_deps: list[tuple[Task, list[str]]] = []
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
    try:
        from .autonomy import load_policy
        quarantine_limit = int(getattr(
            load_policy(store), "task_drop_quarantine_limit", 3) or 0)
    except Exception:  # noqa: BLE001
        quarantine_limit = 3
    for planned in intent.tasks:
        paths = _declared_target_paths(planned.title, planned.detail)
        path_deps = [
            owner for _path, owner in sorted(
                (path, path_owners[path]) for path in paths if path in path_owners
            )
        ]
        # SPEC-46: if this identity has already been dropped to the quarantine
        # threshold, suppress creation and escalate — don't feed planning_churn.
        identity = task_dedupe.identity_key(title=planned.title, paths=paths)
        if quarantine_limit and _drop_ledger.drop_count(store, identity) >= quarantine_limit:
            title_to_id[planned.title] = ""
            drops = _drop_ledger.drop_count(store, identity)
            store.record_decision(
                title=f"task quarantined (dropped repeatedly): {planned.title}",
                context="drop_quarantine", choice="task_quarantined",
                rationale=(f"{planned.title!r} has been created and dropped "
                           f"{drops}× this run; "
                           "quarantined so the run continues on the rest of the backlog"),
                extra={
                    "drop_count": drops,
                    **_drop_reasons.reason_blob(
                        _drop_reasons.PM_PRUNED,
                        detail=f"quarantined after {quarantine_limit} drops"),
                })
            try:
                from . import attention
                attention.raise_task_pathology_problem(
                    store.project_id, identity=identity, title=planned.title,
                    drops=drops, reason_code=_drop_reasons.PM_PRUNED, store=store)
            except Exception:  # noqa: BLE001
                pass
            continue
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
        # Spec 15 (Item 2): an execution-imperative task ("run X and report" /
        # "verify by running") cannot be discharged by a write-only DEV — the
        # exact gravity-golf wedge. With a gate, rewrite it to consume the gate
        # output; without one, refuse at planning time and surface the reason to
        # the PM through the same channel Spec 08 uses for duplicates. Authoring
        # ("write a test that fails on trivial levels") is NOT execution and is
        # untouched — that is valuable DEV work.
        use_title, use_detail = planned.title, planned.detail
        if _capabilities.classify_task_text(
                planned.title, planned.detail) == "execution":
            if _gate_state.gate_available(store):
                use_title, use_detail = _capabilities.routed_execution_task(
                    planned.title)
                store.record_decision(
                    title=f"execution task routed to gate: {planned.title}",
                    context="capability_lint", choice="task_routed_to_gate",
                    rationale=(f"{planned.title!r} asks a DEV to run and report; "
                               "rewritten to consume the acceptance-gate output"),
                    extra={"planned_title": planned.title})
            else:
                # SPEC-45: a capability gap is a PAUSE, not a deletion. Persist the
                # task as `blocked (missing_capability:…)` so it (a) survives to be
                # re-dispatched when the gate opens (the auto-unblock pass) and (b)
                # is visible in board/status. Still record the reason-bearing
                # decision so the PM's `_capability_refusal_note` prompt keeps firing.
                blocked_task = store.add_task(
                    title=planned.title,
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
                store.update_task(
                    blocked_task.task_id, state="blocked",
                    blocked_reason="missing_capability:execution_gate",
                    reason_summary=("needs execution evidence but no gate exists yet; "
                                    "waiting on the execution capability"))
                title_to_id[planned.title] = blocked_task.task_id
                store.record_decision(
                    title=f"task blocked (no executor): {planned.title}",
                    context="capability_lint",
                    choice="task_requires_absent_capability",
                    rationale=(f"{planned.title!r} demands execution evidence, but "
                               "no role can run a command and no acceptance gate "
                               "exists to produce it; blocked at planning time"),
                    related_task_ids=[blocked_task.task_id],
                    extra={
                        "planned_title": planned.title,
                        **_drop_reasons.reason_blob(
                            _drop_reasons.MISSING_CAPABILITY,
                            detail="no executor and no acceptance gate",
                            capability="execution_gate"),
                    })
                open_index.append(task_dedupe.index_entry(
                    task_id=blocked_task.task_id, title=planned.title, role=DEV,
                    paths=paths))
                for path in paths:
                    path_owners.setdefault(path, blocked_task.task_id)
                blocked_for_deps.append(
                    (blocked_task, inherited_deps + list(planned.depends_on) + path_deps))
                continue
        task = store.add_task(
            title=use_title,
            # F087-18: PM plans DEV work only. Review/test/merge are auto-driven
            # by the coding team topology (reviewer/tester members pull PRs off
            # the queue). A PM that names a reviewer/tester role for a planned
            # task is proposing work the topology already handles — coerce it
            # to dev so the review/test loop still runs. Kept independent of
            # the F129 model_assignment surface: proposals in ``planned`` are
            # validated separately by ``model_assignment.build_assignment``.
            role="dev",
            detail=use_detail,
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
            task_id=task.task_id, title=use_title, role=DEV, paths=paths))
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
    for task, dependencies in created + blocked_for_deps:
        resolved: list[str] = []
        for dependency in dependencies:
            dependency_id = title_to_id.get(dependency, dependency)
            if dependency_id and dependency_id not in resolved:
                resolved.append(dependency_id)
        if resolved:
            store.update_task(task.task_id, depends_on=resolved)
    return [task for task, _dependencies in created]


def _reeval_capability_blocked(store: LedgerStore) -> list[str]:
    """SPEC-45: re-dispatch tasks blocked for a now-satisfied capability.

    The gate predicate (`gate_state.gate_available`) is re-read live; when it flips
    true, every task blocked on `missing_capability:<cap>` returns to `todo` so the
    scheduler picks it up — no operator interjection. Best-effort and idempotent."""
    try:
        if not _gate_state.gate_available(store):
            return []
        blocked = [t for t in store.list_tasks(state="blocked")
                   if str((t._extras or {}).get("blocked_reason", "") or "")
                   .startswith("missing_capability:")]
    except Exception:  # noqa: BLE001 — a read failure means "unblock nothing"
        return []
    unblocked: list[str] = []
    for task in blocked:
        try:
            store.update_task(task.task_id, state="todo", blocked_reason="",
                              reason_summary="")
            store.record_decision(
                title=f"capability now available: {task.title}",
                context="capability_lint", choice="capability_unblocked",
                rationale=("the execution gate is now available; the task blocked "
                           "for it is re-dispatched"),
                related_task_ids=[task.task_id],
                extra=_drop_reasons.reason_blob(
                    _drop_reasons.MISSING_CAPABILITY, detail="gate now available",
                    capability="execution_gate"))
            unblocked.append(task.task_id)
        except Exception:  # noqa: BLE001 — best-effort per task
            pass
    if unblocked:
        try:
            from . import attention
            attention.resolve_closed_capability(
                store.project_id, "dev", "execution_gate", store=store)
        except Exception:  # noqa: BLE001 — resolution is advisory
            pass
    return unblocked


def _ack_unrun_acceptance_test(store: LedgerStore) -> None:
    """SPEC-31: if the run is reaching `done` while an authored acceptance test was
    never executed (its runtime is not provisioned — recorded by gate_bootstrap as
    `acceptance_test_unrun`), record an explicit acknowledgement so `done` never
    silently overstates "tested". Rendering is still gated by the web:probe; this
    only makes the un-run of the unit acceptance test legible on the ledger.
    Best-effort; never blocks done (blocking would re-wedge a run whose test simply
    cannot run in this environment)."""
    try:
        unrun = (store.get_run_state() or {}).get("acceptance_test_unrun")
    except Exception:  # noqa: BLE001
        return
    if not unrun:
        return
    try:
        store.record_decision(
            title="done reached with an unexecuted acceptance test",
            context="completion", choice="done_acceptance_test_unrun",
            rationale=(f"the authored acceptance test {unrun.get('command_id')!r} was "
                       f"not executed ({unrun.get('reason')}); rendering is verified "
                       "by the web:probe, but the unit acceptance test did not run. "
                       "Recorded so `done` does not overstate coverage (SPEC-31)."))
    except Exception:  # noqa: BLE001
        pass


# SPEC-36 (fix C) — patterns that mark a file on the delivered master tree as an
# authored test/acceptance artifact. INTENTIONALLY broader than what gate_bootstrap
# can register (*.test.js / pytest): S4's job is honest provenance, not registration,
# so it must catch exactly the tests the bootstrap cannot — the run-11 miss was
# test/acceptance.js (a real straight-line-solver) which, not ending in .test.js, was
# never registered and made S4 falsely report "none authored".
_AUTHORED_TEST_RE = re.compile(
    r"(^|/)tests?/"                        # any file under a test/ or tests/ dir
    r"|\.(test|spec)\.[cm]?[jt]sx?$"       # *.test.* / *.spec.* (js/ts/jsx/tsx/cjs)
    r"|(^|/)acceptance\.[cm]?[jt]sx?$"     # a file literally named acceptance.*
    r"|(^|/)conftest\.py$"                 # pytest
    r"|_test\.py$|(^|/)test_[^/]*\.py$",   # python test files outside a test/ dir
    re.IGNORECASE)


def _tree_has_authored_test(workspace: Any) -> bool:
    """SPEC-36 (fix C): does the MERGED master tree contain a file that looks like an
    authored test/acceptance artifact, or a declared (non-placeholder) `npm test`
    script? Reads git truth (no checkout switch) so it sees what actually shipped.
    Deliberately broad and fully fail-open — a read error returns False (never
    invents an authored test)."""
    if workspace is None:
        return False
    try:
        files = [f for f in workspace.list_files(scope="master") if f != ".gitignore"]
    except Exception:  # noqa: BLE001
        return False
    if any(_AUTHORED_TEST_RE.search(f) for f in files):
        return True
    try:
        raw = workspace.read_master_file("package.json")
        if raw:
            obj = json.loads(raw.decode("utf-8", "replace"))
            scripts = obj.get("scripts") if isinstance(obj, dict) else None
            cmd = scripts.get("test") if isinstance(scripts, dict) else None
            if (isinstance(cmd, str) and cmd.strip()
                    and "no test specified" not in cmd.lower()):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _record_completion_oracles(store: LedgerStore, workspace: Any = None) -> None:
    """SPEC-34 S4: at `done`, record which oracles ACTUALLY verified the artifact,
    so a completion summary cannot conflate the liveness gate (web:probe + runtime
    launch — proves it renders and responds to input) with the team's authored
    acceptance test (proves the mechanic has EFFECT). Best-effort; never blocks.

    Distinguishes states honestly (SPEC-36 fix C — never claim a test ran when none
    was authored, and never claim none was authored when one shipped): NOT executed
    (an unrun was recorded), executed (an acceptance-scoped gate is registered),
    authored-but-not-registered/runnable (a test file is on master the gate could
    neither register nor run — the run-11 case), or none authored."""
    try:
        unrun = (store.get_run_state() or {}).get("acceptance_test_unrun")
    except Exception:  # noqa: BLE001
        unrun = None
    if unrun:
        acc = "NOT executed — " + str((unrun or {}).get("reason") or "unavailable")
    else:
        try:
            registered = any(
                str((c or {}).get("scope") or "") == "acceptance"
                for c in (store.get_test_commands() or {}).values())
        except Exception:  # noqa: BLE001
            registered = False
        if registered:
            acc = "executed (registered acceptance gate)"
        elif _tree_has_authored_test(workspace):
            acc = ("authored but NOT registered/runnable — a test/acceptance "
                   "artifact is present on master that the gate could neither "
                   "register nor run (e.g. a browser test, or a filename the "
                   "bootstrap does not match); it did NOT verify this build")
        else:
            acc = "none authored"
    try:
        store.record_decision(
            title="completion oracle provenance",
            context="completion", choice="completion_oracles",
            rationale=("oracles at done — liveness gate (web:probe + runtime "
                       "launch): verifies the artifact renders and responds to "
                       "input, NOT that a mechanic has effect; authored acceptance "
                       f"test: {acc}. 'verified by the liveness gate' is not "
                       "'verified by acceptance tests' (SPEC-34)."))
    except Exception:  # noqa: BLE001
        pass


def _acceptance_gate_blocks_done(
        store: LedgerStore, workspace: Any) -> Optional[str]:
    """SPEC-35 G3: should `done` be REFUSED because the project's own acceptance gate
    is not green at the current master head? Returns a refusal reason, or ``None`` to
    allow.

    * green / no_gate -> ``None`` (allow).
    * red   -> block. A genuine assertion failure (the test RAN and failed). The team
      fixes the test/mechanic; once master advances the stale-then-fresh in-loop
      GateRun flips it green and this lifts automatically — the recovery property
      SPEC-34's draft lacked (it blocked on a never-cleared record).
    * stale -> block, and arm the in-loop gate (``gate_due`` + ``gate_dirty_head``) so
      the loop runs the acceptance command on this head; the next `done` attempt sees
      a fresh result. This ALSO covers a launch/provisioning failure (a result that
      did not cleanly execute — classified ``stale``, never ``red``), so an
      environmental failure the team cannot fix by editing code never becomes an
      unbounded block.

    Boundedness is NOT a private counter here: every refusal returns through the F128
    ``completion_refused`` ladder (``autonomy._handle_completion_refused``), which
    re-prompts the PM and, at ``completion_refused_limit``, raises ONE human-routed
    ``completion_blocked`` Problem and stops the run truthfully. That is the single
    sanctioned terminal — never a silent permanent wedge, never a false ``done``.

    Fail-open: an absent workspace / unresolvable head / any read error returns
    ``None`` (this never invents a block)."""
    if workspace is None:
        return None
    try:
        head = str(workspace.head() or "")
    except Exception:  # noqa: BLE001
        return None
    if not head:
        return None
    # SPEC-40 (item E): the declared-mechanic gate, checked BEFORE the acceptance gate
    # so its far more actionable reason wins when both are unhappy. Only `red` blocks:
    # `advisory` is the uncertain/marginal verdict that must never terminate a healthy
    # run (the golf-4 lesson — the oracle itself may be the thing that is wrong), and
    # `ok` means the council's own white-box assertion proved the mechanic. Recovery
    # is structural and identical to the acceptance gate's: the probe re-runs on the
    # next merge, so a fixed tree lifts this on its own, and every refusal is bounded
    # by the F128 `completion_refused` ladder.
    if mechanic_gate_status(store, head) == "red":
        why = mechanic_gate_reason(store, head)
        return (f"the declared mechanic is not proven at master head {head[:12]}"
                + (f" — {why}" if why else "")
                + ". `done` is refused until it is proven. The fastest way: expose "
                "window.__probe.solution() (a shot that clears the level, in the same "
                "units shoot() takes) and window.__probe.won() (your own win "
                "predicate) — the probe fires your solution with the mechanic ON "
                "(must win) and the identical shot with it OFF (must NOT win). Or fix "
                "the mechanic. The probe re-runs on the next merge and lifts this "
                "automatically (SPEC-40).")
    status = acceptance_gate_status(store, head)
    if status in ("no_gate", "green"):
        return None
    if status == "red":
        return (f"the acceptance gate is RED at master head {head[:12]} — the team's "
                "own acceptance test RAN and failed; `done` is refused until it is "
                "green (SPEC-35). Fix the test/mechanic: the in-loop gate re-runs on "
                "the next merge and lifts this automatically.")
    # stale: no usable result at this head (never run, ran at a prior head, or a
    # launch/provisioning failure that did not cleanly execute). Arm the in-loop gate
    # to produce a fresh result; the completion_refused ladder bounds the retries.
    try:
        store.set_run_state(gate_due=True, gate_dirty_head=head)
    except Exception:  # noqa: BLE001
        return None
    return (f"the acceptance gate has no usable result at master head {head[:12]} yet "
            "(unrun, stale, or it could not launch) — armed the in-loop gate to run "
            "it; `done` is deferred until it reports green (SPEC-35).")


def _apply_pm_cancels(store: LedgerStore, intent: Any) -> list[str]:
    """SPEC-30 convergence: drop the todo/blocked tasks the PM asked to cancel, so
    it can prune its own over-planned backlog and reach a completion claim.

    Eligibility is deliberately narrow — only a ``todo`` or ``blocked`` task with
    NO live PR is dropped. A ``doing`` (in-flight) or terminal task, or one whose
    work is already in an open PR, is skipped: the PM prunes SCOPE, it does not
    kill work in progress. The delivery + execution gates still judge the real
    artifact, so pruning can never mark an incomplete project done. Returns the
    ids actually dropped (for the caller's progress accounting). Fully guarded."""
    ids = [str(i) for i in (getattr(intent, "cancel_task_ids", None) or [])]
    if not ids:
        return []
    try:
        by_id = {t.task_id: t for t in store.list_tasks()}
        live_pr_task_ids = {
            str(p.get("task_id") or "") for p in store.list_prs()
            if p.get("status") in ("open", "changes_requested", "mergeable", "conflict")
        }
    except Exception:  # noqa: BLE001 — a read failure means "cancel nothing"
        return []
    dropped: list[str] = []
    for tid in ids:
        task = by_id.get(tid)
        if task is None:
            continue
        if str(getattr(task, "state", "")) not in ("todo", "blocked"):
            continue  # never drop in-flight (doing) or already-terminal work
        if tid in live_pr_task_ids:
            continue  # real work is in an open PR — not obsolete scope
        try:
            paths = _declared_target_paths(getattr(task, "title", ""),
                                           getattr(task, "detail", ""))
            identity = task_dedupe.identity_key(
                title=getattr(task, "title", ""), paths=paths)
            n = _drop_ledger.record_drop(store, identity)
            store.update_task(tid, state="dropped",
                              reason_summary="PM pruned this over-scoped task")
            store.record_decision(
                title=f"PM dropped task: {getattr(task, 'title', tid)}",
                context="pm_cancel", choice="pm_task_cancelled",
                rationale="the PM pruned this obsolete / over-scoped task to converge",
                related_task_ids=[tid],
                extra={
                    "drop_count": n,
                    **_drop_reasons.reason_blob(
                        _drop_reasons.PM_PRUNED,
                        detail=f"dropped {n}× this run"),
                })
            dropped.append(tid)
        except Exception:  # noqa: BLE001 — best-effort prune
            pass
    return dropped


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


def _dev_prompt(task: Task, store: LedgerStore, readback: str = "", *,
                repo_read: bool = False) -> str:
    # F087-17: the dev works on its own branch off master. The current contents
    # of the worktree (everything merged so far) are inlined so the dev EXTENDS
    # the project instead of regenerating a file from scratch and clobbering
    # prior work. code_write replaces the WHOLE file, so it must include
    # everything that should remain; code_edit (anchored find/replace) is the
    # targeted alternative for existing files.
    return _register_pending_composition(
        _dev_prompt_segments(task, store, readback, repo_read=repo_read))


def _dev_prompt_segments(task: Task, store: LedgerStore,
                         readback: str = "", *,
                         repo_read: bool = False) -> list[PromptSegment]:
    """F143-01 Slice F: the DEV prompt as ordered labeled segments. Joined verbatim
    this equals the pre-refactor ``_dev_prompt`` string byte-for-byte (golden-locked)."""
    existing = (f"Current files in the worktree (EXTEND these — do not drop "
                f"existing code; code_write replaces the whole file so include "
                f"all of it, or use code_edit for a targeted "
                f"change):\n{readback}\n" if readback
                else "The worktree is empty; create the files from scratch.\n")
    _gate_available = _gate_state.gate_available(store)
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
        # code_edit: an anchored find/replace so a fix to a LARGE file costs
        # output proportional to the change — a whole-file re-emit of a large
        # module gets truncated by the output budget and then (rightly) blocked
        # by the destructive-write guard, making the fix impossible to land.
        "To CHANGE an existing file, prefer code_edit: "
        '{"tool": "code_edit", "args": {"path": "rel/path", "old_string": '
        '"<exact existing text>", "new_string": "<replacement>"}} — '
        "old_string must be copied EXACTLY from the current file (whitespace "
        "included) and must match exactly once; add surrounding lines to make "
        'it unique, or set "replace_all": true to change every occurrence. '
        "For a large file ALWAYS use code_edit (a whole-file re-emit gets "
        "truncated and blocked); use code_write only for a new file, a full "
        "rewrite of a small file, or a binary asset. "
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
        # Prior PM/reviewer context responses threaded to this task. Spec 20
        # passes the task so the rendered ask-budget reads off the persisted
        # counter rather than the answer count alone.
        PromptSegment("prior_outputs",
                      _latest_context_response_text(store, task.task_id,
                                                    task=task)),
        # Slice 1 §4: the design_contract segment (empty unless an approved
        # design_spec exists — byte-identical for non-design projects, the
        # deliberate one-time golden insertion).
        PromptSegment("design_contract", _design_contract_text(store, task)),
        # Spec 17 (Item 3): the last tool failure this task hit, carried forward on
        # its next dispatch (a rejected unknown tool never reaches the corrective-
        # retry path, so the ONLY way the model sees it is on the next composed
        # prompt). Empty (absent) with no carried failure -> byte-identical to
        # before; cleared on the next successful write so it never nags.
        PromptSegment("prior_outputs", _last_tool_failure_text(task)),
        # The current worktree snapshot the dev extends.
        PromptSegment("repo_snapshot", existing),
        # Spec 12 (S1): the latest acceptance-gate output (verbatim), so "iterate
        # until green" has a feedback signal — the dev sees WHY the gate failed
        # instead of re-reasoning from a half-context. Empty (absent) when no gate
        # has run, so a gate-less project's prompt is byte-identical to before.
        PromptSegment("gate_output", _gate_state.latest_gate_text(store)),
        # Tool catalog / how-to-emit-tool-calls guidance. Spec 17: the catalog is
        # capability-aware — repo_read is resolved from THIS member's real
        # invocation (not per-project), and `gate` names where execution evidence
        # comes from.
        PromptSegment(
            "tool_guidance",
            f"{tool_catalog_text(DEV, repo_read=repo_read, gate=_gate_available)} "
            "Do not request merge-back.\n"),
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
                      project_context: str, scope_task: Task | None = None,
                      *, gate_text: str = "", repo_read: bool = False,
                      gate: bool = False, design_contract: str = "") -> str:
    diff = _filter_generated_from_diff(diff)
    cap = diff[:_REVIEW_DIFF_CAP]
    truncated = len(diff) > _REVIEW_DIFF_CAP
    trunc = " [diff truncated]" if truncated else ""
    # SPEC-32: when the worktree is mounted (repo_read), a truncated diff is a READ
    # CUE, not a defect — open the file with your native tools and judge from it.
    # Rejecting for truncation while holding the tree wastes a revise cycle (run 10:
    # the delivery reviewer rejected a truncated acceptance test it could have
    # opened). Only WITHOUT a mount is truncation review-blocking (the code is
    # genuinely unseeable).
    if truncated and repo_read:
        trunc_note = (
            "The diff above was truncated to fit — but you have the full worktree "
            "mounted read-only. OPEN the affected files with your native read tools "
            "(Read/Grep/Glob in your working directory) and judge them from source. "
            "Do NOT set approved=false merely because the diff is truncated — that is "
            "a tooling limit you can resolve by reading, not a code defect.\n")
    elif truncated:
        trunc_note = (
            "The diff above was truncated to fit — code beyond the cut is NOT shown. "
            "This is a tooling limit, not evidence of a source-code defect, but "
            "review coverage is incomplete and unseen code cannot be approved. Set "
            "approved to false and include one finding asking the author to split or "
            "reduce the change so its complete diff can be reviewed. Do not speculate "
            "about defects in code that is not shown.\n")
    else:
        trunc_note = ""
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
    # SPEC-32: the truncation reject-example is only for the NO-MOUNT case. With a
    # mount, the example is a clean approve — the reviewer opens the file instead.
    reject_for_truncation = truncated and not repo_read
    example_findings = ([{
        "severity": "major",
        "title": "Diff exceeds review context",
        "body": "Split or reduce this change so the complete diff can be reviewed.",
    }] if reject_for_truncation else [])
    verdict_example = json.dumps({
        "schema_version": "coding_turn.v1",
        "role": "reviewer",
        "task_id": task.task_id,
        "intent": {
            "kind": "review_verdict",
            "reviewed_head": pr.get("head"),
            "approved": not reject_for_truncation,
            "findings": example_findings,
        },
    })
    return _register_pending_composition(_review_pr_prompt_segments(
        task, pr, project_context, task_scope=task_scope, bar=bar, cap=cap,
        trunc=trunc, trunc_note=trunc_note, verdict_example=verdict_example,
        gate_text=gate_text, repo_read=repo_read, gate=gate,
        design_contract=design_contract))


def _review_pr_prompt_segments(
        task: Task, pr: dict[str, Any], project_context: str, *,
        task_scope: str, bar: str, cap: str, trunc: str, trunc_note: str,
        verdict_example: str, gate_text: str = "", repo_read: bool = False,
        gate: bool = False, design_contract: str = "") -> list[PromptSegment]:
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
        # Spec 14 (Item 3): a blocking finding must cite the file it is about, so a
        # claim about the world with no file behind it can't wedge the PR.
        "Every BLOCKING finding MUST name the file it concerns in \"path\" "
        "(a file:line in the body is better) — an uncited blocking finding is "
        "treated as advisory, not a merge blocker.\n"
        # Spec 14 (Item 5 / Phase 6): if the acceptance-gate output shown above
        # contradicts what this PR claims, that IS a blocking finding — cite the
        # failing file.
        "If acceptance-gate output is shown above and it contradicts this PR's "
        "claim (e.g. the PR says a level is non-trivial but the gate solves it in "
        "0 strokes), request changes with a blocking finding citing the file.\n"
    )
    envelope = (
        f"The PR head you are reviewing is {pr.get('head')!r}; echo it verbatim as "
        '"reviewed_head".\n'
        + ("Read whatever files you need with your native tools first; then reply "
           "with your verdict. Do NOT emit a tool_plan / tool_calls intent — your "
           "reply must be a review_verdict.\n" if repo_read else "")
        + "Reply with ONLY a coding_turn.v1 envelope: "
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
        # Slice 1 §4: the design_contract segment (empty unless an approved
        # design_spec exists). The reviewer's token-compliance check reads it.
        PromptSegment("design_contract", design_contract),
        # Standing review rules + acceptance bar.
        PromptSegment("role_instructions", review_rules),
        # The PR diff under review (+ optional truncation flag).
        PromptSegment("pr_diff", f"PR diff vs master{trunc}:\n```diff\n{cap}\n```\n"),
        # Spec 12 (S1): the latest acceptance-gate output (verbatim). A reviewer
        # that sees the gate is red on the integrated tree can hold the PR to it;
        # empty (absent) when no gate has run, so the prompt is unchanged for a
        # gate-less project.
        PromptSegment("gate_output", gate_text),
        # Spec 17 (Item 1 + Phase 2): the reviewer's capability-aware tool_guidance
        # (new — the reviewer had none). Names Read/Grep/Glob when in-turn retrieval
        # is on (so a finding can be grounded in a real file), or that it judges from
        # the diff when it is off; always states no role can execute. Carries the
        # cite-a-file rule that makes Spec 14's `cited` flag satisfiable, framed
        # around the read capability.
        PromptSegment(
            "tool_guidance",
            f"{tool_catalog_text(REVIEWER, repo_read=repo_read, gate=gate)} "
            "Ground every BLOCKING finding in a real file — name it in \"path\" "
            "(a file:line in the body is better).\n"),
        # Truncation caveat (empty when the diff fit).
        PromptSegment("role_instructions", trunc_note),
        # reviewed_head echo instruction + verdict envelope schema.
        PromptSegment("role_instructions", envelope),
    ]


# --- F146 Slice B: delivery review of the integrated head --------------------

# A stable synthetic task_id for delivery-review turns (the reviewer echoes it;
# parse_coding_turn requires envelope.task_id == this for a non-PM role).
_DELIVERY_TASK_ID = "delivery-review"
# F154: the synthetic command id for the auto-derived build. Distinct from any
# registry id (which the council chooses) so the two can never collide, and so the
# build's recorded run is identifiable on the ledger.
_DEFAULT_BUILD_COMMAND_ID = "build:default"
# ...and its own task_id, NOT _DELIVERY_TASK_ID. The delivery task_id identifies runs
# of the council's REGISTERED suite; filing the engine-derived build under it makes
# "did the delivery suite pass?" unanswerable by task_id alone (a green build would
# read as a green suite). Keeping them separate also means the build cannot perturb
# any detector that partitions runs by task.
_DEFAULT_BUILD_TASK_ID = "delivery-build"


class DeliveryReviewResult(NamedTuple):
    """Outcome of verifying the INTEGRATED delivered head as a unit.

    ``passed`` gates whether ``project_done`` is allowed to stick. ``filed_findings``
    tells the loop whether real rework was queued — a fail that queued dev tasks is
    *progress* (the run re-opens to work them), a fail that could not verify and
    queued nothing counts toward the no-progress stop. ``reason`` is diagnostic."""
    passed: bool
    filed_findings: bool = False
    reason: str = ""


def _delivery_review_prompt(store: LedgerStore, head: str, diff: str,
                            *, repo_read: bool = False) -> str:
    """Ask a reviewer to judge the COMPLETE delivered diff as one integrated unit
    (integration correctness the per-PR reviews cannot see), bound to the delivered
    head. Emits the SAME coding_turn.v1 reviewer envelope as ``_review_pr_prompt``
    so ``parse_coding_turn(REVIEWER, ...)`` validates it; only the framing differs
    (delivery-wide vs one scoped task).

    SPEC-30 fix: ``repo_read`` means the delivered tree is mounted read-only for
    this turn, so the reviewer can LIST/OPEN the delivered files instead of
    inferring them from a (possibly truncated) diff. Ungrounded, run 7's delivery
    reviewer invented a filename ("Missing acceptance test file test/test.js") that
    the DEV then could not act on — the finding named a path that was never the
    team's convention. Grounded, it must verify a file's ABSENCE by looking, and
    describe any missing deliverable by its BEHAVIOUR, not an invented path."""
    diff = _filter_generated_from_diff(diff)
    cap = diff[:_REVIEW_DIFF_CAP]
    truncated = len(diff) > _REVIEW_DIFF_CAP
    trunc = " [diff truncated]" if truncated else ""
    # SPEC-30 fix: a truncated diff must NOT force a delivery rejection. The per-PR
    # review can validly ask to "split" a large PR — but the DELIVERY diff IS the
    # whole finished project, which cannot be split, so "reduce the delivered
    # change" is an UNSATISFIABLE constraint that rejected every large deliverable
    # forever (run 6 stopped planning_churn here with a working game merged). It is
    # also unnecessary now: the execution gate (registered tests + runtime launch +
    # the web:probe that drives the assembled artifact) proves whole-artifact
    # correctness the diff cannot, so the delivery reviewer judges the VISIBLE
    # portion for integration defects and leaves "does it run as assembled" to the
    # gate. Truncation is a note, never an auto-reject.
    # SPEC-32: with the tree mounted (repo_read), truncation is a READ CUE — open
    # the affected files rather than judging (or rejecting) from the diff. Run 10's
    # delivery reviewer rejected a truncated acceptance test it could have opened.
    if truncated and repo_read:
        trunc_note = (
            "The diff above is large and was truncated to fit — but you have the "
            "COMPLETE delivered tree mounted read-only. OPEN the affected files with "
            "your native read tools (Read/Grep/Glob) and judge them from source; do "
            "NOT reject because a file is truncated in the diff, and do NOT reject "
            "because the delivered change is large — it is the whole project and "
            "cannot be split. Rely on the recorded execution gate (tests, runtime "
            "launch, and the web:probe that drove the assembled page) for "
            "whole-artifact correctness the diff cannot show.\n")
    elif truncated:
        trunc_note = (
            "The diff above is large and was truncated to fit — code beyond the cut "
            "is NOT shown. Do NOT reject solely because the delivered change is "
            "large: it is the complete project and cannot be split. Judge the "
            "VISIBLE portion for integration defects (contract/type/import "
            "mismatches across merged parts), and rely on the recorded execution "
            "gate (tests, runtime launch, and the web:probe) for whole-artifact "
            "correctness.\n")
    else:
        trunc_note = ""
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
            "approved": True,
            "findings": [],
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
        + ("The COMPLETE delivered tree is mounted read-only for this turn — open "
           "and list files to check what actually exists. Do NOT invent or assume a "
           "file path: verify a file's ABSENCE by looking. If the Definition of Done "
           "requires a deliverable that is genuinely missing (e.g. an acceptance "
           "test), describe it by its BEHAVIOUR and acceptance criteria in the "
           "finding — never by a made-up filename the team never used.\n"
           if repo_read else "")
        + f"Delivered diff vs the project base{trunc}:\n```diff\n{cap}\n```\n"
        f"{trunc_note}"
        f"The delivered head you are reviewing is {head!r}; echo it verbatim as "
        '"reviewed_head".\n'
        "Reply with ONLY a coding_turn.v1 envelope: "
        f"{verdict_example}. If approved=false you MUST include at least one "
        'finding. Each finding is an object {"severity":"minor|major|blocking",'
        '"title":"...","body":"...","path":"..."}.\n'
    )


# --- F146 Slice C: runtime launch evidence for the delivered head ------------

def _default_verify_command(root: Any) -> Optional[tuple[list[str], Any]]:
    """F154: derive a build/typecheck command for a project with NO registered test
    commands. Returns ``(argv, cwd)``, or ``None`` when nothing is safe to run.

    Why this exists: a greenfield project starts with an EMPTY registry, and two
    gates read that emptiness as success — ``_set_mergeable_if_ready``'s ``tests_ok``
    is vacuously satisfied (so every PR merges on a reviewer model-approval with zero
    compilation ever run) and ``delivery_review`` sets ``tests_passed=True``
    unconditionally. F152/F153 catch an app that fails to *serve or start*; neither
    can catch a compile or type error on a code path never requested at launch. This
    is the zero-config floor that does.

    The table is deliberately SMALL and conservative. ``None`` preserves today's
    behaviour exactly, so an unknown stack costs nothing; a wrong rule, by contrast,
    files a phantom code finding against work that is fine. Order matters — a real
    build is strictly stronger than ``compileall``, which only catches syntax errors.

    The manifest is searched at ``root`` and then one level down, because the CLI
    delivers into a subdirectory; ``cwd`` follows the manifest it found. Fully
    guarded: any read/parse failure yields ``None`` rather than raising into the
    delivery gate."""
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path

    try:
        base = _Path(str(root))
        if not base.is_dir():
            return None
        # The root itself first, then immediate subdirectories (sorted, so the
        # choice is deterministic across runs). `_SCAN_DENY_DIRS` keeps the sweep
        # on code the team actually wrote: once a bare workspaces root stopped
        # ending the scan, every sibling became reachable, and `compileall` is
        # RECURSIVE — one Python-2-syntax file under `vendor/` or `third_party/`
        # would have filed a "fix delivery build" task against code nobody here
        # owns. That is the phantom finding this function's docstring warns about.
        candidates = [base] + sorted(
            (p for p in base.iterdir()
             if p.is_dir() and not p.name.startswith(".")
             and p.name not in _SCAN_DENY_DIRS),
            key=lambda p: p.name)
    except Exception:  # noqa: BLE001 — an unreadable tree derives nothing
        return None

    def _derive(d: Any) -> Optional[tuple[int, list[str], Any]]:
        """(strength, argv, cwd) for one candidate, or None. Lower is stronger."""
        pkg = d / "package.json"
        if pkg.is_file():
            try:
                scripts = (_json.loads(pkg.read_text("utf-8")) or {}).get(
                    "scripts") or {}
            except Exception:  # noqa: BLE001 — unreadable manifest -> no guess
                return None
            if isinstance(scripts, dict) and scripts.get("build"):
                return 0, ["npm", "run", "build"], d
            if (d / "tsconfig.json").is_file():
                # No build script but TypeScript is configured -> typecheck only.
                return 0, ["npx", "--no-install", "tsc", "--noEmit"], d
            # Nothing safe to run FOR THIS CANDIDATE — yield nothing rather than
            # abandoning the sweep. A workspaces root carries a `package.json`
            # with no `build` and no `tsconfig.json`, so returning early here
            # disabled the gate for the whole monorepo even though `<root>/app`
            # had a real build.
            return None
        if (d / "Cargo.toml").is_file():
            return 0, ["cargo", "build", "--quiet"], d
        if (d / "go.mod").is_file():
            return 0, ["go", "build", "./..."], d
        if ((d / "pyproject.toml").is_file() or (d / "setup.py").is_file()
                or any(d.glob("*.py"))):
            # Syntax-level only, and honestly so: it needs no dependencies and
            # cannot fail for environmental reasons. A project wanting type/name
            # checking registers real test commands.
            return 1, [_sys.executable, "-m", "compileall", "-q", str(d)], d
        return None

    # Strength is a GLOBAL ranking, not a per-directory one. The precedence table
    # is only meaningful if "a real build is strictly stronger than compileall"
    # holds ACROSS candidates too: scanning directory-by-directory and returning
    # the first hit let an alphabetically earlier `docs/conf.py` win over a real
    # `vite build` in `web/`. Ties keep the old order (root first, then sorted).
    best: Optional[tuple[int, list[str], Any]] = None
    for d in candidates:
        try:
            found = _derive(d)
        except Exception:  # noqa: BLE001 — a bad candidate is skipped, never fatal
            continue
        if found is not None and (best is None or found[0] < best[0]):
            best = found
            if best[0] == 0:  # nothing outranks a real build; stop early
                break
    if best is None:
        return None
    return best[1], best[2]


def _ensure_delivery_setup(store: LedgerStore, workspace: Any) -> tuple[bool, str]:
    """F154 §3: install dependencies ONCE before the default build runs.

    ``npm run build`` / ``cargo build`` need deps present. Without this, a build that
    fails only because dependencies were never installed would be filed as a code
    finding against work that is perfectly fine — the exact false-finding risk that
    made F154 a fast-follow rather than part of the original F152 PR.

    Idempotent by construction: ``_setup_pending_venv_missing`` is the same gate
    ``launch_probe`` consults, so running setup here makes the later launch probe's
    own setup a no-op, and a project already set up is untouched.

    Returns ``(ok, detail)``. A project with **no runnable profile** is ``(True, "")``
    — there is nothing to set up, and a static site's build needs no deps. A setup
    FAILURE is ``(False, detail)``, which the caller turns into ``cannot_verify``:
    it blocks ``done`` but files NO dev task, exactly as today's launch-setup failure
    does."""
    try:
        from .runtime import RuntimeProfileStore
        rstore = RuntimeProfileStore.for_ledger(store)
        profiles = rstore.list_profiles()
    except Exception:  # noqa: BLE001 — can't enumerate -> nothing to set up
        return True, ""
    runnable = [p for p in profiles
                if getattr(p, "runtime_mode", "") == "managed_local"
                and getattr(p, "start", None)]
    if not runnable:
        return True, ""
    try:
        from .runtime_process import RuntimeProcessManager
        mgr = RuntimeProcessManager(
            project_id=store.project_id, rstore=rstore,
            workspace_root=workspace.root(), work_root=store.dir)
    except Exception as exc:  # noqa: BLE001
        return False, f"setup could not start: {exc}"
    for profile in runnable:
        try:
            if not mgr._setup_pending_venv_missing(profile, profile.profile_id):
                continue
            # `setup()` is the BACKGROUND variant — it returns a `starting` session
            # for the caller to poll. Grading it with `_setup_succeeded` (which
            # requires a TERMINAL `stopped` + exit 0) therefore always failed, AND
            # left a `pip install` running that raced the launch probe: the probe
            # re-checks the same gate, sees the venv interpreter the racing
            # `python -m venv` just created, skips its own setup, and starts against
            # a half-installed venv — filing a phantom "fix runtime launch crash"
            # against correct code. `_setup_sync` runs it inline and returns the
            # terminal session, which is what this gate needs.
            from .runtime_process import _setup_succeeded
            session = mgr._setup_sync(profile.profile_id)
            if not _setup_succeeded(session):
                return False, f"{profile.profile_id}: dependency setup failed"
        except Exception as exc:  # noqa: BLE001 — cannot set up -> cannot verify
            return False, f"{profile.profile_id}: setup error: {exc}"
    return True, ""


# Dependency markers per stack. `_ensure_delivery_setup` only stands up a PYTHON
# venv — `_setup_pending_venv_missing` keys on `_is_pip_install_step`, which is
# pip-only — and `run_test_commands` runs with `network_allowed=False` by contract.
# So for a stack whose build must resolve dependencies, running the build without
# them present would fail for an ENVIRONMENTAL reason and be filed as a code
# finding. Absent the marker, the honest answer is "cannot verify".
_SCAN_DENY_DIRS: frozenset[str] = frozenset({
    # Dependency trees, vendored code, and sample/doc trees. None of these are
    # the delivered program, and `compileall` recurses — see `_default_verify_command`.
    "node_modules", "vendor", "third_party", "thirdparty", "bower_components",
    "examples", "example", "samples", "fixtures", "testdata", "docs", "doc",
    "dist", "build", "target", "out", "__pycache__", "site-packages", "venv",
})

_BUILD_DEP_MARKERS: tuple[tuple[str, str], ...] = (
    ("npm", "node_modules"),
    ("npx", "node_modules"),
    ("cargo", "target"),
    ("go", "go.sum"),
)


def _missing_build_dep(
    argv: list[str], cwd: Any, root: Any,
) -> Optional[tuple[str, str]]:
    """F154: ``(tool, marker)`` when a build's dependencies are absent, else None.

    The marker is resolved by walking UP from ``cwd`` to the delivered ``root``,
    because that is how the toolchains themselves resolve it. npm workspaces HOIST
    `node_modules` to the repo root, so a workspace member legitimately has none of
    its own; probing only ``cwd`` reported "dependencies absent" for a correctly
    installed monorepo. That mattered because ``build_cannot_verify`` blocks `done`
    and files NO task — no code change could clear it, so the run wedged and stopped
    on `no_progress` with nothing telling the team why.

    Extracted from ``_run_default_build`` so the invariant is testable directly: the
    original probe was inline, and the test for it re-implemented the check instead
    of calling it, which is how the hoisted-layout bug survived a green suite."""
    from pathlib import Path as _P

    tool = str(argv[0]).lower() if argv else ""
    try:
        root_stop = _P(str(root)).resolve()
    except Exception:  # noqa: BLE001 — an unresolvable root just stops the walk
        root_stop = None
    for prefix, marker in _BUILD_DEP_MARKERS:
        if not tool.endswith(prefix):
            continue
        try:
            here = _P(str(cwd)).resolve()
        except Exception:  # noqa: BLE001
            return prefix, marker
        while True:
            if (here / marker).exists():
                return None
            if here == here.parent or (root_stop is not None and here == root_stop):
                return prefix, marker
            here = here.parent
    return None


def _run_default_build(
    store: LedgerStore, workspace: Any, head: str,
    *, should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[bool, bool, str]:
    """F154: run the auto-derived build/typecheck at the delivered head.

    Returns ``(built_clean, cannot_verify, detail)`` — deliberately the same
    tri-state shape ``_delivery_launch_evidence`` uses, so the caller's fail-closed
    handling is symmetric:

    * nothing derivable for this stack -> ``(True, False, "")`` (skipped, vacuously
      clean — today's exact behaviour);
    * deps could not be installed -> ``(False, True, detail)`` — a verify error, NOT
      a build failure, so no phantom code finding is filed;
    * the build ran and failed -> ``(False, False, detail)`` — a real code finding;
    * the build ran clean -> ``(True, False, "")``.

    Runs through the same sandboxed executor and ``require_sandbox`` posture as the
    registry path, and records a synthetic test run bound to ``head`` so the evidence
    is on the ledger like any other."""
    try:
        derived = _default_verify_command(workspace.root())
    except Exception:  # noqa: BLE001 — a resolver failure derives nothing
        derived = None
    if derived is None:
        return True, False, ""
    argv, cwd = derived
    setup_ok, setup_detail = _ensure_delivery_setup(store, workspace)
    if not setup_ok:
        return False, True, f"dependency setup failed: {setup_detail}"
    # A build that must resolve dependencies cannot run here without them: the
    # executor is network-off by contract (`testing._run_one`), and
    # `_ensure_delivery_setup` only stands up a Python venv. Running anyway would
    # fail environmentally (cargo 101, go module fetch, npm's missing script binary)
    # and be filed as a code finding — the exact false-finding risk F154 was
    # sequenced as a fast-follow to avoid. Report cannot-verify instead.
    try:
        missing = _missing_build_dep(argv, cwd, workspace.root())
        if missing is not None:
            prefix, marker = missing
            return False, True, (
                f"{prefix} build needs dependencies and {marker!r} is absent; "
                "the delivery executor is network-off and only provisions a "
                "Python venv, so this cannot be verified here")
    except Exception:  # noqa: BLE001 — a marker probe failure must not block
        pass
    from pathlib import Path as _Path
    try:
        rel = str(_Path(str(cwd)).relative_to(_Path(str(workspace.root())))) or "."
    except Exception:  # noqa: BLE001 — a non-relative cwd falls back to the root
        rel = "."
    registry = {_DEFAULT_BUILD_COMMAND_ID: {
        "argv": [str(a) for a in argv], "cwd": rel, "timeout_seconds": 600}}
    try:
        session = run_test_commands(
            workspace.root(), registry, [_DEFAULT_BUILD_COMMAND_ID],
            should_cancel=should_cancel,
            require_sandbox=store.get_require_sandbox())
    except Exception as exc:  # noqa: BLE001 — could not execute -> cannot verify
        return False, True, f"default build could not run: {exc}"
    try:
        store.record_test_run(session, task_id=_DEFAULT_BUILD_TASK_ID, head=head)
    except Exception:  # noqa: BLE001 — recording is best-effort
        pass
    if session.passed:
        return True, False, ""
    # Distinguish "the build ran and reported errors" from "the command could not
    # run at all". The latter is environmental — no code merge flips it green — so it
    # must be cannot_verify, never a phantom code finding.
    #
    # The executor reports `blocked` (sandbox refused) and `timed_out` for the
    # environmental cases; a command that RAN and exited non-zero is `failed` with a
    # real exit code, which is exactly the compile error we want to catch. Exit 127
    # is the other environmental shape — the toolchain is simply not installed (no
    # npm, no cargo, no go), which is not the council's defect.
    detail = "; ".join(f"{r.command_id}={r.status}/{r.exit_code}"
                       for r in session.results)
    appendix = _failed_stderr_appendix(session.results)
    if appendix:
        detail += f"\n\nBuild output:\n{appendix}"
    # An earlier revision keyed only on `blocked`/`timed_out`/exit-127 and MISSED the
    # actual missing-toolchain shape. Execution is argv-only with no shell
    # (`testing._run_one`), so an absent `npm`/`cargo`/`go` never yields 127 — it
    # raises FileNotFoundError in `create_subprocess_exec`, which the runner reports
    # as `status="failed"` with **`exit_code=None`**. That was being read as a real
    # build failure and filed as "fix delivery build" against code that is fine.
    #
    #   exit_code is None  -> spawn/exec failure (toolchain absent, spawn refused)
    #   exit_code < 0      -> killed by signal (OOM)
    #   126 / 127          -> not executable / command-not-found from inside a script
    unusable = any(
        r.status in ("blocked", "timed_out")
        or r.exit_code is None
        or (isinstance(r.exit_code, int) and r.exit_code < 0)
        or r.exit_code in (126, 127)
        for r in session.results)
    if unusable:
        return False, True, f"default build could not run: {detail}"
    return False, False, detail


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
    # Spec 12 (S1): the tester runs on the PR BRANCH, so it may only choose from
    # unit-scoped commands; acceptance commands run on the integrated tree.
    registry = store.get_unit_test_commands()
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
        # Spec 12 (S1): the latest acceptance-gate output (verbatim), so the tester
        # sees the last integrated result before choosing commands. Empty (absent)
        # with no gate run -> byte-identical to before.
        PromptSegment("gate_output", _gate_state.latest_gate_text(store)),
        # Spec 17 (Item 1 + Phase 2): the tester's capability-aware tool_guidance
        # (new — the tester had none). The tester never gets in-turn retrieval
        # (repo_read is False), so the catalog states it judges from the context
        # provided and that no role can execute — the ENGINE runs the registered
        # commands; the tester only chooses them.
        PromptSegment(
            "tool_guidance",
            tool_catalog_text(TESTER, repo_read=False,
                              gate=_gate_state.gate_available(store)) + "\n"),
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


# Spec 13 (S2): "web-only" manifest-bound source — the subset of _MANIFEST_BOUND_EXT
# a browser can resolve itself. A tree whose manifest-bound source is entirely in
# this set MAY be a buildless web project (checked below); one carrying compiled
# source (.go/.rs/.java/…) is never buildless and stays manifest-bound.
_WEB_ONLY_EXT = (".js", ".mjs", ".cjs", ".css", ".html", ".htm")
# Signals that a web tree needs a bundler after all — a bare-specifier import, a
# CommonJS require, or JSX/TS syntax the browser cannot run as-is. The presence of
# ANY re-clamps to "needs a manifest" (the reddit-look-a-like protection).
_BUNDLER_REQUIRED_EXT = (".ts", ".tsx", ".jsx", ".vue", ".svelte")
# `import x from "react"` / `import "react"` (bare) vs `import x from "./m.js"` or
# `"/m.js"` (relative/absolute-path — browser-resolvable). The captured group is the
# module specifier; a leading "." or "/" means path-resolved, anything else is a
# bare specifier that only a bundler resolves.
_JS_IMPORT_FROM_RE = re.compile(
    r"""\bimport\b[^'"]*?\bfrom\s*['"]([^'"]+)['"]""")
_JS_IMPORT_BARE_RE = re.compile(r"""\bimport\s*['"]([^'"]+)['"]""")
# `export {x} from "react"` / `export * from "react"` — a bare RE-EXPORT. The
# import regexes require `\bimport\b`, so a re-export whose specifier is bare
# (needs a bundler) would otherwise slip through. Same specifier semantics as
# the import case: a leading "." or "/" is browser-resolvable, anything else is
# a bare specifier only a bundler resolves.
_JS_EXPORT_FROM_RE = re.compile(
    r"""\bexport\b[^'"]*?\bfrom\s*['"]([^'"]+)['"]""")
_JS_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"][^'"]+['"]\s*\)""")
# JSX inside a `.js` file — a transpiler/bundler signal (a browser can't run it
# as-is). Conservative: match a real JSX element, never a bare `<` or an `a < b`
# comparison. Three unambiguous shapes: a self-closing element `<Tag ... />`, a
# closing tag `</Tag>`, or a tag returned from a render function
# (`return <Tag` / `=> <Tag`). `a < b` never matches (a space or non-letter
# follows `<`); `x >> 1` / generics never match (no tag/`/>`/`</` shape).
# JSX in a .js file needs a transpiler (a browser can't run it). Match a `<Tag`
# ONLY in an expression-introducing position — right after return / => / = / ( / ,
# (with optional wrapping paren), never after a quote. Real JSX always appears in
# one of these positions; HTML built as a STRING (`innerHTML = "<div>"`,
# `insertAdjacentHTML(x, "<li>")`, a `</section>` in a comment) is always
# quote/space-preceded, so it can't match — that string-literal false positive
# would wrongly mark a vanilla-JS no-build app as bundler-required (review Δ).
_JSX_RE = re.compile(r"""(?:\breturn|=>|[=(,])\s*\(?\s*<[A-Za-z][\w.-]*""")
# `<script src="…">` / `<link rel="stylesheet" href="…">` — captures the URL.
_HTML_SCRIPT_SRC_RE = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_HTML_LINK_HREF_RE = re.compile(
    r"""<link\b[^>]*\bhref\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_BUILDLESS_READ_CAP = 200_000  # per-file read bound for the scan (fail closed if larger)


def _is_relative_url(url: str) -> bool:
    """A URL the browser resolves against the page's own origin (so it maps to a
    file on master), as opposed to an external/CDN dependency. Absolute
    (`//`, `http:`, `https:`, `data:`) and root-anchored protocol-relative URLs are
    NOT self-contained for this gate's purpose (a CDN `<script src>` means the tree
    depends on something not on master)."""
    u = url.strip()
    if not u:
        return False
    low = u.lower()
    if low.startswith(("http://", "https://", "//", "data:", "blob:")):
        return False
    return True


def _resolve_against(base_rel: str, url: str) -> str:
    """Resolve a relative URL from an HTML/JS file at ``base_rel`` to a
    master-relative path, normalized (``./``, ``../`` collapsed). A leading "/" is
    treated as project-root-relative."""
    href = url.split("?", 1)[0].split("#", 1)[0]
    if href.startswith("/"):
        joined = href.lstrip("/")
    else:
        base_dir = base_rel.rsplit("/", 1)[0] if "/" in base_rel else ""
        joined = f"{base_dir}/{href}" if base_dir else href
    parts: list[str] = []
    for seg in joined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _buildless_web_ready(files: list[str], read: Any) -> bool:
    """Spec 13 (S2): True iff master is a self-contained, no-build web project —
    an ``index.html`` whose relative ``<script src>`` / ``<link href>`` graph
    resolves entirely against files on master, with no bundler-required signal
    anywhere.

    This is the missing distinction in the F142 foundation gate: "web/JS" is not
    one ecosystem. A bundled app (Next.js/Vite — bare-specifier imports, JSX)
    genuinely needs ``package.json`` to resolve imports, and stays manifest-bound.
    A buildless site (the gravity-golf North Star — ``index.html`` + relative
    ``<script src>`` modules the browser resolves itself) never legitimately
    produces a manifest, so requiring one clamps worker concurrency to 1 forever.

    ``read(rel) -> str | None`` returns a master file's text (``None`` if absent /
    binary / unreadable). Fail-closed: anything unreadable or ambiguous returns
    ``False`` so the clamp stays on — this can only ever RELAX the manifest
    requirement for a tree we can fully vouch for, never tighten it. Because
    ``refresh_foundation_status`` re-derives from git each call, the classification
    self-heals: the moment a bare import or a ``.tsx`` lands, the tree flips back to
    manifest-bound.
    """
    fileset = set(files)
    if "index.html" not in fileset:
        return False
    # (3) any bundler-required file anywhere on master disqualifies immediately —
    # a .tsx/.vue/.svelte tree is a framework app, buildless or not.
    if any(f.endswith(_BUNDLER_REQUIRED_EXT) for f in files):
        return False

    html = read("index.html")
    if not isinstance(html, str) or len(html) > _BUILDLESS_READ_CAP:
        return False

    # (1)+(2)+(4): every relative script/style resolves on master, no external
    # dependency, at least one script actually exists.
    referenced_scripts: list[str] = []
    for m in _HTML_SCRIPT_SRC_RE.finditer(html):
        url = m.group(1)
        if not _is_relative_url(url):
            return False  # a CDN/external <script src> — not self-contained
        target = _resolve_against("index.html", url)
        if target not in fileset:
            return False  # references a file not on master
        referenced_scripts.append(target)
    for m in _HTML_LINK_HREF_RE.finditer(html):
        url = m.group(1)
        # Only stylesheet links are load-bearing; a non-relative one (Google
        # Fonts, a CDN reset) is an external dependency and disqualifies.
        if not _is_relative_url(url):
            # A <link> may be a preconnect/icon/manifest with an off-site href;
            # only treat a stylesheet href as disqualifying.
            if re.search(r"stylesheet", m.group(0), re.IGNORECASE):
                return False
            continue
        target = _resolve_against("index.html", url)
        if target not in fileset and target.endswith((".css",)):
            return False

    if not referenced_scripts:
        return False  # index.html with no resolvable script is a stub, not a base

    # (3) re-check inside every referenced script (one level deep): a bare-specifier
    # import or a require() means a bundler is needed after all.
    for rel in referenced_scripts:
        body = read(rel)
        if not isinstance(body, str) or len(body) > _BUILDLESS_READ_CAP:
            return False  # unreadable/oversized referenced script -> fail closed
        if _JS_REQUIRE_RE.search(body):
            return False
        if _JSX_RE.search(body):
            return False  # JSX in a .js file -> needs a transpiler/bundler
        for spec in _JS_IMPORT_FROM_RE.findall(body):
            if not spec.startswith((".", "/")):
                return False  # bare specifier -> needs a bundler
        for spec in _JS_IMPORT_BARE_RE.findall(body):
            if not spec.startswith((".", "/")):
                return False
        for spec in _JS_EXPORT_FROM_RE.findall(body):
            if not spec.startswith((".", "/")):
                return False  # bare re-export specifier -> needs a bundler
    return True


def _buildless_stall_reason(files: list[str], read: Any) -> Optional[str]:
    """Spec 13 (Item 2): for a WEB-ONLY tree that is not yet buildless-ready, name
    the specific Item-1 condition that is failing — a cause the PM can act on
    (e.g. "index.html references src/main.js, absent on master") instead of the
    generic "a foundation is one of three shapes" sentence. Returns ``None`` when
    the tree is already buildless-ready or nothing can be diagnosed (fall back to
    the generic rationale). Mirrors ``_buildless_web_ready``'s condition order."""
    fileset = set(files)
    if "index.html" not in fileset:
        return "no index.html has merged to master yet"
    bundler = next((f for f in files if f.endswith(_BUNDLER_REQUIRED_EXT)), None)
    if bundler is not None:
        return f"{bundler} is a bundler-required file, so a manifest is needed"
    html = read("index.html")
    if not isinstance(html, str) or len(html) > _BUILDLESS_READ_CAP:
        return "index.html on master could not be read"
    referenced_scripts: list[str] = []
    for m in _HTML_SCRIPT_SRC_RE.finditer(html):
        url = m.group(1)
        if not _is_relative_url(url):
            return (f"index.html loads an external <script src> ({url}), so "
                    "the tree is not self-contained")
        target = _resolve_against("index.html", url)
        if target not in fileset:
            return f"index.html references {target}, absent on master"
        referenced_scripts.append(target)
    for m in _HTML_LINK_HREF_RE.finditer(html):
        url = m.group(1)
        if not _is_relative_url(url):
            if re.search(r"stylesheet", m.group(0), re.IGNORECASE):
                return (f"index.html links an external stylesheet ({url}), so "
                        "the tree is not self-contained")
            continue
        target = _resolve_against("index.html", url)
        if target not in fileset and target.endswith((".css",)):
            return f"index.html references {target}, absent on master"
    if not referenced_scripts:
        return "index.html references no local <script src> yet"
    for rel in referenced_scripts:
        body = read(rel)
        if not isinstance(body, str) or len(body) > _BUILDLESS_READ_CAP:
            return f"referenced script {rel} could not be read"
        if _JS_REQUIRE_RE.search(body) or _JSX_RE.search(body):
            return (f"{rel} uses a bundler-required construct "
                    "(require()/JSX), so a manifest is needed")
        for spec in (*_JS_IMPORT_FROM_RE.findall(body),
                     *_JS_IMPORT_BARE_RE.findall(body),
                     *_JS_EXPORT_FROM_RE.findall(body)):
            if not spec.startswith((".", "/")):
                return f'{rel} imports the bare specifier "{spec}", so a bundler/manifest is needed'
    return None


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
    - Buildless web projects (Spec 13): a web-only tree whose `index.html`
      `<script src>`/`<link href>` graph resolves entirely against files on
      master, with no bundler-required signal (bare imports / require / JSX),
      is a complete foundation with no manifest — the gravity-golf North Star
      that opens directly in a browser. A bundled app (bare imports, .tsx) stays
      manifest-bound, so the reddit-look-a-like protection holds.

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
    # Spec 13 (S2): a BUILDLESS web project is foundation-ready without a manifest.
    # Only consider it when the manifest-bound source is entirely web-only
    # (.js/.css/.html — a browser can resolve it) and there is no compiled source;
    # a tree carrying .go/.rs/.java/… stays manifest-bound unconditionally. The
    # predicate reads referenced files, so gate it on the cheap extension check
    # first and only reach for the workspace reader for a genuine web-only tree.
    if has_manifest_bound and not any(
            f.endswith(_MANIFEST_BOUND_EXT) and not f.endswith(_WEB_ONLY_EXT)
            for f in files):
        try:
            def _read(rel: str) -> str | None:
                raw = workspace.read_master_file(rel)
                if raw is None:
                    return None
                try:
                    return raw.decode("utf-8")
                except (UnicodeDecodeError, AttributeError):
                    return None
            if _buildless_web_ready(files, _read):
                return True
        except Exception:  # noqa: BLE001 — unsure -> fall through to the manifest rule
            pass
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
    # Spec 13 (Item 2): while pending, persist a shape-aware stall reason for a
    # WEB-ONLY tree so `_account_foundation_stall` (which sees only the ledger)
    # can name the specific failing Item-1 condition instead of a generic list.
    # Best-effort — cleared unless we can diagnose a web-only shape.
    reason = ""
    if status == "pending":
        try:
            files = [f for f in workspace.list_files(scope="master")
                     if f != ".gitignore"]
            web_only = any(f.endswith(_WEB_ONLY_EXT) for f in files) and not any(
                f.endswith(_MANIFEST_BOUND_EXT) and not f.endswith(_WEB_ONLY_EXT)
                for f in files)
            if web_only:
                def _read(rel: str) -> str | None:
                    raw = workspace.read_master_file(rel)
                    if raw is None:
                        return None
                    try:
                        return raw.decode("utf-8")
                    except (UnicodeDecodeError, AttributeError):
                        return None
                reason = _buildless_stall_reason(files, _read) or ""
        except Exception:  # noqa: BLE001 — diagnosis is best-effort
            reason = ""
    try:
        store.set_run_state(foundation_status=status,
                            foundation_stall_reason=reason)
    except Exception:  # noqa: BLE001
        pass
    # F141 WS-I: a `new` project crosses into the steering phase (Current Focus
    # becomes relevant) only when its initial North Star is MET — i.e. the project
    # reaches `done` (stamped in set_project_status), NOT at the first
    # foundation-merge. A foundation landing means the build is underway, not that
    # the North Star is complete, so the Current Focus panel stays hidden until
    # completion ("Building toward <North Star>" shows instead).
    return status


def _last_turn_grounding() -> tuple[Optional[int], Optional[int]]:
    """Spec 14: (num_turns, duration_ms) of the most recent member call, from the
    F143 thread-local usage sink. ``num_turns`` is the claude CLI's agentic turn
    count (None for vendors that don't report it); ``duration_ms`` is the latency
    fallback. Both None when unavailable."""
    last = getattr(_usage_sink, "last", None)
    if not isinstance(last, dict):
        return None, None
    nt = last.get("num_turns")
    dur = last.get("duration_ms")
    return (nt if isinstance(nt, int) else None,
            dur if isinstance(dur, int) else None)


def _is_empty_approval(parsed: Any, head: str) -> bool:
    """Spec 14: a fresh-head approval that filed NO findings — the shape a reflex
    rubber-stamp takes. A parse error, a stale head, or any findings disqualify."""
    if isinstance(parsed, TurnParseError):
        return False
    intent = getattr(parsed, "intent", None)
    if intent is None or getattr(intent, "reviewed_head", None) != head:
        return False
    return bool(getattr(intent, "approved", False)) and not getattr(
        intent, "findings", None)


def _mark_finding_citations(findings: list[dict[str, Any]], *, workspace: Any,
                            pr: dict[str, Any]) -> list[dict[str, Any]]:
    """Spec 14 (Item 3): flag every BLOCKING finding whose ``path`` is empty or not
    in the PR tree with ``cited: false``. Severity and the ``approved`` verdict are
    UNTOUCHED — the PR still goes changes_requested; this is a quality signal
    (consumed by Spec 15, which decides whether the rework task is spawned), not a
    re-scoring. A well-formed finding that cites a changed/known file is
    ``cited: true``. Non-blocking findings are left as-is."""
    try:
        in_tree = set(workspace.changed_paths(pr["branch"])) if workspace else set()
    except Exception:  # noqa: BLE001
        in_tree = set()
    try:
        in_tree |= set(workspace.list_files(scope="master")) if workspace else set()
    except Exception:  # noqa: BLE001
        pass
    out: list[dict[str, Any]] = []
    for f in findings:
        g = dict(f)
        if g.get("blocking"):
            path = str(g.get("path") or "").strip()
            g["cited"] = bool(path) and (not in_tree or path in in_tree)
            # GL02 (Item 1): tag the lane next to Spec 14's ``cited`` flag so
            # ``errorta prs`` / post-mortems show which lane decided each blocking
            # finding. Machine lane = a runtime/execution claim a diff cannot
            # evidence; everything else (and a pre-GL02 record with no tag) is
            # judgment — the conservative default.
            g["lane"] = "machine" if is_execution_claim(g) else "judgment"
        out.append(g)
    return out


def _runnable_managed_local_profile(store: LedgerStore) -> bool:
    """Spec 12 Item 5: whether the project has a launchable ``managed_local``
    runtime profile — the exact class the F146 Slice C launch probe
    (``_delivery_launch_evidence`` / ``launch_probe``) actually runs. Narrower
    than ``evidence._has_runnable_runtime`` (which counts ANY ``start`` argv,
    including a container profile the launch probe skips), so the in-loop runtime
    arm and the ``gate_due`` arming only trip when the machinery will really
    execute — a container-only project must not churn a no-op GateRun. Fully
    guarded: a read error means "nothing to probe"."""
    try:
        from .runtime import RuntimeProfileStore
        profiles = RuntimeProfileStore.for_ledger(store).list_profiles()
    except Exception:  # noqa: BLE001
        return False
    return any(getattr(p, "runtime_mode", "") == "managed_local"
               and getattr(p, "start", None) for p in profiles)


def _runtime_gate_probe(
    store: LedgerStore, workspace: Any, *, head: str,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Optional[TestRunResult]:
    """Spec 12 Item 5: drive the F146 Slice C launch machinery for the in-loop
    gate and classify the delivered runtime's startup as a synthetic
    ``runtime:launch`` ``TestRunResult`` (``passed`` only on a clean launch), so a
    crash-on-start is a RED gate in-loop — caught here instead of only at
    delivery. REUSES ``_delivery_launch_evidence`` (the RuntimeProcessManager,
    port allocation, sandboxing, headless env, and teardown) rather than
    duplicating that fragile process lifecycle.

    Returns ``None`` when the launch could not be VERIFIED — an environmental
    inability (sandbox unavailable / setup / spawn failure), which is not a code
    crash and must not churn a red gate; it records a decision instead. Fully
    guarded: any failure records a decision and returns ``None``, never raising
    into the caller (the whole in-loop gate is fail-open)."""
    try:
        launched_clean, cannot_verify, detail = _delivery_launch_evidence(
            store, workspace, head, should_cancel=should_cancel)
    except Exception as exc:  # noqa: BLE001 — a probe failure never fails the turn
        _record_runtime_probe_decision(
            store, choice="gate_runtime_error", rationale=str(exc))
        return None
    if cannot_verify:
        _record_runtime_probe_decision(
            store, choice="gate_runtime_cannot_verify",
            rationale=detail or "runtime launch could not be verified")
        return None
    return TestRunResult(
        command_id="runtime:launch", argv_sha256="",
        status="completed" if launched_clean else "failed",
        exit_code=0 if launched_clean else 1, passed=bool(launched_clean),
        duration_ms=0, stdout_sha256="", stdout_preview="",
        stderr_preview=str(detail or "")[:4000],
        reason="" if launched_clean else "runtime crashed on start")


def _record_runtime_probe_decision(store: LedgerStore, *, choice: str,
                                   rationale: str) -> None:
    """Best-effort audit of a runtime-probe outcome that did not become a gate
    result (an inability to verify, or a probe error). Never raises."""
    try:
        store.record_decision(
            title="in-loop runtime probe", context="in_loop_gate",
            choice=choice, rationale=str(rationale)[:2000])
    except Exception:  # noqa: BLE001
        pass


def _run_gate(store: LedgerStore, workspace: Any, *, head: str, task_id: str,
              should_cancel: Optional[Callable[[], bool]] = None,
              probe_runtime: bool = False) -> Any:
    """Spec 12 (S1): run EVERY registered command (unit + acceptance) against the
    integrated master tree, bound to ``head``, and record the session. This is the
    deterministic gate executor — no model command selection, so the verdict
    cannot be gamed — factored out of ``delivery_review`` so the in-loop gate and
    the delivery gate share one code path.

    Returns the ``TestRunSession`` (its ``.passed`` is the verdict), or ``None``
    when there is nothing to run. Runs against ``workspace.root()`` (the merged
    tree) precisely because the interesting defects — a self-sabotaging harness, a
    black-screen init race — are INTEGRATION defects invisible on a single branch.

    Spec 12 Item 5 — the runtime arm. When ``probe_runtime`` is set (the in-loop
    gate) AND the project has a runnable ``managed_local`` runtime profile, the
    F146 Slice C launch machinery is also driven and FOLDED into the recorded
    session as a synthetic ``runtime:launch`` result — so a runtime that crashes
    on start is a red gate in-loop, and a runtime-ONLY project (a runnable
    profile, no commands) still produces a gate record. ``delivery_review`` leaves
    ``probe_runtime`` False: it runs its own ``_delivery_launch_evidence``
    separately, so the shared executor must not double-probe there."""
    registry = store.get_test_commands()
    cmd_session = None
    if registry:
        command_ids = list(registry.keys())
        cmd_session = run_test_commands(
            workspace.root(), registry, command_ids,
            should_cancel=should_cancel,
            require_sandbox=store.get_require_sandbox())

    runtime_result: Optional[TestRunResult] = None
    if probe_runtime and _runnable_managed_local_profile(store):
        runtime_result = _runtime_gate_probe(
            store, workspace, head=head, should_cancel=should_cancel)

    if runtime_result is None:
        # No runtime arm -> today's exact behavior (delivery + command-only gate).
        if cmd_session is None:
            return None
        store.record_test_run(cmd_session, task_id=task_id, head=head)
        return cmd_session

    # Item 5: fold the runtime probe into ONE session alongside the command
    # results, so the newest gate record carries both verdicts — a single
    # ``latest_gate_run`` for Spec 04's stall detector and the prompt segments.
    command_ids = list(cmd_session.command_ids) if cmd_session else []
    results = list(cmd_session.results) if cmd_session else []
    unknown_ids = list(cmd_session.unknown_ids) if cmd_session else []
    passed = bool(cmd_session.passed) if cmd_session else True
    sandbox = str(getattr(cmd_session, "sandbox", "") or "") if cmd_session else ""
    command_ids.append(runtime_result.command_id)
    results.append(runtime_result)
    passed = passed and bool(runtime_result.passed)
    session = TestRunSession(
        command_ids=command_ids, results=results, unknown_ids=unknown_ids,
        passed=passed, sandbox=sandbox)
    store.record_test_run(session, task_id=task_id, head=head)
    return session


def _web_probe_pr_arm(store: LedgerStore, workspace: Any, *, task_id: str,
                      branch: str, head: str,
                      should_cancel: Optional[Callable[[], bool]] = None) -> None:
    """SPEC-30 (S4): probe a PR's OWN tree, pre-merge, bound to the PR's head.

    The post-merge ``_web_probe_arm`` serves master and records at the master head
    — which no open PR ever carries, so its verdict never reached the reviewer.
    This arm serves the PR BRANCH's worktree and records at ``head`` = the PR head,
    so ``web_probe._attach_verdict_to_prs`` stamps THIS PR (the reviewer then reads
    ``probe_passed`` / ``probe_reason`` off the record and, post-S4b, in its
    prompt). Runs at PR-open, once per PR head. Fully fail-open: a non-web project,
    an unresolvable worktree, or a headless-browser inability records nothing and
    never fails the dev turn."""
    try:
        from .autonomy import load_policy
        if not getattr(load_policy(store), "web_probe", True):
            return
    except Exception:  # noqa: BLE001
        return
    try:
        serve_root = workspace.task_root(task_id, branch=branch)
    except Exception:  # noqa: BLE001 — no worktree -> no evidence (fail-open)
        return
    try:
        from . import web_probe
        # pr_scoped: recorded under PR_PROBE_TASK_ID so the gate detectors exclude
        # it (branch evidence, not the integrated gate), and NO master-anchor
        # reconcile — anchors track the master timeline, not per-branch heads.
        web_probe.run_and_record(
            store, workspace, head=head, serve_root=serve_root,
            should_cancel=should_cancel, pr_scoped=True)
    except Exception:  # noqa: BLE001 — the probe never fails the dev turn
        return


def _web_probe_arm(store: LedgerStore, workspace: Any, *, head: str,
                   should_cancel: Optional[Callable[[], bool]] = None) -> None:
    """GL01 (Item 1 + Item 2): the sibling arm to ``_run_gate`` — the default web
    probe (the black-canvas oracle) plus the anchor reconcile.

    Registry-INDEPENDENCE is the load-bearing property. ``_run_gate`` returns
    ``None`` on an empty registry, so a buildless web project that authored no test
    gets no did-it-render signal from it. This arm runs REGARDLESS of the registry:
    it stands the served web runtime up, drives the headless liveness probe, and
    records a ``web:probe`` runtime-test bound to ``head`` (Item 1). It then
    reconciles test anchors off that recorded run — promoting a green probe/command
    to an anchor and recording an ``anchor_regressed`` decision + one deduped alert
    when a previously-green anchor flips red at a new head (Item 2).

    ON by default (``policy.web_probe``); fully fail-open: a non-web project, a
    headless-browser inability, or any failure records NO evidence and never fails
    the turn (mirrors ``_runtime_gate_probe``'s cannot-verify posture)."""
    try:
        from .autonomy import load_policy
        policy = load_policy(store)
    except Exception:  # noqa: BLE001
        return
    if not getattr(policy, "web_probe", True):
        return None
    try:
        from . import anchors, web_probe
        run = web_probe.run_and_record(
            store, workspace, head=head,
            frames=int(getattr(policy, "web_probe_frames", 30)),
            should_cancel=should_cancel)
    except Exception:  # noqa: BLE001 — the probe never fails the turn
        return None
    if not run:
        return None  # non-web project, or fail-open (no evidence recorded)
    try:
        anchors.reconcile(store, run, project_id=store.project_id)
    except Exception:  # noqa: BLE001 — the anchor lock is best-effort
        pass
    # SPEC-30 (S2): hand the recorded verdict back so the completion gate can
    # require it. A probe that RAN and came back red (black canvas, a console
    # crash, or an inert canvas that ignored input) must block `done`; a probe
    # that could not run returned None above (fail-open, never blocks).
    return run


# Spec 12 (S1): a merge that touches ONLY these is not gate-relevant — running the
# acceptance gate again would waste the suite's wall time with no chance of a
# changed verdict. Everything else (source, web assets, test files, configs) is.
_GATE_IRRELEVANT_EXT = (".md", ".markdown", ".rst", ".txt")
_GATE_IRRELEVANT_NAMES = ("LICENSE", "LICENSE.md", "NOTICE", "AUTHORS", "CHANGELOG",
                          "CHANGELOG.md", ".gitignore", ".gitattributes")


def _merge_is_gate_relevant(changed: list[str]) -> bool:
    """True unless every changed path is pure documentation/metadata. Conservative:
    an unrecognized path counts as relevant (better a redundant gate run than a
    missed regression)."""
    real = [p for p in changed if p and p != ".gitignore"]
    if not real:
        return False
    for p in real:
        base = p.rsplit("/", 1)[-1]
        if base in _GATE_IRRELEVANT_NAMES:
            continue
        if base.startswith("docs/") or p.startswith("docs/"):
            continue
        if any(p.endswith(ext) for ext in _GATE_IRRELEVANT_EXT):
            continue
        return True  # a non-doc path -> re-run the gate
    return False


def _arm_gate_after_merge(store: LedgerStore, workspace: Any, *,
                          changed: list[str], head: str,
                          closure: Optional["RoleClosure"] = None) -> None:
    """Spec 12 (S1): after a merge advances master, (1) acquire a gate if the
    project has none, then (2) count a gate-relevant merge and arm ``gate_due``
    once ``gate_min_merge_interval`` such merges have accumulated — so a later
    mechanical GateRun executes the suite off this (merge) turn. Fully guarded:
    a failure here never fails the merge that already landed.

    SPEC-26 (Item 3): this is also the one quiescent moment a ``deferred``
    capability can have arrived, so the closure verdicts are re-evaluated here —
    after the bootstrap re-attempt, and unconditionally, including on the paths that
    return without arming anything."""
    try:
        from .autonomy import load_policy
        policy = load_policy(store)
    except Exception:  # noqa: BLE001
        policy = None
    if policy is not None and getattr(policy, "gate_bootstrap", True):
        try:
            from . import gate_bootstrap
            gate_bootstrap.maybe_bootstrap(store, workspace, policy)
        except Exception:  # noqa: BLE001 — bootstrap is best-effort
            pass
    _reevaluate_role_closure(closure)
    if policy is None or not getattr(policy, "gate_bootstrap", True):
        return
    # Only arm when the GateRun will actually EXECUTE something. Spec 12 Item 5
    # landed the runtime-probe arm, so `_run_gate(probe_runtime=True)` now executes
    # on registered COMMANDS or a runnable managed_local runtime profile — arm on
    # either. NOT the broader `gate_available`/`_has_runnable_runtime` (which count
    # a container profile the launch probe skips): a container-only project would
    # otherwise churn a no-op GateRun every interval.
    try:
        if not (store.get_test_commands() or _runnable_managed_local_profile(store)):
            return
    except Exception:  # noqa: BLE001
        return
    if not _merge_is_gate_relevant(changed):
        return
    try:
        rs = store.get_run_state()
        pending = int(rs.get("gate_pending_merges", 0) or 0) + 1
        interval = max(1, int(getattr(policy, "gate_min_merge_interval", 3)))
        if pending >= interval:
            store.set_run_state(gate_due=True, gate_dirty_head=str(head),
                                gate_pending_merges=0)
        else:
            store.set_run_state(gate_pending_merges=pending)
    except Exception:  # noqa: BLE001
        pass


def _foundation_files_in(paths: list[str]) -> list[str]:
    """Spec 13 (S2): the subset of ``paths`` that are foundation elements — a build
    manifest, or a source entrypoint. These are what a foundation-UNLOCKING PR
    adds while the clamp is pending; a rejection whose findings don't touch any of
    them is unrelated to the foundation."""
    out: list[str] = []
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base in _BUILD_MANIFESTS or p.endswith(_SOURCE_EXT):
            out.append(p)
    return out


def _pr_unlocks_foundation(store: LedgerStore, changed: list[str]) -> bool:
    """Spec 13 (S2): whether an open PR touching ``changed`` adds a foundation
    element while ``foundation_status`` is still pending. Best-effort — a read
    failure yields False (no escalation), never raises into the dispatch path."""
    if not changed:
        return False
    try:
        if str(store.get_run_state().get("foundation_status", "")) != "pending":
            return False
    except Exception:  # noqa: BLE001
        return False
    return bool(_foundation_files_in(changed))


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
        # F159's off switch: no escalation, no centralize owner, no freeze —
        # pre-F159 conflict handling exactly. Checked BEFORE `force`, which is an
        # F159 escalation too (a resolve-retry exhaustion).
        #
        # An OFF SWITCH MUST FAIL CLOSED. This resolution deliberately sits OUTSIDE
        # the threshold's try/except: when that except also covered the flag, an
        # unreadable `autonomy.json` fell through to `esc = 4` and escalated — i.e.
        # a corrupt policy file re-enabled the exact mechanism the operator had
        # turned off. A policy we cannot read is not permission to act.
        try:
            from .autonomy import load_policy
            _policy = load_policy(store)
            serialization_on = bool(_policy.hot_file_serialization)
            esc = max(1, int(_policy.hot_file_escalation_threshold))
        except Exception:  # noqa: BLE001
            serialization_on = False
            esc = 4
        if not serialization_on:
            return
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
    dev_repo_read: bool = False,
    # SPEC-42: policy gate for the model-derived per-turn output budget. Reaches the
    # turn by the same route as `dev_repo_read` — the seam
    # `gateway_member_caller(gateway) -> (member, prompt) -> str` takes no policy, so
    # the flag rides the per-turn member copy set in the shadowing `caller` below.
    # Default True so the six in-loop call sites get the fix; the paths that build
    # their own caller (wizard, pm-ask, the directive interpreter, the scripts) also
    # get it, since the seam defaults the flag on when the member does not carry it.
    reasoning_output_budget: bool = True,
    # SPEC-41 Moves 2+3. `local_structured_format` is gated on `local_think_false`
    # inside the gateway, so the #84-harmful pairing (format on, thinking live) is
    # unreachable through these knobs rather than merely discouraged.
    local_think_false: bool = True,
    local_structured_format: bool = True,
    reviewer_repo_read: bool = False,
    review_min_latency_ms: int = 0,
    # SPEC-44 Move 2: how many capability ranks the INITIAL assignment may drop when
    # nothing in the pool sits at the requested tier. `model_assignment` has no policy
    # in scope, so the value reaches it the way `dev_repo_read` does. Default 0 —
    # the legacy value — for this factory, so the ~50 direct test callers keep today's
    # hard-block behaviour; `CodingRunner.run` passes the policy field (default 1).
    difficulty_downgrade_limit: int = 0,
    role_closure_state: Optional["RoleClosure"] = None,
) -> Callable[[Any, Any], TurnOutcome]:
    """Construct the ``run_turn`` the autonomy loop drives.

    Spec 11 (P1a): ``dev_repo_read`` (the ``CodingAutonomyPolicy`` field) enables
    read-only in-turn worktree retrieval for DEV turns. Default False so the ~50
    direct test callers of this factory keep the legacy single-shot behavior; the
    production ``CodingRunner.run`` passes ``policy.dev_repo_read`` (default OFF;
    the dataclass field in ``autonomy.py`` is the single source of truth — see the
    Spec 12-18 prep P0.3 note there).

    SPEC-26 (S4): ``role_closure_state`` carries the live seated roster so the two
    sites that can WEDGE a run on an unseated TESTER — the ``test PR:`` task spawn
    and ``_set_mergeable_if_ready``'s tests-green gate — read the same predicate the
    capability audit does. ``None`` (every direct test caller) means "no role was
    unseated", which is today's behaviour exactly.
    """
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
        # SPEC-42: carry the policy gate on the per-turn member copy. Suppression
        # only — never stamp a budget literal into `turn_limits`: two live teams
        # persist an operator-set 8192/6144, and writing the legacy 2048 over those
        # would be a 4x demotion of a deliberate choice.
        if not reasoning_output_budget:
            member = {**member, "reasoning_output_budget": False}
        # SPEC-41 Moves 2+3: every model call that reaches this shadowing caller is a
        # STRUCTURED turn by construction — all six in-loop call sites feed
        # `parse_coding_turn` / `parse_governance_turn` immediately. So the flag is
        # set once here rather than threaded through ~50 `run_turn` action branches,
        # and the gateway is told the turn shape rather than inferring it from a role.
        member = {**member,
                  "structured_output": True,
                  "local_think_false": local_think_false,
                  "local_structured_format": local_structured_format}
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

    def _tester_seated() -> bool:
        """SPEC-26 (S4) — is a TESTER actually on the board right now?

        THE coupling that makes unseating safe. Without it, unseating a TESTER on a
        project that HAS a unit command would wedge every approved PR: the spawn
        below creates a ``test PR:`` task without checking that anyone can take it,
        and ``_set_mergeable_if_ready`` then holds the PR until ``tests_passed is
        True``. Trading an unread advisory for a wedge is not a fix, so both sites
        and the audit read one predicate. Re-checked on every call, not captured:
        a deferred TESTER can be re-seated mid-run (Item 3)."""
        return role_closure_state is None or role_closure_state.seated(TESTER)

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
                prompt, parsed, retry=retries, max_retries=max_retries,
                role=role, task_id=task_id)
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
        # Thin wrapper: the gate itself is module-level (`_apply_merge_gate`) so the
        # stale-base / conflict revalidators, which are plain module functions with
        # no turn closure in scope, can apply the SAME gate instead of open-coding a
        # second writer of `status="mergeable"`.
        _apply_merge_gate(store, pr_id, tester_seated=_tester_seated())

    def _execute(action: Any, ledger: Any) -> TurnOutcome:
        if isinstance(action, DesignPlan):
            # Slice 1 §2/§4 — the Designer authors the design_spec. A valid body is
            # accepted (state approved) and spawns the one materialize DEV task. A
            # PARSE error OR a semantically-invalid body (missing axis/section) is
            # re-prompted in-turn with the named problem (bounded by
            # `_INTENT_CORRECTIVE_RETRIES`); only if it is STILL invalid after the
            # retry is it appended as `changes_requested` (§8). Doing the recovery
            # in-turn — not by re-scheduling — keeps the authoring turn one-shot and
            # avoids a scheduler loop, while closing the "invalid body wedges UI
            # dispatch forever" hole (the Designer almost always fixes a named field
            # on the second try). NOTE: TurnParseError / parse_coding_turn are used
            # module-globals here (imported at module scope) — do NOT re-import them
            # locally, or Python makes them locals across the whole _execute body and
            # every other dispatch arm UnboundLocalErrors.
            from .design_materialize import spawn_materialize_task_if_needed
            from .design_spec import validate_design_body
            from .governance import GovernanceStore

            member = _member(DESIGNER, action.member_id)
            governance = GovernanceStore.for_ledger(store)
            record_turn_skill(
                store, member_id=member.get("id", "m-designer"),
                task_id="design", role=DESIGNER)
            prompt = _designer_prompt(store)
            parsed = parse_coding_turn(DESIGNER, None, caller(member, prompt))
            body_json: dict[str, Any] = {}
            ok, errors = False, ["no valid design_spec turn produced"]
            attempts = 0
            while True:
                if isinstance(parsed, TurnParseError):
                    problem_code, problem_detail = parsed.code.value, parsed.detail
                else:
                    body_json = getattr(parsed.intent, "body_json", {}) or {}
                    ok, errors = validate_design_body(body_json)
                    if ok:
                        break
                    problem_code = "design_spec_invalid_body"
                    problem_detail = "; ".join(errors)
                store.record_decision(
                    title="designer turn rejected", context="design",
                    choice="designer_turn_rejected",
                    rationale=f"{problem_code}: {problem_detail}")
                if attempts >= _INTENT_CORRECTIVE_RETRIES:
                    break
                attempts += 1
                prompt = _governance_corrective_prompt(
                    prompt, problem_code, problem_detail,
                    retry=attempts, max_retries=_INTENT_CORRECTIVE_RETRIES)
                parsed = parse_coding_turn(DESIGNER, None, caller(member, prompt))
            if isinstance(parsed, TurnParseError):
                # Never parsed a turn at all — a clear blocker, not a silent stall.
                return TurnOutcome(kind="governance_progress", made_progress=False,
                                   hard_blocker=True, reason="designer_turn_unparseable")
            state = "approved" if ok else "changes_requested"
            governance.append_artifact(
                kind="design_spec",
                title=getattr(parsed.intent, "title", "") or "Design contract",
                body_markdown=getattr(parsed.intent, "body_markdown", ""),
                body_json=body_json, state=state,
                author={"role": DESIGNER, "member_id": str(member.get("id", ""))})
            if ok:
                created = spawn_materialize_task_if_needed(store, governance)
                store.record_decision(
                    title="design contract approved", context="design",
                    choice="design_spec_approved",
                    rationale=("design_spec approved; "
                               + ("materialize task spawned" if created
                                  else "materialize task already present")))
                return TurnOutcome(kind="governance_progress", made_progress=True)
            store.record_decision(
                title="design contract needs changes", context="design",
                choice="design_spec_changes_requested",
                rationale="; ".join(errors))
            return TurnOutcome(kind="governance_progress", made_progress=False)

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
            elif isinstance(parsed.intent, BlockedIntent):
                # Spec 25: a PM that cannot re-scope this task says so. It rides the
                # EXISTING bounded pm-assist rung (recorded, counted against
                # `pm_assist_limit`) rather than blocking the task from under the
                # ladder that is already handling it — the task is mid-recovery, and
                # two mechanisms mutating its state in the same turn is how a
                # stranded `doing` task happens.
                invalid_reason = f"pm assist blocked — {_blocked_reason_text(parsed.intent)}"
                _record_capability_ask(store, parsed.intent, role=PM, task=task,
                                       context=f"task {task.task_id}")
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

        if isinstance(action, LastWord):
            # SPEC-23 (Item 2) — the last word. The loop injected this turn at the
            # exact moment a HEURISTIC stop would have been returned; the PM is
            # asked to propose a concrete next action or confirm the halt.
            #
            # The runner CLASSIFIES the answer because only it can see what
            # survived materialization: "a task the engine can act on" (not "a
            # response arrived") is the reset condition, or the intervention is a
            # licence to loop. The verdict rides back on `TurnOutcome.last_word`;
            # `autonomy._intervene` applies the reset map and records the outcome.
            member = _member(PM, action.member_id)
            record_turn_skill(store, member_id=member.get("id", "m-pm"),
                              task_id="last-word", role=PM)
            parsed = _parse_member_turn(
                PM, None, member, _last_word_prompt(store, action),
                context=f"last_word:{action.detector}")
            if isinstance(parsed, TurnParseError):
                # UNHEARD, never agreement. This is the Spec 21 lesson in its
                # purest form — repeated schema rejection was read as PM idleness
                # and terminated a healthy run. The run still stops (the original
                # reason, unchanged), but the record must say the PM was not heard
                # rather than that it agreed.
                store.record_decision(
                    title="pm last word rejected",
                    context=f"last_word:{action.detector}",
                    choice="pm_turn_rejected",
                    rationale=f"{parsed.code.value}: {parsed.detail}")
                return TurnOutcome(
                    kind="noop", made_progress=False,
                    reason=f"last_word_unparsed: {parsed.code.value}",
                    last_word={
                        "outcome": "unparsed",
                        "rationale": f"{parsed.code.value}: {parsed.detail}"})
            intent = parsed.intent
            if isinstance(intent, BlockedIntent):
                # Spec 25's typed "I am blocked". An honest answer, and an honest
                # ABSTENTION: the PM has no next action to propose, so the halt
                # stands — with its reason on the record instead of nothing.
                reason = _blocked_reason_text(intent)
                store.record_decision(
                    title="pm last word blocked",
                    context=f"last_word:{action.detector}", choice="blocked",
                    rationale=reason)
                _record_capability_ask(store, intent, role=PM, task=None,
                                       context=f"last_word:{action.detector}")
                return TurnOutcome(
                    kind="noop", made_progress=False,
                    last_word={"outcome": "confirmed",
                               "rationale": f"the PM answered blocked — {reason}"})
            for dec in intent.decisions:
                store.record_decision(
                    title=dec.title, context="pm_decision",
                    choice="pm_decision", rationale=dec.rationale)
            said = "; ".join(
                f"{d.title}: {d.rationale}".strip(": ") for d in intent.decisions)
            if intent.done:
                # No special authority to declare victory: the F128 completion gate
                # judges this claim exactly as it judges any other.
                open_items = pending_completion_work(store)
                if open_items:
                    store.record_decision(
                        title="last word completion refused: open work remains",
                        context=f"last_word:{action.detector}",
                        choice="pm_completion_refused",
                        rationale=summarize_open_items(open_items))
                    return TurnOutcome(
                        kind="noop", made_progress=False,
                        last_word={
                            "outcome": "confirmed",
                            "rationale": ("the PM claimed done, but open work "
                                          "remains: "
                                          f"{summarize_open_items(open_items)}")})
                gate_block = _acceptance_gate_blocks_done(store, workspace)
                if gate_block:
                    store.record_decision(
                        title="last word completion refused: acceptance gate",
                        context=f"last_word:{action.detector}",
                        choice="pm_completion_refused", rationale=gate_block)
                    return TurnOutcome(
                        kind="noop", made_progress=False,
                        last_word={"outcome": "confirmed", "rationale": gate_block})
                _ack_unrun_acceptance_test(store)
                _record_completion_oracles(store, workspace)
                store.set_completion(intent.completion_summary)
                return TurnOutcome(
                    kind="project_done",
                    last_word={"outcome": "done",
                               "rationale": intent.completion_summary})
            created = _materialize_pm_tasks(store, intent)
            dropped = _apply_pm_cancels(store, intent)
            if created or dropped:
                return TurnOutcome(
                    kind="planned", made_progress=True,
                    last_word={
                        "outcome": "accepted",
                        "rationale": said or ("proposed new work" if created
                                              else "pruned obsolete tasks"),
                        "task_ids": [t.task_id for t in created]})
            # Decisions only, or every proposed task rejected as a duplicate /
            # unexecutable. Nothing materialized, so there is nothing for the loop
            # to act on — an abstention, and the halt stands. Deliberately STRICTER
            # than Spec 21's "a decisions-only turn is legal" rule: legal for a
            # routine turn, not sufficient to reset a detector window.
            return TurnOutcome(
                kind="planned", made_progress=False,
                last_word={"outcome": "confirmed",
                           "rationale": said or "the PM proposed nothing new"})

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
                # Spec 25 (Item 3a): a turn rejected for SHAPE is not a turn that
                # made no PROGRESS. Trying to comply used to accelerate
                # termination — four rejected PM turns walked `pm_idle` straight
                # into `no_progress` with two PRs open and nothing recorded about
                # why. The counters are separated here and bounded separately in
                # `_apply_outcome` (`schema_reject_limit`).
                return TurnOutcome(kind="planned", made_progress=False,
                                   schema_rejected=True)
            # F087-07-E: the interjections were delivered to (and accepted by) the
            # PM this turn — mark them consumed (read-once) only now.
            store.mark_interjections_consumed()
            intent = parsed.intent
            # Spec 25 (Item 1): the PM's own blocked turn. Unlike a worker's, this
            # DOES count toward `pm_idle`: a PM saying "there is nothing I can add"
            # IS the idle state, and this spec does not make runs immortal — it
            # makes the idle state LEGIBLE. Where the run used to stop after four
            # rejected turns with no recorded reason, it now stops after
            # `pm_idle_limit` honest ones with the PM's reason on the ledger.
            if isinstance(intent, BlockedIntent):
                store.record_decision(
                    title="pm blocked", context="plan", choice="blocked",
                    rationale=_blocked_reason_text(intent))
                _record_capability_ask(store, intent, role=PM, task=None,
                                       context="plan")
                return TurnOutcome(kind="planned", made_progress=False)
            # F088-04: PM decisions are durable project truth — persist them so
            # the grounding layer can promote them (previously dropped on the
            # floor). The ledger remains the source; grounding derives from it.
            #
            # Spec 25 (Item 3b): snapshot the ALREADY-recorded decision titles
            # BEFORE writing this turn's, so the progress judgement below can tell
            # a new decision from one the PM is re-emitting. Taken here (not in the
            # scorer) because after this loop the ledger no longer knows which
            # decisions arrived on this turn. Best-effort: an unreadable ledger
            # degrades to the pre-Spec-25 rule (every decision counts) rather than
            # failing a legal turn.
            try:
                prior_decision_titles = {
                    str(d.get("title") or "") for d in store.list_decisions()
                    if d.get("choice") == "pm_decision"}
            except Exception:  # noqa: BLE001 — scoring input, never control
                prior_decision_titles = None
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
                # SPEC-35: `done` is also refused while the project's own acceptance
                # gate is not green at the current master head (red -> fix it; stale
                # -> arm the in-loop gate). Recoverable by construction: the in-loop
                # gate re-runs on merges and lifts the block automatically.
                gate_block = _acceptance_gate_blocks_done(store, workspace)
                if gate_block:
                    store.record_decision(
                        title="completion refused: acceptance gate not green",
                        context="plan", choice="pm_completion_refused",
                        rationale=gate_block)
                    return TurnOutcome(
                        kind="completion_refused", made_progress=False,
                        reason="acceptance_gate_not_green")
                # F093: persist the PM's completion justification so the UI can
                # show "✓ Complete — here's why". (intent.completion_summary is
                # validated non-empty when done=true, schemas.py PMPlanIntent.)
                _ack_unrun_acceptance_test(store)
                _record_completion_oracles(store, workspace)
                store.set_completion(intent.completion_summary)
                return TurnOutcome(kind="project_done")
            created = _materialize_pm_tasks(store, intent)
            dropped = _apply_pm_cancels(store, intent)
            return TurnOutcome(
                kind="planned",
                made_progress=_pm_turn_made_progress(
                    intent, created, prior_decision_titles, dropped=dropped))

        if isinstance(action, GateRun):
            # Spec 12 (S1): run the acceptance gate on the integrated master tree,
            # off the merge turn. Mechanical (0 model calls). Clears the armed flag
            # whether the run succeeds, finds nothing to run, or raises — so a
            # failure can never re-arm into a tight loop.
            try:
                rs = store.get_run_state()
                head = str(rs.get("gate_dirty_head", "") or "")
            except Exception:  # noqa: BLE001
                head = ""
            try:
                if workspace is not None:
                    if not head:
                        head = workspace.head() or ""
                    session = _run_gate(store, workspace, head=head,
                                        task_id="in-loop-gate",
                                        should_cancel=should_cancel,
                                        probe_runtime=True)
                    passed = None if session is None else bool(session.passed)
                    store.record_decision(
                        title="in-loop acceptance gate",
                        context="in_loop_gate",
                        choice=("gate_passed" if passed else
                                "gate_no_commands" if passed is None else "gate_failed"),
                        rationale=(f"ran the acceptance gate on master head {head[:12]}"
                                   if session is not None
                                   else "no registered commands to run"))
                    # GL01 (Item 2): reconcile anchors off the command gate too, so
                    # a registered command that went green becomes an anchor and a
                    # later red flips it — the gate's results already carry per-
                    # command passed. The gate session isn't recorded with a head
                    # dict, so synthesize the run shape the reconcile reads.
                    if session is not None:
                        try:
                            from . import anchors as _anchors
                            _anchors.reconcile(store, {
                                "head": head,
                                "results": [r.to_dict() for r in session.results],
                            }, project_id=store.project_id)
                        except Exception:  # noqa: BLE001
                            pass
                    # GL01 (Item 1): the registry-INDEPENDENT web probe — the
                    # black-canvas oracle — runs REGARDLESS of what `_run_gate` did
                    # (it returns None on an empty registry; the probe must still
                    # run). Sibling arm, off the same merge-armed GateRun turn.
                    _web_probe_arm(store, workspace, head=head,
                                   should_cancel=should_cancel)
            except Exception as exc:  # noqa: BLE001 — a gate error never breaks the loop
                try:
                    store.record_decision(
                        title="in-loop gate could not run", context="in_loop_gate",
                        choice="gate_error", rationale=str(exc))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    store.set_run_state(gate_due=False)
                except Exception:  # noqa: BLE001
                    pass
            return TurnOutcome(kind="gate_run", model_calls=0)

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
                # Spec 12 (S1): master advanced — acquire a gate if the project has
                # none yet (detect runtime profiles + register a smoke-proven
                # acceptance command), then ARM an in-loop gate run when this merge
                # is gate-relevant and the min-merge interval has elapsed. The gate
                # itself runs off THIS turn (a later GateRun), so the suite never
                # serializes the merge critical section.
                _arm_gate_after_merge(store, workspace, changed=list(_changed),
                                      head=res.get("head", pr["head"]),
                                      closure=role_closure_state)
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

            assignment, override_reason = resolve_task_assignment(
                task, member,
                difficulty_downgrade_limit=difficulty_downgrade_limit,
            )
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
                    # SPEC-44: the persisted, non-best-effort surface for a
                    # downgrade. Inside the `!= prior_assignment` branch, so it is
                    # written exactly when the assignment is newly minted — never
                    # per turn.
                    model_difficulty_downgraded_from=(
                        assignment.difficulty_downgraded_from or None),
                )
                if assignment.difficulty_downgraded_from:
                    store.record_decision(
                        title=f"difficulty downgraded: {task.title}",
                        context=f"task {task.task_id}",
                        choice="difficulty_downgraded",
                        rationale=(
                            "No route in the pool satisfies "
                            f"{assignment.difficulty_downgraded_from}; running at "
                            f"{assignment.difficulty_tier} on {assignment.route_id}."
                        ),
                        related_task_ids=[task.task_id],
                        extra={
                            "requested_difficulty_tier":
                                assignment.difficulty_downgraded_from,
                            "satisfied_difficulty_tier": assignment.difficulty_tier,
                            "route_id": assignment.route_id,
                            "assignment_id": assignment.assignment_id,
                            "pool": pool_snapshot,
                        },
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
                # Spec 11 (P1a): when the dev_repo_read policy is on, tag a
                # per-turn COPY of the member with the task worktree root so the
                # gateway can run a claude_cli DEV turn read-only in-turn (cwd =
                # worktree, Read/Grep/Glob only) — the dev can grep the rest of
                # the repo and see both sides of a cross-file contract. A shallow
                # copy so the shared member config is never mutated. Best-effort:
                # any failure to resolve the root falls back to the unchanged
                # member (single-shot empty-temp-dir path).
                # Spec 17 (Item 1): gate on the member's ACTUAL vendor, not the
                # raw policy flag — only a repo_read-honoring vendor (claude_cli
                # today) receives the read-only cwd turn, so only it gets the
                # `repo_read_root` tag. A codex/cursor DEV keeps the plain member
                # (no key), so its catalog and metadata never promise tools it
                # won't get.
                dev_member = member
                if (_member_honors_repo_read(member, dev_repo_read)
                        and workspace is not None and branch is not None):
                    try:
                        repo_root = workspace.task_root(task.task_id, branch=branch)
                        # Spec 14: canonical `repo_read_root` (was dev_repo_read_root).
                        dev_member = {**member, "repo_read_root": str(repo_root)}
                    except Exception:  # noqa: BLE001 — retrieval is best-effort
                        dev_member = member
                # Spec 17 (Item 1 / edge case): resolve repo_read from THIS member's
                # actual invocation, not the policy, so a mixed-vendor team's prompt
                # never lies. Read BOTH the canonical `repo_read_root` and the legacy
                # `dev_repo_read_root` key (Spec 14 renamed it on a parallel branch;
                # the two must land in either order). NOTE: the prompt is composed
                # here, BEFORE any mid-turn retrieval fallback (async_claude_cli), so
                # a fallback turn may not have the tools its prompt named — harmless
                # (the fallback still emits a normal envelope), just not chased.
                dev_repo_read_active = bool(
                    dev_member.get("repo_read_root")
                    or dev_member.get("dev_repo_read_root"))
                parsed = _parse_member_turn(
                    DEV, task.task_id, dev_member,
                    _dev_prompt(task, store, readback,
                                repo_read=dev_repo_read_active),
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
                # Spec 25 (Item 1/S2): the dev says it cannot proceed. Every OTHER
                # dev dead end below returns `unproductive=True` and feeds the F127
                # escalate-up ladder — so honesty and failure were the same signal,
                # and a dev with nothing legal left to emit was punished for saying
                # so. This routes to the `task_blocked` transition that has existed
                # in `_apply_outcome`/`topology.block_task` all along and that NO
                # turn shape could reach: the task goes `blocked` with the dev's own
                # words on the ledger, `pm_idle`/`plan_streak` reset, and the PM
                # picks it up. `unproductive` is deliberately NOT set — that is the
                # entire behavioural change. Bounded by `blocked_turn_limit`
                # (autonomy.py) so the escape shape cannot become a way to idle.
                if isinstance(intent, BlockedIntent):
                    _record_capability_ask(store, intent, role=DEV, task=task,
                                           context=f"task {task.task_id}")
                    # The turn wrote nothing, so the branch opened above holds no
                    # commits — drop it exactly as the no-net-change path does,
                    # rather than accumulating an empty branch per blocked turn.
                    if workspace is not None and branch is not None:
                        try:
                            workspace.delete_branch(branch)
                        except Exception:  # noqa: BLE001 — cleanup is best-effort
                            pass
                    return TurnOutcome(
                        kind="task_blocked", task=task,
                        member_id=str(member.get("id", "")), member_role=DEV,
                        member_route=str(member.get("gateway_route_id", "")),
                        reason=_blocked_reason_text(intent))
                # F088-09: a read-only context request — answer from grounding,
                # record it, and re-queue the task so the dev acts on the answer.
                # No file writes, no durable mutation. Spec 20 bounds it below.
                from .schemas import DeveloperContextRequestIntent
                if isinstance(intent, DeveloperContextRequestIntent):
                    # Spec 20: bound the channel. This was the ONLY dev dead-end
                    # that requeued to `todo` without `unproductive=True`, so no
                    # counter moved, no rung of the F127 ladder fired, and the
                    # task was re-dispatched forever (live: a dev asking the same
                    # question every turn). `schemas.py` also relabels ANY unknown
                    # dev intent kind carrying a non-empty `question` into
                    # `context_request`, so a schema-confused dev is funnelled
                    # straight into this branch — the guard therefore has to live
                    # here, on the runner, not in the schema.
                    ctx_extras = getattr(task, "_extras", {}) or {}
                    ctx_key = _context_question_key(intent.question)
                    ctx_prior = str(
                        ctx_extras.get("last_context_question_key") or "")
                    ctx_attempts = _context_attempts_of(ctx_extras) + 1
                    # A VERBATIM repeat proves the previous answer did not help;
                    # re-answering retrieves the same hits and is guaranteed to
                    # loop, so it spends the whole budget at once instead of
                    # burning one slot per wasted model call.
                    ctx_repeat = bool(ctx_prior) and ctx_key == ctx_prior
                    if ctx_repeat or ctx_attempts > _CONTEXT_REQUEST_LIMIT:
                        store.record_decision(
                            title=f"context request exhausted: {task.title}",
                            context=f"task {task.task_id}",
                            choice="context_request_exhausted",
                            rationale=(
                                f"{str(intent.question or '')[:_CONTEXT_QUESTION_CAP]} "
                                f"(ask {ctx_attempts} of {_CONTEXT_REQUEST_LIMIT}"
                                + ("; verbatim repeat of the previous question"
                                   if ctx_repeat else "") + ")"),
                            related_task_ids=[task.task_id])
                        # Persist EXACTLY the limit, never `ctx_attempts` — an
                        # exhausted task keeps getting dispatched while the F127
                        # ladder walks its rungs, and a counter that kept growing
                        # would both drift the rendered budget past the cap and
                        # give a rescued task a bogus starting point. The limit is
                        # the saturated "spent" value: the gate is `> LIMIT`, so
                        # the very next ask still exhausts. A verbatim repeat on
                        # ask 1 saturates here too — that is the short-circuit.
                        store.update_task(
                            task.task_id, state="todo",
                            context_request_attempts=_CONTEXT_REQUEST_LIMIT,
                            last_context_question_key=ctx_key)
                        # Hand off to the EXISTING F127 escalate-up ladder (model
                        # escalation -> member exclusion -> PM assist -> blocking
                        # Problem) rather than inventing a second mechanism.
                        return TurnOutcome(
                            kind="noop", unproductive=True,
                            member_id=str(member.get("id", "")), member_role=DEV,
                            member_route=str(member.get("gateway_route_id", "")),
                            reason="context_request_exhausted")
                    # Under budget: answer from grounding, record it, and re-queue
                    # the task so the dev acts on the answer. No file writes, no
                    # durable mutation — and NOT unproductive, so a legitimate
                    # single question never starts the dev up the ladder.
                    _answer_dev_context_request(store, task, intent)
                    store.update_task(task.task_id, state="todo",
                                      context_request_attempts=ctx_attempts,
                                      last_context_question_key=ctx_key)
                    return TurnOutcome(kind="noop")
                data = {"task_type": intent.task_type,
                        "tool_calls": [{"tool": tc.tool, "args": tc.args}
                                       for tc in intent.tool_calls]}
                writes = controller.execute_dev_turn(task=task, member=member, data=data)
                if writes.failures:
                    tool_not_allowed_reason = ""
                    for path, reason in writes.failures:
                        # Spec 17 (Item 3a): the reason is now the enriched
                        # `tool_not_allowed: <tool> — ...` string, so match on the
                        # PREFIX, not equality, to keep the `tool_failed` decision
                        # classification (and its test) stable.
                        is_disallowed = reason.startswith(
                            TurnErrorCode.tool_not_allowed.value)
                        choice = "tool_failed" if is_disallowed else "write_failed"
                        title = "tool failed" if choice == "tool_failed" else "write failed"
                        store.record_decision(
                            title=f"{title}: {task.title}",
                            context=f"task {task.task_id}", choice=choice,
                            rationale=f"{path}: {reason}",
                            related_task_ids=[task.task_id])
                        if is_disallowed and not tool_not_allowed_reason:
                            tool_not_allowed_reason = reason
                        # GL03 (Item 1): a disallowed-tool failure is not only a
                        # logged event — an ungranted-tool-call attempt whose intent
                        # maps to a capability the role LACKS is a capability-gap
                        # signal (the DEV inventing a "run tests" tool is telling you
                        # what interface the task needs). Detect it, threshold+dedupe
                        # it, and route it to the PM. The `tool_failed` decision above
                        # is still recorded verbatim — this routes a signal ALONGSIDE
                        # it, it does not rewrite the ledger event. `path` is the
                        # invented tool NAME on a `tool_not_allowed` failure.
                        if is_disallowed:
                            try:
                                _detect_tool_confabulation(
                                    store, task, DEV, path, reason)
                            except Exception:  # noqa: BLE001 — telemetry never fails a turn
                                pass
                    # Spec 17 (Item 3b): carry the disallowed-tool reason forward on
                    # the task so its NEXT composed dev prompt shows the corrective
                    # hint (no corrective-retry path reaches a post-parse
                    # `tool_not_allowed`). A write-failure (bad path/binary) is a
                    # different class and is not carried.
                    if tool_not_allowed_reason:
                        store.update_task(task.task_id, state="todo",
                                          last_tool_failure=tool_not_allowed_reason)
                    else:
                        # This turn had no disallowed-tool failure (a write-only
                        # failure, or a clean requeue) — clear any stale hint so the
                        # next prompt doesn't claim "your last turn had a tool call
                        # rejected" when the last turn did not (review Minor #1).
                        store.update_task(task.task_id, state="todo",
                                          last_tool_failure="")
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
                        rationale="implementation task completed no successful write tool event (code_write/code_edit)",
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
                    # Spec 17: a write landed -> clear any carried tool-failure hint.
                    store.update_task(task.task_id, state="done", last_tool_failure="")
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
                    # Spec 17: the tool was used correctly (a code_write succeeded,
                    # it just changed nothing) — clear the carried failure so a
                    # resolved rejection does not nag on the requeue.
                    store.update_task(task.task_id, state="todo", last_tool_failure="")
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
                    # Spec 13 (S2): flag a PR that adds a missing foundation element
                    # while the clamp is pending, so an unrelated rejection of it
                    # can be surfaced instead of silently holding concurrency at 1.
                    if _pr_unlocks_foundation(store, _opened_changed):
                        store.update_pr(pr["pr_id"], unlocks_foundation=True)
                except Exception:  # noqa: BLE001 — best-effort observability signal
                    pass
                # Spec 17 (Item 3b): a real write landed (PR opened) — clear any
                # carried tool-failure hint so it never becomes stale nagging.
                store.update_task(task.task_id, state="done", last_tool_failure="")
                store.add_task(title=f"review PR: {task.title}", role=REVIEWER,
                               pr_id=pr["pr_id"], depends_on=[task.task_id])
                store.record_decision(
                    title=f"opened PR: {task.title}", context=f"task {task.task_id}",
                    choice="pr_opened", rationale=f"branch {branch}",
                    related_task_ids=[task.task_id], extra={"pr_id": pr["pr_id"]})
                # SPEC-30 (S4): drive the PR's own tree headless, pre-merge, and
                # stamp the verdict onto THIS PR so the reviewer has execution
                # evidence for the code it is about to approve (grounding the
                # ungrounded reviewer — the 26-92% false-rejection seat). Fail-open.
                _web_probe_pr_arm(store, workspace, task_id=task.task_id,
                                  branch=branch, head=str(pr.get("head") or ""),
                                  should_cancel=should_cancel)
                return TurnOutcome(kind="pr_opened", task=task)

            if action.role == REVIEWER:
                pr = store.get_pr(task.pr_id) if task.pr_id else None
                if pr is None or workspace is None:
                    store.update_task(task.task_id, state="done")
                    return TurnOutcome(kind="noop")
                diff = workspace.pr_diff(pr["branch"])
                ctx = _review_project_context(store, workspace, pr)
                # Spec 14 (Item 2): mount the PR worktree read-only on a per-turn
                # member copy when reviewer_repo_read is on, so the reviewer can open
                # the files the diff only shows a hunk of. Shallow copy (shared config
                # never mutated); best-effort (a resolution failure falls back to the
                # plain single-shot path — retrieval must never fail a turn that would
                # otherwise succeed).
                review_member = member
                review_root = None
                # Spec 17 (Item 1): only a repo_read-honoring vendor gets the
                # tag, so the reviewer catalog / metadata / grounding-reflex all
                # match the reviewer's real invocation.
                if _member_honors_repo_read(member, reviewer_repo_read):
                    try:
                        review_root = workspace.task_root(pr["task_id"],
                                                          branch=pr["branch"])
                        review_member = {**member, "repo_read_root": str(review_root)}
                    except Exception:  # noqa: BLE001 — retrieval is best-effort
                        review_member, review_root = member, None

                # Spec 17: resolve repo_read from the reviewer's OWN per-turn member
                # (both the canonical and legacy key), so the catalog matches its
                # real invocation.
                review_repo_read = bool(
                    review_member.get("repo_read_root")
                    or review_member.get("dev_repo_read_root"))

                def _review_once(extra: str = "") -> Any:
                    scope_task = _fetch_task(store, str(pr.get("task_id") or ""))
                    prompt = _review_pr_prompt(
                        task, pr, diff, ctx,
                        scope_task=scope_task,
                        gate_text=_gate_state.latest_gate_text(store),
                        repo_read=review_repo_read,
                        gate=_gate_state.gate_available(store),
                        design_contract=_design_contract_text(store, scope_task))
                    return _parse_member_turn(
                        REVIEWER, task.task_id, review_member, prompt + extra,
                        context=f"task {task.task_id}",
                        related_task_ids=[task.task_id])

                parsed = _review_once()
                # Spec 25 (Item 1): a reviewer that cannot review — no diff it can
                # read, a demand it cannot satisfy, a contradiction between the task
                # and the PR — blocks the REVIEW task with its own words instead of
                # being forced to invent a verdict. The PR is left exactly as it is
                # (not approved, not rejected): a block is a question for the PM,
                # and fabricating either verdict here is precisely the failure this
                # spec exists to stop. Checked before the grounding heuristics
                # below, none of which apply to a non-verdict intent.
                if (not isinstance(parsed, TurnParseError)
                        and isinstance(parsed.intent, BlockedIntent)):
                    _record_capability_ask(store, parsed.intent, role=REVIEWER,
                                           task=task,
                                           context=f"task {task.task_id}")
                    return TurnOutcome(
                        kind="task_blocked", task=task,
                        member_id=str(member.get("id", "")), member_role=REVIEWER,
                        member_route=str(member.get("gateway_route_id", "")),
                        reason=_blocked_reason_text(parsed.intent))
                # Spec 14 (Item 4/5): grounding check. `num_turns > 1` means the
                # reviewer actually ran Read/Grep before deciding; `== 1` (or, for a
                # vendor that doesn't report it, a sub-floor latency) on an EMPTY
                # approval is a reflex — retry ONCE asking it to open the changed
                # files. A second ungrounded verdict is ACCEPTED but surfaced, never
                # blocked (blocking would be a new way to wedge a run).
                num_turns, dur_ms = _last_turn_grounding()
                retrieval = review_root is not None
                review_ungrounded = False
                if retrieval and _is_empty_approval(parsed, pr["head"]):
                    reflex = (num_turns is not None and num_turns <= 1)
                    if num_turns is None and review_min_latency_ms > 0:
                        reflex = (dur_ms is not None and dur_ms < review_min_latency_ms)
                    if reflex:
                        parsed = _review_once(
                            "\nBefore approving, OPEN the files this PR changes with "
                            "Read/Grep and confirm they are correct — do not approve "
                            "without having read them.\n")
                        num_turns, dur_ms = _last_turn_grounding()
                        still_reflex = (
                            _is_empty_approval(parsed, pr["head"])
                            and ((num_turns is not None and num_turns <= 1)
                                 or (num_turns is None and review_min_latency_ms > 0
                                     and dur_ms is not None
                                     and dur_ms < review_min_latency_ms)))
                        if still_reflex:
                            review_ungrounded = True
                            store.record_decision(
                                title=f"ungrounded review verdict: {pr['branch']}",
                                context=f"pr {pr['pr_id']}", choice="review_ungrounded",
                                rationale=("the reviewer approved with no findings "
                                           "without reading the worktree, twice; "
                                           "verdict accepted but flagged"),
                                related_task_ids=[task.task_id, pr["task_id"]])
                            try:
                                from . import attention
                                attention.raise_review_alert(
                                    store.project_id, stage="development",
                                    title="reviewer approved without reading the code",
                                    summary=(f"PR on branch {pr['branch']} was approved "
                                             "with an empty, ungrounded verdict."),
                                    store=store)
                            except Exception:  # noqa: BLE001
                                pass
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
                    review_findings = _mark_finding_citations(
                        [{"severity": f.severity, "title": f.title, "body": f.body,
                          "path": f.path, "blocking": f.severity == "blocking"}
                         for f in parsed.intent.findings],
                        workspace=workspace, pr=pr)
                store.update_pr(pr["pr_id"], reviewer_approved=approved,
                                reviewed_head=pr["head"],
                                review_findings=review_findings,
                                review_grounded=bool(retrieval and (num_turns or 0) > 1),
                                review_num_turns=num_turns,
                                review_ungrounded=review_ungrounded)
                store.record_decision(
                    title=f"review verdict: {pr['branch']}",
                    context=f"pr {pr['pr_id']}",
                    choice="review_approved" if approved else "review_rejected",
                    rationale=f"reviewer verdict for {pr['branch']}",
                    related_task_ids=[task.task_id, pr["task_id"]],
                    extra={"reviewed_head": pr["head"], "pr_id": pr["pr_id"]})
                store.update_task(task.task_id, state="done")
                if approved:
                    # Only queue a tester when there's something to run AND somebody
                    # to run it. With no registered test commands — or (SPEC-26) no
                    # seated TESTER — the PR is already mergeable on approval (see
                    # _set_mergeable_if_ready); spawning a tester task would just
                    # starve in the backlog forever.
                    if store.get_unit_test_commands() and _tester_seated():
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
                    _handle_review_rejection(
                        store, workspace, pr=pr, task=task,
                        findings=review_findings, source="reviewer", diff=diff)
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
                # Spec 12 (S1): the tester validates its BRANCH against unit
                # commands only (acceptance commands are integration-scoped).
                registry = store.get_unit_test_commands()

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
                # Spec 25 (Item 1): a tester that cannot test says so instead of
                # naming a command it knows will not exercise the slice. This must
                # NOT go through `_changes_requested` — that marks the PR
                # tests-failed and spawns a "fix tests" dev task, i.e. it converts
                # "I cannot answer" into "the code is broken", which is the same
                # fabrication the reviewer branch above refuses. The test task
                # blocks; the PR's tests_passed is left untouched (still ungated),
                # and the PM decides.
                if isinstance(parsed.intent, BlockedIntent):
                    _record_capability_ask(store, parsed.intent, role=TESTER,
                                           task=task,
                                           context=f"task {task.task_id}")
                    return TurnOutcome(
                        kind="task_blocked", task=task,
                        member_id=str(member.get("id", "")), member_role=TESTER,
                        member_route=str(member.get("gateway_route_id", "")),
                        reason=_blocked_reason_text(parsed.intent))
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
                    # F156 (G5): count the escape per run. The declaration itself is
                    # legitimate for a partial slice, so it is never refused — but a
                    # run that leans on it slice after slice is merging on review
                    # alone, and that must not stay invisible. Past
                    # `not_applicable_soft_limit` the deduped non-blocking alert is
                    # escalated to a recorded, operator-visible signal. Fully guarded:
                    # a counter write failure degrades to today's behaviour and never
                    # fails the turn.
                    na_count = 0
                    na_limit = 0
                    try:
                        na_count = int(store.get_run_state().get(
                            "tests_not_applicable_count", 0) or 0) + 1
                        store.set_run_state(tests_not_applicable_count=na_count)
                        from .autonomy import load_policy
                        na_limit = int(getattr(
                            load_policy(store),
                            "not_applicable_soft_limit", 0) or 0)
                    except Exception:  # noqa: BLE001 — accounting is best-effort
                        na_count, na_limit = 0, 0
                    over_limit = bool(na_limit) and na_count > na_limit
                    if over_limit:
                        try:
                            store.record_decision(
                                title="tests not applicable over soft limit",
                                context=f"pr {pr['pr_id']}",
                                choice="tests_not_applicable_over_limit",
                                rationale=(
                                    f"{na_count} slices in this run have declared "
                                    f"tests not-applicable (soft limit {na_limit}). "
                                    "The per-PR merge gate is running on review "
                                    "alone for those slices. The delivered head is "
                                    "still gated deterministically at delivery, but "
                                    "consider registering test commands."),
                                related_task_ids=[task.task_id, pr["task_id"]])
                        except Exception:  # noqa: BLE001
                            pass
                    # F142 WS-C observability: surface a non-blocking Alert (deduped
                    # to one per run) so a human sees that a slice merged without any
                    # test running — otherwise a run could merge to done with tests
                    # never executed and nothing telling the operator.
                    try:
                        from . import attention
                        _extra = (
                            f" This is slice {na_count} in this run to skip tests "
                            f"(soft limit {na_limit}) — the merge gate is running on "
                            "review alone." if over_limit else "")
                        attention.raise_tests_skipped_alert(
                            store.project_id, stage="build",
                            summary=(f"PR on branch {pr['branch']} merged without "
                                     "running tests (tester declared the slice "
                                     f"not-applicable). Verify test coverage.{_extra}"),
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
                    # Spec 11 (P1b): carry the raw failing stderr into the
                    # fix-task detail, not just cmd=status/exit_code.
                    fix_detail = f"Tests failed: {exits}"
                    appendix = _failed_stderr_appendix(session.results)
                    if appendix:
                        fix_detail += f"\n\nFailing test output:\n{appendix}"
                    store.add_task(title=f"fix tests: {pr['branch']}", role=DEV,
                                   detail=fix_detail)
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
                # Spec 14 (Item 2): mount the PR worktree for the PM's review too —
                # otherwise the second half of the strict-mode dual review stays
                # blind. Best-effort, per-turn copy (shared config never mutated).
                pm_review_member = member
                # Spec 17 (Item 1): vendor-honor the tag here too.
                if _member_honors_repo_read(member, reviewer_repo_read):
                    try:
                        _pmr = workspace.task_root(pr["task_id"], branch=pr["branch"])
                        pm_review_member = {**member, "repo_read_root": str(_pmr)}
                    except Exception:  # noqa: BLE001
                        pm_review_member = member
                pm_review_repo_read = bool(
                    pm_review_member.get("repo_read_root")
                    or pm_review_member.get("dev_repo_read_root"))
                parsed = _parse_member_turn(
                    REVIEWER, task.task_id, pm_review_member,
                    _review_pr_prompt(
                        task, pr, diff, ctx,
                        scope_task=(_pm_scope_task := _fetch_task(
                            store, str(pr.get("task_id") or ""))),
                        gate_text=_gate_state.latest_gate_text(store),
                        repo_read=pm_review_repo_read,
                        gate=_gate_state.gate_available(store),
                        design_contract=_design_contract_text(store, _pm_scope_task)),
                    context=f"task {task.task_id}", related_task_ids=[task.task_id])
                pm_findings: list[dict[str, Any]] = []
                if isinstance(parsed, TurnParseError):
                    store.record_decision(
                        title=f"pm review turn rejected: {task.title}",
                        context=f"task {task.task_id}", choice="pm_review_turn_rejected",
                        rationale=f"{parsed.code.value}: {parsed.detail}",
                        related_task_ids=[task.task_id])
                    approved = False
                elif isinstance(parsed.intent, BlockedIntent):
                    # Spec 25: the PM half of the strict-mode dual review could not
                    # be given. Recorded with its reason; NOT approved — the gate
                    # stays unsatisfied rather than passing on a non-verdict. The
                    # review task itself blocks below only for a worker reviewer;
                    # here the PM review task completes so the dual-review gate can
                    # be re-driven by an ordinary plan turn.
                    store.record_decision(
                        title=f"pm review blocked: {task.title}",
                        context=f"task {task.task_id}", choice="blocked",
                        rationale=_blocked_reason_text(parsed.intent),
                        related_task_ids=[task.task_id])
                    _record_capability_ask(store, parsed.intent, role=PM, task=task,
                                           context=f"task {task.task_id}")
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
                    pm_findings = _mark_finding_citations(
                        [{"severity": f.severity, "title": f.title, "body": f.body,
                          "path": f.path, "blocking": f.severity == "blocking"}
                         for f in parsed.intent.findings],
                        workspace=workspace, pr=pr)
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
                    _handle_review_rejection(
                        store, workspace, pr=pr, task=task,
                        findings=pm_findings, source="pm_review", diff=diff)
                _set_mergeable_if_ready(pr["pr_id"])
                return TurnOutcome(kind="pr_reviewed", task=task)

        return TurnOutcome(kind="noop")

    def run_turn(action: Any, ledger: Any) -> TurnOutcome:
        # F087-19 #2: clean up stale/superseded PRs + corrective tasks before each
        # turn so the backlog/context reflects what master actually still needs.
        _reconcile_stale(store, workspace)
        # SPEC-30 (Fix B): drop engine-filed execution-fix tasks that are now moot
        # (no failing evidence at the delivered head), so a satisfied blocked task
        # cannot wedge the completion gate as human-required (run 8).
        _reconcile_moot_gate_fixes(store, workspace)
        # F087-16: record a verbatim transcript entry for every member turn
        # (the captured prompt + raw response + the resulting outcome), and emit
        # a one-line log so a live run is reviewable end to end.
        _cap = _cap_of()
        _cap.clear()
        if isinstance(action, Plan):
            role, task_id = PM, "plan"
        elif isinstance(action, PMAssist):
            role, task_id = PM, action.task_id
        elif isinstance(action, LastWord):
            # SPEC-23: attributed to the PM, keyed by the detector that asked, so
            # the transcript shows WHY the harness spent this turn.
            role, task_id = PM, f"last-word:{action.detector}"
        elif isinstance(action, DesignPlan):
            role, task_id = DESIGNER, "design"
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
        # A reviewer (falling back to the PM) gives a real REVIEW VERDICT. With
        # neither configured no verdict is fabricated — but F156 (G7): that must skip
        # ONLY the verdict. The old early return sat here, BEFORE steps 2 and 3, so
        # "no reviewer" silently also meant "no tests, no launch probe, no web probe",
        # and a team with neither REVIEWER nor PM reached `project_done` with ZERO
        # delivery verification. `approved` therefore defaults True (a team that
        # cannot produce a verdict must not be blocked by its absence) while every
        # deterministic check below still runs for real, so `passed` continues to
        # require a delivered head that builds, launches and renders.
        reviewer_members = members_by_role.get(REVIEWER) or members_by_role.get(PM)
        reviewer_member = reviewer_members[0] if reviewer_members else None
        approved = True
        findings: list[dict[str, Any]] = []

        # 1) Reviewer over the WHOLE delivered diff, bound to `head`. A preview
        #    failure means a corrupt/missing worktree (F087-15 M1) — do NOT review
        #    a blank diff and pass; block done as an unverifiable delivery.
        #    The preview + its fail-closed guard live INSIDE this branch on purpose:
        #    with no reviewer there is no diff to review, and failing delivery on an
        #    unreadable preview nobody would have read would be a new false block.
        if reviewer_member is not None:
            approved = False
            try:
                diff = str((workspace.preview() or {}).get("diff") or "")
            except Exception:  # noqa: BLE001
                return _cannot_verify("delivered diff unavailable (preview failed)")
            # SPEC-30 fix: ground the delivery reviewer in the delivered tree so it
            # cannot invent file paths (run 7's "Missing acceptance test file
            # test/test.js" wedge). Mount the master working tree read-only when
            # reviewer_repo_read honors this vendor; fall back to the plain member.
            delivery_reviewer = reviewer_member
            delivery_repo_read = False
            if _member_honors_repo_read(reviewer_member, reviewer_repo_read):
                try:
                    delivery_reviewer = {**reviewer_member,
                                         "repo_read_root": str(workspace.root())}
                    delivery_repo_read = True
                except Exception:  # noqa: BLE001 — grounding is best-effort
                    delivery_reviewer, delivery_repo_read = reviewer_member, False
            try:
                parsed = _parse_member_turn(
                    REVIEWER, _DELIVERY_TASK_ID, delivery_reviewer,
                    _delivery_review_prompt(store, head, diff,
                                            repo_read=delivery_repo_read),
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
            elif isinstance(parsed.intent, BlockedIntent):
                # Spec 25: the delivery reviewer said it could not review the delivered
                # head. Recorded as a NON-verdict (the same fail-closed treatment as a
                # stale head): `done` does not stick, and nothing is fabricated in
                # either direction.
                store.record_decision(
                    title="delivery review blocked",
                    context="delivery_review", choice="blocked",
                    rationale=_blocked_reason_text(parsed.intent),
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
                # Spec 12 (S1): shared deterministic executor (also the in-loop
                # gate). Runs all commands against the merged head, records the run.
                session = _run_gate(store, workspace, head=head,
                                    task_id=_DELIVERY_TASK_ID,
                                    should_cancel=should_cancel)
                tests_passed = bool(session.passed)
                if not tests_passed:
                    tests_failed_detail = "; ".join(
                        f"{r.command_id}={r.status}/{r.exit_code}"
                        for r in session.results)
                    # Spec 11 (P1b): append the failing commands' raw stderr so
                    # the fix task (filed below at "fix delivery tests") carries
                    # the real error, not just cmd=status/exit_code.
                    _appendix = _failed_stderr_appendix(session.results)
                    if _appendix:
                        tests_failed_detail += (
                            f"\n\nFailing test output:\n{_appendix}")
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

        # 2') F154: the zero-config compile floor. A greenfield project's EMPTY
        #     registry currently reads as success at both gates — `tests_ok` is
        #     vacuously satisfied per-PR and `tests_passed` is unconditionally True
        #     here — so a project can reach `done` with nothing ever compiled.
        #     F152/F153 catch an app that fails to serve or start; neither catches a
        #     compile error on a path never requested at launch. Only when the
        #     registry is empty: when the council registered real commands they are
        #     the stronger signal and no build is injected on top.
        built_clean, build_cannot_verify, build_detail = True, False, ""
        try:
            from .autonomy import load_policy
            default_build_gate = bool(getattr(
                load_policy(store), "default_build_gate", True))
        except Exception:  # noqa: BLE001 — unreadable policy -> the default (on)
            default_build_gate = True
        if not registry and default_build_gate:
            built_clean, build_cannot_verify, build_detail = _run_default_build(
                store, workspace, head, should_cancel=should_cancel)
            if build_detail or not built_clean:
                try:
                    store.record_decision(
                        title="delivery build", context="delivery_review",
                        choice=("built_pass" if built_clean
                                else "build_cannot_verify" if build_cannot_verify
                                else "built_fail"),
                        rationale=build_detail[:1000] or "default build clean")
                except Exception:  # noqa: BLE001
                    pass

        # 3) Runtime launch evidence (F146 Slice C): for a runnable managed_local
        #    profile, LAUNCH the delivered program headless + bounded and require
        #    it to get past startup without a traceback — catching runtime-only
        #    crashes (the `pygame.font` case) that per-PR review + unit tests miss.
        #    Deterministic launch of the exact `head`; recorded against it.
        #    Non-runnable projects skip the probe (launched_clean vacuously True).
        launched_clean, launch_cannot_verify, launch_detail = \
            _delivery_launch_evidence(store, workspace, head,
                                      should_cancel=should_cancel)

        # GL01 (Item 1) + SPEC-30 (S2): the web probe rides delivery — a runnable
        # web tree's delivered head gets a did-it-RUN liveness check (render + no
        # console crash + responds to input) recorded as evidence, anchors
        # reconciled. Fail-open: a probe that could NOT run returns None and never
        # blocks (a headless-browser inability must not fail delivery). But a probe
        # that RAN and came back RED — black canvas, a crash on interaction, or an
        # inert canvas that ignored input — now BLOCKS `done`. This is the change
        # that stops the "big square" and the crash-on-shot from shipping: before
        # SPEC-30 the probe recorded a red verdict here and delivery ignored it.
        probe_run = _web_probe_arm(store, workspace, head=head,
                                   should_cancel=should_cancel)
        probe_ok = True
        if isinstance(probe_run, dict) and probe_run.get("passed") is False:
            probe_ok = False
            store.record_decision(
                title="delivery web probe failed", context="delivery_review",
                choice="probe_fail",
                rationale=str((probe_run.get("results") or [{}])[0].get("reason")
                              if probe_run.get("results") else "web probe red")[:500])

        # `passed` requires a clean launch AND a non-red web probe. A launch or
        # probe cannot_verify (None) leaves its flag True (fail-open); only a
        # definitive red fails `passed`.
        passed = (approved and tests_passed and launched_clean and probe_ok
                  and built_clean)
        # Cache once-per-head ONLY for a real verdict. A cannot_verify (inability
        # to launch, or to install deps for the F154 build) is NOT cached, so the
        # next completion claim retries instead of resting on a false negative
        # (matches _cannot_verify above; a persistent failure stops via no_progress,
        # never a false `done`).
        if not launch_cannot_verify and not build_cannot_verify:
            try:
                store.set_run_state(delivery_reviewed_head=head,
                                    delivery_review_passed=passed)
            except Exception:  # noqa: BLE001
                pass
        if passed:
            # F156 (G7): a distinct reason keeps the reviewer-less path visible in
            # the record — it passed on deterministic evidence alone, with no verdict.
            return DeliveryReviewResult(
                passed=True,
                reason=("reviewed" if reviewer_member is not None
                        else "reviewed_no_reviewer"))

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
        if build_cannot_verify:
            # F154: deps could not be installed, or the toolchain is absent. This is
            # environmental — no code merge flips it green — so it records a decision
            # and files NO dev task, mirroring the launch-error branch below. It
            # still blocks `done` and is not cached, so the next claim retries.
            store.record_decision(
                title="delivery build could not run",
                context="delivery_review", choice="delivery_build_error",
                rationale=build_detail[:1000])
        elif not built_clean:
            # A build that RAN and reported errors is a real delivered-code defect —
            # exactly the compile/type error F152's launch probe cannot see because
            # the faulting path is never requested at startup.
            store.add_task(
                title="fix delivery build", role=DEV,
                reason_summary="The delivered code does not build",
                detail=("The project has no registered test commands, so delivery "
                        "ran an auto-derived build/typecheck at the delivered head "
                        f"and it FAILED. Fix the build errors: {build_detail}"))
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
        if not probe_ok:
            # SPEC-30 (S2/S4): the delivered web artifact rendered but is not a
            # working deliverable — it crashed when driven, or ignored input
            # entirely (the "big square"). File it as dev work carrying the probe's
            # VERBATIM reason (the console crash line, or "did not respond to
            # input") so the DEV gets grounded runtime evidence a diff cannot give.
            probe_detail = ""
            try:
                res = (probe_run.get("results") or [{}])[0] if isinstance(probe_run, dict) else {}
                probe_detail = str(res.get("stderr_preview") or res.get("reason") or "")
            except Exception:  # noqa: BLE001
                probe_detail = ""
            store.add_task(
                title="fix web artifact runtime behavior", role=DEV,
                reason_summary="The delivered page does not run correctly when driven",
                detail=("The delivered web artifact was loaded headless and driven "
                        "with a pointer interaction. It either crashed on input or "
                        "did not respond (an inert canvas). Fix it so the page runs "
                        "without console errors AND visibly responds to input: "
                        f"{probe_detail}"))
            filed = True
        reason = ("launch_cannot_verify" if launch_cannot_verify
                  else "build_cannot_verify" if build_cannot_verify
                  else "rejected")
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
        # Spec 11 (P1a) / Spec 14: the DEV-turn dispatch (dev_repo_read) and the
        # REVIEWER / PM-review dispatch (reviewer_repo_read) tag the per-turn copy
        # of the member with the task worktree root. Forward it through the gateway
        # metadata so a claude_cli turn runs read-only in-turn retrieval
        # (cwd=worktree, Read/Grep/Glob only). Absent for planning turns and when
        # the policy is off -> unchanged single-shot path. The provider accepts the
        # generic `repo_read_root`; the legacy `dev_repo_read_root` key is still
        # forwarded so a mixed-version pair keeps working.
        # Spec 17 (Item 1): forward the read-only worktree root ONLY to a vendor
        # that actually honors it (claude_cli today). The dispatch sites already
        # gate the tag on the vendor, so in the normal flow a non-honoring member
        # never carries the key; this second check keeps the seam honest against
        # any other path that might set it, and guarantees the forwarded metadata
        # never disagrees with the prompt catalog.
        metadata: dict[str, Any] = {}
        # SPEC-41 Moves 2+3: forward the structured-turn flags the shadowing caller
        # set. Absent (any path that builds its own caller) -> no keys -> the gateway
        # sends neither `think` nor `format`, i.e. today's request exactly.
        if member.get("structured_output"):
            metadata["structured_output"] = True
            metadata["local_think_false"] = bool(
                member.get("local_think_false", True))
            metadata["local_structured_format"] = bool(
                member.get("local_structured_format", True))
        repo_read_root = (member.get("repo_read_root")
                          or member.get("dev_repo_read_root"))
        if (isinstance(repo_read_root, str) and repo_read_root.strip()
                and _vendor_honors_repo_read(member)):
            metadata["repo_read_root"] = repo_read_root.strip()
        # SPEC-42: resolve the per-turn budget from the model instead of hardcoding
        # 2048. A reasoning model spends its budget on a hidden trace BEFORE the
        # answer — `qwen3.5:9b` averages 2197 generated tokens on a reviewer verdict —
        # so 2048 truncated every such turn and the empty `content` then arrived at
        # the council disguised as an answer by gateway_local's THINKING_TRACE_MARKER
        # substitution. An explicit `turn_limits` value always wins.
        #
        # The vendor gate is mandatory, not a refinement: this seam is
        # provider-agnostic and the marker list matches real HOSTED ids
        # (`openai.o1`/`o3`, `*-thinking`), whose handlers treat max_output_tokens as
        # a hard cap — raising it there would silently change paid-token behaviour
        # that SPEC-42 puts out of scope.
        _model_id = _member_model_id(member)
        _budget = _resolve_turn_budget(
            _model_id,
            explicit=tl,
            enabled=(_member_vendor(member) == "local"
                     and bool(member.get("reasoning_output_budget", True))),
        )
        req = LocalCouncilModelRequest(
            role=str(member.get("role", "answerer")),
            route_id=str(member.get("gateway_route_id", "")),
            provider=str(member.get("provider_kind", "local")),
            model=_model_id,
            messages=[{"role": "user", "content": prompt}],
            max_output_tokens=_budget[0],
            temperature=float(gen.get("temperature", 0.3) or 0.3),
            metadata=metadata,
            # CLI-backed members (claude_cli/codex_cli/cursor_cli) run a full agentic loop per
            # turn and routinely need minutes — a 180s cap timed turns out
            # constantly (each crash requeues the task, so the team spun without
            # landing anything). Default to 10 min; per-room override via
            # turn_limits.timeout_seconds.
            timeout_seconds=_budget[1],
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
            # Spec 14: agentic turn count (claude CLI) for the reviewer-grounding
            # check; None for providers that don't report it. duration_ms is the
            # latency fallback for vendors that don't report num_turns.
            "num_turns": getattr(result, "num_turns", None),
            "duration_ms": getattr(result, "duration_ms", None),
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
        # SPEC-45: a capability enabled between runs (or mid-run) re-dispatches the
        # tasks that were blocked waiting for it — on start and every iteration.
        _reeval_capability_blocked(self.store)
        # SPEC-23 (Item 6): the last-word SNAPSHOT (which detector was asked, what
        # it answered) describes one run and must not be read as this run's. A
        # FRESH start clears it; a resume/continue — the only caller that passes
        # `counters` — keeps it, because the budget it belongs to is carried too.
        if counters is None:
            try:
                # SPEC-27 rides the SAME rule for the SAME reason: the narrowing
                # ladder (and the narrowing FLAGS it engaged) belong to the run
                # whose budget carried them. A fresh start must not inherit a
                # mid-ladder rung map or a still-engaged clamp from the last run;
                # a resume/continue keeps both, because `counters_from_run_state`
                # carried the budget that bounds them.
                # F156 (G5) rides the same rule again: the not-applicable counter
                # is documented as "per run" and the escalation it drives is
                # phrased "N slices in THIS run". It was never cleared, so a
                # second run inherited run 1's total and escalated on its FIRST
                # legitimate declaration.
                #
                # KNOWN LIMITATION, shared with every key cleared here: `counters
                # is None` is a proxy for "fresh start", not a run identity. The
                # counters block is persisted only at a CLEAN terminal stop, so a
                # resume-after-interruption (the only state `/run/resume` accepts)
                # arrives with counters None and is treated as fresh. That resets
                # this counter mid-run, and the escalation for the remainder of
                # the run under-counts. It degrades observability only — no merge
                # or delivery decision reads this key — so it is not worth a
                # run-identity token here; fixing the proxy is a change to all
                # five keys and belongs with SPEC-23/27, not with F156.
                #
                # SPEC-46's drop ledger rides the same rule for the same reason: it
                # is specified per-run ("a fresh run starts clean"). Left uncleared,
                # a task the PM prunes ONCE in each of three separate runs is
                # silently quarantined on run 4 and never re-created. The resume
                # path keeps it, because the create->drop cycles it counts belong to
                # the run whose budget is being carried.
                self.store.set_run_state(
                    last_words=None, narrow_ladder=None,
                    integration_only=False, planning_clamped=False,
                    tests_not_applicable_count=0,
                    **{_drop_ledger.RUN_STATE_KEY: {}})
            except Exception:  # noqa: BLE001 — never fail a start on a hygiene write
                pass
        # SPEC-24 (Item 1 / Edge cases): clear the published detector snapshot
        # UNCONDITIONALLY, resume included. Every detector window re-arms on
        # `errorta continue` (`c = counters or LoopCounters()`), so a surviving
        # snapshot would have the resumed run's first PM turn reading windows that
        # no longer exist. The loop republishes at its first quiescent point.
        _detector_state.clear(self.store)
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
        # Spec 12 (S1) Item 1: acquire a gate at run start too, not only after a
        # merge. An `existing`/imported target already carrying tests on master
        # gets its gate — and its one-shot smoke run — here, off the loop and off
        # any merge turn. Fully guarded internally; a hiccup never fails the run.
        try:
            from . import gate_bootstrap
            if getattr(policy, "gate_bootstrap", True):
                gate_bootstrap.maybe_bootstrap(self.store, self.workspace, policy)
        except Exception:  # noqa: BLE001
            pass
        by_role = members_by_coding_role(self.members)
        member_pairs = [(m["id"], coding_role_of(m)) for m in self.members
                        if m.get("enabled", True)]
        # SPEC-26 (Item 2): role-capability closure. GL05's audit scored the same
        # council and did nothing with the answer; this SEATS the consequence —
        # every seated role discharges its duty, or it is not seated, or
        # `capability_overrides` names it. Never a refused run: the shipped defaults
        # flag both the TESTER and the REVIEWER on a fresh project, so a refusal
        # would refuse the product's own default config. Mutates `member_pairs` and
        # `by_role` in place (both must be filtered — filtering one seats a ghost).
        # Evaluated HERE rather than in the confirm route so `resume`/`continue`,
        # which both come through `CodingRunner.run`, re-derive against current state.
        role_closure_state = _apply_role_closure(
            self.store, member_pairs, by_role, policy)
        # The pre-closure roster: the concurrent pool is sized from it so a role
        # re-seated mid-run (Item 3) cannot silently serialize behind a full pool.
        pool_members = list(role_closure_state.full_pairs)
        # F127: member tier ranks so the escalate-up ladder reassigns a task a
        # weak member can't do to a stronger one.
        from .model_tier import member_rank
        member_tiers = {
            m["id"]: member_rank(m) for m in self.members if m.get("enabled", True)
        }
        run_turn = build_run_turn(
            self.store, self.workspace, by_role, self.caller,
            guardrail_enabled=self.guardrail_enabled,
            should_cancel=should_cancel,
            dev_repo_read=bool(getattr(policy, "dev_repo_read", False)),
            reasoning_output_budget=bool(
                getattr(policy, "reasoning_output_budget", True)),
            local_think_false=bool(getattr(policy, "local_think_false", True)),
            local_structured_format=bool(
                getattr(policy, "local_structured_format", True)),
            reviewer_repo_read=bool(getattr(policy, "reviewer_repo_read", False)),
            review_min_latency_ms=int(getattr(policy, "review_min_latency_ms", 0)),
            difficulty_downgrade_limit=int(
                getattr(policy, "difficulty_downgrade_limit", 1)),
            role_closure_state=role_closure_state)
        try:
            res = run_coding_loop(self.store, member_pairs, policy,
                                  run_turn=run_turn, counters=counters,
                                  should_cancel=should_cancel,
                                  member_tiers=member_tiers,
                                  pool_members=pool_members,
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
                    # Spec 22-28 P0.2: the detector windows whose SUBJECT outlives
                    # the run (a frozen path, a red gate, a broken revise lineage).
                    # Additive keys; `counters_from_run_state` restores them on a
                    # resume/continue so a window cannot silently re-arm while the
                    # state it bounds is still there. Every other consumer of this
                    # block reads by key, so the extra keys are inert for them.
                    **window_counters_to_dict(res.counters),
                })
        return res
