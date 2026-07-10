"""Deterministic Steward Packet construction (F038 Slice 2)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from errorta_council.context.tokens import HeuristicEstimator
from errorta_council.schema import CouncilEvent, EventType, RunMeta

PACKET_FORMAT = "errorta.council_steward_packet.v1"
CONFIDENCE = {"high", "medium", "low"}


class StewardPacketError(ValueError):
    """Raised when a packet violates the F038 source-ref contract."""


def build_deterministic_packet(
    *,
    run_meta: RunMeta,
    events: list[CouncilEvent],
    created_at: str | None = None,
    created_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a source-referenced Steward Packet from durable run events."""
    created_at = created_at or _utcnow()
    created_by = created_by or {
        "mode": "deterministic",
        "member_id": None,
        "route_id": None,
    }
    consumed = [e for e in events if _is_consumed_event(e)]
    source_event_ids = [e.id for e in consumed]
    if consumed:
        from_sequence = min(e.sequence for e in consumed)
        to_sequence = max(e.sequence for e in consumed)
    else:
        from_sequence = 0
        to_sequence = 0

    prompt_source_ids = _prompt_source_ids(consumed)
    member_messages = [e for e in consumed if e.type == EventType.MEMBER_MESSAGE]
    member_positions = [_member_position(e) for e in member_messages]
    open_disagreements = _open_disagreements(member_messages)
    open_questions = _open_questions(member_messages)
    consensus = _current_consensus(member_positions, member_messages)
    next_action_sources = [member_messages[-1].id] if member_messages else prompt_source_ids

    packet: dict[str, Any] = {
        "format": PACKET_FORMAT,
        "run_id": run_meta.id,
        "created_at": created_at,
        "created_by": dict(created_by),
        "coverage": {
            "from_sequence": from_sequence,
            "to_sequence": to_sequence,
            "source_event_ids": source_event_ids,
        },
        "user_goal": {
            "text": run_meta.prompt,
            "source_event_ids": prompt_source_ids,
        },
        "current_consensus": consensus,
        "member_positions": member_positions,
        "open_disagreements": open_disagreements,
        "open_questions": open_questions,
        "risk_flags": [],
        "next_best_action": {
            "text": _next_best_action(open_disagreements, open_questions),
            "source_event_ids": next_action_sources,
        },
        "callout_recommendation": {
            "recommended": False,
            "target_id": None,
            "reason_code": None,
            "source_event_ids": [],
        },
    }
    content_sha256 = packet_content_sha256(packet)
    packet["packet_id"] = f"sp_{content_sha256[:16]}"
    packet["content_sha256"] = content_sha256
    packet["packet_stats"] = _packet_stats(packet, consumed)
    validate_steward_packet(packet)
    return packet


def validate_steward_packet(packet: dict[str, Any]) -> None:
    if packet.get("format") != PACKET_FORMAT:
        raise StewardPacketError("unsupported_packet_format")
    coverage = _dict(packet.get("coverage"))
    source_event_ids = _source_ids(coverage)
    if not source_event_ids:
        raise StewardPacketError("coverage_missing_source_event_ids")
    _require_sources(packet, "user_goal")
    _require_sources(packet, "current_consensus", allow_empty_text=True)
    _require_sources(packet, "next_best_action")
    for idx, item in enumerate(packet.get("member_positions") or []):
        _require_item_sources(item, f"member_positions[{idx}]")
        confidence = str(item.get("confidence") or "medium")
        if confidence not in CONFIDENCE:
            raise StewardPacketError(f"member_positions[{idx}]_bad_confidence")
    for idx, item in enumerate(packet.get("open_disagreements") or []):
        _require_item_sources(item, f"open_disagreements[{idx}]")
    for idx, item in enumerate(packet.get("open_questions") or []):
        _require_item_sources(item, f"open_questions[{idx}]")
    for idx, item in enumerate(packet.get("risk_flags") or []):
        _require_item_sources(item, f"risk_flags[{idx}]")
    rec = _dict(packet.get("callout_recommendation"))
    if rec.get("recommended"):
        _require_item_sources(rec, "callout_recommendation")


