"""Phase-0 deterministic fake run writer (invariant 10).

Drives ``RunStore`` to write a short, ordered transcript without any
real scheduler, topology, context router, or provider. The Phase-1
scheduler in F031-09 *replaces* this function with the same store
contract, so this is intentionally marked as scaffolding.

Use it from route tests, recovery tests, and the byte-identity gate.
"""
from __future__ import annotations

from .members.fake import FakeCouncilMember
from .run_store import RunStore
from .schema import EventStatus, EventType, MemberSnapshot


def _snapshot(member_id: str) -> MemberSnapshot:
    return MemberSnapshot(
        member_id=member_id, name=f"Fake {member_id}", role="answerer",
        provider_display="Fake", model_display="deterministic",
        locality="fake", context_access="prompt_only",
        transcript_access="own_messages", catalog_version=None,
    )


def run_fake_council(
    store: RunStore,
    run_id: str,
    *,
    member_ids: list[str],
    fail: bool = False,
    cancel_after: int | None = None,
) -> None:
    """Drive ``run_id`` through a small ordered transcript.

    Phase 1: acquires a transient writer token so the post-Fix-4 RunStore
    accepts the appends; releases on exit so future scheduler writes can
    acquire fresh ownership.
    """
    token = store.acquire_writer(run_id)
    try:
        store.append_event(
            run_id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={"member_ids": list(member_ids), "fake_members": True},
            writer=token,
        )
        for idx, mid in enumerate(member_ids):
            member = FakeCouncilMember(member_id=mid)
            if cancel_after is not None and idx == cancel_after:
                store.append_event(
                    run_id,
                    type=EventType.RUN_CANCEL_REQUESTED,
                    status=EventStatus.CANCEL_REQUESTED,
                    payload={"requested_by": "user", "reason": "test_cancel",
                             "in_flight_event_id": None},
                    writer=token,
                )
                store.append_event(
                    run_id,
                    type=EventType.MEMBER_CANCELLED,
                    status=EventStatus.CANCELLED,
                    payload={}, member_id=mid, member_snapshot=_snapshot(mid),
                    writer=token,
                )
                store.append_event(
                    run_id,
                    type=EventType.RUN_CANCELLED,
                    status=EventStatus.CANCELLED,
                    payload={"terminal_reason": "cancelled"},
                    writer=token,
                )
                return
            if fail and idx == len(member_ids) - 1:
                store.append_event(
                    run_id,
                    type=EventType.MEMBER_FAILED,
                    status=EventStatus.FAILED,
                    payload={"error_code": "fake_injected_failure"},
                    member_id=mid, member_snapshot=_snapshot(mid),
                    writer=token,
                )
                store.append_event(
                    run_id,
                    type=EventType.RUN_FAILED,
                    status=EventStatus.FAILED,
                    payload={"terminal_reason": "failed"},
                    writer=token,
                )
                return
            store.append_event(
                run_id,
                type=EventType.MEMBER_MESSAGE,
                status=EventStatus.COMPLETED,
                payload={"content": member.canned_content, "finish_reason": "stop"},
                member_id=mid, member_snapshot=_snapshot(mid),
                writer=token,
            )
        store.append_event(
            run_id,
            type=EventType.RUN_COMPLETED,
            status=EventStatus.COMPLETED,
            payload={"terminal_reason": "topology_exhausted"},
            writer=token,
        )
    finally:
        store.release_writer(token)
