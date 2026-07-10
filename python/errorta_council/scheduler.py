"""TurnScheduler — async, single writer task per run (invariants 1, 2).

The scheduler acquires its RunWriterToken at run start and holds it for
the lifetime of the writer task. Every append_event call inside the
scheduler passes this token. Control events (pause/resume/cancel) flow
through RunControl, which is constructed with the same token so its
appends ride the scheduler's writer reservation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from errorta_council.callouts.admission import evaluate_callout
from errorta_council.callouts.policy import find_target, resolve_callout_policy
from errorta_council.callouts.queue import CalloutQueue, CalloutRecord
from errorta_council.context.citations import CitationRegistry, citation_registry_path
from errorta_council.context.dialect.parser import parse_digest_v1
from errorta_council.context.efficiency import resolve_context_efficiency
from errorta_council.context.overflow import classify_context_overflow
from errorta_council.control import RunControl
from errorta_council.limits import ReasonCode, SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.resources import AdmissionResult
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType, MemberSnapshot, RunMeta, ToolPolicy
from errorta_council.state import CounterRebuilder
from errorta_council.steward.packet import build_deterministic_packet
from errorta_council.steward.policy import resolve_steward_policy
from errorta_council.steward.store import StewardPacketStore
from errorta_council.topologies.round_robin import (
    RoundRobinTopology,
    RunCompletion,
    TurnProposal,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Prompt for the consensus synthesizer turn (finalization mode
# "consensus_report"). The synthesizer reads the deliberation and writes one
# consolidated answer in the council's shared voice.
CONSENSUS_SYNTHESIS_PROMPT = (
    "You are the council's consensus writer. The members have finished "
    "deliberating and broadly agree. Write a single, clear, self-contained "
    "answer that represents the council's shared conclusion. Integrate the "
    "members' points and resolve minor wording differences. Do not introduce "
    "claims that none of the members made. Do not describe the deliberation "
    "process, mention that you are summarizing, or refer to 'the council' or "
    "'the members' — answer the user's question directly, as one voice."
)

# F031-28 — the abstractive ``summary`` finalization mode. Unlike consensus, this
# does NOT pretend the members converged: it runs for ANY terminal reason and must
# faithfully present the range of views, preserving disagreement rather than
# flattening it into false certainty.
SUMMARY_SYNTHESIS_PROMPT = (
    "You are the council's rapporteur. The members have finished deliberating. "
    "Write a clear, self-contained summary of the discussion for the user: state "
    "the main conclusion(s) the members reached, and faithfully present the full "
    "range of views — including any disagreement, caveats, or minority positions. "
    "Do not take a side, do not introduce claims that none of the members made, "
    "and do not pretend there is more agreement than there was. Attribute differing "
    "views plainly (for example, 'most members concluded X, but one argued Y'). "
    "Answer the user's question directly; do not narrate the deliberation process "
    "or refer to yourself as summarizing."
)

# F080 — the neutral leader-judge. It watches the members deliberate, holds NO
# opinion of its own, and decides only WHETHER they have converged. It never
# takes a deliberation turn and never adds facts of its own.
NEUTRAL_JUDGE_PROMPT = (
    "You are the council's NEUTRAL JUDGE. You hold NO opinion of your own and "
    "must NEVER take a side, add facts, or answer from your own knowledge — even "
    "if you would answer differently than the members. Your ONLY job is to read "
    "what the members have said and decide whether they have converged on a "
    "shared conclusion. Respond with a SINGLE JSON object and nothing else:\n"
    '{"verdict": "reached" | "continue", "answer": "<if reached: the conclusion '
    'the members agreed on, in plain language, using ONLY what they actually '
    'said>", "agreed_member_ids": ["..."], "dissenting_member_ids": ["..."], '
    '"reason": "<one short sentence>"}\n'
    'Use "reached" only when the members substantively agree (minor wording '
    'differences are fine). Otherwise use "continue". Never invent an answer the '
    "members did not state."
)
# Tie-break variant: the deliberation hit its limit without clear agreement. The
# judge picks strictly among the members' stated positions — never its own.
NEUTRAL_JUDGE_TIEBREAK_PROMPT = (
    "You are the council's NEUTRAL JUDGE. The deliberation reached its limit "
    "without clear agreement. You hold NO opinion of your own and must choose "
    "STRICTLY among the positions the members actually stated — never introduce "
    "your own answer or new facts. Respond with a SINGLE JSON object and nothing "
    'else:\n{"verdict": "decide" | "no_consensus", "answer": "<the '
    'best-supported position among the members, in plain language>", '
    '"chosen_member_id": "<the member whose position you adopted, if any>", '
    '"reason": "<one short sentence>"}\n'
    'Use "no_consensus" with an empty answer only if the members are genuinely '
    "irreconcilable."
)
# F084: in a credibility room the judge does not gate stopping only — at the end
# it states a NEUTRAL verdict over the verified evidence so an advocate (incl. a
# steelman) never authors the headline. Free prose, not a verdict JSON.
NEUTRAL_JUDGE_CREDIBILITY_PROMPT = (
    "You are the council's NEUTRAL JUDGE. The members have finished deliberating "
    "and a source-verified evidence report has been compiled. You hold NO opinion "
    "of your own and you do not take a side. In 2-4 sentences, state the council's "
    "verdict on the user's question: say plainly what the fetched sources actually "
    "support and what they do NOT, and keep the LITERAL question distinct from any "
    "weaker reframing a member may have offered. Do not adopt any member's "
    "advocacy, do not introduce new claims or citations, and base your verdict "
    "only on the deliberation. If the literal question is not supported by the "
    "sources, say so directly. Reply with plain prose only — no JSON."
)


def _exc_detail(exc: BaseException, *, limit: int = 500) -> str:
    """A human-readable failure detail for the event log.

    The previous behavior stored only ``type(exc).__name__`` (e.g. the bare
    string "FatalError"), which threw away the message that actually says WHY a
    member failed — "claude_cli_not_authenticated: run 'claude' and log in",
    "codex_cli_rate_limited", "claude_cli_failed: exit 1: ...", etc. The
    message is generated by our own gateway/handler code (the prompt is sent on
    stdin, never echoed into these strings), so it is safe to surface here for
    diagnosis. Truncated defensively.
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    return f"{name}: {msg}"[:limit] if msg else name


# Model-name substrings that identify reasoning ("thinking") models. These emit
# a hidden reasoning trace before the visible answer and need a larger default
# output budget or they thinking-burn (no visible answer). Matched case-insensitively
# against the member's model id; a per-member max_output_tokens always overrides.
_REASONING_MODEL_MARKERS = (
    "qwen3", "qwq", "deepseek-r1", "deepseek-reasoner", "r1-", "-r1",
    "thinking", "reasoning", "o1", "o3", "gpt-5-thinking",
)


def _is_reasoning_model(model: str) -> bool:
    name = model.lower()
    return any(marker in name for marker in _REASONING_MODEL_MARKERS)


_CREDIBILITY_TYPE_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    (".gov", "government"),
    (".gov.uk", "government"),
    (".mil", "government"),
    (".edu", "peer_reviewed_paper"),
    (".ac.uk", "peer_reviewed_paper"),
    (".org", "official"),
)


def _credibility_source_type(url: str) -> str:
    """Cheap, visible source-type heuristic from the host (F078: metadata for
    reviewers, NOT truth). Defaults to 'unknown'."""
    from errorta_council.credibility.evidence_store import registrable_domain

    domain = registrable_domain(url)
    for suffix, kind in _CREDIBILITY_TYPE_BY_SUFFIX:
        if domain.endswith(suffix):
            return kind
    return "unknown"


def _format_credibility_answer(report: Any) -> str:
    """Deterministic human-readable rendering of a CredibilityReport when no
    model-written leader answer is available (fail-safe; a model synthesis can
    replace this later)."""
    lines: list[str] = []
    # P2: lead with the leader's own prose answer when they wrote one; the
    # deterministic evidence sections follow as the verified source map.
    if getattr(report, "answer", ""):
        lines.append(report.answer)
        lines.append("")
    if report.verification_incomplete:
        lines.append("**Verification incomplete** — required research could not complete.")
    if getattr(report, "quality_flag", "") == "unchallenged_consensus":
        lines.append(
            "**Unchallenged consensus** — no opposing case survived the gate; "
            "treat with caution."
        )
    fin_fail = getattr(report, "finalizer_citation_failures", None) or []
    if fin_fail:
        lines.append(
            f"**Council leader mis-cited {len(fin_fail)} of its own sources** — "
            "its conclusion is downgraded."
        )
    used = len(report.claims_used)
    srcs = len(report.source_map)
    if used:
        lines.append(f"{used} verified claim(s) backed by {srcs} fetched source(s).")
    else:
        lines.append("No claims could be verified against fetched sources.")
    for c in report.caveats:
        lines.append(f"- Caveat: {c}")
    if report.excluded_claims:
        lines.append(
            f"{len(report.excluded_claims)} claim(s) excluded "
            "(unverified or contradicted)."
        )
    steel = getattr(report, "steelman_claims", None) or []
    if steel:
        topic = next((str(s.get("topic") or "") for s in steel if s.get("topic")), "")
        header = (
            "\n**Steelman arguments (UNVERIFIED — may include constructed evidence)"
            + (f" — arguing: {topic}" if topic else "")
            + ":**"
        )
        lines.append(header)
        for s in steel:
            lines.append(f"  - {str(s.get('text') or '').strip()}")
    if report.source_map:
        lines.append("\nSources:")
        has_soft = False  # any opinion/unverified source → show the legend
        for i, s in enumerate(report.source_map, 1):
            url = str(s.get("url") or "")
            title = str(s.get("title") or "").strip()
            # F085: provenance tag — label every source with its tier so an
            # opinion/blog citation never reads as corroborated fact.
            tier = str(s.get("tier") or "")
            label = str(s.get("tier_label") or tier)
            if tier in ("opinion", "unknown"):
                has_soft = True
            tag = f" · {label}" if label else ""
            # Show "title — url" only when a real title exists; otherwise just
            # the URL once (avoids the "url — url" duplication).
            base = (
                f"  [{i}] {title} — {url}" if title and title != url
                else f"  [{i}] {url}"
            )
            lines.append(base + tag)
        if has_soft:
            lines.append(
                "  (opinion = an individual viewpoint, not corroborated "
                "reporting — weigh accordingly.)"
            )
    lines.append(f"\nConfidence: {report.confidence}.")
    return "\n".join(lines)


def build_credibility_sources(run_id: str, events: list[Any]) -> Any:
    """Build an EvidenceStore from CREDIBILITY_SOURCE_CAPTURED events.

    Those events are written at fetch time (the only moment the final URL
    exists — the audit projection + side store keep only hashes), so "a source
    exists only when Errorta fetched it" holds. Pure + testable."""
    from errorta_council.credibility import EvidenceStore
    from errorta_council.schema import EventType as _ET

    store = EvidenceStore(run_id=run_id)
    for ev in events:
        if getattr(ev, "type", None) != _ET.CREDIBILITY_SOURCE_CAPTURED:
            continue
        payload = dict(getattr(ev, "payload", {}) or {})
        url = str(payload.get("url") or "").strip()
        if not url:
            continue
        store.ingest_source(
            url=url,
            tool_call_event_id=str(payload.get("tool_call_event_id") or ""),
            content_sha256=str(payload.get("content_sha256") or ""),
            fetched_at=str(payload.get("fetched_at") or ""),
            source_type=_credibility_source_type(url),
        )
    return store


def _namespace_packet(member_id: str, pkt: Any) -> Any:
    """Prefix a claim packet's claim ids with the member id (idempotent) so two
    members' identically-named claims don't collide during credidation."""
    from dataclasses import replace as _replace

    ns = []
    for c in pkt.claims:
        cid = c.claim_id if c.claim_id.startswith(f"{member_id}:") else f"{member_id}:{c.claim_id}"
        ns.append(_replace(c, claim_id=cid))
    return _replace(pkt, claims=ns)


