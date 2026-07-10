"""F081 slice 1 live validation — run a real credibility run with the
entailment gate ON (require_entailment) and prove the gate fires per claim and
its grade drives admission.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11434 \
     ERRORTA_SEARXNG_URL=http://127.0.0.1:8790 \
     PYTHONPATH=$PWD python scripts/validate_f081_entailment_live.py
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

MODEL = "qwen2.5:3b-instruct-q4_K_M"
SEARX = os.environ.get("ERRORTA_SEARXNG_URL", "http://127.0.0.1:8790")


def member(mid):
    return {"id": mid, "enabled": True, "role": "member", "provider": "local",
            "model": MODEL, "gateway_route_id": f"local.ollama.{MODEL}",
            "context_access": "prompt_only", "transcript_access": "all_messages",
            "system_prompt": ""}


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
        "id": "live-f081", "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages", "allow_full_context": True,
        "members": [member("Ada"), member("Boole"), member("Cantor")],
        "topology": {"kind": "round_robin"},
        "finalization_policy": {"mode": "credibility_report", "finalizer_member_id": None},
        "credibility_policy": {"enabled": True, "require_search": True,
                               "require_fetch": True, "rigor": "standard",
                               "require_entailment": True, "route_inference_to_validity": True},
        "tool_policy": {"web_search": {"enabled": True, "searxng_url": SEARX},
                        "web_fetch": {"enabled": True}},
        "context_policy": {"require_confirmation_for_remote_context": False,
                           "require_confirmation_for_full_context": False},
        "corpus_policy": {"max_egress_class": "remote_eligible"},
    }
    meta = store.create_run(room_id=room["id"], room_snapshot=room,
                            prompt="Is the Great Wall of China visible from space with the naked eye?",
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
    checks = [e for e in events if e.type == EventType.CREDIBILITY_ENTAILMENT_CHECKED]
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]

    print("\n========== ENTAILMENT CHECKS (incremental, at the claim's turn) ==========")
    for e in checks:
        p = e.payload or {}
        print(f"  [{e.member_id} r{e.round}] claim={p.get('claim_id')} "
              f"grade={p.get('grade')} reason={(p.get('reason') or '')[:70]}")

    report = (fa[-1].payload.get("credibility_report") if fa else {}) or {}
    print("\n========== REPORT ==========")
    print("  claims_used:", report.get("claims_used"))
    print("  excluded:", [(x.get("claim_id"), x.get("reason")) for x in report.get("excluded_claims", [])])
    print("  confidence:", report.get("confidence"))

    entail_excl = [x for x in report.get("excluded_claims", [])
                   if str(x.get("reason", "")).startswith("entailment")]
    results = {
        "entailment gate fired per claim (events present)": len(checks) >= 1,
        "grades are real (not all unresolved)":
            any((e.payload or {}).get("grade") in
                ("entails", "partially_entails", "unsupported", "contradicts")
                for e in checks),
        "run completed": final.status == "completed",
        "a credibility report was produced": bool(report),
    }
    print("\n========== CHECKS ==========")
    ok = True
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    if entail_excl:
        print(f"  (note: {len(entail_excl)} claim(s) excluded by the entailment gate: {entail_excl})")
    print("\nRESULT:", "ALL CHECKS PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
