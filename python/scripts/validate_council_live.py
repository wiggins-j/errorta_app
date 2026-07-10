"""Live Council validation against real Ollama models.

Drives build_and_run with the real LocalGateway (no scripting) across several
room configurations and prints the actual model behavior so we can validate the
results. Non-deterministic by nature — this is validation evidence, not a CI
test (the deterministic guarantees live in tests/council/test_room_config_matrix.py).

Usage:
    ERRORTA_HOME=$(mktemp -d) python scripts/validate_council_live.py [model]
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import LocalGateway
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType

# The real council models live on the example-host server, reached via the Ollama
# host Errorta is configured with (set ERRORTA_OLLAMA_HOST when running this).
GEMMA = "gemma3:27b"
MISTRAL = "mistral-small3.1:latest"
QWEN = "qwen3.5:9b"


def member(mid: str, model: str, *, role="member", ctx="prompt_only",
           trans="all_messages", max_out=None, system_prompt=""):
    m = {
        "id": mid, "enabled": True, "role": role,
        "provider": "local", "model": model,
        "gateway_route_id": f"local.ollama.{model}",
        "context_access": ctx, "transcript_access": trans,
        "system_prompt": system_prompt,
    }
    # Leave the budget unset so the engine applies its per-model default
    # (reasoning models like qwen3.5 get 8192, others 2048). Only pin it when
    # a case wants to test an explicit override.
    if max_out is not None:
        m["turn_limits"] = {"max_output_tokens": max_out}
        m["max_output_tokens"] = max_out
    return m


def cli_member(mid: str, provider: str, model: str, *, role="member",
               ctx="prompt_only", trans="all_messages", system_prompt=""):
    """A subscription-CLI member (F040): claude_cli / codex_cli."""
    return {
        "id": mid, "enabled": True, "role": role,
        "provider": provider, "model": model,
        "gateway_route_id": f"{provider}.{model}",
        "context_access": ctx, "transcript_access": trans,
        "system_prompt": system_prompt,
    }


def room(rid, members, *, kind="round_robin", consensus_threshold=None,
         finalization=None, efficiency=None, steward=None, remote=False):
    topo = {"kind": kind}
    if consensus_threshold is not None:
        topo["consensus_threshold"] = consensus_threshold
    r = {
        "id": rid, "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages", "allow_full_context": True,
        "members": members, "topology": topo,
        "finalization_policy": finalization or {"mode": "transcript_only", "finalizer_member_id": None},
    }
    if remote:
        # Permit remote egress so subscription-CLI / API members run.
        r["context_policy"] = {
            "require_confirmation_for_remote_context": False,
            "require_confirmation_for_full_context": False,
        }
        r["corpus_policy"] = {"max_egress_class": "remote_eligible"}
        r["residency"] = {"destination_scope": "remote"}
    if efficiency:
        r["context_efficiency"] = efficiency
    if steward:
        r["steward_policy"] = steward
    return r


async def run_case(name, rm, prompt, *, rounds, msgs, runs_dir):
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    store = RunStore(runs_dir=runs_dir)
    meta = store.create_run(room_id=rm["id"], room_snapshot=rm, prompt=prompt, corpus_ids=[])
    gw = LocalGateway()
    policy = SchedulerPolicy(max_rounds=rounds, max_messages_per_member=msgs,
                             per_turn_timeout_seconds=120)
    # Reasoning members get a 300s floor in the scheduler; a 3-round, 3-member
    # case of heavy models can run long, so give the whole case generous room.
    final = await asyncio.wait_for(
        build_and_run(run_store=store, run_meta=meta, policy=policy,
                      gateway_meta=gw, hardware_scan_present=True),
        timeout=2400.0,
    )
    _, events = store.read_run(meta.id)
    reason = ""
    done = [e for e in events if e.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)]
    if done:
        reason = (done[-1].payload or {}).get("reason", "")
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]
    msgs_ev = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    print(f"\n=== {name} ===")
    print(f"  status={final.status} reason={reason} member_messages={len(msgs_ev)}")
    for m in msgs_ev:
        c = (m.payload or {}).get("content", "").replace("\n", " ")[:90]
        print(f"    [{m.member_id} r{m.round}] {c}")
    if fa:
        c = (fa[-1].payload or {}).get("content", "").replace("\n", " ")[:120]
        print(f"  FINAL_ANSWER (from {fa[-1].payload.get('member_id')}): {c}")
    packets = [e for e in events if e.type == EventType.STEWARD_PACKET_CREATED]
    if packets:
        print(f"  steward_packets_created={len(packets)}")
    return final.status, reason, len(msgs_ev), bool(fa), len(packets)


async def main():
    base = Path(tempfile.mkdtemp())
    results = []

    # 1. round_robin — the real 3-model council (Gem/Mist/Qwen), 1 round.
    r1 = await run_case(
        "round_robin / Gemma + Mistral + Qwen / 1 round",
        room("live-rr", [member("Gem", GEMMA), member("Mist", MISTRAL),
                         member("Qwen", QWEN)]),
        "In one sentence, what is the capital of France?",
        rounds=1, msgs=1, runs_dir=base / "rr",
    )
    results.append(("round_robin (3 real models)",
                    r1[0] == "completed" and r1[2] == 3 and r1[3]))

    # 2. single_finalizer — Mistral thinks, Gemma decides.
    r2 = await run_case(
        "single_finalizer / Mistral → Gemma decides",
        room("live-fin",
             [member("Mist", MISTRAL),
              member("Gem", GEMMA, role="finalizer")],
             finalization={"mode": "single_finalizer", "finalizer_member_id": "Gem"}),
        "Name one good potato chip brand. One short sentence.",
        rounds=1, msgs=1, runs_dir=base / "fin",
    )
    results.append(("single_finalizer→finalizer is final",
                    r2[0] == "completed" and r2[3]))

    # 3. consensus_deliberation + digest_v1, 3 rounds — Gemma + Qwen.
    r3 = await run_case(
        "consensus_deliberation / digest_v1 / Gemma + Qwen / 3 rounds",
        room("live-cd",
             [member("Gem", GEMMA, system_prompt="Be concise."),
              member("Qwen", QWEN, system_prompt="Be concise.")],
             kind="consensus_deliberation",
             efficiency={"deliberation_dialect": "digest_v1"}),
        "What is 2+2? Agree on the answer.",
        rounds=3, msgs=3, runs_dir=base / "cd",
    )
    results.append(("consensus flow runs", r3[0] == "completed"))

    # 4. telegraphic style → short answers. Uses the two non-reasoning models
    #    (Gemma + Mistral); a small intermediate cap on a reasoning model would
    #    just force a thinking-burn, so telegraphic + reasoner is a poor combo.
    r4 = await run_case(
        "telegraphic style / Gemma + Mistral",
        room("live-tel", [member("Gem", GEMMA), member("Mist", MISTRAL)],
             efficiency={"deliberation_style": "telegraphic",
                         "intermediate_max_output_tokens": 128}),
        "List two pizza toppings.",
        rounds=1, msgs=1, runs_dir=base / "tel",
    )
    results.append(("telegraphic runs", r4[0] == "completed" and r4[2] == 2))

    # 5. The actual scenario you hit — angry-skeptic persona, 3 models, consensus.
    r5 = await run_case(
        "persona / angry skeptic Gem / 3 models / consensus / 3 rounds",
        room("live-persona",
             [member("Gem", GEMMA, system_prompt=(
                 "You are an angry, combative skeptic. You disagree at first and "
                 "are very hard to convince. Only concede when given an airtight, "
                 "evidence-backed argument, and say so explicitly when you do.")),
              member("Mist", MISTRAL),
              member("Qwen", QWEN)],
             kind="consensus_deliberation",
             efficiency={"deliberation_dialect": "digest_v1"}),
        "Determine the best brand of potato chips. Reach agreement if you can.",
        rounds=3, msgs=3, runs_dir=base / "persona",
    )
    results.append(("persona debate runs", r5[0] == "completed"))

    # 6. Council Steward (F038) — the "context leader". Gemma is the steward;
    #    after round 1 it produces a curated packet the members get in round 2
    #    instead of the full transcript. Expect >=1 steward packet created.
    r6 = await run_case(
        "steward (context leader) / Gemma steward / Mist+Qwen members / 2 rounds",
        room("live-steward",
             [member("Mist", MISTRAL, trans="all_messages"),
              member("Qwen", QWEN, trans="all_messages")],
             steward={
                 "enabled": True,
                 "assignment": {"mode": "external", "name": "Steward",
                                "gateway_route_id": f"local.ollama.{GEMMA}",
                                "provider_kind": "local"},
                 "cadence": "after_each_round",
                 "recipient_mode": "shared",
             }),
        "Briefly: are kettle chips healthier than regular chips?",
        rounds=2, msgs=2, runs_dir=base / "steward",
    )
    results.append(("steward produces a packet",
                    r6[0] == "completed" and r6[4] >= 1))

    # 7. THE MARQUEE — a heterogeneous council: example-host Ollama (Gemma) +
    #    Claude Pro subscription (claude_cli) + ChatGPT subscription (codex_cli),
    #    all deliberating together through the one engine. Proves F040
    #    subscription members run alongside local models in a real council.
    r7 = await run_case(
        "HETEROGENEOUS / Gemma(Ollama) + Claude(sub CLI) + ChatGPT(sub CLI) / 1 round",
        room("live-hetero",
             [member("Gem", GEMMA),
              cli_member("ClaudeSub", "claude_cli", "haiku"),
              cli_member("ChatGPTSub", "codex_cli", "default")],
             remote=True),
        "In one short sentence: what is the capital of France?",
        rounds=1, msgs=1, runs_dir=base / "hetero",
    )
    results.append(("heterogeneous council (local + 2 subscriptions)",
                    r7[0] == "completed" and r7[2] == 3 and r7[3]))

    print("\n\n========== VALIDATION SUMMARY ==========")
    allok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        allok = allok and ok
    print("=" * 40)
    print("ALL LIVE CASES OK" if allok else "SOME LIVE CASES FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    asyncio.run(main())