def packet_content_sha256(packet: dict[str, Any]) -> str:
    body = {
        k: v
        for k, v in packet.items()
        if k not in {"packet_id", "content_sha256", "packet_stats"}
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _packet_stats(packet: dict[str, Any], events: list[CouncilEvent]) -> dict[str, Any]:
    estimator = HeuristicEstimator()
    packet_tokens = estimator.estimate(
        json.dumps(packet, sort_keys=True),
        content_kind="json",
    )
    raw_text = "\n".join(_event_text(e) for e in events)
    raw_tokens = estimator.estimate(raw_text, content_kind="mixed") if raw_text else 1
    return {
        "estimated_tokens": packet_tokens,
        "source_event_count": len(events),
        "compression_ratio_estimate": round(packet_tokens / max(raw_tokens, 1), 4),
    }


def _member_position(event: CouncilEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    digest = payload.get("digest") if isinstance(payload.get("digest"), dict) else None
    if digest:
        claims = [
            str(c.get("text") or "")
            for c in digest.get("claims") or []
            if isinstance(c, dict) and c.get("text")
        ]
        confidence = _confidence_from_digest(digest)
        stance = str(digest.get("position") or payload.get("content") or "")
        reasons = claims[:4]
    else:
        confidence = "medium"
        stance = _truncate(str(payload.get("content") or ""), 320)
        reasons = []
    return {
        "member_id": event.member_id,
        "stance": stance,
        "confidence": confidence,
        "reasons": reasons,
        "source_event_ids": [event.id],
    }


def _open_disagreements(member_messages: list[CouncilEvent]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in member_messages:
        digest = event.payload.get("digest") if isinstance(event.payload, dict) else None
        if not isinstance(digest, dict):
            continue
        for item in digest.get("dispute") or []:
            text = _digest_item_text(item)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append({
                "topic": text,
                "sides": [],
                "source_event_ids": [event.id],
            })
    return out


def _open_questions(member_messages: list[CouncilEvent]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in member_messages:
        digest = event.payload.get("digest") if isinstance(event.payload, dict) else None
        if not isinstance(digest, dict):
            continue
        for raw in digest.get("open") or []:
            question = str(raw)
            if not question or question in seen:
                continue
            seen.add(question)
            out.append({"question": question, "source_event_ids": [event.id]})
    return out


def _current_consensus(
    positions: list[dict[str, Any]],
    member_messages: list[CouncilEvent],
) -> dict[str, Any]:
    if not positions:
        return {"text": "", "confidence": "low", "source_event_ids": _prompt_source_ids([])}
    last = positions[-1]
    source_ids = [e.id for e in member_messages[-3:]]
    return {
        "text": _truncate(str(last.get("stance") or ""), 360),
        "confidence": str(last.get("confidence") or "medium")
        if str(last.get("confidence") or "medium") in CONFIDENCE
        else "medium",
        "source_event_ids": source_ids,
    }


def _next_best_action(
    disagreements: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> str:
    if disagreements:
        return "Resolve the listed open disagreements before finalization."
    if questions:
        return "Answer the listed open questions before finalization."
    return "Continue with the configured Council topology."


def _confidence_from_digest(digest: dict[str, Any]) -> str:
    confidences = [
        str(c.get("confidence") or "")
        for c in digest.get("claims") or []
        if isinstance(c, dict)
    ]
    for c in confidences:
        if c in CONFIDENCE:
            return c
    return "medium"


def _is_consumed_event(event: CouncilEvent) -> bool:
    # F037 expert-callout answers are MEMBER_MESSAGE events with a non-member
    # target id. They are advisory side-channel turns and must not be folded
    # into member positions / consensus / coverage (they would present the
    # expert as an ordinary member). Exclude them.
    if event.type == EventType.MEMBER_MESSAGE and (event.payload or {}).get("is_callout"):
        return False
    return event.type in {
        EventType.RUN_STARTED,
        EventType.MEMBER_MESSAGE,
        EventType.FINAL_ANSWER,
        EventType.RUN_COMPLETED,
    }


def _prompt_source_ids(events: list[CouncilEvent]) -> list[str]:
    for event in events:
        if event.type == EventType.RUN_STARTED:
            return [event.id]
    return [events[0].id] if events else []


def _event_text(event: CouncilEvent) -> str:
    payload = dict(event.payload or {})
    content = payload.get("content")
    if content:
        return str(content)
    return json.dumps(payload, sort_keys=True)


def _digest_item_text(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("topic", "text", "claim", "id"):
            value = item.get(key)
            if value:
                return str(value)
        return json.dumps(item, sort_keys=True)
    return str(item)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _require_sources(packet: dict[str, Any], key: str, *, allow_empty_text: bool = False) -> None:
    item = _dict(packet.get(key))
    if allow_empty_text and not item.get("text"):
        return
    _require_item_sources(item, key)


def _require_item_sources(item: Any, label: str) -> None:
    if not _source_ids(_dict(item)):
        raise StewardPacketError(f"{label}_missing_source_event_ids")


def _source_ids(item: dict[str, Any]) -> list[str]:
    return [str(x) for x in (item.get("source_event_ids") or []) if str(x)]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = [
    "PACKET_FORMAT",
    "StewardPacketError",
    "build_deterministic_packet",
    "packet_content_sha256",
    "validate_steward_packet",
]
