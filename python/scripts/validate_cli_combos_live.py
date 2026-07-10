"""Live validation of the subscription-CLI providers ACROSS council configs.

Closes the gap from the main live matrix: there the CLIs only ran in a single
round-robin round. Here claude_cli + codex_cli run in consensus/digest_v1,
as a finalizer, under telegraphic style, and across multiple rounds — the
combinations that exercise digest emission, the efficiency path, finalization,
and peer reaction for the subscription members.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11435 python scripts/validate_cli_combos_live.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import validate_council_live as V  # reuse room/member/cli_member/run_case


async def main():
    base = Path(tempfile.mkdtemp())
    results = []

    # 1. Consensus + digest_v1, two subscription CLIs, 3 rounds. Do the real
    #    Claude/ChatGPT responses emit digest_v1 JSON the consensus detector
    #    reads, and can they converge?
    r1 = await V.run_case(
        "CLI consensus/digest_v1 / Claude + ChatGPT / 3 rounds",
        V.room("cli-consensus",
               [V.cli_member("ClaudeSub", "claude_cli", "haiku", system_prompt="Be concise."),
                V.cli_member("ChatGPTSub", "codex_cli", "default", system_prompt="Be concise.")],
               kind="consensus_deliberation",
               efficiency={"deliberation_dialect": "digest_v1"},
               remote=True),
        "What is 2+2? Agree on the answer.",
        rounds=3, msgs=3, runs_dir=base / "c1",
    )
    results.append(("CLI consensus/digest runs", r1[0] == "completed"))

    # 2. Single finalizer where the FINALIZER is a subscription CLI.
    r2 = await V.run_case(
        "CLI single_finalizer / Gemma member -> Claude(sub) finalizes",
        V.room("cli-fin",
               [V.member("Gem", V.GEMMA),
                V.cli_member("ClaudeFin", "claude_cli", "haiku", role="finalizer")],
               finalization={"mode": "single_finalizer", "finalizer_member_id": "ClaudeFin"},
               remote=True),
        "Name one good potato chip brand. One short sentence.",
        rounds=1, msgs=1, runs_dir=base / "c2",
    )
    results.append(("CLI finalizer is final answer",
                    r2[0] == "completed" and r2[3]))

    # 3. Telegraphic style with subscription CLIs (the efficiency path).
    r3 = await V.run_case(
        "CLI telegraphic / Claude + ChatGPT",
        V.room("cli-tel",
               [V.cli_member("ClaudeSub", "claude_cli", "haiku"),
                V.cli_member("ChatGPTSub", "codex_cli", "default")],
               efficiency={"deliberation_style": "telegraphic",
                           "intermediate_max_output_tokens": 128},
               remote=True),
        "List two pizza toppings.",
        rounds=1, msgs=1, runs_dir=base / "c3",
    )
    results.append(("CLI telegraphic runs", r3[0] == "completed" and r3[2] == 2))

    # 4. Multi-round round-robin with two subscription CLIs (peer reaction:
    #    each sees the other's prior message).
    r4 = await V.run_case(
        "CLI multi-round / Claude + ChatGPT / 2 rounds (peer visibility)",
        V.room("cli-multi",
               [V.cli_member("ClaudeSub", "claude_cli", "haiku", trans="all_messages"),
                V.cli_member("ChatGPTSub", "codex_cli", "default", trans="all_messages")],
               remote=True),
        "Briefly debate: is a hotdog a sandwich? Refer to each other.",
        rounds=2, msgs=2, runs_dir=base / "c4",
    )
    results.append(("CLI multi-round runs", r4[0] == "completed" and r4[2] == 4))

    print("\n\n========== CLI-COMBO VALIDATION SUMMARY ==========")
    allok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        allok = allok and ok
    print("=" * 50)
    print("ALL CLI-COMBO CASES OK" if allok else "SOME CLI-COMBO CASES FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    asyncio.run(main())