_JSON_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?([\s\S]*?)\n?```$")


def _extract_json_object(content: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model reply that may wrap it in a ```json
    fence or surround it with prose (mirrors the desktop simple view + mobile
    projection). Returns None when no JSON object is present."""
    body = (content or "").strip()
    fence = _JSON_FENCE_RE.match(body)
    if fence:
        body = fence.group(1).strip()
    try:
        obj = json.loads(body)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start = body.find("{")
    end = body.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(body[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _extract_tool_call(content: str) -> dict[str, Any] | None:
    """Parse the slice-1 structured tool-call request dialect.

    This is intentionally strict and non-magical: no regex over prose, no
    markdown scraping. Native provider tool-calling can replace this fallback
    later without changing the scheduler->ToolGateway seam.
    """
    try:
        raw = json.loads((content or "").strip())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    call = raw.get("tool_call")
    if not isinstance(call, dict):
        return None
    tool_id = call.get("tool_id") or call.get("name")
    if not isinstance(tool_id, str) or not tool_id.strip():
        return None
    arguments = call.get("arguments", call.get("args", {}))
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None
    reason = call.get("reason")
    return {
        "tool_id": tool_id.strip(),
        "arguments": dict(arguments),
        "reason": str(reason) if reason is not None else None,
    }


def _extract_child_task(content: str) -> dict[str, Any] | None:
    """Parse the strict F042 child-task request dialect."""
    try:
        raw = json.loads((content or "").strip())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    task = raw.get("child_task")
    if not isinstance(task, dict):
        return None
    task_kind = task.get("task_kind") or task.get("kind")
    if not isinstance(task_kind, str) or not task_kind.strip():
        return None
    prompt = task.get("prompt") or task.get("instructions") or ""
    title = task.get("title") or task_kind
    worker_kind = task.get("worker_kind") or "scripted"
    artifact_refs = task.get("artifact_refs") or []
    if not isinstance(artifact_refs, list):
        artifact_refs = []
    return {
        "task_kind": task_kind.strip(),
        "title": str(title).strip() or task_kind.strip(),
        "prompt": str(prompt),
        "worker_kind": str(worker_kind),
        "result": str(task["result"]) if task.get("result") is not None else None,
        "artifact_refs": [a for a in artifact_refs if isinstance(a, dict)],
    }


def _tool_result_mismatch_reason(*, request: Any, result: Any) -> str | None:
    """Validate handler output before raw bytes enter the side store."""
    if getattr(result, "call_id", None) != getattr(request, "call_id", None):
        return "tool_result_call_id_mismatch"
    if getattr(result, "tool_id", None) != getattr(request, "tool_id", None):
        return "tool_result_tool_id_mismatch"
    content = str(getattr(result, "content", ""))
    expected_sha = hashlib.sha256(content.encode()).hexdigest()
    if getattr(result, "content_sha256", None) != expected_sha:
        return "tool_result_hash_mismatch"
    return None


class _ContextBuilder(Protocol):
    async def build(self, *, run_meta: RunMeta, member: dict, transcript: list) -> dict: ...


class _ResourceGuard(Protocol):
    async def admit(self, *, proposal: TurnProposal, member: dict) -> AdmissionResult: ...
    def release(self, turn_id: str) -> None: ...


class _Gateway(Protocol):
    async def call(self, request: Any) -> Any: ...


class _ToolGateway(Protocol):
    async def invoke(self, request: Any) -> Any: ...


class TurnScheduler:
    def __init__(
        self,
        *,
        run_store: RunStore,
        run_meta: RunMeta,
        topology: RoundRobinTopology,
        context_builder: _ContextBuilder,
        resource_guard: _ResourceGuard,
        gateway: _Gateway,
        control: RunControl,
        policy: SchedulerPolicy,
        tool_gateway: _ToolGateway | None = None,
    ) -> None:
        self._store = run_store
        self._meta = run_meta
        self._topology = topology
        self._ctx = context_builder
        self._guard = resource_guard
        self._gateway = gateway
        self._tool_gateway = tool_gateway
        self._control = control
        self._policy = policy
        self._writer = None  # type: ignore[assignment]
        self._dialect_forced_prose: set[str] = set()
        self._dialect_downgrade_emitted: set[str] = set()
        # Tracks the run's answer-of-record so the terminal path can emit a
        # FINAL_ANSWER event. A finalizer member's message always wins; absent
        # a finalizer the most recent member message is used as the answer.
        self._last_answer: dict[str, Any] | None = None
        self._last_finalizer_answer: dict[str, Any] | None = None
        # A synthesized consensus answer (finalization mode "consensus_report"),
        # written by a synthesizer turn after the council converges. Takes
        # precedence over the answer-of-record when present.
        self._consensus_answer: dict[str, Any] | None = None
        # F078: research findings captured by the forced credibility search at
        # run start (injected into each member's context so they cite real,
        # fetched sources). Empty when not a credibility run / search failed.
        self._credibility_findings: str = ""
        # F080 neutral leader-judge: the judge-decided answer (verdict reached or
        # tie-break) takes precedence over every other answer-of-record. And the
        # highest round the judge has already evaluated (so each round boundary
        # is judged at most once).
        self._judge_answer: dict[str, Any] | None = None
        self._last_judged_round: int = 0
        # F084: a neutral judge-authored verdict for a credibility room's final
        # report (computed async at finalize, read by the sync finalizer). Empty
        # when no judge is enabled — then the report keeps the leader's prose.
        self._credibility_judge_answer: str = ""

    def _read_counters(self):
        _, events = self._store.read_run(self._meta.id)
        return CounterRebuilder.from_events(events)

    def _build_run_state(self) -> dict[str, Any]:
        counters = self._read_counters()
        # ConsensusDeliberationTopology and any other topology that needs
        # to inspect past messages reads ``state["events"]``. RoundRobin
        # ignores the field. Read fresh per proposal so the latest member
        # message is visible. (Sequential dispatch model — no risk of
        # in-flight tearing.)
        _, events = self._store.read_run(self._meta.id)
        # F080: the neutral judge never takes a deliberation turn — exclude it
        # from the members every topology iterates so it can never be proposed
        # for a normal turn (it speaks only via _run_judge_evaluation).
        judge_id = self._judge_member_id()
        members = [
            m for m in self._meta.room_snapshot.get("members", [])
            if not (judge_id and str(m.get("id")) == str(judge_id))
        ]
        return {
            "members": members,
            "counters": counters,
            "policy": self._policy,
            "events": events,
        }

    def _resolve_member(self, member_id: str) -> dict:
        for m in self._meta.room_snapshot.get("members", []):
            if m["id"] == member_id:
                return m
        raise KeyError(f"unknown_member_id: {member_id}")

    def _context_efficiency(self):
        return resolve_context_efficiency({"room": self._meta.room_snapshot or {}})

    def _steward_policy(self):
        return resolve_steward_policy(self._meta.room_snapshot or {})

    def _tool_policy(self) -> ToolPolicy:
        return ToolPolicy.from_dict(
            (self._meta.room_snapshot or {}).get("tool_policy")
        )

    def _tool_calls_started(self) -> int:
        _, events = self._store.read_run(self._meta.id)
        return sum(1 for e in events if e.type == EventType.TOOL_CALL_STARTED)

    async def _maybe_handle_tool_call(
        self,
        *,
        content: str,
        member: dict[str, Any],
        snapshot: MemberSnapshot,
        proposal: TurnProposal,
        context_id: str,
        parent_event_id: str,
    ) -> RunMeta | None:
        """Invoke one structured tool-call request through ToolGateway.

        Slice 1 only accepts a strict JSON object:

        ``{"tool_call": {"tool_id": "web_fetch", "arguments": {...}}}``

        The parser is gated behind room ``tool_policy`` so rooms with no tool
        policy keep today's behavior even if a member happens to answer in
        JSON. Raw result content is written to the side store; events carry
        only hashes/provenance.
        """
        policy = self._tool_policy()
        if not policy.enabled_tool_ids():
            return None
        spec = _extract_tool_call(content)
        if spec is None:
            return None

        from errorta_tools.gateway import (
            FatalToolError,
            RetryableToolError,
            ToolCallRequest,
        )

        call_id = "tc-" + uuid.uuid4().hex[:16]
        request = ToolCallRequest(
            call_id=call_id,
            run_id=self._meta.id,
            turn_id=f"{member['id']}-r{proposal.round}",
            member_id=str(member["id"]),
            tool_id=spec["tool_id"],
            arguments=dict(spec.get("arguments") or {}),
            reason=spec.get("reason"),
            context_id=context_id,
            # Surface the room's tool_policy so handlers enforce per-room limits
            # (domain allowlist, caps, workspace path, SearXNG endpoint, exec
            # location). Config only — never secrets.
            metadata={"round": proposal.round, "tool_policy": policy.to_dict()},
        )
        requested_payload = request.without_raw_arguments()
        self._store.append_event(
            self._meta.id,
            type=EventType.TOOL_CALL_REQUESTED,
            status=EventStatus.PENDING,
            payload=requested_payload,
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )

        block_reason = self._tool_block_reason(request.tool_id, policy)
        if block_reason is None and policy.budget.max_tool_calls_per_run is not None:
            if self._tool_calls_started() >= int(policy.budget.max_tool_calls_per_run):
                block_reason = "tool_budget_exhausted"
        if block_reason is None and self._tool_gateway is None:
            block_reason = "tool_gateway_unavailable"
        approval_mode = "room_policy"
        pending_decision_id: str | None = None
        if block_reason is None:
            from errorta_policy import (
                PendingDecisionStore,
                PolicyAction,
                PolicyContext,
                PolicyEngine,
                PolicyPhase,
            )

            require_consent = (
                policy.require_first_use_consent
                and not self._tool_consent_approved(request.tool_id)
            )
            decision = PolicyEngine().evaluate(
                PolicyContext(
                    phase=PolicyPhase.TOOL_CALL,
                    run_id=self._meta.id,
                    room_id=self._meta.room_id,
                    member_id=str(member["id"]),
                    tool_id=request.tool_id,
                    request_sha256=request.args_sha256,
                    requester={
                        "type": "council_member",
                        "member_id": str(member["id"]),
                    },
                    safe_request=requested_payload,
                    policy={
                        "enabled_tool_ids": sorted(policy.enabled_tool_ids()),
                        "require_first_use_consent": require_consent,
                    },
                    metadata={"round": proposal.round},
                )
            )
            if decision.action == PolicyAction.DENY:
                block_reason = decision.reason_code or "policy_denied"
            elif decision.action == PolicyAction.ASK:
                if decision.pending_request is None:
                    block_reason = "policy_pending_decision_missing"
                else:
                    pending = PendingDecisionStore(runs_dir=self._store.runs_dir).create(
                        decision.pending_request
                    )
                    pending_decision_id = pending.decision_id
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.POLICY_DECISION_CREATED,
                        status=EventStatus.AWAITING_USER_DECISION,
                        payload=pending.audit_projection(),
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        parent_event_ids=[parent_event_id],
                        writer=self._writer,
                    )
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.RUN_STATUS_CHANGED,
                        status=EventStatus.AWAITING_USER_DECISION,
                        payload={
                            "status_change": "awaiting_user_decision",
                            "reason_code": pending.reason_code,
                            "decision_id": pending.decision_id,
                            "phase": pending.phase.value,
                            "member_id": member["id"],
                            "round": proposal.round,
                        },
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        parent_event_ids=[parent_event_id],
                        writer=self._writer,
                    )
                    self._control.enter_awaiting_user_decision(
                        question_code=pending.reason_code,
                        member_id=member["id"],
                        round=proposal.round,
                    )
                    outcome = await self._await_policy_decision(pending.decision_id)
                    self._control.exit_awaiting_user_decision()
                    if outcome != "approved":
                        self._store.append_event(
                            self._meta.id,
                            type=EventType.POLICY_DECISION_REJECTED,
                            status=EventStatus.SKIPPED,
                            payload={
                                "decision_id": pending.decision_id,
                                "phase": pending.phase.value,
                                "reason_code": pending.reason_code,
                            },
                            member_id=member["id"],
                            member_snapshot=snapshot,
                            round=proposal.round,
                            parent_event_ids=[parent_event_id],
                            writer=self._writer,
                        )
                        block_reason = decision.reason_code or "policy_rejected"
                    else:
                        approval_mode = "policy_decision"
                        self._store.append_event(
                            self._meta.id,
                            type=EventType.POLICY_DECISION_APPROVED,
                            status=EventStatus.COMPLETED,
                            payload={
                                "decision_id": pending.decision_id,
                                "phase": pending.phase.value,
                                "reason_code": pending.reason_code,
                            },
                            member_id=member["id"],
                            member_snapshot=snapshot,
                            round=proposal.round,
                            parent_event_ids=[parent_event_id],
                            writer=self._writer,
                        )
        if block_reason is not None:
            blocked_payload = {
                **requested_payload,
                "reason": block_reason,
            }
            if pending_decision_id is not None:
                blocked_payload["pending_decision_id"] = pending_decision_id
            self._store.append_event(
                self._meta.id,
                type=EventType.TOOL_CALL_BLOCKED,
                status=EventStatus.BLOCKED,
                payload=blocked_payload,
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None

        self._store.append_event(
            self._meta.id,
            type=EventType.TOOL_CALL_APPROVED,
            status=EventStatus.COMPLETED,
            payload={
                **requested_payload,
                "approval_mode": approval_mode,
                **(
                    {"policy_decision_id": pending_decision_id}
                    if pending_decision_id is not None
                    else {}
                ),
            },
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )
        self._store.append_event(
            self._meta.id,
            type=EventType.TOOL_CALL_STARTED,
            status=EventStatus.RUNNING,
            payload=requested_payload,
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )
        try:
            result = await self._tool_gateway.invoke(request)  # type: ignore[union-attr]
        except RetryableToolError:
            self._store.append_event(
                self._meta.id,
                type=EventType.TOOL_CALL_FAILED,
                status=EventStatus.FAILED,
                payload={**requested_payload, "retryable": True},
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None
        except FatalToolError:
            self._store.append_event(
                self._meta.id,
                type=EventType.TOOL_CALL_FAILED,
                status=EventStatus.FAILED,
                payload={**requested_payload, "retryable": False},
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None
        except Exception as exc:
            self._store.append_event(
                self._meta.id,
                type=EventType.TOOL_CALL_FAILED,
                status=EventStatus.FAILED,
                payload={
                    **requested_payload,
                    "retryable": False,
                    "reason": "tool_gateway_exception",
                    # Bare class only — a tool-handler exception could carry raw
                    # tool output bytes; never surface its message in the log.
                    "detail": type(exc).__name__,
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None

        result_mismatch_reason = _tool_result_mismatch_reason(
            request=request,
            result=result,
        )
        if result_mismatch_reason is not None:
            self._store.append_event(
                self._meta.id,
                type=EventType.TOOL_CALL_FAILED,
                status=EventStatus.FAILED,
                payload={
                    **requested_payload,
                    "retryable": False,
                    "reason": result_mismatch_reason,
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None

        from errorta_tools.result_store import ToolResultStore

        ToolResultStore(root=council_root() / "tool-results").write(
            run_id=self._meta.id,
            result=result,
        )
        result_payload = {
            **requested_payload,
            **result.audit_projection(),
            "result_ref": {
                "store": "tool_results_v1",
                "run_id": self._meta.id,
                "call_id": result.call_id,
            },
        }
        completed_ev = self._store.append_event(
            self._meta.id,
            type=EventType.TOOL_CALL_COMPLETED,
            status=EventStatus.COMPLETED,
            payload=result_payload,
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )
        # F078: capture the fetched source NOW, while the final URL is still in
        # hand. The audit projection + side store keep only hashes, so the URL
        # is unrecoverable later. The credibility finalizer reads these events
        # (replay-safe) to build the source set; a citation that doesn't match a
        # captured source is dropped (the marquee "fetched-or-it-isn't-evidence"
        # guarantee). Only in Credibility mode and only for web_fetch.
        if request.tool_id == "web_fetch" and self._is_credibility_run():
            final_url = str(dict(getattr(result, "provenance", {}) or {}).get("final_url") or "")
            if final_url:
                self._store.append_event(
                    self._meta.id,
                    type=EventType.CREDIBILITY_SOURCE_CAPTURED,
                    status=EventStatus.COMPLETED,
                    payload={
                        "url": final_url,
                        "content_sha256": result.content_sha256,
                        "fetched_at": result.produced_at,
                        "tool_call_event_id": getattr(completed_ev, "id", None)
                        or result.call_id,
                        "egress_class": result.egress_class,
                    },
                    member_id=member["id"],
                    round=proposal.round,
                    writer=self._writer,
                )
        return None

    def _is_credibility_run(self) -> bool:
        snap = self._meta.room_snapshot or {}
        if dict(snap.get("finalization_policy") or {}).get("mode") == "credibility_report":
            return True
        return str(dict(snap.get("topology") or {}).get("kind") or "") == "credibility"

    def _stop_on_member_failure(self) -> bool:
        """Whether ONE member-turn failure (timeout / gateway error) aborts the
        run. Credibility runs are resilient — a single flaky member (e.g. a CLI
        provider) is skipped and the run continues so the finalizer can still
        report on the claims/reviews already gathered. Other topologies honor
        stop_behavior."""
        if self._is_credibility_run():
            return False
        return self._policy.stop_behavior == "stop"

    async def _run_forced_credibility_research(self) -> None:
        """F078: the system itself runs web_search + web_fetch on the user prompt
        at run start, so a Credibility run ALWAYS has fetched sources to cite —
        independent of whether the models emit tool-call JSON. Captured sources
        feed both the finalizer (CREDIBILITY_SOURCE_CAPTURED) and each member's
        context (research findings)."""
        if self._tool_gateway is None:
            return
        query = str(getattr(self._meta, "prompt", "") or "").strip()
        if not query:
            return
        import re as _re

        from errorta_council.paths import council_root
        from errorta_tools.gateway import ToolCallRequest
        from errorta_tools.result_store import ToolResultStore

        pol = self._tool_policy().to_dict()
        cred = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
        max_fetches = max(1, int(cred.get("max_fetches_per_member") or 4))
        results_store = ToolResultStore(root=council_root() / "tool-results")

        def _req(tool_id: str, args: dict, n: int) -> Any:
            return ToolCallRequest(
                call_id=f"forced-{tool_id}-{n}", run_id=self._meta.id,
                turn_id="forced-research", member_id="__system__",
                tool_id=tool_id, arguments=args, context_id="forced-research",
                metadata={"tool_policy": pol},
            )

        self._store.append_event(
            self._meta.id, type=EventType.CREDIBILITY_RESEARCH_STARTED,
            status=EventStatus.COMPLETED, payload={"query_len": len(query)},
            writer=self._writer,
        )
        try:
            search = await self._tool_gateway.invoke(_req("web_search", {"query": query}, 0))
        except Exception:
            self._store.append_event(
                self._meta.id, type=EventType.CREDIBILITY_RESEARCH_COMPLETED,
                status=EventStatus.COMPLETED,
                payload={"source_count": 0, "search_failed": True}, writer=self._writer,
            )
            return

        urls: list[str] = []
        seen: set[str] = set()
        for u in _re.findall(r"https?://[^\s\]\)]+", str(getattr(search, "content", "") or "")):
            u = u.rstrip(".,);")
            if u not in seen:
                seen.add(u)
                urls.append(u)

        findings: list[str] = []
        for i, url in enumerate(urls[:max_fetches]):
            try:
                fetched = await self._tool_gateway.invoke(_req("web_fetch", {"url": url}, i + 1))
            except Exception:
                continue
            final_url = str(dict(getattr(fetched, "provenance", {}) or {}).get("final_url") or url)
            try:
                results_store.write(run_id=self._meta.id, result=fetched)
            except Exception:
                pass
            self._store.append_event(
                self._meta.id, type=EventType.CREDIBILITY_SOURCE_CAPTURED,
                status=EventStatus.COMPLETED,
                payload={
                    "url": final_url,
                    "content_sha256": getattr(fetched, "content_sha256", ""),
                    "fetched_at": getattr(fetched, "produced_at", ""),
                    "tool_call_event_id": f"forced-web_fetch-{i + 1}",
                    "egress_class": getattr(fetched, "egress_class", "remote"),
                },
                writer=self._writer,
            )
            excerpt = " ".join(str(getattr(fetched, "content", "") or "").split())[:280]
            findings.append(f"- {final_url}\n  {excerpt}")

        if findings:
            self._credibility_findings = "\n".join(findings)
        self._store.append_event(
            self._meta.id, type=EventType.CREDIBILITY_RESEARCH_COMPLETED,
            status=EventStatus.COMPLETED, payload={"source_count": len(findings)},
            writer=self._writer,
        )

    def _credibility_messages(
        self, messages: list[Any], member: dict | None = None, proposal: Any = None,
    ) -> list[Any]:
        """Phase-aware credibility instruction injected into a member's turn.

        Round 1 = CLAIM phase: post a JSON claim packet citing the fetched
        sources. Round >= 2 = CREDIDATION phase: review peers' claims against
        their cited sources (peer review is REQUIRED for admission). This is how
        the round-robin run actually produces the reviews the admission gate
        needs — claims are never admitted on a fetched source alone."""
        if not self._is_credibility_run():
            return messages
        round_n = int(getattr(proposal, "round", 1) or 1)
        mid = str((member or {}).get("id") or "")

        if round_n <= 1:
            # F084: a designated steelman argues its assigned topic as forcefully
            # as possible and MAY construct supporting evidence/citations — it is
            # NOT bound to the fetched sources. Its claims are quarantined +
            # labeled unverified downstream (never admitted, never source-
            # supported, never promoted to the corpus).
            if mid and self._member_is_steelman(mid):
                topic = self._steelman_topic(mid)
                target = (
                    f'that the following is TRUE: "{topic}"'
                    if topic
                    else "your assigned position (in your system instructions)"
                )
                findings_block = (
                    f"For reference, Errorta searched the web and fetched these "
                    f"sources for the question:\n\n{self._credibility_findings}\n\n"
                    if self._credibility_findings
                    else ""
                )
                instruction = (
                    "CREDIBILITY MODE — STEELMAN ADVOCATE. You are the designated "
                    f"steelman. Argue as forcefully and persuasively as possible "
                    f"{target}. Mount the strongest possible version of this case. "
                    "You MAY construct supporting evidence, examples, and "
                    "citations even if the fetched sources do not establish it — "
                    "your job is the best version of this position, not a hedged "
                    "one. Argue in your own voice; do not break character or "
                    "disclaim.\n\n"
                    f"{findings_block}"
                    "Reply with ONE JSON object:\n"
                    '{"answer_fragment": "<your position stated in 1-2 natural '
                    'sentences, in your own voice>", "claims": [{"claim_id": "c1", '
                    '"text": "<a substantive point you are arguing, in plain '
                    'language>", "kind": "factual", "source_ids": ["<a URL '
                    'supporting your point>"]}]}'
                )
                return list(messages) + [{"role": "user", "content": instruction}]
            if not self._credibility_findings:
                return messages
            is_opponent = mid and mid == self._credibility_opponent_id()
            if is_opponent:
                # F081: the assigned opponent must steelman the OPPOSING case so
                # a real contest exists even in a monoculture. Still sourced +
                # entailment-gated like any other claim.
                stance = (
                    "CREDIBILITY MODE — OPENING ARGUMENT (DEVIL'S ADVOCATE). You "
                    "are assigned to argue the STRONGEST OPPOSING case to the "
                    "majority view, regardless of your own opinion — give the "
                    "best version of the other side (a steelman), not a weak one."
                )
            else:
                stance = (
                    "CREDIBILITY MODE — OPENING ARGUMENT. Make your ACTUAL "
                    "argument on the question, in your own voice. Take a position."
                )
            instruction = (
                f"{stance} You are in a meeting with the other members. Errorta "
                "already searched the web and fetched these sources for the "
                f"question:\n\n{self._credibility_findings}\n\n"
                "Back each point by quoting or pointing to one of the fetched "
                "sources above. Do NOT describe the task, do NOT talk about "
                "'the sources' in the abstract, and do NOT hedge — argue the "
                "substance.\nReply with ONE JSON object:\n"
                '{"answer_fragment": "<your position stated in 1-2 natural '
                'sentences, in your own voice>", "claims": [{"claim_id": "c1", '
                '"text": "<a substantive point you are arguing, in plain '
                'language>", "kind": "factual", "source_ids": ["<one fetched URL '
                'above>"]}]}\n'
                "Cite ONLY the fetched URLs above; never invent a URL."
            )
            return list(messages) + [{"role": "user", "content": instruction}]

        # Credidation phase: react to the OTHER members' arguments.
        peer = self._credibility_peer_claims(mid)
        if not peer:
            return messages
        listing = "\n".join(
            f'- {c["claim_id"]}: "{c["text"]}"  (cited: {c["cite"]})'
            for c in peer
        )
        instruction = (
            "CREDIBILITY MODE — DISCUSSION. You are still in the meeting. React "
            "to the other members' arguments in your own voice: name them, say "
            "what you agree with and what you push back on, and quote the sources "
            "to make your case — like a real person arguing a point. Whenever you "
            "quote or lean on a source, NAME THE WEBSITE it came from in "
            "parentheses, e.g. (samwoolfe.com) — every claim below lists the URL "
            "it cited, so use its domain. THEN record your structured assessment "
            "of each of their claims so the room can tally it.\n"
            "Reply with ONE JSON object:\n"
            '{"comment": "<2-4 sentences reacting to the other members by name, '
            "quoting sources and naming the website each came from in parentheses "
            'like (samwoolfe.com), in your own voice>", "reviews": [{"claim_id": '
            '"<id>", "status": '
            '"verified|partially_supported|unsupported|contradicted", '
            '"support_quality": "direct|indirect|does_not_support", "reason": '
            '"<short>"}]}\n'
            "Mark verified only if the cited source actually supports the claim.\n\n"
            f"The other members' claims to weigh in on:\n{listing}"
        )
        return list(messages) + [{"role": "user", "content": instruction}]

    def _credibility_peer_claims(self, reviewer_id: str) -> list[dict[str, str]]:
        """Parse claims posted so far by OTHER members (for the credidation
        phase prompt). Reads the run transcript; JSON packets + digest fallback."""
        from errorta_council.credibility import parse_claim_packet, parse_digest_claims

        try:
            _, events = self._store.read_run(self._meta.id)
        except Exception:
            return []
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        steelmen = self._credibility_steelman_member_ids()
        for ev in events:
            if getattr(ev, "type", None) != EventType.MEMBER_MESSAGE:
                continue
            mid = str(getattr(ev, "member_id", "") or "")
            if not mid or mid == reviewer_id:
                continue
            content = str(dict(getattr(ev, "payload", {}) or {}).get("content") or "")
            # Defense in depth: a peer's message is raw, model-authored text and
            # any one of them can be off-shape. A parse failure here must never
            # crash the run — skip the message and keep building the prompt.
            try:
                pkt = parse_claim_packet(mid, content)
                claims = list(pkt.claims) if pkt else parse_digest_claims(mid, content)
            except Exception:
                continue
            # F084: mark a steelman's claims UNVERIFIED in the listing so peers
            # (and the leader) don't treat its constructed evidence as real.
            is_steelman = mid in steelmen
            for c in claims:
                cid = c.claim_id if c.claim_id.startswith(f"{mid}:") else f"{mid}:{c.claim_id}"
                if cid in seen:
                    continue
                seen.add(cid)
                text = c.text[:200]
                cite = (c.source_ids[0] if c.source_ids else "(none)")
                if is_steelman:
                    text = f"[STEELMAN — UNVERIFIED, may be constructed] {text}"
                    cite = f"{cite} (unverified)"
                out.append({"claim_id": cid, "text": text, "cite": cite})
        return out[:12]

    @staticmethod
    def _tool_block_reason(tool_id: str, policy: ToolPolicy) -> str | None:
        if tool_id not in {
            "web_fetch",
            "web_search",
            "code_read",
            "code_write",
            "code_exec",
        }:
            return "tool_unknown"
        if tool_id not in policy.enabled_tool_ids():
            return "tool_not_granted"
        return None

    def _tool_consent_approved(self, tool_id: str) -> bool:
        from errorta_policy import PendingDecisionStore

        store = PendingDecisionStore(runs_dir=self._store.runs_dir)
        try:
            decisions = store.list(self._meta.id, state="approved")
        except ValueError:
            return False
        consent_key = f"tool_consent:{tool_id}"
        for decision in decisions:
            if any(w.key == consent_key for w in decision.applied_state_writes):
                return True
        return False

    async def _await_policy_decision(self, decision_id: str) -> str:
        """Poll a pending policy decision until approve/reject/cancel."""
        from errorta_policy import PendingDecisionNotFound, PendingDecisionStore

        store = PendingDecisionStore(runs_dir=self._store.runs_dir)
        while True:
            if self._control.is_cancelled():
                return "rejected"
            try:
                decision = store.get(self._meta.id, decision_id)
            except PendingDecisionNotFound:
                return "rejected"
            if decision.state in {"approved", "rejected", "expired"}:
                return "approved" if decision.state == "approved" else "rejected"
            await asyncio.sleep(0.05)

    def _child_run_policy(self) -> dict[str, Any]:
        raw = (self._meta.room_snapshot or {}).get("child_run_policy") or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _child_runs_started(self) -> int:
        _, events = self._store.read_run(self._meta.id)
        return sum(1 for e in events if e.type == EventType.CHILD_RUN_STARTED)

    async def _maybe_handle_child_task(
        self,
        *,
        content: str,
        member: dict[str, Any],
        snapshot: MemberSnapshot,
        proposal: TurnProposal,
        parent_event_id: str,
    ) -> RunMeta | None:
        policy = self._child_run_policy()
        if not bool(policy.get("enabled")):
            return None
        spec = _extract_child_task(content)
        if spec is None:
            return None
        max_children = policy.get("max_children_per_run")
        if max_children is not None and self._child_runs_started() >= int(max_children):
            self._store.append_event(
                self._meta.id,
                type=EventType.CHILD_RUN_FAILED,
                status=EventStatus.FAILED,
                payload={
                    "reason": "child_run_budget_exhausted",
                    "task_kind": spec["task_kind"],
                    "title": spec["title"],
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None
        if spec["worker_kind"] != "scripted":
            self._store.append_event(
                self._meta.id,
                type=EventType.CHILD_RUN_FAILED,
                status=EventStatus.FAILED,
                payload={
                    "reason": "child_worker_unavailable",
                    "worker_kind": spec["worker_kind"],
                    "task_kind": spec["task_kind"],
                    "title": spec["title"],
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None

        from errorta_council.children import (
            AsyncInbox,
            ChildRunController,
            ChildRunStore,
        )
        from errorta_policy import PolicyAction, PolicyContext, PolicyEngine, PolicyPhase

        creation_decision = PolicyEngine().evaluate(
            PolicyContext(
                phase=PolicyPhase.CHILD_RUN,
                run_id=self._meta.id,
                room_id=self._meta.room_id,
                member_id=str(member["id"]),
                requester={"type": "council_member", "member_id": str(member["id"])},
                safe_request={
                    "task_kind": spec["task_kind"],
                    "title": spec["title"],
                    "worker_kind": spec["worker_kind"],
                },
                policy={"action": str(policy.get("creation_policy") or "allow")},
            )
        )
        if creation_decision.action == PolicyAction.DENY:
            self._store.append_event(
                self._meta.id,
                type=EventType.CHILD_RUN_FAILED,
                status=EventStatus.FAILED,
                payload={
                    "reason": creation_decision.reason_code or "child_run_denied",
                    "task_kind": spec["task_kind"],
                    "title": spec["title"],
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None

        controller = ChildRunController(
            store=ChildRunStore(runs_dir=self._store.runs_dir),
            inbox=AsyncInbox(runs_dir=self._store.runs_dir),
        )
        record = controller.create(
            parent_run_id=self._meta.id,
            member_id=str(member["id"]),
            task_kind=spec["task_kind"],
            title=spec["title"],
            prompt=spec["prompt"],
            worker_kind=spec["worker_kind"],
            metadata={"round": proposal.round},
        )
        record = controller.start(record)
        self._store.append_event(
            self._meta.id,
            type=EventType.CHILD_RUN_STARTED,
            status=EventStatus.RUNNING,
            payload=record.event_projection(),
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )
        try:
            completed, summary_ref = controller.run_scripted(
                record=record,
                prompt=spec["prompt"],
                result=spec["result"],
                artifact_refs=list(spec["artifact_refs"]),
            )
        except Exception as exc:
            failed = ChildRunStore(runs_dir=self._store.runs_dir).mark_failed(
                record,
                reason_code="child_worker_failed",
                detail=type(exc).__name__,
            )
            self._store.append_event(
                self._meta.id,
                type=EventType.CHILD_RUN_FAILED,
                status=EventStatus.FAILED,
                payload=failed.event_projection(),
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None

        self._store.append_event(
            self._meta.id,
            type=EventType.CHILD_RUN_INBOX_MESSAGE,
            status=EventStatus.COMPLETED,
            payload={
                "parent_run_id": self._meta.id,
                "child_run_id": completed.child_run_id,
                "message_id": summary_ref["message_id"],
                "message_kind": "summary",
                "content_sha256": summary_ref["content_sha256"],
                "payload_bytes": summary_ref["payload_bytes"],
            },
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )
        output_decision = PolicyEngine().evaluate(
            PolicyContext(
                phase=PolicyPhase.CHILD_RUN_OUTPUT,
                run_id=self._meta.id,
                room_id=self._meta.room_id,
                member_id=str(member["id"]),
                requester={"type": "child_run", "child_run_id": completed.child_run_id},
                safe_request={
                    "child_run_id": completed.child_run_id,
                    "task_kind": completed.task_kind,
                    "content_sha256": summary_ref["content_sha256"],
                    "payload_bytes": summary_ref["payload_bytes"],
                },
                policy={
                    "action": str(policy.get("output_policy") or "allow"),
                    "reason_code": "child_output_policy_denied",
                },
            )
        )
        if output_decision.action == PolicyAction.DENY:
            failed = ChildRunStore(runs_dir=self._store.runs_dir).mark_failed(
                completed,
                reason_code=output_decision.reason_code or "child_output_policy_denied",
            )
            self._store.append_event(
                self._meta.id,
                type=EventType.CHILD_RUN_FAILED,
                status=EventStatus.FAILED,
                payload=failed.event_projection(),
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                parent_event_ids=[parent_event_id],
                writer=self._writer,
            )
            return None
        completed = ChildRunStore(runs_dir=self._store.runs_dir).mark_completed(
            completed,
            summary_ref=summary_ref,
            artifact_refs=list(spec["artifact_refs"]),
        )
        self._store.append_event(
            self._meta.id,
            type=EventType.CHILD_RUN_COMPLETED,
            status=EventStatus.COMPLETED,
            payload=completed.event_projection(),
            member_id=member["id"],
            member_snapshot=snapshot,
            round=proposal.round,
            parent_event_ids=[parent_event_id],
            writer=self._writer,
        )
        return None

    def _cancel_outstanding_child_runs(self) -> None:
        from errorta_council.children import ChildRunStore

        store = ChildRunStore(runs_dir=self._store.runs_dir)
        for record in store.cancel_outstanding(self._meta.id):
            self._store.append_event(
                self._meta.id,
                type=EventType.CHILD_RUN_CANCELLED,
                status=EventStatus.CANCELLED,
                payload=record.event_projection(),
                writer=self._writer,
            )

    def _steward_packets(self) -> StewardPacketStore:
        return StewardPacketStore(runs_dir=self._store.runs_dir)

    # --- F037 expert callouts ----------------------------------------------
    _REMOTE_PROVIDER_KINDS = frozenset({
        "remote", "anthropic", "openai", "google", "custom",
    })

    def _callout_queue(self) -> CalloutQueue:
        return CalloutQueue(runs_dir=self._store.runs_dir, run_id=self._meta.id)

    def _callout_policy(self):
        return resolve_callout_policy(self._meta.room_snapshot or {})

    _LOCAL_PROVIDER_KINDS = frozenset({"local", "fake"})

    def _target_route_kind(self, target) -> str | None:
        """Resolve a roster target's route kind, failing safe to ``remote``.

        Only a ``fake.`` route or an explicit local/fake ``provider_kind`` is
        treated as local. Anything else — including the ``unknown`` default —
        is treated as ``remote`` so a mislabeled remote target cannot slip past
        the remote-callout budget cap or the first-remote-callout approval
        gate. (The earlier version returned ``local`` for ``unknown``, which
        let a genuinely-remote route bypass both.)
        """
        route = str(target.gateway_route_id or "")
        if not route:
            return None
        if route.startswith("fake."):
            return "local"
        if target.provider_kind in self._LOCAL_PROVIDER_KINDS:
            return "local"
        return "remote"

    def _callout_counts(self) -> tuple[int, int]:
        """(callouts_started_this_run, remote_callouts_started_this_run)."""
        _, events = self._store.read_run(self._meta.id)
        total = 0
        remote = 0
        for e in events:
            if e.type == EventType.CALLOUT_STARTED:
                total += 1
                if (e.payload or {}).get("remote"):
                    remote += 1
        return total, remote

    def _callout_member_dict(self, target) -> dict[str, Any]:
        provider = (
            "fake"
            if str(target.gateway_route_id or "").startswith("fake.")
            else ("local" if target.provider_kind == "local" else target.provider_kind)
        )
        gen = dict(target.generation or {})
        return {
            "id": target.id,
            "name": target.name or target.id,
            "role": target.role or "expert",
            "enabled": True,
            "gateway_route_id": target.gateway_route_id,
            "provider": provider,
            "provider_kind": target.provider_kind,
            "provider_display": target.provider_display,
            "model": target.model_display,
            "model_display": target.model_display,
            "context_access": target.context_access,
            "transcript_access": target.transcript_access,
            "system_prompt": target.system_prompt,
            "max_output_tokens": (target.turn_limits or {}).get("max_output_tokens"),
            "temperature": gen.get("temperature", 0.2),
            "_is_callout_target": True,
        }

    async def _await_callout_approval(self, callout_id: str) -> str:
        """Poll the callout record for an approve/reject decision.

        Returns "approved" or "rejected". Treats run cancellation as a
        rejection so the await never hangs a cancelled run.
        """
        queue = self._callout_queue()
        while True:
            if self._control.is_cancelled():
                return "rejected"
            rec = queue.get(callout_id)
            if rec is not None and rec.approval in {"approved", "rejected"}:
                return rec.approval
            await asyncio.sleep(0.05)

    def _apply_callout_disposition(self, disposition: str) -> RunMeta | None:
        """Apply on_callout_rejected / on_callout_failed: continue|pause|stop."""
        if disposition == "stop":
            return self._emit_terminal(ReasonCode.LIMITS_EXHAUSTED.value, "failed")
        if disposition == "pause":
            self._store.merge_meta_fields(self._meta.id, status="paused", paused_at=_utcnow())
        return None

    async def _drain_callouts(self) -> RunMeta | None:
        """Process queued user callout requests before the next ordinary turn.

        Returns a terminal RunMeta when a callout disposition stops the run,
        else None. All events are emitted under the scheduler's writer token
        (invariant 2). All provider calls go through the existing gateway
        (invariant 3).
        """
        queue = self._callout_queue()
        pending = queue.requested()
        if not pending:
            return None
        policy = self._callout_policy()
        for rec in pending:
            target = find_target(self._meta.room_snapshot or {}, rec.target_id)
            self._store.append_event(
                self._meta.id,
                type=EventType.CALLOUT_REQUESTED,
                status=EventStatus.PENDING,
                payload={
                    "callout_id": rec.callout_id,
                    "requested_by": rec.requested_by,
                    "target_id": rec.target_id,
                    "reason_code": rec.reason_code,
                    "question": rec.question,
                },
                writer=self._writer,
            )
            done, remote_done = self._callout_counts()
            decision = evaluate_callout(
                policy=policy,
                target=target,
                requester_type=str(rec.requested_by.get("type") or "user"),
                callouts_done=done,
                remote_callouts_done=remote_done,
                route_kind=self._target_route_kind(target) if target else None,
                run_terminal=self._control.is_cancelled(),
            )
            if decision.rejected:
                self._reject_callout(rec, decision.reason_code or "rejected")
                terminal = self._apply_callout_disposition(policy.on_callout_rejected)
                if terminal is not None:
                    return terminal
                continue
            if decision.needs_approval:
                queue.update(rec.callout_id, state="awaiting_approval")
                self._store.append_event(
                    self._meta.id,
                    type=EventType.CALLOUT_APPROVAL_REQUIRED,
                    status=EventStatus.AWAITING_USER_DECISION,
                    payload={"callout_id": rec.callout_id, "target_id": rec.target_id},
                    writer=self._writer,
                )
                self._control.enter_awaiting_user_decision(
                    question_code="callout_approval"
                )
                outcome = await self._await_callout_approval(rec.callout_id)
                self._control.exit_awaiting_user_decision()
                if outcome != "approved":
                    self._reject_callout(rec, "approval_rejected")
                    terminal = self._apply_callout_disposition(policy.on_callout_rejected)
                    if terminal is not None:
                        return terminal
                    continue
            # Admitted (auto) or approved.
            self._store.append_event(
                self._meta.id,
                type=EventType.CALLOUT_APPROVED,
                status=EventStatus.COMPLETED,
                payload={"callout_id": rec.callout_id, "target_id": rec.target_id},
                writer=self._writer,
            )
            terminal = await self._execute_callout(rec, target, policy)
            if terminal is not None:
                return terminal
        return None

    def _reject_callout(self, rec: CalloutRecord, reason_code: str) -> None:
        self._callout_queue().update(rec.callout_id, state="rejected", reject_reason=reason_code)
        self._store.append_event(
            self._meta.id,
            type=EventType.CALLOUT_REJECTED,
            status=EventStatus.SKIPPED,
            payload={
                "callout_id": rec.callout_id,
                "target_id": rec.target_id,
                "reason_code": reason_code,
            },
            writer=self._writer,
        )

    async def _execute_callout(self, rec: CalloutRecord, target, policy) -> RunMeta | None:
        queue = self._callout_queue()
        route_kind = self._target_route_kind(target)
        advisory = bool((target.callout or {}).get("advisory", True))
        member = self._callout_member_dict(target)
        snapshot = self._member_snapshot(member)
        queue.update(rec.callout_id, state="started", advisory=advisory)
        self._store.append_event(
            self._meta.id,
            type=EventType.CALLOUT_STARTED,
            status=EventStatus.RUNNING,
            payload={
                "callout_id": rec.callout_id,
                "target_id": rec.target_id,
                "advisory": advisory,
                "remote": route_kind == "remote",
            },
            member_id=member["id"],
            member_snapshot=snapshot,
            writer=self._writer,
        )
        try:
            _, prior_events = self._store.read_run(self._meta.id)
            context = await self._ctx.build(
                run_meta=self._meta, member=member, transcript=prior_events
            )
            if context.get("blocked"):
                raise RuntimeError(str(context.get("blocked_reason") or "context_blocked"))
            from errorta_council.gateway_local import LocalCouncilModelRequest
            request = LocalCouncilModelRequest(
                role=member["role"],
                route_id=str(member["gateway_route_id"]),
                provider=member["provider"],
                model=member.get("model", ""),
                messages=context["messages"],
                max_output_tokens=self._max_output_tokens_for(member),
                temperature=member.get("temperature", 0.2),
                timeout_seconds=self._per_turn_timeout_for(member),
                metadata={
                    "context_id": context["context_id"],
                    "member_id": member["id"],
                    "destination_scope": str(context.get("destination_scope") or "local"),
                    "egress_class": str(context.get("egress_class") or "local"),
                    "council_callout": True,
                    "callout_id": rec.callout_id,
                    "target_id": rec.target_id,
                    "requested_by": rec.requested_by,
                    "reason_code": rec.reason_code,
                },
                cache_hints=list(context.get("cache_hints") or []),
            )
            result = await asyncio.wait_for(
                self._gateway.call(request),
                timeout=self._per_turn_timeout_for(member),
            )
        except Exception as exc:
            queue.update(rec.callout_id, state="failed")
            self._store.append_event(
                self._meta.id,
                type=EventType.CALLOUT_FAILED,
                status=EventStatus.FAILED,
                payload={
                    "callout_id": rec.callout_id,
                    "target_id": rec.target_id,
                    "reason": "callout_failed",
                    "detail": _exc_detail(exc),
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                writer=self._writer,
            )
            return self._apply_callout_disposition(policy.on_callout_failed)
        answer = self._store.append_event(
            self._meta.id,
            type=EventType.MEMBER_MESSAGE,
            status=EventStatus.COMPLETED,
            payload={
                "content": result.content,
                "provider": result.provider,
                "model": result.model,
                "duration_ms": result.duration_ms,
                "is_thinking_burn": result.is_thinking_burn,
                "is_callout": True,
                "callout_id": rec.callout_id,
                "target_id": rec.target_id,
                "advisory": advisory,
                "requested_by": rec.requested_by,
                "reason_code": rec.reason_code,
            },
            member_id=member["id"],
            member_snapshot=snapshot,
            usage={
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
            writer=self._writer,
        )
        answer_event_id = getattr(answer, "id", None)
        queue.update(rec.callout_id, state="completed", answer_event_id=answer_event_id)
        self._store.append_event(
            self._meta.id,
            type=EventType.CALLOUT_COMPLETED,
            status=EventStatus.COMPLETED,
            payload={
                "callout_id": rec.callout_id,
                "target_id": rec.target_id,
                "advisory": advisory,
                "answer_event_id": answer_event_id,
            },
            member_id=member["id"],
            member_snapshot=snapshot,
            writer=self._writer,
        )
        return None

    def _is_finalizer_member(self, member: dict) -> bool:
        finalizer = dict(
            (self._meta.room_snapshot or {}).get("finalization_policy") or {}
        ).get("finalizer_member_id")
        if finalizer and str(finalizer) == str(member.get("id")):
            return True
        return "finalizer" in str(member.get("role") or "").lower()

    def _member_for_context(self, member: dict) -> dict:
        if str(member.get("id")) not in self._dialect_forced_prose:
            return member
        clone = dict(member)
        clone["force_deliberation_dialect"] = "prose"
        return clone

    # Default per-turn output budget when a member sets no explicit limit.
    # Sent to Ollama as ``num_predict``. Reasoning ("thinking") models spend
    # this budget on their hidden reasoning trace *before* the visible answer,
    # so a low value makes them emit a thinking-burn with no answer. Non-reasoning
    # models get 2048; known reasoning families default higher so they finish
    # thinking AND answer. Either way, a per-member turn_limits.max_output_tokens
    # always wins.
    DEFAULT_MAX_OUTPUT_TOKENS = 2048
    REASONING_MAX_OUTPUT_TOKENS = 8192
    # Reasoning models generate a long hidden trace before answering, so they
    # need more wall-clock than a normal turn. Give them at least this many
    # seconds regardless of the (smaller) policy default, or the bigger budget
    # just trades a thinking-burn for a timeout.
    REASONING_TIMEOUT_FLOOR_SECONDS = 300

    def _base_output_tokens_for(self, member: dict) -> int:
        explicit = member.get("max_output_tokens")
        if explicit:
            return int(explicit)
        if _is_reasoning_model(str(member.get("model", ""))):
            return self.REASONING_MAX_OUTPUT_TOKENS
        return self.DEFAULT_MAX_OUTPUT_TOKENS

    def _per_turn_timeout_for(self, member: dict) -> int:
        base = self._policy.per_turn_timeout_seconds
        if _is_reasoning_model(str(member.get("model", ""))):
            return max(base, self.REASONING_TIMEOUT_FLOOR_SECONDS)
        return base

    def _max_output_tokens_for(self, member: dict) -> int:
        base = self._base_output_tokens_for(member)
        efficiency = self._context_efficiency()
        cap = efficiency.intermediate_max_output_tokens
        # Apply cap for WS2 (telegraphic style) OR WS4 (digest_v1 dialect) —
        # WS2 is the primary owner; the cap was mistakenly gated on WS4 only.
        if (
            cap
            and not self._is_finalizer_member(member)
            and str(member.get("id")) not in self._dialect_forced_prose
            and (
                efficiency.deliberation_style == "telegraphic"
                or efficiency.deliberation_dialect == "digest_v1"
            )
        ):
            return min(base, int(cap))
        return base

    def _digest_enabled_for(self, member: dict) -> bool:
        # Credibility mode members emit structured JSON claim packets / reviews
        # (driven by the injected credibility instruction), which conflicts with
        # the digest_v1 dialect — leaving digest on caused "digest_parse_failed"
        # downgrades and raw JSON in the transcript. Force it off here.
        if self._is_credibility_run():
            return False
        efficiency = self._context_efficiency()
        return (
            efficiency.deliberation_dialect == "digest_v1"
            and not self._is_finalizer_member(member)
            and str(member.get("id")) not in self._dialect_forced_prose
        )

    def _known_citation_ids(self) -> set[str]:
        try:
            registry = CitationRegistry(
                path=citation_registry_path(
                    self._meta.id, council_root=council_root(),
                )
            )
            return {entry.citation_id for entry in registry.list()}
        except Exception:
            return set()

    def _enabled_member_ids_for_round(self) -> list[str]:
        # F080: the neutral judge never emits a MEMBER_MESSAGE, so a steward
        # whose round-completion check expects every id to have spoken would
        # wait forever. Exclude the judge here (mirrors _build_run_state).
        judge_id = self._judge_member_id()
        members = [
            m for m in self._meta.room_snapshot.get("members", [])
            if m.get("enabled", True)
            and not (judge_id and str(m.get("id")) == str(judge_id))
        ]
        enabled_ids = {str(m["id"]) for m in members if m.get("id")}
        topology = dict(self._meta.room_snapshot.get("topology") or {})
        order = [str(x) for x in topology.get("speaker_order") or []]
        ordered = [mid for mid in order if mid in enabled_ids]
        ordered.extend(
            str(m["id"]) for m in members
            if m.get("id") and str(m["id"]) not in set(ordered)
        )
        return ordered

    def _round_complete_for_steward(self, round_number: int) -> bool:
        member_ids = self._enabled_member_ids_for_round()
        if not member_ids:
            return False
        counters = self._read_counters()
        return all(
            counters.completed_messages_by_member.get(mid, 0) >= round_number
            for mid in member_ids
        )

    def _steward_packet_already_covers(
        self,
        *,
        events: list,
        to_sequence: int,
    ) -> bool:
        for ev in reversed(events):
            if ev.type != EventType.STEWARD_PACKET_CREATED:
                continue
            coverage = dict((ev.payload or {}).get("coverage") or {})
            if int(coverage.get("to_sequence") or 0) >= to_sequence:
                return True
        return False

    def _maybe_build_steward_packet(self, *, proposal: TurnProposal) -> RunMeta | None:
        policy = self._steward_policy()
        if not policy.enabled or policy.cadence != "after_each_round":
            return
        if not self._round_complete_for_steward(proposal.round):
            return
        _, events = self._store.read_run(self._meta.id)
        member_messages = [
            e for e in events
            if e.type == EventType.MEMBER_MESSAGE
            and not (e.payload or {}).get("is_callout")  # F037: skip callout turns
        ]
        if not member_messages:
            return
        to_sequence = max(e.sequence for e in member_messages)
        if self._steward_packet_already_covers(events=events, to_sequence=to_sequence):
            return

        self._store.append_event(
            self._meta.id,
            type=EventType.STEWARD_PACKET_REQUESTED,
            status=EventStatus.PENDING,
            payload={"cadence": policy.cadence, "round": proposal.round},
            round=proposal.round,
            writer=self._writer,
        )
        try:
            packet = build_deterministic_packet(
                run_meta=self._meta,
                events=events,
                created_by={
                    "mode": "deterministic",
                    "member_id": None,
                    "route_id": None,
                },
            )
            self._steward_packets().write(self._meta.id, packet)
        except FileExistsError:
            # Idempotent: an identical-content packet already exists for this
            # coverage. Not a failure — nothing to emit, nothing to stop.
            return None
        except Exception as exc:
            self._store.append_event(
                self._meta.id,
                type=EventType.STEWARD_PACKET_FAILED,
                status=EventStatus.FAILED,
                payload={
                    "reason": "packet_build_failed",
                    # Bare class only — packet building touches transcript
                    # content; an exception could echo a snippet.
                    "detail": type(exc).__name__,
                    "round": proposal.round,
                },
                round=proposal.round,
                writer=self._writer,
            )
            # F038: honor fallback_on_failure. Default "full_transcript" keeps
            # the run going (context builder falls back to raw transcript);
            # "stop" fails the run closed, as the policy opted into.
            if policy.fallback_on_failure == "stop":
                return self._emit_terminal("steward_packet_failed", "failed")
            return None

        self._store.append_event(
            self._meta.id,
            type=EventType.STEWARD_PACKET_CREATED,
            status=EventStatus.COMPLETED,
            payload={
                "packet_id": packet["packet_id"],
                "mode": packet["created_by"]["mode"],
                "created_by_member_id": packet["created_by"].get("member_id"),
                "created_by_route_id": packet["created_by"].get("route_id"),
                "coverage": packet["coverage"],
                "content_sha256": packet["content_sha256"],
                "estimated_tokens": packet["packet_stats"]["estimated_tokens"],
                "compression_ratio_estimate": packet["packet_stats"][
                    "compression_ratio_estimate"
                ],
                "source_event_ids": packet["coverage"]["source_event_ids"],
            },
            round=proposal.round,
            writer=self._writer,
        )

    def _maybe_emit_dialect_downgrade(
        self,
        *,
        member: dict,
        snapshot: MemberSnapshot,
        proposal: TurnProposal,
        context_id: str,
        warnings: list[str],
    ) -> None:
        member_id = str(member["id"])
        self._dialect_forced_prose.add(member_id)
        if member_id in self._dialect_downgrade_emitted:
            return
        self._dialect_downgrade_emitted.add(member_id)
        self._store.append_event(
            self._meta.id,
            type=EventType.DIALECT_DOWNGRADED,
            status=EventStatus.COMPLETED,
            payload={
                "member_id": member_id,
                "context_id": context_id,
                "from": "digest_v1",
                "to": "prose",
                "reason": "digest_parse_failed",
                "warnings": list(warnings),
            },
            member_id=member_id,
            member_snapshot=snapshot,
            round=proposal.round,
            writer=self._writer,
        )

    def _drain_pending_control_events(self) -> None:
        """Emit each queued control event under the scheduler's writer.

        The route layer pushes pause/resume/cancel/decision events into
        `meta.pending_control_events` whenever the scheduler held the
        writer at request time. Draining at the top of each loop ensures
        the audit trail reflects every user action even though only the
        scheduler thread can legitimately write to the event log.
        """
        pending = self._store.pop_pending_control_events(self._meta.id)
        for spec in pending:
            try:
                ev_type = EventType(spec["type"])
                ev_status = EventStatus(spec["status"])
            except (KeyError, ValueError):
                # Malformed spec — skip it but do not crash the run.
                continue
            payload = dict(spec.get("payload") or {})
            try:
                self._store.append_event(
                    self._meta.id,
                    type=ev_type,
                    status=ev_status,
                    payload=payload,
                    writer=self._writer,
                )
            except Exception:
                # If the event log rejected this (e.g. run already terminal),
                # drop the entry — the durable meta projection already
                # reflects the state change.
                continue

    def _member_snapshot(self, member: dict) -> MemberSnapshot:
        """Build a MemberSnapshot from a room-snapshot member dict.

        The audit aggregator branches on snapshot.locality to count fake vs
        local calls; an absent snapshot makes everything look local (P1).
        """
        provider = member.get("provider") or ""
        provider_kind = member.get("provider_kind") or ""
        route_id = member.get("gateway_route_id") or ""
        is_fake = (
            provider == "fake"
            or provider_kind == "fake"
            or route_id.startswith("fake.")
            or route_id.startswith("fake/")
        )
        locality = "fake" if is_fake else "local"
        return MemberSnapshot(
            member_id=str(member["id"]),
            name=str(member.get("name") or member["id"]),
            role=str(member.get("role") or "member"),
            provider_display=str(member.get("provider_display") or provider or "local"),
            model_display=str(
                member.get("model_display") or member.get("model") or ""
            ),
            locality=locality,
            context_access=str(member.get("context_access") or "prompt_only"),
            transcript_access=str(member.get("transcript_access") or "own_messages"),
            catalog_version=member.get("catalog_version"),
        )

    async def _apply_post_skip_policy(
        self,
        *,
        reason_code: str,
        member: dict,
        snapshot: MemberSnapshot,
        proposal: TurnProposal,
    ) -> RunMeta | None:
        """Shared post-skip policy branch (F031-09 + F031-05 fail-closed).

        Returns a terminal ``RunMeta`` when the run should terminate, or
        ``None`` when the scheduler should release the guard reservation
        and continue. Used by BOTH admission-blocked and context-blocked
        paths so the ask flow and counter advance behavior are identical
        — without this, ``stop_behavior=ask`` would silently skip the
        Phase 3 context-blocked case (QA review finding).
        """
        if self._policy.stop_behavior == "stop":
            return self._emit_terminal(reason_code, "failed")
        if self._policy.stop_behavior == "ask":
            self._store.append_event(
                self._meta.id,
                type=EventType.RUN_STATUS_CHANGED,
                status=EventStatus.AWAITING_USER_DECISION,
                payload={
                    "status_change": "awaiting_user_decision",
                    "reason_code": reason_code,
                    "member_id": member["id"],
                    "round": proposal.round,
                },
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                writer=self._writer,
            )
            self._control.enter_awaiting_user_decision(
                question_code=reason_code,
                member_id=member["id"], round=proposal.round,
            )
            decision = await self._control.await_decision_or_cancelled()
            if decision is None or self._control.is_cancelled():
                return self._emit_terminal(
                    ReasonCode.CANCEL_REQUESTED.value, "cancelled",
                )
            choice = str(decision.get("choice") or "")
            scope = str(decision.get("scope") or "current_turn")
            self._control.exit_awaiting_user_decision()
            self._control.clear_last_decision()
            if choice == "stop":
                return self._emit_terminal(
                    ReasonCode.CANCEL_REQUESTED.value, "cancelled",
                )
            if choice == "skip_member" and scope == "remainder_of_run":
                return self._emit_terminal(
                    ReasonCode.LIMITS_EXHAUSTED.value, "completed",
                )
            # current_turn / current_round / continue_local_only — fall
            # through. The MEMBER_SKIPPED event already advanced the
            # attempts counter so round_robin will pick a different
            # member next (or terminate via the cap if none remain).
        # ``continue`` policy (default): just keep going. Release the
        # guard reservation so resource quotas don't drift; round_robin
        # will pick a new candidate on the next loop iteration.
        self._guard.release(turn_id=f"{member['id']}-{proposal.round}")
        return None

    def _finalization_mode(self) -> str:
        return str(
            dict((self._meta.room_snapshot or {}).get("finalization_policy") or {}).get("mode")
            or "transcript_only"
        )

    def _consensus_synthesizer_member(self) -> dict | None:
        """Pick who writes the consensus: the named finalizer, else the steward
        leader, else the last member to answer, else the first enabled member."""
        # F080: the neutral judge never writes a member-voiced answer — exclude
        # it from the synthesizer candidates.
        # F084: a designated steelman is an advocate of a (possibly false) thesis
        # whose claims are quarantined as unverified — it must NEVER author the
        # answer-of-record, or its constructed case would become the headline
        # verdict. Back it out of the finalizer pool like the judge.
        judge_id = self._judge_member_id()
        steelmen = self._credibility_steelman_member_ids()
        all_enabled = [
            m for m in (self._meta.room_snapshot or {}).get("members", [])
            if m.get("enabled", True)
            and not (judge_id and str(m.get("id")) == str(judge_id))
        ]
        members = [m for m in all_enabled if str(m.get("id")) not in steelmen]
        # Degenerate room (every non-judge member is a steelman): fall back to the
        # unfiltered set so a report is still produced rather than none.
        if not members:
            members = all_enabled
        if not members:
            return None
        fin_policy = dict((self._meta.room_snapshot or {}).get("finalization_policy") or {})
        finalizer_id = fin_policy.get("finalizer_member_id")
        if finalizer_id:
            for m in members:
                if str(m.get("id")) == str(finalizer_id):
                    return m
        steward = dict((self._meta.room_snapshot or {}).get("steward_policy") or {})
        if steward.get("enabled"):
            assignment = dict(steward.get("assignment") or {})
            if assignment.get("mode") == "member" and assignment.get("member_id"):
                for m in members:
                    if str(m.get("id")) == str(assignment["member_id"]):
                        return m
        if self._last_answer is not None:
            for m in members:
                if str(m.get("id")) == str(self._last_answer["member_id"]):
                    return m
        return members[0]

    # ---- F081 entailment gate ------------------------------------------

    def _require_entailment(self) -> bool:
        cred = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
        if bool(cred.get("require_entailment")):
            return True
        return str(cred.get("rigor") or "lenient") in ("standard", "adversarial")

    def _credibility_source_texts(self, events: list[Any]) -> dict[str, str]:
        """{content_sha256: full fetched text} for every fetched source, read
        from the F039 side store (hash-verified). Reads BOTH event kinds so it
        covers forced-research fetches (CREDIBILITY_SOURCE_CAPTURED, whose
        tool_call_event_id IS the call_id) and member fetches (TOOL_CALL_COMPLETED,
        whose payload carries the real call_id). Matching on content_sha256
        sidesteps the tool_call_event_id != call_id skew entirely."""
        try:
            from errorta_council.paths import council_root
            from errorta_tools.result_store import ToolResultStore
        except Exception:
            return {}
        store = ToolResultStore(root=council_root() / "tool-results")
        out: dict[str, str] = {}

        def _ingest(call_id: str, sha: str) -> None:
            if not call_id or not sha or sha in out:
                return
            try:
                rec = store.read(run_id=self._meta.id, call_id=call_id)
            except Exception:
                return
            content = str(rec.get("content") or "")
            if hashlib.sha256(content.encode("utf-8")).hexdigest() == sha:
                out[sha] = content

        for ev in events:
            t = getattr(ev, "type", None)
            p = dict(getattr(ev, "payload", {}) or {})
            if t == EventType.CREDIBILITY_SOURCE_CAPTURED:
                _ingest(str(p.get("tool_call_event_id") or ""),
                        str(p.get("content_sha256") or ""))
            elif t == EventType.TOOL_CALL_COMPLETED:
                _ingest(str(p.get("call_id") or ""),
                        str(p.get("content_sha256") or ""))
        return out

    def _credibility_verifier_route(self) -> dict:
        """Pick a STABLE, CHEAP verifier route, once per run.

        Order: an explicit ``credibility_policy.verifier_route_id`` member, else a
        LOCAL Ollama member (fast, reliable, no subscription quota). Returns {}
        when neither exists — and the entailment/validity gate then SKIPS rather
        than falling back to a CLI/remote provider. The verifier runs once per
        (claim x source); a CLI fallback spawned a subprocess STORM (dozens of
        `codex`/`claude` launches) that pegged the machine, starved the single
        sidecar worker — so a concurrent room-save failed with
        `sidecar_unreachable` — and returned only 'unresolved' anyway. No cheap
        local route ⇒ no gate, never a subprocess storm."""
        members = [
            m for m in (self._meta.room_snapshot or {}).get("members", [])
            if m.get("enabled", True) and str(m.get("id")) != str(self._judge_member_id())
        ]
        cred = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
        explicit = str(cred.get("verifier_route_id") or "")
        if explicit:
            for m in members:
                rid = str(m.get("gateway_route_id") or m.get("route_id") or m.get("id") or "")
                if rid == explicit:
                    return m
        local = [m for m in members if str(m.get("provider") or "local") == "local"]
        # NO CLI/remote fallback — see docstring. A local route or nothing.
        return local[0] if local else {}

    def _credibility_verifier(self):
        """A GatewayEntailmentVerifier backed by a neutral model call over a
        stable local route. Cache is per-run so re-checks are free."""
        from errorta_council.credibility.entailment import GatewayEntailmentVerifier
        from errorta_council.gateway_local import LocalCouncilModelRequest

        base = self._credibility_verifier_route()
        route_id = str(base.get("gateway_route_id") or base.get("route_id") or base.get("id") or "")

        async def _call(system_prompt: str, user: str) -> str:
            req = LocalCouncilModelRequest(
                role="verifier", route_id=route_id,
                provider=base.get("provider", "local"), model=base.get("model", ""),
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user}],
                max_output_tokens=512, temperature=0.0,
                timeout_seconds=self._per_turn_timeout_for(base) if base else 120,
                metadata={"member_id": str(base.get("id") or "__verifier__"),
                          "destination_scope": "local", "egress_class": "local",
                          "credibility_entailment": True},
            )
            result = await asyncio.wait_for(
                self._gateway.call(req),
                timeout=self._per_turn_timeout_for(base) if base else 120,
            )
            return str(getattr(result, "content", "") or "")

        if not hasattr(self, "_entailment_cache"):
            self._entailment_cache = {}
        return GatewayEntailmentVerifier(_call, cache=self._entailment_cache)

    async def _run_entailment_for_message(
        self, member_id: str, content: str, round_n: int
    ) -> None:
        """F081: at the claim's turn, check each cited fetched source actually
        entails the claim and emit CREDIBILITY_ENTAILMENT_CHECKED per (claim,
        source). Fail-soft: any error leaves grades 'unresolved' (admission
        holds the claim) and never aborts the run."""
        # F084: a steelman's claims are quarantined (never admitted, never
        # source-supported) and may cite constructed URLs, so grading them
        # against the real fetched set is pointless — skip the gate for them.
        if self._member_is_steelman(member_id):
            return
        # Skip entirely when there's no cheap local verifier route — running the
        # per-claim gate through a CLI/remote provider spawns a subprocess storm
        # that starves the sidecar (see _credibility_verifier_route).
        if not self._credibility_verifier_route():
            return
        from errorta_council.credibility import parse_claim_packet, parse_digest_claims

        try:
            pkt = parse_claim_packet(member_id, content)
            claims = list(pkt.claims) if pkt else parse_digest_claims(member_id, content)
        except Exception:
            return
        _, events = self._store.read_run(self._meta.id)
        sha_to_text = self._credibility_source_texts(events)
        if not sha_to_text:
            return
        store = build_credibility_sources(self._meta.id, events)
        url_to_source = {(s.canonical_url or s.url): s for s in store.sources.values()}
        for s in store.sources.values():
            url_to_source.setdefault(s.source_id, s)
        # F082 Pillar 2: if the message carried NO structured claim packet, extract
        # free-prose factual citations (the gap where "Source says X" prose escapes
        # the gate) and gate them as synthetic claims.
        if not claims:
            from errorta_council.credibility.entailment import extract_prose_citations
            from errorta_council.credibility.models import Claim
            source_urls = [(s.canonical_url or s.url) for s in store.sources.values()]
            prose = extract_prose_citations(content, source_urls)
            claims = [
                Claim(claim_id=f"prose-{round_n}-{i}", text=sent, source_ids=[url])
                for i, (sent, url) in enumerate(prose)
            ]
        if not claims:
            return
        verifier = self._credibility_verifier()
        for claim in claims:
            # Namespace the claim id exactly as the finalizer does
            # (_namespace_packet), so entailment_by_claim keys match the
            # admission pass — otherwise the grade is silently dropped.
            cid = (
                claim.claim_id if claim.claim_id.startswith(f"{member_id}:")
                else f"{member_id}:{claim.claim_id}"
            )
            for token in claim.source_ids:
                src = url_to_source.get(str(token).strip())
                if src is None:
                    continue
                text = sha_to_text.get(src.content_sha256)
                if not text:
                    continue
                try:
                    res = await verifier.verify(
                        claim_text=claim.text, source_text=text,
                        source_sha256=src.content_sha256,
                    )
                except Exception:
                    continue
                # F082: revise-down — re-verify the verifier's narrowed claim
                # against the SAME span; only keep revised_text if it is itself
                # entailed (never admit a model-invented claim).
                revised = ""
                if res.grade == "overclaim" and res.revised_text:
                    try:
                        recheck = await verifier.verify(
                            claim_text=res.revised_text, source_text=text,
                            source_sha256=src.content_sha256 + ":revcheck",
                        )
                        if recheck.grade in ("entails", "overclaim"):
                            revised = res.revised_text
                    except Exception:
                        revised = ""
                self._store.append_event(
                    self._meta.id,
                    type=EventType.CREDIBILITY_ENTAILMENT_CHECKED,
                    status=EventStatus.COMPLETED,
                    payload={
                        "claim_id": cid,
                        "source_id": src.source_id,
                        "grade": res.grade,
                        "span_sha256": res.span_sha256,
                        "source_sha256": src.content_sha256,
                        "reason": res.reason,
                        "revised_text": revised,
                    },
                    member_id=member_id,
                    round=round_n,
                    writer=self._writer,
                )

    def _entailment_by_claim(self, events: list[Any]) -> dict[str, str]:
        """Aggregate CREDIBILITY_ENTAILMENT_CHECKED events into one grade per
        claim (multi-source rule)."""
        from errorta_council.credibility.entailment import aggregate_grades

        by_claim: dict[str, list[str]] = {}
        for ev in events:
            if getattr(ev, "type", None) != EventType.CREDIBILITY_ENTAILMENT_CHECKED:
                continue
            p = dict(getattr(ev, "payload", {}) or {})
            cid = str(p.get("claim_id") or "")
            grade = str(p.get("grade") or "")
            if cid and grade:
                by_claim.setdefault(cid, []).append(grade)
        return {cid: aggregate_grades(gs) for cid, gs in by_claim.items()}

    def _revised_text_by_claim(self, events: list[Any]) -> dict[str, str]:
        """F082: the revised-down text for claims whose aggregate grade is
        overclaim (the first event that carries one)."""
        grades = self._entailment_by_claim(events)
        out: dict[str, str] = {}
        for ev in events:
            if getattr(ev, "type", None) != EventType.CREDIBILITY_ENTAILMENT_CHECKED:
                continue
            p = dict(getattr(ev, "payload", {}) or {})
            cid = str(p.get("claim_id") or "")
            rev = str(p.get("revised_text") or "")
            if cid and rev and grades.get(cid) == "overclaim" and cid not in out:
                out[cid] = rev
        return out

    def _credibility_claim_texts(self, events: list[Any]) -> dict[str, str]:
        """{namespaced claim_id: text} for every parsed claim (used to feed the
        validity judge)."""
        from errorta_council.credibility import parse_claim_packet, parse_digest_claims
        out: dict[str, str] = {}
        for ev in events:
            if getattr(ev, "type", None) != EventType.MEMBER_MESSAGE:
                continue
            mid = str(getattr(ev, "member_id", "") or "")
            content = str(dict(getattr(ev, "payload", {}) or {}).get("content") or "")
            try:
                pkt = parse_claim_packet(mid, content)
                claims = list(pkt.claims) if pkt else parse_digest_claims(mid, content)
            except Exception:
                continue
            for c in claims:
                cid = c.claim_id if c.claim_id.startswith(f"{mid}:") else f"{mid}:{c.claim_id}"
                out.setdefault(cid, c.text)
        return out

    def _credibility_validity_judge(self):
        from errorta_council.credibility.validity import ArgumentValidityJudge
        from errorta_council.gateway_local import LocalCouncilModelRequest
        base = self._credibility_verifier_route()
        route_id = str(base.get("gateway_route_id") or base.get("route_id") or base.get("id") or "")

        async def _call(system_prompt: str, user: str) -> str:
            req = LocalCouncilModelRequest(
                role="verifier", route_id=route_id,
                provider=base.get("provider", "local"), model=base.get("model", ""),
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user}],
                max_output_tokens=400, temperature=0.0,
                timeout_seconds=self._per_turn_timeout_for(base) if base else 120,
                metadata={"member_id": str(base.get("id") or "__validity__"),
                          "destination_scope": "local", "egress_class": "local",
                          "credibility_validity": True},
            )
            result = await asyncio.wait_for(
                self._gateway.call(req),
                timeout=self._per_turn_timeout_for(base) if base else 120,
            )
            return str(getattr(result, "content", "") or "")

        if not hasattr(self, "_validity_cache"):
            self._validity_cache = {}
        return ArgumentValidityJudge(_call, cache=self._validity_cache)

    async def _run_credibility_validity(self) -> None:
        """F082: for each claim the entailment gate graded 'inference' (source
        silent), ask the argument-validity judge whether the leap is licensed by
        the established sourced facts; emit CREDIBILITY_VALIDITY_CHECKED and
        record the verdict for the finalizer. Fail-soft."""
        self._validity_by_claim_map: dict[str, str] = {}
        cred = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
        if not cred.get("route_inference_to_validity"):
            return
        # Same guard as the entailment gate: no cheap local route ⇒ skip rather
        # than storm CLI subprocesses.
        if not self._credibility_verifier_route():
            return
        _, events = self._store.read_run(self._meta.id)
        grades = self._entailment_by_claim(events)
        inference_ids = [cid for cid, g in grades.items() if g == "inference"]
        if not inference_ids:
            return
        texts = self._credibility_claim_texts(events)
        supporting = [
            texts[c] for c, g in grades.items()
            if g in ("entails", "overclaim") and c in texts
        ]
        judge = self._credibility_validity_judge()
        for cid in inference_ids:
            ct = texts.get(cid)
            if not ct:
                continue
            try:
                res = await judge.assess(claim_text=ct, supporting_texts=supporting)
            except Exception:
                continue
            self._validity_by_claim_map[cid] = res.verdict
            self._store.append_event(
                self._meta.id,
                type=EventType.CREDIBILITY_VALIDITY_CHECKED,
                status=EventStatus.COMPLETED,
                payload={"claim_id": cid, "verdict": res.verdict, "reason": res.reason},
                round=None, writer=self._writer,
            )

    def _credibility_novelty_set(self, events: list[Any], up_to_round: int) -> set:
        """The set of NEW information available as of ``up_to_round``: entailing
        claims, fetched sources, and distinct peer reviews. Agreement is NOT
        novelty — only new evidence/arguments are. Used to detect when a debate
        has run out of things to say (vs. capitulated)."""
        from errorta_council.credibility import parse_review

        items: set = set()
        for ev in events:
            r = getattr(ev, "round", None)
            r = int(r) if r is not None else 0
            if r > up_to_round:
                continue
            t = getattr(ev, "type", None)
            p = dict(getattr(ev, "payload", {}) or {})
            if t == EventType.CREDIBILITY_ENTAILMENT_CHECKED:
                if str(p.get("grade") or "") in ("entails", "partially_entails"):
                    items.add(("claim", str(p.get("claim_id") or "")))
            elif t == EventType.CREDIBILITY_SOURCE_CAPTURED:
                items.add(("src", str(p.get("content_sha256") or "")))
            elif t == EventType.MEMBER_MESSAGE:
                mid = str(getattr(ev, "member_id", "") or "")
                for rv in parse_review(mid, str(p.get("content") or "")):
                    items.add(("rev", rv.reviewer_member_id, rv.claim_id, rv.status))
        return items

    def _member_by_id(self, member_id: str) -> dict | None:
        for m in (self._meta.room_snapshot or {}).get("members", []):
            if str(m.get("id")) == str(member_id):
                return dict(m)
        return None

    def _member_is_steelman(self, member_id: str) -> bool:
        """F084: True when this member is a designated steelman advocate."""
        from errorta_council.credibility import member_is_steelman
        return member_is_steelman(self._member_by_id(member_id))

    def _steelman_topic(self, member_id: str) -> str:
        """F084: the proposition a steelman member argues FOR."""
        from errorta_council.credibility import steelman_topic
        return steelman_topic(self._member_by_id(member_id))

    def _credibility_steelman_member_ids(self) -> set[str]:
        """F084: ids of all enabled steelman members in the room. The pipeline
        uses this to quarantine their claims (never admitted/source-supported)."""
        from errorta_council.credibility import member_is_steelman
        out: set[str] = set()
        for m in (self._meta.room_snapshot or {}).get("members", []):
            if m.get("enabled", True) and member_is_steelman(m):
                out.add(str(m.get("id")))
        return out

    def _credibility_opponent_id(self) -> str | None:
        """The member assigned to argue the opposing (steelman) case. Explicit
        ``metadata.debate_role == "opponent"`` wins; else, when the room
        auto-assigns one (rigor adversarial or auto_assign_opponent), the last
        enabled non-judge member. None in lenient/standard rooms with no
        explicit opponent (so today's behavior is unchanged).

        F084: a designated steelman is already a deliberate advocate, so it is
        never picked as the GENERIC auto-opponent — that stance would override
        its assigned topic with 'argue regardless of your own opinion'."""
        members = [
            m for m in (self._meta.room_snapshot or {}).get("members", [])
            if m.get("enabled", True)
        ]
        judge_id = self._judge_member_id()
        steelmen = self._credibility_steelman_member_ids()
        non_judge = [m for m in members if str(m.get("id")) != str(judge_id)]
        for m in non_judge:
            if str(dict(m.get("metadata") or {}).get("debate_role") or "") == "opponent":
                return str(m.get("id"))
        cred = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
        auto = (
            bool(cred.get("auto_assign_opponent"))
            or str(cred.get("rigor") or "") == "adversarial"
        )
        eligible = [m for m in non_judge if str(m.get("id")) not in steelmen]
        if auto and len(eligible) >= 2:
            return str(eligible[-1].get("id"))
        return None

    def _credibility_steelman_mounted(self, events: list[Any], admissions: list[Any]) -> bool:
        """True when the assigned opponent got at least one claim ADMITTED — i.e.
        a real opposing case survived the gate. A pure count over admitted
        claim ids tagged by author, no classifier."""
        opp = self._credibility_opponent_id()
        if not opp:
            return True  # no opponent configured ⇒ don't flag (lenient rooms)
        admitted_ids = {
            a.claim_id for a in admissions
            if getattr(a, "admission", "") in ("admitted", "admitted_with_caveat")
        }
        # Admitted ids are namespaced "<member>:<cid>"; the opponent's are
        # prefixed with its id.
        return any(cid.startswith(f"{opp}:") for cid in admitted_ids)

    def _maybe_credibility_novelty_stop(self, next_round: int) -> str | None:
        """End a credibility run early when N consecutive rounds added no new
        claim / source / review — the debate is out of new material (NOT a
        capitulation signal). Returns 'novelty_exhausted' or None."""
        if not self._is_credibility_run() or not self._require_entailment():
            return None
        cred = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
        n = max(1, int(cred.get("novelty_exhaustion_rounds") or 2))
        prev = int(next_round) - 1
        if prev <= n:  # need n+1 rounds of history to compare
            return None
        _, events = self._store.read_run(self._meta.id)
        now = self._credibility_novelty_set(events, prev)
        back = self._credibility_novelty_set(events, prev - n)
        # Monotonic: back ⊆ now, so equal sizes ⇒ no growth over n rounds.
        if len(now) == len(back):
            return "novelty_exhausted"
        return None

    # ---- F080 neutral leader-judge -------------------------------------

    def _judge_policy(self) -> dict[str, Any]:
        return dict((self._meta.room_snapshot or {}).get("judge_policy") or {})

    def _judge_member_id(self) -> str | None:
        pol = self._judge_policy()
        if not pol.get("enabled"):
            return None
        explicit = pol.get("judge_member_id")
        if explicit:
            return str(explicit)
        for m in (self._meta.room_snapshot or {}).get("members", []):
            if str(m.get("role") or "") == "judge":
                return str(m.get("id"))
        return None

    def _judge_member(self) -> dict | None:
        jid = self._judge_member_id()
        if not jid:
            return None
        for m in (self._meta.room_snapshot or {}).get("members", []):
            if str(m.get("id")) == jid:
                return m
        return None

    def _judge_enabled(self) -> bool:
        """A judge runs only when one is configured AND there is at least one
        non-judge enabled member for it to watch."""
        if self._judge_member() is None:
            return False
        jid = self._judge_member_id()
        others = [
            m for m in (self._meta.room_snapshot or {}).get("members", [])
            if m.get("enabled", True) and str(m.get("id")) != str(jid)
        ]
        return len(others) >= 1

    @staticmethod
    def _parse_judge_verdict(content: str) -> dict[str, Any] | None:
        """Parse the judge's single-JSON-object verdict. Fail-soft: any
        unparseable / off-shape reply returns None (treated as 'keep going')."""
        obj = _extract_json_object(content)
        if not isinstance(obj, dict):
            return None
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in {"reached", "continue", "decide", "no_consensus"}:
            return None
        out: dict[str, Any] = {"verdict": verdict}
        out["answer"] = str(obj.get("answer") or "").strip()
        out["reason"] = str(obj.get("reason") or "").strip()
        for key in ("agreed_member_ids", "dissenting_member_ids"):
            val = obj.get(key)
            out[key] = [str(x) for x in val] if isinstance(val, list) else []
        if obj.get("chosen_member_id"):
            out["chosen_member_id"] = str(obj.get("chosen_member_id"))
        return out

    async def _run_judge_evaluation(
        self, round_n: int, *, final: bool
    ) -> dict[str, Any] | None:
        """Run ONE neutral judge turn over the deliberation transcript and return
        the parsed verdict (or None on any failure — fail-soft, never aborts the
        run). The judge reads peers' messages exactly as an ``all_messages``
        member would (same router, same byte-isolation); its own configured
        persona is REPLACED with the neutral judge prompt so it holds no opinion.
        """
        judge = self._judge_member()
        if judge is None:
            return None
        prompt = NEUTRAL_JUDGE_TIEBREAK_PROMPT if final else NEUTRAL_JUDGE_PROMPT
        judge_turn = {
            **dict(judge),
            "system_prompt": prompt,
            "transcript_access": "all_messages",
            "force_deliberation_dialect": "",
        }
        self._store.append_event(
            self._meta.id,
            type=EventType.JUDGE_EVALUATION_STARTED,
            status=EventStatus.RUNNING,
            payload={"round": round_n, "final": final, "member_id": judge["id"]},
            member_id=judge["id"],
            round=round_n,
            writer=self._writer,
        )
        try:
            _, prior_events = self._store.read_run(self._meta.id)
            context_member = self._member_for_context(judge_turn)
            context = await self._ctx.build(
                run_meta=self._meta, member=context_member, transcript=prior_events
            )
            if context.get("blocked"):
                return None
            from errorta_council.gateway_local import LocalCouncilModelRequest
            route_id = str(
                judge.get("gateway_route_id") or judge.get("route_id") or judge["id"]
            )
            destination_scope = str(
                context.get("destination_scope")
                or ("fake" if judge.get("provider") == "fake" else "local")
            )
            egress_class = str(context.get("egress_class") or "local")
            request = LocalCouncilModelRequest(
                role="judge",
                route_id=route_id,
                provider=judge.get("provider", "local"),
                model=judge.get("model", ""),
                messages=context["messages"],
                max_output_tokens=self._max_output_tokens_for(judge_turn),
                temperature=judge.get("temperature", 0.0),
                timeout_seconds=self._per_turn_timeout_for(judge_turn),
                metadata={
                    "context_id": context["context_id"],
                    "member_id": judge["id"],
                    "destination_scope": destination_scope,
                    "egress_class": egress_class,
                    "judge": "tie_break" if final else "round",
                },
                cache_hints=list(context.get("cache_hints") or []),
            )
            result = await asyncio.wait_for(
                self._gateway.call(request),
                timeout=self._per_turn_timeout_for(judge_turn),
            )
            verdict = self._parse_judge_verdict(str(result.content))
        except Exception:
            verdict = None
        payload: dict[str, Any] = {
            "round": round_n,
            "final": final,
            "member_id": judge["id"],
            "verdict": (verdict or {}).get("verdict", "continue" if not final else "no_consensus"),
            "reason": (verdict or {}).get("reason", ""),
            "agreed_member_ids": (verdict or {}).get("agreed_member_ids", []),
            "dissenting_member_ids": (verdict or {}).get("dissenting_member_ids", []),
        }
        if verdict and verdict.get("chosen_member_id"):
            payload["chosen_member_id"] = verdict["chosen_member_id"]
        self._store.append_event(
            self._meta.id,
            type=EventType.JUDGE_VERDICT,
            status=EventStatus.COMPLETED,
            payload=payload,
            member_id=judge["id"],
            round=round_n,
            writer=self._writer,
        )
        return verdict

    async def _run_credibility_judge_answer(self) -> None:
        """F084: at finalize of a credibility room with a neutral judge, run ONE
        judge turn that states a NEUTRAL verdict over the deliberation, and stash
        it so the sync report finalizer uses it as the headline (instead of an
        advocate member's prose). Fail-soft: any failure leaves it empty and the
        finalizer falls back to the leader's prose."""
        if self._finalization_mode() != "credibility_report":
            return
        if not self._judge_enabled():
            return
        judge = self._judge_member()
        if judge is None:
            return
        judge_turn = {
            **dict(judge),
            "system_prompt": NEUTRAL_JUDGE_CREDIBILITY_PROMPT,
            "transcript_access": "all_messages",
            "force_deliberation_dialect": "",
        }
        try:
            _, prior_events = self._store.read_run(self._meta.id)
            context_member = self._member_for_context(judge_turn)
            context = await self._ctx.build(
                run_meta=self._meta, member=context_member, transcript=prior_events
            )
            if context.get("blocked"):
                return
            from errorta_council.gateway_local import LocalCouncilModelRequest
            route_id = str(
                judge.get("gateway_route_id") or judge.get("route_id") or judge["id"]
            )
            request = LocalCouncilModelRequest(
                role="judge",
                route_id=route_id,
                provider=judge.get("provider", "local"),
                model=judge.get("model", ""),
                messages=context["messages"],
                max_output_tokens=self._max_output_tokens_for(judge_turn),
                temperature=judge.get("temperature", 0.0),
                timeout_seconds=self._per_turn_timeout_for(judge_turn),
                metadata={
                    "context_id": context["context_id"],
                    "member_id": judge["id"],
                    "destination_scope": str(
                        context.get("destination_scope")
                        or ("fake" if judge.get("provider") == "fake" else "local")
                    ),
                    "egress_class": str(context.get("egress_class") or "local"),
                    "judge": "credibility_verdict",
                },
                cache_hints=list(context.get("cache_hints") or []),
            )
            result = await asyncio.wait_for(
                self._gateway.call(request),
                timeout=self._per_turn_timeout_for(judge_turn),
            )
            text = str(getattr(result, "content", "") or "").strip()
            if text and not getattr(result, "is_thinking_burn", False):
                self._credibility_judge_answer = text
        except Exception:
            self._credibility_judge_answer = ""

    async def _maybe_judge_between_rounds(self, next_round: int) -> str | None:
        """Before a new round starts, let the judge end the run if the members
        have converged. Returns a terminal reason when the run should finalize
        now, else None. Judges each round boundary at most once."""
        if not self._judge_enabled():
            return None
        prev_round = int(next_round) - 1
        start = int(self._judge_policy().get("start_round", 1) or 1)
        # Credibility mode: round 1 is only the claim phase — peer credidation
        # happens in round 2. Ending after round 1 would force a
        # "verification incomplete" report (no reviews yet), so never let the
        # judge stop a credibility run before the credidation round has run.
        if self._finalization_mode() == "credibility_report":
            start = max(start, 2)
        if prev_round < 1 or prev_round <= self._last_judged_round or prev_round < start:
            return None
        self._last_judged_round = prev_round
        verdict = await self._run_judge_evaluation(prev_round, final=False)
        if not verdict or verdict.get("verdict") != "reached":
            return None
        # Credibility runs keep the source-verified report as the answer; the
        # judge only decides WHEN to stop. Every other mode adopts the judge's
        # agreed answer as the answer-of-record.
        if self._finalization_mode() != "credibility_report":
            answer = verdict.get("answer", "")
            if answer:
                self._judge_answer = {
                    "content": answer,
                    "member_id": str((self._judge_member() or {}).get("id") or "judge"),
                    "round": prev_round,
                    "synthesis_mode": "judge",
                    "judge": verdict,
                }
        return "verdict_reached"

    async def _maybe_judge_final(self, reason: str) -> None:
        """At the round/budget limit, let the judge break the tie (decide among
        the members' positions). Sets ``self._judge_answer`` on a decision.
        Credibility runs are left to their own report finalizer."""
        if not self._judge_enabled():
            return
        # Don't tie-break a run that already reached genuine consensus — the
        # consensus answer stands; a tie-break here would burn a call and
        # wrongly replace it with a "best-supported position".
        if reason == "consensus_reached":
            return
        if self._finalization_mode() == "credibility_report":
            return
        if not self._judge_policy().get("tie_break", True):
            return
        if self._judge_answer is not None:
            return
        round_n = int((self._last_answer or {}).get("round", self._last_judged_round or 1) or 1)
        verdict = await self._run_judge_evaluation(round_n, final=True)
        if not verdict or verdict.get("verdict") != "decide":
            return
        answer = verdict.get("answer", "")
        if not answer:
            return
        self._judge_answer = {
            "content": answer,
            "member_id": str((self._judge_member() or {}).get("id") or "judge"),
            "round": round_n,
            "synthesis_mode": "judge",
            "judge": verdict,
        }

    async def _finalize(self, reason: str, detail: dict[str, Any] | None) -> RunMeta:
        """Single finalization path shared by the topology-completion branch and
        the judge's early-stop: run consensus / credibility synthesis (fail-soft)
        then emit the terminal answer-of-record."""
        self._consensus_answer = await self._maybe_synthesize_consensus(reason)
        if self._consensus_answer is None:
            # F031-28: abstractive summary (any terminal reason). Mutually
            # exclusive with consensus/credibility (one finalization mode wins).
            self._consensus_answer = await self._maybe_synthesize_summary(reason)
        if self._consensus_answer is None:
            # F082: run the argument-validity pass (async) BEFORE the sync
            # credibility finalizer so 'inference' claims carry a verdict.
            if self._finalization_mode() == "credibility_report":
                try:
                    await self._run_credibility_validity()
                except Exception:
                    self._validity_by_claim_map = {}
                # F084: neutral judge authors the verdict (if one is enabled).
                try:
                    await self._run_credibility_judge_answer()
                except Exception:
                    self._credibility_judge_answer = ""
            self._consensus_answer = self._maybe_synthesize_credibility_report()
        return self._emit_terminal(reason, "completed", detail=detail)

    async def _maybe_synthesize_consensus(self, reason: str) -> dict[str, Any] | None:
        """Write a single consolidated answer that represents the council's
        shared conclusion (finalization mode ``consensus_report``).

        This runs ONE extra synthesizer turn through the SAME context router as
        a normal member turn — so byte-isolation, redaction, and egress policy
        all apply (the synthesizer sees the deliberation transcript exactly as
        an ``all_messages`` member would). Fail-soft: any failure returns None
        and the run falls back to the answer-of-record, so a synthesis problem
        never breaks a run that already reached its conclusion.
        """
        # Only label a synthesized answer "Consensus" when the members actually
        # converged. A run that stopped at the round/budget limit keeps the
        # honest answer-of-record + "did not reach consensus" warning.
        if reason != "consensus_reached":
            return None
        if self._finalization_mode() != "consensus_report":
            return None
        return await self._run_synthesizer_turn(
            CONSENSUS_SYNTHESIS_PROMPT, synthesis_mode="consensus"
        )

    async def _maybe_synthesize_summary(self, reason: str) -> dict[str, Any] | None:
        """Write an abstractive ``summary`` of the deliberation (F031-28).

        Unlike consensus, this runs for ANY terminal reason — a run that stopped at
        the round/budget limit still gets a faithful summary — and the prompt is
        framed to PRESERVE disagreement, not flatten it. Same byte-isolation /
        redaction / egress path as the consensus synthesizer (one extra turn through
        the context router); fail-soft (any failure falls back to the
        answer-of-record).
        """
        if self._finalization_mode() != "summary":
            return None
        return await self._run_synthesizer_turn(
            SUMMARY_SYNTHESIS_PROMPT, synthesis_mode="summary"
        )

    async def _run_synthesizer_turn(
        self, prompt: str, *, synthesis_mode: str
    ) -> dict[str, Any] | None:
        """Run ONE finalizer synthesis turn through the SAME context router as a
        normal member turn — so byte-isolation, redaction, and egress policy all
        apply — using ``prompt`` as the synthesizer's system prompt. Returns the
        synthesized answer dict (tagged ``synthesis_mode``) or None on any failure.
        Shared by the ``consensus_report`` and ``summary`` finalization modes.
        """
        base = self._consensus_synthesizer_member()
        if base is None:
            return None
        synth_member = {
            **dict(base),
            "system_prompt": prompt,
            "transcript_access": "all_messages",
            # The synthesized answer is for the user — never the digest dialect.
            "force_deliberation_dialect": "",
        }
        round_n = int((self._last_answer or {}).get("round", 1) or 1)
        try:
            _, prior_events = self._store.read_run(self._meta.id)
            context_member = self._member_for_context(synth_member)
            context = await self._ctx.build(
                run_meta=self._meta, member=context_member, transcript=prior_events
            )
            if context.get("blocked"):
                return None
            from errorta_council.gateway_local import LocalCouncilModelRequest
            route_id = str(
                base.get("gateway_route_id") or base.get("route_id") or base["id"]
            )
            destination_scope = str(
                context.get("destination_scope")
                or ("fake" if base.get("provider") == "fake" else "local")
            )
            egress_class = str(context.get("egress_class") or "local")
            request = LocalCouncilModelRequest(
                role="finalizer",
                route_id=route_id,
                provider=base.get("provider", "local"),
                model=base.get("model", ""),
                messages=context["messages"],
                max_output_tokens=self._max_output_tokens_for(synth_member),
                temperature=base.get("temperature", 0.2),
                timeout_seconds=self._per_turn_timeout_for(synth_member),
                metadata={
                    "context_id": context["context_id"],
                    "member_id": base["id"],
                    "destination_scope": destination_scope,
                    "egress_class": egress_class,
                    "synthesis": synthesis_mode,
                },
                cache_hints=list(context.get("cache_hints") or []),
            )
            result = await asyncio.wait_for(
                self._gateway.call(request),
                timeout=self._per_turn_timeout_for(synth_member),
            )
            if getattr(result, "is_thinking_burn", False) or not str(result.content).strip():
                return None
            return {
                "content": result.content,
                "member_id": base["id"],
                "round": round_n,
                "synthesis_mode": synthesis_mode,
            }
        except Exception:
            return None

    def _credibility_sources(self, events: list[Any]) -> Any:
        return build_credibility_sources(self._meta.id, events)

    def _maybe_synthesize_credibility_report(self) -> dict[str, Any] | None:
        """F078: parse the finished transcript + fetched sources, admit verified
        claims, emit the credibility report, and return it as the answer-of-
        record.

        Fail CLOSED (Reviewer P1): for a credibility_report room this NEVER
        returns None — a parser bug or malformed packet yields an explicit
        ``verification_incomplete`` credibility report rather than silently
        falling back to a normal final answer that was never source-verified.
        """
        if self._finalization_mode() != "credibility_report":
            return None  # not a credibility room — leave the normal path alone.

        from errorta_council.credibility.report import CredibilityReport

        base = self._consensus_synthesizer_member() or {}
        round_n = int((self._last_answer or {}).get("round", 2) or 2)
        report = CredibilityReport(verification_incomplete=True, confidence="low")
        store: Any = None
        try:
            from errorta_council.credibility import (
                parse_claim_packet,
                parse_digest_claims,
                parse_review,
            )
            from errorta_council.credibility.models import ClaimPacket
            from errorta_council.credibility.report import run_credibility_pipeline
            from errorta_council.schema import CredibilityPolicy

            _, events = self._store.read_run(self._meta.id)
            store = self._credibility_sources(events)

            leader_id = str(base.get("id") or "")
            packets_by_member: dict[str, Any] = {}
            reviews: list[Any] = []
            leader_prose = ""
            for ev in events:
                if getattr(ev, "type", None) != EventType.MEMBER_MESSAGE:
                    continue
                payload = dict(getattr(ev, "payload", {}) or {})
                content = str(payload.get("content") or "")
                mid = str(getattr(ev, "member_id", "") or payload.get("member_id") or "")
                if not mid:
                    continue
                pkt = parse_claim_packet(mid, content)
                parsed_reviews = parse_review(mid, content)
                # Fallback: models that emit the digest_v1 dialect instead of a
                # JSON packet still get their claims (+ [c:url] citations) read.
                if pkt is None and not parsed_reviews:
                    digest_claims = parse_digest_claims(mid, content)
                    if digest_claims:
                        pkt = ClaimPacket(
                            packet_id=f"pkt_{mid}", member_id=mid, claims=digest_claims,
                        )
                if pkt is not None:
                    # Namespace claim ids by member so two members' "c1" don't
                    # collide (the credidation prompt shows the namespaced id and
                    # reviewers echo it).
                    pkt = _namespace_packet(mid, pkt)
                    packets_by_member[mid] = pkt  # latest packet per member wins
                    # Use a packet's answer_fragment (or first claim text) from the
                    # leader as the human-readable answer when present.
                    if mid == leader_id:
                        frag = pkt.answer_fragment.strip() or (
                            pkt.claims[0].text.strip() if pkt.claims else ""
                        )
                        if frag:
                            leader_prose = frag
                reviews.extend(parsed_reviews)
                # P2: if the leader wrote a plain-prose message (not a packet /
                # review JSON), use it as the human-readable answer.
                if (
                    mid == leader_id
                    and pkt is None
                    and not parsed_reviews
                    and content.strip()
                    and not content.lstrip().startswith("{")
                ):
                    leader_prose = content.strip()

            packets = list(packets_by_member.values())
            policy_raw = dict((self._meta.room_snapshot or {}).get("credibility_policy") or {})
            policy = CredibilityPolicy.from_dict(policy_raw)

            # F082 Pillar 2: audit the FINALIZER's own citations. Its claims went
            # through the gate like any member's; any that the source contradicts
            # or doesn't support is a finalizer citation failure (its verdicts are
            # exempt, its citations are not).
            grades = self._entailment_by_claim(events)
            # F084: when the neutral judge authors the verdict it cites nothing of
            # its own, so audit the actual author. (A judge headline has no
            # citations → no finalizer citation failures.)
            verdict_text = self._credibility_judge_answer.strip()
            audit_id = (
                str(self._judge_member_id() or "") if verdict_text
                else str(base.get("id") or "")
            )
            finalizer_failures = [
                {"claim_id": cid, "reason": g}
                for cid, g in grades.items()
                if audit_id and cid.startswith(f"{audit_id}:")
                and g in ("contradicts", "unsupported")
            ]

            # F084: quarantine designated steelman advocates' claims.
            steelman_ids = self._credibility_steelman_member_ids()
            steelman_topics = {
                sid: self._steelman_topic(sid) for sid in steelman_ids
            }
            # F084: a neutral judge, when enabled, authors the verdict headline
            # (verdict_text, computed above) instead of an advocate's closing prose.
            report = run_credibility_pipeline(
                packets=packets, reviews=reviews, store=store, policy=policy,
                leader_answer=(verdict_text or leader_prose), repair_exhausted=True,
                entailment_by_claim=grades,
                revised_text_by_claim=self._revised_text_by_claim(events),
                validity_by_claim=getattr(self, "_validity_by_claim_map", {}),
                finalizer_citation_failures=finalizer_failures,
                steelman_member_ids=steelman_ids,
                steelman_topics=steelman_topics,
            )
            # F084: a designated steelman that actually posted claims IS a mounted
            # opposing case (deliberately unverified) — so the room was not
            # "unchallenged". Suppress the unchallenged-consensus smell + cap in
            # that case (real-claim confidence already excludes steelman claims).
            steelman_posted = any(mid in steelman_ids for mid in packets_by_member)
            # F081: flag a debate where no opposing case survived the gate —
            # convergence with no steelman is a smell, not a success. Cap
            # confidence so an "unchallenged" answer can't read as high.
            if not steelman_posted and not self._credibility_steelman_mounted(
                events, report.admissions
            ):
                from dataclasses import replace as _replace
                report = _replace(
                    report, quality_flag="unchallenged_consensus",
                    confidence="low" if report.confidence == "high" else report.confidence,
                )
            # F082: a finalizer that mis-cited its own sources taints its
            # synthesis — never let that read as high confidence.
            if finalizer_failures and report.confidence == "high":
                from dataclasses import replace as _replace
                report = _replace(report, confidence="medium")
        except Exception:
            # Fail closed: keep the verification_incomplete report built above.
            pass

        try:
            self._emit_credibility_events(store, report)
        except Exception:
            pass
        # F084: attribute the headline to the neutral judge when it authored the
        # verdict, so the UI shows "Council Leader: <judge>" (a neutral), not an
        # advocate. Falls back to the synthesizer member otherwise.
        author_id = (
            str(self._judge_member_id() or base.get("id") or "leader")
            if self._credibility_judge_answer.strip()
            else str(base.get("id") or "leader")
        )
        return {
            "content": _format_credibility_answer(report),
            "member_id": author_id,
            "round": round_n,
            "synthesis_mode": "credibility",
            "credibility_report": report.to_dict(),
        }

    def _emit_credibility_events(self, store: Any, report: Any) -> None:
        """Emit the F078 audit trail — ids/hashes only, never raw content."""
        source_count = len(store.sources) if store is not None else 0
        try:
            self._store.append_event(
                self._meta.id, type=EventType.CREDIBILITY_FINALIZATION_STARTED,
                status=EventStatus.COMPLETED,
                payload={"source_count": source_count, "claim_count": len(report.admissions)},
                writer=self._writer,
            )
            # Source-captured events were already emitted at fetch time.
            for adm in report.admissions:
                admitted = adm.admission in ("admitted", "admitted_with_caveat")
                self._store.append_event(
                    self._meta.id,
                    type=EventType.CREDIBILITY_CLAIM_ADMITTED if admitted
                    else EventType.CREDIBILITY_CLAIM_EXCLUDED,
                    status=EventStatus.COMPLETED,
                    payload={"claim_id": adm.claim_id, "admission": adm.admission,
                             "final_status": adm.final_status,
                             "required_repairs": list(adm.required_repairs)},
                    writer=self._writer,
                )
            self._store.append_event(
                self._meta.id, type=EventType.CREDIBILITY_REPORT_CREATED,
                status=EventStatus.COMPLETED,
                payload={"claims_used": list(report.claims_used),
                         "source_map": [dict(s) for s in report.source_map],
                         "caveats": list(report.caveats),
                         "excluded_claims": [dict(e) for e in report.excluded_claims],
                         "confidence": report.confidence,
                         "verification_incomplete": report.verification_incomplete},
                writer=self._writer,
            )
        except Exception:
            pass

    def _emit_terminal(
        self, reason: str, status: str, detail: dict[str, Any] | None = None,
    ) -> RunMeta:
        ev_type = {
            "completed": EventType.RUN_COMPLETED,
            "failed": EventType.RUN_FAILED,
            "cancelled": EventType.RUN_CANCELLED,
        }[status]
        ev_status = {
            "completed": EventStatus.COMPLETED,
            "failed": EventStatus.FAILED,
            "cancelled": EventStatus.CANCELLED,
        }[status]
        if status == "cancelled":
            self._cancel_outstanding_child_runs()
        # On a clean completion, surface the answer-of-record as a
        # FINAL_ANSWER event so the transcript can render the conclusion
        # without the UI having to guess which member message was final.
        if status == "completed":
            answer = (
                self._judge_answer
                or self._consensus_answer
                or self._last_finalizer_answer
                or self._last_answer
            )
            if answer is not None:
                payload = {
                    "content": answer["content"],
                    "member_id": answer["member_id"],
                    "round": answer["round"],
                }
                # A synthesized consensus answer is labeled so the UI can render
                # it as a distinct "Consensus" block rather than a verbatim
                # last-speaker message.
                if answer.get("synthesis_mode"):
                    payload["synthesis_mode"] = answer["synthesis_mode"]
                # F078: carry the structured credibility report so the UI can
                # render sources / caveats / excluded claims (ids + hashes only).
                if answer.get("credibility_report"):
                    payload["credibility_report"] = answer["credibility_report"]
                # F064: stamp the consensus detail (who agreed, threshold,
                # round) so the UI can explain HOW consensus was reached.
                if reason == "consensus_reached" and isinstance(detail, dict):
                    payload["consensus"] = detail
                # F080: stamp the judge's verdict (verdict / reason / who agreed)
                # so the UI can show the neutral judge's call.
                if answer.get("judge"):
                    payload["judge"] = answer["judge"]
                self._store.append_event(
                    self._meta.id,
                    type=EventType.FINAL_ANSWER,
                    status=EventStatus.COMPLETED,
                    payload=payload,
                    member_id=answer["member_id"],
                    round=answer["round"],
                    writer=self._writer,
                )
        # F049: a user interjection that arrived after the last member turn is
        # durably recorded but no member ever consumed it (the run ended). The
        # route already returned 200, so signal it here rather than silently
        # swallow — the UI can tell the user it landed in a finished run.
        self._note_undelivered_interjections()
        self._store.append_event(
            self._meta.id,
            type=ev_type,
            status=ev_status,
            payload={"reason": reason},
            writer=self._writer,
        )
        # Use merge_meta_fields so the lock guarantees this terminal write
        # is atomic vs. concurrent control writes.
        new = self._store.merge_meta_fields(self._meta.id, terminal_reason=reason)
        return new

    def _note_undelivered_interjections(self) -> None:
        """Emit a DIAGNOSTIC_NOTE for any interjection no member spoke after.

        "Delivered" = a real member turn (MEMBER_MESSAGE) has a higher sequence
        than the interjection, proving a member built context after seeing it.
        FINAL_ANSWER is excluded — it is a terminal copy of an earlier answer,
        not a fresh turn. A synthesized consensus answer counts as delivery: the
        synthesizer builds context from the full transcript (incl. the
        interjection) before the run completes.
        """
        if self._consensus_answer is not None or self._judge_answer is not None:
            return
        _, events = self._store.read_run(self._meta.id)
        consumed_seqs = [
            int(e.sequence or 0) for e in events
            if e.type == EventType.MEMBER_MESSAGE
        ]
        last_consumed = max(consumed_seqs) if consumed_seqs else -1
        undelivered = [
            int(e.sequence or 0) for e in events
            if e.type == EventType.USER_INTERJECTION
            and int(e.sequence or 0) > last_consumed
        ]
        if not undelivered:
            return
        self._store.append_event(
            self._meta.id,
            type=EventType.DIAGNOSTIC_NOTE,
            status=EventStatus.COMPLETED,
            payload={
                "note": "interjections_after_final_turn",
                "count": len(undelivered),
                "sequences": undelivered,
                "detail": ("user message(s) arrived after the council's final "
                           "turn and were not delivered to a member"),
            },
            writer=self._writer,
        )

    async def run(self) -> RunMeta:
        # Acquire writer reservation for the lifetime of this run.
        self._writer = self._store.acquire_writer(self._meta.id)
        # Share the writer with RunControl so control events flow through it.
        self._control._scheduler_writer = self._writer  # type: ignore[attr-defined]
        try:
            return await self._run_loop()
        finally:
            self._control._scheduler_writer = None  # type: ignore[attr-defined]
            self._store.release_writer(self._writer)

    async def _run_loop(self) -> RunMeta:
        # Emit RUN_STARTED unless already in the log.
        _, existing = self._store.read_run(self._meta.id)
        if not any(e.type == EventType.RUN_STARTED for e in existing):
            self._store.append_event(
                self._meta.id,
                type=EventType.RUN_STARTED,
                status=EventStatus.RUNNING,
                payload={"started_at": _utcnow()},
                writer=self._writer,
            )
            # append_event already advanced last_sequence and event_count; the
            # subsequent loop reads fresh meta. Use the locked merge for the
            # subsequent counter writes (RACE-safe vs. control writes).
            self._meta, _ = self._store.read_run(self._meta.id)

        # F078: Credibility mode forces a real internet search up front so the
        # finalizer always has fetched sources to verify claims against — we do
        # NOT rely on the models reliably emitting tool-call JSON. Fail-soft: a
        # search/tool failure leaves findings empty and the run produces an
        # honest "verification incomplete" report.
        if self._is_credibility_run():
            try:
                await self._run_forced_credibility_research()
            except Exception:
                self._credibility_findings = ""

        while True:
            # Checkpoint 0: drain any control events the route queued while
            # we held the writer token. Each entry was a real user action
            # (pause/resume/cancel/decision) and must surface in the
            # transcript before we evaluate cancel/pause state below.
            self._drain_pending_control_events()
            # Checkpoint 1: pause/cancel.
            if self._control.is_cancelled():
                return self._emit_terminal(ReasonCode.CANCEL_REQUESTED.value, "cancelled")
            await self._control.await_unpaused_or_cancelled()
            if self._control.is_cancelled():
                return self._emit_terminal(ReasonCode.CANCEL_REQUESTED.value, "cancelled")

            # F037: drain any queued expert callouts before asking topology
            # for the next ordinary turn, so an admitted/approved callout
            # pre-empts the normal speaker order. Provider calls go through
            # the same gateway path; all events use the writer token.
            callout_terminal = await self._drain_callouts()
            if callout_terminal is not None:
                return callout_terminal
            if self._control.is_cancelled():
                return self._emit_terminal(ReasonCode.CANCEL_REQUESTED.value, "cancelled")

            # Topology proposal.
            state = self._build_run_state()
            proposal = self._topology.propose_next(state, transcript=[])
            if isinstance(proposal, RunCompletion):
                # F080: the run hit its round/budget limit. Let the neutral judge
                # break the tie (decide among the members' positions) before we
                # finalize — fail-soft, leaves the answer-of-record untouched on
                # "no_consensus". Credibility runs keep their own report.
                await self._maybe_judge_final(proposal.reason)
                # _finalize runs consensus / credibility synthesis (fail-soft)
                # then emits the terminal answer-of-record.
                return await self._finalize(proposal.reason, proposal.detail)
            assert isinstance(proposal, TurnProposal)

            # F080: before a new round begins, let the neutral judge end the run
            # early if the members have already converged. Returns a terminal
            # reason ("verdict_reached") to finalize now, else None.
            judge_reason = await self._maybe_judge_between_rounds(proposal.round)
            if judge_reason is not None:
                return await self._finalize(
                    judge_reason,
                    {"judged_round": self._last_judged_round, "via": "judge"},
                )

            # F081: end a credibility run when the debate stops producing new
            # claims/sources/reviews (novelty exhaustion) — agreement alone
            # never stops it.
            novelty_reason = self._maybe_credibility_novelty_stop(proposal.round)
            if novelty_reason is not None:
                return await self._finalize(novelty_reason, {"via": "novelty"})

            member = self._resolve_member(proposal.member_id)

            # F129 no-PM fallback for generic Council topologies. Resolve the
            # concrete route before EVERY route-dependent boundary below.
            if str(member.get("model_mode") or "single") == "multi":
                from errorta_council.coding.model_assignment import (
                    bind_member_route,
                    make_assignment,
                )
                from errorta_council.coding.model_availability import (
                    available_route_ids,
                    resolve_route_availability,
                )
                from errorta_council.coding.model_catalog import load_catalog
                from errorta_council.coding.model_selector import NoCapableModel, select

                pool = [str(route) for route in member.get("model_pool", []) if str(route)]
                availability = resolve_route_availability(pool)
                selected = select(
                    pool, available_route_ids(availability), load_catalog(pool), "mid",
                    task_type="investigation",
                )
                if isinstance(selected, NoCapableModel):
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.MEMBER_SKIPPED,
                        status=EventStatus.BLOCKED,
                        payload={"reason": selected.reason, "blocked": True},
                        member_id=member["id"], round=proposal.round,
                        writer=self._writer,
                    )
                    terminal = await self._apply_post_skip_policy(
                        reason_code=selected.reason, member=member,
                        snapshot=self._member_snapshot(member), proposal=proposal,
                    )
                    if terminal is not None:
                        return terminal
                    continue
                assignment = make_assignment(
                    task_id=f"turn-{proposal.turn_index}",
                    member_id=str(member["id"]), route_id=selected.route_id,
                    task_type="investigation", difficulty_tier="mid",
                    rationale=selected.rationale, source="selector",
                )
                member = bind_member_route(member, assignment)
                self._store.append_event(
                    self._meta.id,
                    type=EventType.MODEL_ASSIGNED,
                    status=EventStatus.COMPLETED,
                    payload={
                        "assignment_id": assignment.assignment_id,
                        "route_id": assignment.route_id,
                        "source": assignment.source,
                        "difficulty_tier": assignment.difficulty_tier,
                    },
                    member_id=member["id"], round=proposal.round,
                    writer=self._writer,
                )

            # Resource guard.
            snapshot = self._member_snapshot(member)
            self._store.append_event(
                self._meta.id,
                type=EventType.LOCAL_RESOURCE_CHECK_STARTED,
                status=EventStatus.PENDING,
                payload={"member_id": member["id"]},
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                writer=self._writer,
            )
            admission = await self._guard.admit(proposal=proposal, member=member)
            if not admission.admitted:
                self._store.append_event(
                    self._meta.id,
                    type=EventType.MEMBER_SKIPPED,
                    status=EventStatus.BLOCKED,
                    payload={"reason": admission.reason_code or "blocked",
                             "blocked": True},
                    member_id=member["id"],
                    member_snapshot=snapshot,
                    round=proposal.round,
                    writer=self._writer,
                )
                terminal = await self._apply_post_skip_policy(
                    reason_code=admission.reason_code or "blocked",
                    member=member, snapshot=snapshot, proposal=proposal,
                )
                if terminal is not None:
                    return terminal
                continue

            # Context build.
            self._store.append_event(
                self._meta.id,
                type=EventType.CONTEXT_BUILD_STARTED,
                status=EventStatus.PENDING,
                payload={},
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                writer=self._writer,
            )
            _, prior_events = self._store.read_run(self._meta.id)
            # Topology may freeze visibility for this turn (e.g. consensus
            # deliberation round 1: every member sees the same blind
            # transcript). When ``transcript_cursor`` is set, slice
            # prior_events to that point before context build.
            cursor_override = getattr(proposal, "transcript_cursor", None)
            if cursor_override is not None and cursor_override >= 0:
                prior_events = prior_events[:cursor_override]
            context_member = self._member_for_context(member)
            context = await self._ctx.build(
                run_meta=self._meta, member=context_member, transcript=prior_events
            )
            # Phase 3 (F031-05) fail-closed: a BlockedContextResult flows
            # back as ``blocked=True``. Map to MEMBER_SKIPPED, then route
            # through the shared post-skip policy helper so context-blocks
            # respect ``stop_behavior=ask`` exactly the way admission
            # failures do.
            if context.get("blocked"):
                blocked_reason = str(
                    context.get("blocked_reason") or "context_blocked"
                )
                self._store.append_event(
                    self._meta.id,
                    type=EventType.MEMBER_SKIPPED,
                    status=EventStatus.BLOCKED,
                    payload={
                        "reason": blocked_reason,
                        "blocked": True,
                        "context_id": context.get("context_id"),
                        "manifest_id": context.get("manifest_id"),
                    },
                    member_id=member["id"],
                    member_snapshot=snapshot,
                    round=proposal.round,
                    writer=self._writer,
                )
                terminal = await self._apply_post_skip_policy(
                    reason_code=blocked_reason,
                    member=member, snapshot=snapshot, proposal=proposal,
                )
                if terminal is not None:
                    return terminal
                continue
            built_payload: dict[str, Any] = {"context_id": context["context_id"]}
            if context.get("manifest_id"):
                built_payload["manifest_id"] = context["manifest_id"]
            self._store.append_event(
                self._meta.id,
                type=EventType.CONTEXT_BUILT,
                status=EventStatus.COMPLETED,
                payload=built_payload,
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                writer=self._writer,
            )
            steward_meta = dict(
                (context.get("metadata") or {}).get("steward") or {}
            )
            if steward_meta.get("packet_id") and not steward_meta.get("fallback"):
                self._store.append_event(
                    self._meta.id,
                    type=EventType.STEWARD_PACKET_USED,
                    status=EventStatus.COMPLETED,
                    payload={
                        "packet_id": steward_meta.get("packet_id"),
                        "recipient_member_id": member["id"],
                        "context_id": context["context_id"],
                        "recent_full_message_count": steward_meta.get(
                            "recent_full_message_count", 0
                        ),
                    },
                    member_id=member["id"],
                    member_snapshot=snapshot,
                    round=proposal.round,
                    writer=self._writer,
                )
            elif steward_meta.get("fallback") and steward_meta.get("reason") == "packet_missing":
                self._store.append_event(
                    self._meta.id,
                    type=EventType.STEWARD_PACKET_FAILED,
                    status=EventStatus.FAILED,
                    payload={
                        "reason": "packet_missing",
                        "recipient_member_id": member["id"],
                        "context_id": context["context_id"],
                    },
                    member_id=member["id"],
                    member_snapshot=snapshot,
                    round=proposal.round,
                    writer=self._writer,
                )

            # Dispatch with per-turn timeout (invariant 9: no automatic retry).
            # The adapter surfaces the payload's destination_scope / egress_class
            # so the gateway boundary re-check (invariant 5) can compare them
            # against the resolved route before any provider HTTP.
            from errorta_council.gateway_local import LocalCouncilModelRequest
            route_id = str(
                member.get("gateway_route_id")
                or member.get("route_id")
                or member["id"]
            )
            destination_scope = str(
                context.get("destination_scope")
                or ("fake" if member.get("provider") == "fake" else "local")
            )
            egress_class = str(context.get("egress_class") or "local")
            request = LocalCouncilModelRequest(
                role=member.get("role", "member"),
                route_id=route_id,
                provider=member.get("provider", "local"),
                model=member.get("model", ""),
                messages=self._credibility_messages(
                    context["messages"], member, proposal,
                ),
                max_output_tokens=self._max_output_tokens_for(member),
                temperature=member.get("temperature", 0.2),
                timeout_seconds=self._per_turn_timeout_for(member),
                metadata={
                    "context_id": context["context_id"],
                    "member_id": member["id"],
                    "destination_scope": destination_scope,
                    "egress_class": egress_class,
                    "context_efficiency": context.get("metadata", {}).get(
                        "context_efficiency", {}
                    ),
                },
                cache_hints=list(context.get("cache_hints") or []),
            )
            self._store.append_event(
                self._meta.id,
                type=EventType.MEMBER_CALL_STARTED,
                status=EventStatus.RUNNING,
                payload={"context_id": context["context_id"]},
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                writer=self._writer,
            )
            try:
                result = await asyncio.wait_for(
                    self._gateway.call(request),
                    timeout=self._per_turn_timeout_for(member),
                )
            except asyncio.TimeoutError:
                self._store.append_event(
                    self._meta.id,
                    type=EventType.MEMBER_FAILED,
                    status=EventStatus.FAILED,
                    payload={"reason": ReasonCode.LOCAL_TIMEOUT.value, "retryable": True},
                    member_id=member["id"],
                    member_snapshot=snapshot,
                    round=proposal.round,
                    writer=self._writer,
                )
                if self._stop_on_member_failure():
                    return self._emit_terminal(ReasonCode.LOCAL_TIMEOUT.value, "failed")
                self._guard.release(turn_id=f"{member['id']}-{proposal.round}")
                continue
            except Exception as exc:
                overflow = classify_context_overflow(exc)
                if overflow is not None:
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.MEMBER_FAILED,
                        status=EventStatus.FAILED,
                        payload={
                            **overflow.to_event_payload(retryable=True),
                            "context_id": context["context_id"],
                            "retry": "aggressive_tool_result_compaction",
                        },
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        writer=self._writer,
                    )
                    retry_member = {
                        **dict(member),
                        "force_tool_result_compaction": "aggressive",
                    }
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.CONTEXT_BUILD_STARTED,
                        status=EventStatus.PENDING,
                        payload={
                            "retry_reason": "context_window_exceeded",
                            "retry": "aggressive_tool_result_compaction",
                        },
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        writer=self._writer,
                    )
                    context = await self._ctx.build(
                        run_meta=self._meta,
                        member=self._member_for_context(retry_member),
                        transcript=prior_events,
                    )
                    if context.get("blocked"):
                        blocked_reason = str(
                            context.get("blocked_reason") or "context_blocked"
                        )
                        self._store.append_event(
                            self._meta.id,
                            type=EventType.MEMBER_SKIPPED,
                            status=EventStatus.BLOCKED,
                            payload={
                                "reason": blocked_reason,
                                "blocked": True,
                                "context_id": context.get("context_id"),
                                "manifest_id": context.get("manifest_id"),
                                "retry_reason": "context_window_exceeded",
                            },
                            member_id=member["id"],
                            member_snapshot=snapshot,
                            round=proposal.round,
                            writer=self._writer,
                        )
                        terminal = await self._apply_post_skip_policy(
                            reason_code=blocked_reason,
                            member=member,
                            snapshot=snapshot,
                            proposal=proposal,
                        )
                        if terminal is not None:
                            return terminal
                        continue
                    built_payload = {
                        "context_id": context["context_id"],
                        "retry_reason": "context_window_exceeded",
                    }
                    if context.get("manifest_id"):
                        built_payload["manifest_id"] = context["manifest_id"]
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.CONTEXT_BUILT,
                        status=EventStatus.COMPLETED,
                        payload=built_payload,
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        writer=self._writer,
                    )
                    destination_scope = str(
                        context.get("destination_scope")
                        or ("fake" if member.get("provider") == "fake" else "local")
                    )
                    egress_class = str(context.get("egress_class") or "local")
                    request = LocalCouncilModelRequest(
                        role=member.get("role", "member"),
                        route_id=route_id,
                        provider=member.get("provider", "local"),
                        model=member.get("model", ""),
                        messages=context["messages"],
                        max_output_tokens=self._max_output_tokens_for(member),
                        temperature=member.get("temperature", 0.2),
                        timeout_seconds=self._per_turn_timeout_for(member),
                        metadata={
                            "context_id": context["context_id"],
                            "member_id": member["id"],
                            "destination_scope": destination_scope,
                            "egress_class": egress_class,
                            "context_efficiency": context.get("metadata", {}).get(
                                "context_efficiency", {}
                            ),
                            "retry_reason": "context_window_exceeded",
                        },
                        cache_hints=list(context.get("cache_hints") or []),
                    )
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.MEMBER_CALL_STARTED,
                        status=EventStatus.RUNNING,
                        payload={
                            "context_id": context["context_id"],
                            "retry_reason": "context_window_exceeded",
                        },
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        writer=self._writer,
                    )
                    try:
                        result = await asyncio.wait_for(
                            self._gateway.call(request),
                            timeout=self._per_turn_timeout_for(member),
                        )
                    except asyncio.TimeoutError:
                        self._store.append_event(
                            self._meta.id,
                            type=EventType.MEMBER_FAILED,
                            status=EventStatus.FAILED,
                            payload={
                                "reason": ReasonCode.LOCAL_TIMEOUT.value,
                                "retryable": True,
                                "retry_reason": "context_window_exceeded",
                            },
                            member_id=member["id"],
                            member_snapshot=snapshot,
                            round=proposal.round,
                            writer=self._writer,
                        )
                        if self._stop_on_member_failure():
                            return self._emit_terminal(
                                ReasonCode.LOCAL_TIMEOUT.value,
                                "failed",
                            )
                        self._guard.release(turn_id=f"{member['id']}-{proposal.round}")
                        continue
                    except Exception as retry_exc:
                        retry_overflow = classify_context_overflow(retry_exc)
                        if retry_overflow is not None:
                            fail_payload = {
                                **retry_overflow.to_event_payload(retryable=False),
                                "context_id": context["context_id"],
                                "retry": "exhausted",
                            }
                            terminal_reason = "context_window_exceeded"
                        else:
                            fail_payload = {
                                "reason": ReasonCode.GATEWAY_ERROR.value,
                                "retryable": False,
                                "detail": _exc_detail(retry_exc),
                                "retry_reason": "context_window_exceeded",
                            }
                            terminal_reason = ReasonCode.GATEWAY_ERROR.value
                        self._store.append_event(
                            self._meta.id,
                            type=EventType.MEMBER_FAILED,
                            status=EventStatus.FAILED,
                            payload=fail_payload,
                            member_id=member["id"],
                            member_snapshot=snapshot,
                            round=proposal.round,
                            writer=self._writer,
                        )
                        if self._stop_on_member_failure():
                            return self._emit_terminal(terminal_reason, "failed")
                        self._guard.release(turn_id=f"{member['id']}-{proposal.round}")
                        continue
                    # Retry succeeded; fall through to normal settle path with
                    # the rebuilt compacted context and retry result.
                    pass
                else:
                    self._store.append_event(
                        self._meta.id,
                        type=EventType.MEMBER_FAILED,
                        status=EventStatus.FAILED,
                        payload={"reason": ReasonCode.GATEWAY_ERROR.value, "retryable": False,
                                 "detail": _exc_detail(exc)},
                        member_id=member["id"],
                        member_snapshot=snapshot,
                        round=proposal.round,
                        writer=self._writer,
                    )
                    if self._stop_on_member_failure():
                        return self._emit_terminal(ReasonCode.GATEWAY_ERROR.value, "failed")
                    self._guard.release(turn_id=f"{member['id']}-{proposal.round}")
                    continue
            payload: dict[str, Any] = {
                "content": result.content,
                "provider": result.provider,
                "model": result.model,
                "duration_ms": result.duration_ms,
                "is_thinking_burn": result.is_thinking_burn,
            }
            if self._digest_enabled_for(member):
                parsed = parse_digest_v1(
                    result.content,
                    known_citations=self._known_citation_ids(),
                )
                if parsed.ok and parsed.digest is not None:
                    payload["digest"] = parsed.digest
                    if parsed.warnings:
                        payload["digest_warnings"] = list(parsed.warnings)
                else:
                    warnings = list(parsed.warnings or ["digest_parse_failed"])
                    payload["dialect_fallback"] = True
                    payload["digest_warnings"] = warnings
                    self._maybe_emit_dialect_downgrade(
                        member=member,
                        snapshot=snapshot,
                        proposal=proposal,
                        context_id=context["context_id"],
                        warnings=warnings,
                    )

            usage = {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            }
            cache_read = getattr(result, "cache_read_input_tokens", None)
            cache_write = getattr(result, "cache_write_input_tokens", None)
            if cache_read is not None:
                usage["cache_read_input_tokens"] = cache_read
            if cache_write is not None:
                usage["cache_write_input_tokens"] = cache_write

            if hasattr(self._ctx, "reconcile_usage"):
                try:
                    ratio = self._ctx.reconcile_usage(
                        context_id=context["context_id"],
                        provider=result.provider,
                        model=result.model,
                        reported_input_tokens=result.input_tokens,
                    )
                    if ratio is not None:
                        payload["token_calibration_factor"] = ratio
                except Exception:
                    pass

            # Settle: emit MEMBER_MESSAGE.
            message_event = self._store.append_event(
                self._meta.id,
                type=EventType.MEMBER_MESSAGE,
                status=EventStatus.COMPLETED,
                payload=payload,
                member_id=member["id"],
                member_snapshot=snapshot,
                round=proposal.round,
                usage=usage,
                writer=self._writer,
            )
            # F081: at a credibility claim turn, run the entailment gate now so
            # admission state is durable mid-run (incremental). Fail-soft.
            if self._is_credibility_run() and self._require_entailment():
                try:
                    await self._run_entailment_for_message(
                        member["id"], result.content, proposal.round
                    )
                except Exception:
                    pass
            tool_terminal = await self._maybe_handle_tool_call(
                content=result.content,
                member=member,
                snapshot=snapshot,
                proposal=proposal,
                context_id=context["context_id"],
                parent_event_id=message_event.id,
            )
            if tool_terminal is not None:
                return tool_terminal
            child_terminal = await self._maybe_handle_child_task(
                content=result.content,
                member=member,
                snapshot=snapshot,
                proposal=proposal,
                parent_event_id=message_event.id,
            )
            if child_terminal is not None:
                return child_terminal
            # Record the answer-of-record for the terminal FINAL_ANSWER event.
            # Thinking-burn outputs (no visible answer) never become the
            # final answer. A finalizer's message takes precedence.
            if not result.is_thinking_burn:
                answer = {
                    "content": result.content,
                    "member_id": member["id"],
                    "round": proposal.round,
                }
                self._last_answer = answer
                if self._is_finalizer_member(member):
                    self._last_finalizer_answer = answer
            self._guard.release(turn_id=f"{member['id']}-{proposal.round}")
            steward_terminal = self._maybe_build_steward_packet(proposal=proposal)
            if steward_terminal is not None:
                return steward_terminal

            # Layer counter fields onto the latest meta atomically — using
            # merge_meta_fields keeps us race-safe vs. concurrent
            # RunControl.submit_decision / request_cancel writes.
            counters = self._read_counters()
            self._meta = self._store.merge_meta_fields(
                self._meta.id,
                completed_messages_by_member=dict(counters.completed_messages_by_member),
                total_messages_completed=counters.total_messages_completed,
            )
