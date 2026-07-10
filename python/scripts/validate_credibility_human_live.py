"""Live validation — a real credibility run (forced web research + claim +
discussion) against local Ollama, then run the SAME humanizer the UI/phone use
over the actual model output to prove the simple view reads like people arguing
while quoting citations — not robotic "verified — X:c1" lines.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11434 \
     ERRORTA_SEARXNG_URL=http://127.0.0.1:8790 \
     PYTHONPATH=$PWD python scripts/validate_credibility_human_live.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import LocalGateway
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType
from errorta_mobile.projections import humanize_credibility

MODEL = "qwen2.5:3b-instruct-q4_K_M"
SEARX = os.environ.get("ERRORTA_SEARXNG_URL", "http://127.0.0.1:8790")


def member(mid):
    return {
        "id": mid, "enabled": True, "role": "member",
        "provider": "local", "model": MODEL,
        "gateway_route_id": f"local.ollama.{MODEL}",
        "context_access": "prompt_only", "transcript_access": "all_messages",
        "system_prompt": "",
    }


def tool_gateway():
    from errorta_tools.builtins import register_builtins
    from errorta_tools.gateway import DefaultToolGateway
    register_builtins()
    return DefaultToolGateway()


async def main():
    base = Path(tempfile.mkdtemp())
    (base / "runs").mkdir(parents=True, exist_ok=True)
    store = RunStore(runs_dir=base / "runs")
    room = {
        "id": "live-cred",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": [member("Ada"), member("Boole"), member("Cantor")],
        "topology": {"kind": "round_robin"},
        "finalization_policy": {"mode": "credibility_report", "finalizer_member_id": None},
        "credibility_policy": {"enabled": True, "require_search": True, "require_fetch": True},
        "tool_policy": {
            "web_search": {"enabled": True, "searxng_url": SEARX},
            "web_fetch": {"enabled": True},
        },
        "context_policy": {
            "require_confirmation_for_remote_context": False,
            "require_confirmation_for_full_context": False,
        },
        "corpus_policy": {"max_egress_class": "remote_eligible"},
    }
    meta = store.create_run(
        room_id=room["id"], room_snapshot=room,
        prompt="Is a hot dog a sandwich? Argue it out and reach a verdict.",
        corpus_ids=[])
    policy = SchedulerPolicy(max_rounds=2, max_messages_per_member=2,
                             per_turn_timeout_seconds=120)
    final = await asyncio.wait_for(
        build_and_run(run_store=store, run_meta=meta, policy=policy,
                      gateway_meta=LocalGateway(), hardware_scan_present=True,
                      tool_gateway=tool_gateway()),
        timeout=900.0,
    )
    _, events = store.read_run(meta.id)
    sources = [e for e in events if e.type == EventType.CREDIBILITY_SOURCE_CAPTURED]
    msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]

    print("\n========== FETCHED SOURCES ==========")
    for s in sources:
        print("  •", (s.payload or {}).get("url", "")[:100])

    print("\n========== SIMPLE VIEW (humanized, as the UI/phone render it) ==========")
    robotic = 0
    raw_json = 0
    for m in msgs:
        raw = (m.payload or {}).get("content", "")
        human = humanize_credibility(raw)
        print(f"\n[{m.member_id} r{m.round}]\n  {human[:400]}")
        if "verified — " in human or "Peer review:" in human:
            robotic += 1
        if human.strip().startswith("{") or '"reviews"' in human or '"claims"' in human:
            raw_json += 1

    print("\n========== CHECKS ==========")
    checks = {
        "forced research captured >=1 real source": len(sources) >= 1,
        "members produced messages": len(msgs) >= 2,
        "no robotic 'verified — X:c1' / 'Peer review:' lines": robotic == 0,
        "no raw JSON leaked into the simple view": raw_json == 0,
        "run completed": final.status == "completed",
    }
    ok = True
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\nRESULT:", "ALL CHECKS PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
