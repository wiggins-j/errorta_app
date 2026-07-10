"""Live validation of F049 — a mid-run user interjection steers REAL models.

Drives a 3-member round-robin against the real example-host Ollama models. After
member 2 speaks, the harness injects a user message (exactly what the
/interjection route does when the scheduler holds the writer). Member 3's
gateway request must carry the interjection, and member 3's REAL response is
printed so we can see it actually steered the deliberation.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11435 \
     ERRORTA_HOME=$(mktemp -d) python scripts/validate_f049_live.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import validate_council_live as V

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType

INTERJECTION = "Actually — optimize for MEMORY over speed; assume a tiny device."


class _InterjectingRealGateway(LocalGateway):
    """Real Ollama gateway that injects a user message during member 2's call."""

    def __init__(self, store: RunStore, run_id: str) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []
        self._store = store
        self._run_id = run_id

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        if request.metadata.get("member_id") == "m2":
            self._store.push_pending_control_event(
                self._run_id,
                event_spec={"type": "user_interjection", "status": "completed",
                            "payload": {"content": INTERJECTION, "author": "user",
                                        "requested_by": "user"}},
            )
        return await super().call(request)


async def main() -> None:
    runs_dir = Path(tempfile.mkdtemp()) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    store = RunStore(runs_dir=runs_dir)
    room = V.room(
        "f049-live",
        [V.member("m1", V.GEMMA, system_prompt="Propose a concrete design. Be brief."),
         V.member("m2", V.MISTRAL, system_prompt="Critique and refine. Be brief."),
         V.member("m3", V.GEMMA, system_prompt="Give the final design. Be brief.")],
    )
    room["topology"] = {"kind": "round_robin", "speaker_order": ["m1", "m2", "m3"]}
    meta = store.create_run(
        room_id=room["id"], room_snapshot=room,
        prompt="Design a data structure for a real-time game leaderboard.",
        corpus_ids=[],
    )
    gw = _InterjectingRealGateway(store, meta.id)
    await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=180),
            gateway_meta=gw, hardware_scan_present=True, gateway=gw,
        ),
        timeout=600,
    )

    results = []
    by_member = {r.metadata.get("member_id"): r for r in gw.requests}

    def _msgs(req):
        return "\n".join(m.get("content", "") for m in req.messages)

    # 1. Member 3's request carries the interjection; earlier members' did not.
    m3_sees = "m3" in by_member and INTERJECTION in _msgs(by_member["m3"])
    m1_clean = "m1" in by_member and INTERJECTION not in _msgs(by_member["m1"])
    m2_clean = "m2" in by_member and INTERJECTION not in _msgs(by_member["m2"])
    results.append(("m3 request carries the interjection", m3_sees))
    results.append(("m1 + m2 requests did NOT (read-once)", m1_clean and m2_clean))

    # 2. The interjection is durably recorded.
    _, events = store.read_run(meta.id)
    recorded = [e for e in events if e.type == EventType.USER_INTERJECTION]
    results.append(("interjection recorded as a transcript event", len(recorded) == 1))

    # 3. Member 3's REAL response (soft signal it steered toward "memory").
    m3_msg = next((e for e in events if e.type == EventType.MEMBER_MESSAGE
                   and e.member_id == "m3"), None)
    m3_text = (m3_msg.payload.get("content") if m3_msg else "") or ""
    steered = "memor" in m3_text.lower()
    print("\n=== member 3 response (after the user's memory-over-speed steer) ===")
    print(m3_text[:800])
    results.append(("m3 response references memory (soft steer signal)", steered))

    print("\n========== F049 LIVE VALIDATION ==========")
    allok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        allok = allok and ok
    print("=" * 42)
    # The soft steer signal is non-deterministic; require the hard checks.
    hard = all(ok for name, ok in results if "soft" not in name)
    print("F049 LIVE: HARD CHECKS OK" if hard else "F049 LIVE: HARD CHECKS FAILED")
    sys.exit(0 if hard else 1)


if __name__ == "__main__":
    asyncio.run(main())
