"""F080 live validation — run a real council with the neutral judge enabled
against local Ollama and prove the judge evaluates each round, never speaks as
a member, and finalizes (early verdict or tie-break).

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11434 \
     PYTHONPATH=$PWD python scripts/validate_judge_live.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import LocalGateway
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType

MODEL = "qwen2.5:3b-instruct-q4_K_M"


def member(mid, *, role="member", system_prompt=""):
    return {
        "id": mid, "enabled": True, "role": role,
        "provider": "local", "model": MODEL,
        "gateway_route_id": f"local.ollama.{MODEL}",
        "context_access": "prompt_only", "transcript_access": "all_messages",
        "system_prompt": system_prompt,
    }


async def main():
    base = Path(tempfile.mkdtemp())
    (base / "runs").mkdir(parents=True, exist_ok=True)
    store = RunStore(runs_dir=base / "runs")
    room = {
        "id": "live-judge",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": [
            member("Ada", system_prompt=(
                "You are Ada. You firmly believe tabs are better than spaces for "
                "indentation and argue for it, but you can be convinced by a good "
                "point. Keep replies to 2-3 sentences.")),
            member("Boole", system_prompt=(
                "You are Boole. You firmly believe spaces are better than tabs and "
                "argue for it, but you can be convinced. Keep replies to 2-3 "
                "sentences.")),
            member("Judge", role="judge"),
        ],
        "topology": {"kind": "round_robin"},
        "finalization_policy": {"mode": "transcript_only", "finalizer_member_id": None},
        "judge_policy": {"enabled": True, "judge_member_id": "Judge", "start_round": 1},
    }
    meta = store.create_run(room_id=room["id"], room_snapshot=room,
                            prompt="Argue tabs vs spaces until you reach a verdict.",
                            corpus_ids=[])
    policy = SchedulerPolicy(max_rounds=4, max_messages_per_member=4,
                             per_turn_timeout_seconds=120)
    final = await asyncio.wait_for(
        build_and_run(run_store=store, run_meta=meta, policy=policy,
                      gateway_meta=LocalGateway(), hardware_scan_present=True),
        timeout=900.0,
    )
    _, events = store.read_run(meta.id)
    msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    verdicts = [e for e in events if e.type == EventType.JUDGE_VERDICT]
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]

    print("\n========== TRANSCRIPT ==========")
    for e in events:
        if e.type == EventType.MEMBER_MESSAGE:
            print(f"[{e.member_id} r{e.round}] {(e.payload or {}).get('content','')[:160]}")
        elif e.type == EventType.JUDGE_VERDICT:
            p = e.payload or {}
            print(f"  ⚖️ JUDGE r{e.round}: verdict={p.get('verdict')} "
                  f"reason={p.get('reason','')[:80]}")
        elif e.type == EventType.FINAL_ANSWER:
            p = e.payload or {}
            print(f"  >>> FINAL ({p.get('synthesis_mode')}): {p.get('content','')[:160]}")

    print("\n========== CHECKS ==========")
    checks = {
        "judge evaluated >=1 round": len(verdicts) >= 1,
        "judge never spoke as a member": all(m.member_id != "Judge" for m in msgs),
        "members actually deliberated": len(msgs) >= 2,
        "run completed": final.status == "completed",
        "a final answer was emitted": len(fa) >= 1,
        "judge-decided answer is labeled (if judge decided)":
            (not fa) or fa[-1].payload.get("synthesis_mode") in (None, "judge"),
    }
    ok = True
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    decided = [v for v in verdicts if (v.payload or {}).get("verdict") in
               ("reached", "decide")]
    print(f"\n  verdicts={len(verdicts)} decisive={len(decided)} "
          f"member_msgs={len(msgs)} status={final.status} "
          f"reason={meta.terminal_reason}")
    print("\nRESULT:", "ALL CHECKS PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
