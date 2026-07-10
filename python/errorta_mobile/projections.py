"""Mobile-safe projections for desktop state."""
from __future__ import annotations

import json
import re
from typing import Any

from errorta_council.schema import CouncilEvent, EventType, RunMeta
from errorta_policy import PendingDecisionRecord

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?([\s\S]*?)\n?```$")


def _extract_json_object(s: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model reply that may wrap it in a ```json
    fence or surround it with preamble (mirrors the desktop simple view)."""
    body = s.strip()
    fence = _FENCE_RE.match(body)
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


def humanize_digest(s: str) -> str:
    """F036 digest_v1 dialect → readable prose, so the phone's simple transcript
    shows the member's stance + answer instead of a raw JSON envelope. Mirrors
    the desktop CouncilTranscript `humanizeDigest`. Non-digest text passes through."""
    if "digest_v1" not in s:
        return s
    obj = _extract_json_object(s)
    if not obj or obj.get("v") != "digest_v1":
        return s
    parts: list[str] = []
    answer = obj.get("answer_fragment") or obj.get("position")
    if isinstance(answer, str) and answer.strip():
        parts.append(answer.strip())
    claims = obj.get("claims") if isinstance(obj.get("claims"), list) else []
    claim_texts = [
        c["text"].strip()
        for c in claims
        if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()
    ]
    if claim_texts:
        parts.append("• " + "\n• ".join(claim_texts))
    delta = obj.get("delta")
    if isinstance(delta, str) and delta.strip() and delta != "no_changed_views":
        parts.append(f"(changed: {delta.strip()})")
    return "\n".join(parts) if parts else s


def _citation_host(source_ids: Any) -> str:
    """The website a claim cites, as a clean host (drops "www.", skips minted
    source ids like "src_0001"). Mirrors the desktop CouncilTranscript
    ``citationHost``. Returns "" when no URL citation is present."""
    if not isinstance(source_ids, list):
        return ""
    for sid in source_ids:
        raw = str(sid or "").strip()
        if not raw:
            continue
        m = re.match(r"https?://([^/\s:]+)", raw)
        if m:
            return m.group(1).lower().removeprefix("www.")
    return ""


def humanize_credibility(s: str) -> str:
    """F078 Credibility mode: a member replies with a JSON claim packet
    ({answer_fragment, claims:[{text, source_ids}]}) or a peer review
    ({reviews:[{claim_id, status}]}), sometimes fenced in ```json. Surface it as
    readable prose (with the cited website) so the phone matches the desktop
    simple view instead of showing raw JSON. Non-credibility text passes through."""
    obj = _extract_json_object(s)
    if not obj:
        return s
    if isinstance(obj.get("claims"), list):
        parts: list[str] = []
        answer = obj.get("answer_fragment") or obj.get("position")
        if isinstance(answer, str) and answer.strip():
            parts.append(answer.strip())
        texts: list[str] = []
        for c in obj["claims"]:
            if not isinstance(c, dict):
                continue
            text = c.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            cite = _citation_host(c.get("source_ids"))
            texts.append(f"{text.strip()} ({cite})" if cite else text.strip())
        if texts:
            parts.append("• " + "\n• ".join(texts))
        return "\n".join(parts) if parts else s
    if isinstance(obj.get("reviews"), list):
        # Prefer the member's own words reacting to the others.
        comment = obj.get("comment")
        if isinstance(comment, str) and comment.strip():
            return comment.strip()
        return _humanize_review_summary(obj["reviews"]) or s
    return s


def _humanize_review_summary(reviews: Any) -> str:
    """Plain-English summary of structured reviews, grouped by stance — never
    'verified — Claude:c1'."""
    verb = {
        "verified": "agree with",
        "partially_supported": "partly agree with",
        "unsupported": "am not convinced by",
        "contradicted": "disagree with",
    }
    by_stance: dict[str, list[str]] = {}
    for r in reviews if isinstance(reviews, list) else []:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("claim_id") or "").strip()
        if not cid:
            continue
        v = verb.get(str(r.get("status") or "").strip(), "note")
        by_stance.setdefault(v, []).append(cid)
    clauses = [f"I {v} {', '.join(ids)}" for v, ids in by_stance.items()]
    return ("; ".join(clauses) + ".") if clauses else ""


def _run_title(meta: RunMeta) -> str:
    prompt = str(getattr(meta, "prompt", "") or "").strip()
    if not prompt:
        return "Untitled run"
    first = " ".join(prompt.split())
    return first if len(first) <= 80 else f"{first[:77]}..."


def _attention_from_events(events: list[CouncilEvent]) -> tuple[bool, int]:
    # Net resolved decisions out of the created count — otherwise the phone
    # shows "needs attention" forever after the operator already approved.
    created = 0
    resolved = 0
    for event in events:
        if event.type == EventType.POLICY_DECISION_CREATED:
            created += 1
        elif event.type in {
            EventType.POLICY_DECISION_APPROVED,
            EventType.POLICY_DECISION_REJECTED,
            EventType.POLICY_DECISION_EXPIRED,
        }:
            resolved += 1
    pending = max(0, created - resolved)
    return pending > 0, pending


def run_projection(meta: RunMeta, events: list[CouncilEvent] | None = None) -> dict[str, Any]:
    """Return a compact mobile-safe run projection.

    The projection intentionally excludes event payload bodies, tool-result raw
    content, runner artifacts, and filesystem paths. F059 will expand this with
    explicit mobile transcript projections.
    """

    event_list = list(events or [])
    needs_attention, pending_count = _attention_from_events(event_list)
    room = meta.room_snapshot or {}
    return {
        "run_id": meta.id,
        "title": _run_title(meta),
        "status": meta.status,
        "room_id": room.get("id"),
        "room_name": room.get("name"),
        "started_at": meta.created_at,
        "updated_at": meta.updated_at,
        "needs_attention": needs_attention,
        "pending_decision_count": pending_count,
        "latest_summary": None,
        "source": "desktop",
    }


def event_projection(event: CouncilEvent) -> dict[str, Any]:
    payload = event.payload or {}
    out: dict[str, Any] = {
        "event_id": event.id,
        "sequence": event.sequence,
        "type": event.type.value,
        "created_at": event.created_at,
        "actor": _actor(event, payload),
        "body": _event_body(event, payload),
        "mobile_visibility": "visible",
    }
    if out["body"] is None:
        out["mobile_visibility"] = "metadata"
    return out


def event_projections(
    events: list[CouncilEvent],
    *,
    after_sequence: int = 0,
    max_events: int = 100,
) -> list[dict[str, Any]]:
    projected = [
        event_projection(event)
        for event in events
        if event.sequence > after_sequence
    ]
    if max_events > 0 and len(projected) > max_events:
        kept = projected[-max_events:]
        omitted = len(projected) - len(kept)
        first_kept = kept[0]["sequence"] if kept else after_sequence + 1
        summary = {
            "event_id": f"mobile-summary:{after_sequence}:{first_kept}",
            "sequence": max(after_sequence, first_kept - 1),
            "type": "mobile_summary",
            "created_at": kept[0]["created_at"] if kept else None,
            "actor": {"kind": "system", "id": None, "name": "Errorta"},
            "body": {
                "type": "summary",
                "text": f"{omitted} earlier events hidden on mobile.",
                "event_count": omitted,
            },
            "mobile_visibility": "summary",
        }
        return [summary, *kept]
    return projected


def pending_decision_projection(
    decision: PendingDecisionRecord,
    *,
    device: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision_class = _decision_class(decision)
    risk = decision.risk_class or _risk_for_decision_class(decision_class)
    capability = _capability_for_decision(decision_class, risk)
    caps = dict((device or {}).get("capabilities") or {})
    can_act = decision.state == "pending" and bool(caps.get(capability, False))
    safe_details = _safe_decision_details(decision.safe_request)
    return {
        "decision_id": decision.decision_id,
        "run_id": decision.run_id,
        "state": decision.state,
        "revision": _decision_revision(decision),
        "title": _decision_title(decision_class, safe_details),
        "summary": _decision_summary(decision_class, decision, safe_details),
        "risk": risk,
        "phase": decision.phase.value,
        "requester": _requester_projection(decision.requester),
        "decision_class": decision_class,
        "safe_details": safe_details,
        "actions": {
            "can_approve": can_act,
            "can_deny": can_act,
            "requires_confirmation": risk in {"medium", "high"},
            "required_capability": capability,
        },
        "created_at": decision.created_at,
        "expires_at": decision.metadata.get("expires_at"),
        "resolved_at": decision.resolved_at,
        "resolved_by": decision.resolved_by,
    }


def attention_run_projection(
    meta: RunMeta,
    *,
    pending_decision_count: int,
) -> dict[str, Any] | None:
    reasons: list[str] = []
    if pending_decision_count > 0:
        reasons.append("pending_decision")
    if meta.status == "failed":
        reasons.append("run_failed")
    if not reasons:
        return None
    room = meta.room_snapshot or {}
    return {
        "run_id": meta.id,
        "title": room.get("name") or "Errorta run",
        "status": meta.status,
        "room_id": room.get("id"),
        "room_name": room.get("name"),
        "needs_attention": True,
        "attention_reasons": reasons,
        "latest_attention_at": meta.updated_at,
        "pending_decision_count": pending_decision_count,
    }


def _actor(event: CouncilEvent, payload: dict[str, Any]) -> dict[str, Any]:
    if event.type == EventType.MOBILE_MESSAGE:
        return {
            "kind": "mobile_device",
            "id": payload.get("device_id"),
            "name": payload.get("display_name") or "iPhone",
        }
    if event.type == EventType.USER_INTERJECTION:
        # F049 interjection — attribute mobile-origin ones to the device.
        requested_by = str(payload.get("requested_by") or "")
        if requested_by.startswith("mobile_device:"):
            return {"kind": "mobile_device",
                    "id": requested_by.split(":", 1)[1], "name": "iPhone"}
        return {"kind": "user", "id": "operator", "name": "You"}
    if event.member_snapshot is not None:
        return {
            "kind": "member",
            "id": event.member_snapshot.member_id,
            "name": event.member_snapshot.name,
        }
    if event.member_id:
        return {"kind": "member", "id": event.member_id, "name": event.member_id}
    return {"kind": "system", "id": None, "name": "Errorta"}


def _event_body(
    event: CouncilEvent,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if event.type in {EventType.MEMBER_MESSAGE, EventType.FINAL_ANSWER}:
        text = str(payload.get("content") or payload.get("text") or "").strip()
        if not text:
            return None
        # Simple transcript: show prose, not the raw digest_v1 / credibility
        # JSON envelope (mirrors the desktop CouncilTranscript simple view).
        return {"format": "markdown", "text": humanize_credibility(humanize_digest(text))}
    if event.type == EventType.MOBILE_MESSAGE:
        text = str(payload.get("message") or "").strip()
        if not text:
            return None
        return {
            "format": "markdown",
            "text": text,
            "source": "mobile",
            "source_inbox_item_id": payload.get("source_inbox_item_id"),
        }
    if event.type == EventType.USER_INTERJECTION:
        # The user's live message (incl. mobile follow-ups, F049/F059) so the
        # phone shows its own message in the transcript.
        text = str(payload.get("content") or "").strip()
        if not text:
            return None
        return {"format": "markdown", "text": text, "source": "user"}
    if event.type in {
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_STARTED,
        EventType.TOOL_CALL_COMPLETED,
        EventType.TOOL_CALL_FAILED,
        EventType.TOOL_CALL_BLOCKED,
    }:
        return {
            "type": "tool_call",
            "tool_id": payload.get("tool_id"),
            "status": _tool_status(event),
            "summary": payload.get("summary") or payload.get("result_summary"),
            "content_sha256": payload.get("content_sha256")
            or payload.get("result_sha256")
            or payload.get("sha256"),
            "artifact_count": int(payload.get("artifact_count") or 0),
            "decision_id": payload.get("decision_id")
            or payload.get("pending_decision_id")
            or payload.get("policy_decision_id"),
        }
    if event.type == EventType.POLICY_DECISION_CREATED:
        return {
            "type": "pending_decision",
            "decision_id": payload.get("decision_id"),
            "phase": payload.get("phase"),
            "reason_code": payload.get("reason_code"),
        }
    if event.type == EventType.JUDGE_VERDICT:
        # F080: surface the neutral judge's decisive calls on the phone; hide
        # the routine "keep deliberating" ticks so the small screen stays clean.
        verdict = str(payload.get("verdict") or "")
        if verdict in {"continue", ""}:
            return None
        headline = {
            "reached": "members reached a verdict",
            "decide": "broke the tie",
            "no_consensus": "no consensus",
        }.get(verdict, verdict)
        reason = str(payload.get("reason") or "").strip()
        text = f"⚖️ Judge: {headline}" + (f" — {reason}" if reason else "")
        return {"format": "markdown", "text": text, "source": "judge"}
    if event.type in {
        EventType.RUN_STARTED,
        EventType.RUN_STATUS_CHANGED,
        EventType.RUN_CANCEL_REQUESTED,
        EventType.RUN_CANCELLED,
        EventType.RUN_FAILED,
        EventType.RUN_COMPLETED,
    }:
        return {
            "type": "run_status",
            "status": event.status.value,
            "reason": payload.get("reason") or payload.get("terminal_reason"),
        }
    return None


def _tool_status(event: CouncilEvent) -> str:
    mapping = {
        EventType.TOOL_CALL_REQUESTED: "requested",
        EventType.TOOL_CALL_STARTED: "running",
        EventType.TOOL_CALL_COMPLETED: "completed",
        EventType.TOOL_CALL_FAILED: "failed",
        EventType.TOOL_CALL_BLOCKED: "blocked",
    }
    return mapping.get(event.type, event.status.value)


def _decision_class(decision: PendingDecisionRecord) -> str:
    safe = decision.safe_request or {}
    reason = decision.reason_code
    tool_id = str(safe.get("tool_id") or "")
    if "merge" in reason:
        return "merge_back"
    if "code_write" in reason or tool_id == "code_write":
        return "code_write_auto_apply"
    if "code_exec" in reason or tool_id == "code_exec":
        return "code_exec"
    if "mcp" in reason:
        return "mcp_elicitation"
    if tool_id == "web_search" or "web_search" in reason:
        return "web_search_remote_egress"
    if tool_id == "web_fetch" or "web_fetch" in reason or "remote" in reason:
        return "web_fetch_remote_egress"
    return "tool_first_use"


def _risk_for_decision_class(decision_class: str) -> str:
    if decision_class in {"code_exec", "code_write_auto_apply"}:
        return "high"
    if decision_class in {
        "web_fetch_remote_egress",
        "web_search_remote_egress",
        "mcp_elicitation",
    }:
        return "medium"
    if decision_class == "merge_back":
        return "high"
    return "low"


def _capability_for_decision(decision_class: str, risk: str) -> str:
    if decision_class in {"web_fetch_remote_egress", "web_search_remote_egress"}:
        return "approve_remote_egress"
    if decision_class == "mcp_elicitation":
        return "approve_mcp_elicitation"
    if decision_class == "code_exec":
        return "approve_code_exec"
    if decision_class == "code_write_auto_apply":
        return "approve_code_write"
    if decision_class == "merge_back":
        return "approve_merge_back"
    if risk != "low":
        return "approve_remote_egress"
    return "approve_low_risk"


def _safe_decision_details(safe_request: dict[str, Any]) -> dict[str, Any]:
    allow = {
        "tool_id",
        "domain",
        "url_label",
        "args_sha256",
        "query_label",
        "server_label",
        "command_label",
        "path_label",
    }
    return {key: safe_request[key] for key in allow if key in safe_request}


def _requester_projection(requester: dict[str, Any]) -> dict[str, Any]:
    member_id = requester.get("member_id") or requester.get("id")
    return {
        "kind": "member" if member_id else "system",
        "id": member_id,
        "name": requester.get("name") or member_id or "Errorta",
    }


def _decision_title(decision_class: str, safe_details: dict[str, Any]) -> str:
    tool_id = safe_details.get("tool_id")
    if decision_class == "web_fetch_remote_egress":
        return "Allow web fetch"
    if decision_class == "web_search_remote_egress":
        return "Allow web search"
    if decision_class == "code_exec":
        return "Allow code execution"
    if decision_class == "code_write_auto_apply":
        return "Allow code write"
    if decision_class == "merge_back":
        return "Allow merge-back"
    if tool_id:
        return f"Allow {tool_id}"
    return "Allow policy action"


def _decision_summary(
    decision_class: str,
    decision: PendingDecisionRecord,
    safe_details: dict[str, Any],
) -> str:
    requester = _requester_projection(decision.requester)["name"]
    target = (
        safe_details.get("url_label")
        or safe_details.get("domain")
        or safe_details.get("tool_id")
        or decision.reason_code
    )
    return f"{requester} requests {decision_class.replace('_', ' ')}: {target}."


def _decision_revision(decision: PendingDecisionRecord) -> int:
    return 1 if decision.state == "pending" else 2


__all__ = [
    "attention_run_projection",
    "event_projection",
    "event_projections",
    "pending_decision_projection",
    "run_projection",
]
