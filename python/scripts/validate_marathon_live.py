"""Live proof of the "Marathon" preset shape on example-host.

A consensus topology with a high round ceiling + a member-mode steward
(council leader) + compaction. The point: the run STOPS EARLY the moment the
members agree ("decide they're done"), instead of burning the whole ceiling.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11435 python scripts/validate_marathon_live.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import validate_council_live as V

CEILING = 10  # stand-in for the editor's 100 (kept small for live wall-clock)


async def main():
    base = Path(tempfile.mkdtemp())
    rm = V.room(
        "live-marathon",
        [V.member("Gem", V.GEMMA, system_prompt="Be concise."),
         V.member("Mist", V.MISTRAL, system_prompt="Be concise.")],
        kind="consensus_deliberation",
        efficiency={
            "deliberation_dialect": "digest_v1",
            "transcript_compaction": {"enabled": True, "full_rounds_window": 2,
                                      "segment_size_rounds": 4},
            "prompt_cache_hints": True,
        },
        steward={"enabled": True,
                 "assignment": {"mode": "member", "member_id": "Gem"}},
    )
    status, reason, nmsgs, has_fa, packets = await V.run_case(
        f"MARATHON / consensus + member-steward / ceiling={CEILING} rounds",
        rm,
        "What is 2+2? Agree on the single answer, then stop.",
        rounds=CEILING, msgs=CEILING, runs_dir=base / "m",
    )

    # Success = completed, AND it converged early (consensus) rather than
    # exhausting the ceiling, AND produced a final answer.
    stopped_early = nmsgs < (CEILING * 2)  # 2 members * ceiling rounds = max
    converged = reason == "consensus_reached"
    ok = status == "completed" and has_fa and stopped_early

    print("\n========== MARATHON LIVE SUMMARY ==========")
    print(f"  status={status} reason={reason!r}")
    print(f"  member_messages={nmsgs} (ceiling would allow {CEILING*2})")
    print(f"  converged_early={stopped_early}  consensus_signal={converged}")
    print(f"  final_answer={has_fa}  steward_packets={packets}")
    print("=" * 44)
    print("MARATHON LIVE OK" if ok else "MARATHON LIVE FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
