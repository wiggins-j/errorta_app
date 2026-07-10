"""F047 — bundled example Council profiles (Omnigent Polly/Debby inspired).

Secret-free, route-light (local Ollama defaults so they import without any
provider configuration), and policy-safe (coding profile is propose-only writes
with NO code-exec until the user grants it).
"""
from __future__ import annotations

from typing import Any

from .schema import PROFILE_FORMAT_VERSION


def _member(mid, name, role, system_prompt, *, route="local.ollama.llama3.2:3b",
            ctx="prompt_only", trans="all_messages"):
    return {
        "id": mid,
        "name": name,
        "role": role,
        "enabled": True,
        "provider_kind": "local",
        "gateway_route_id": route,
        "model": route.split(".", 2)[-1] if "." in route else route,
        "context_access": ctx,
        "transcript_access": trans,
        "system_prompt": system_prompt,
    }


def brainstorm_council() -> dict[str, Any]:
    return {
        "format_version": PROFILE_FORMAT_VERSION,
        "name": "Brainstorm Council",
        "description": "Fan a question out to several members, then converge on "
        "a written consensus. No tools.",
        "members": [
            _member("ideator-1", "Ideator A", "member",
                    "Generate bold, distinct ideas. Disagree productively."),
            _member("ideator-2", "Ideator B", "member",
                    "Stress-test ideas and surface risks the others miss."),
            _member("synthesizer", "Synthesizer", "member",
                    "Find the strongest shared direction across the members."),
        ],
        "topology": {
            "kind": "consensus_deliberation",
            "max_rounds": 3,
            "max_messages_per_member": 3,
        },
        "finalization_policy": {"mode": "consensus_report"},
        "steward_policy": {
            "enabled": True,
            "assignment": {"mode": "member", "member_id": "synthesizer"},
        },
    }


def coding_council() -> dict[str, Any]:
    return {
        "format_version": PROFILE_FORMAT_VERSION,
        "name": "Coding Council",
        "description": "Orchestrator + programmer + reviewer + tester. "
        "Propose-only writes; NO code execution until you grant it.",
        "members": [
            _member("orchestrator", "Orchestrator", "member",
                    "Break the task into steps and delegate to the programmer, "
                    "reviewer, and tester. Keep the plan tight."),
            _member("programmer", "Programmer", "member",
                    "Propose code changes as diffs. Do not assume you can run "
                    "anything until told."),
            _member("reviewer", "Reviewer", "member",
                    "Review the programmer's diffs for correctness and risk."),
            _member("tester", "Tester", "member",
                    "Describe the tests that should run and what they prove."),
        ],
        "topology": {
            "kind": "round_robin",
            "max_rounds": 4,
            "max_messages_per_member": 4,
        },
        "finalization_policy": {"mode": "single_finalizer",
                                "finalizer_member_id": "orchestrator"},
        "steward_policy": {
            "enabled": True,
            "assignment": {"mode": "member", "member_id": "orchestrator"},
        },
        # Tool REQUESTS (still gated by F041 consent at runtime). Reading +
        # proposing writes are requested; code execution is deliberately OFF.
        "tool_policy": {
            "code_read": {"enabled": True},
            "code_write": {"enabled": True, "mode": "propose_only"},
            "code_exec": {"enabled": False},
            "require_first_use_consent": True,
        },
        "child_run_policy": {"enabled": False},
    }


def credibility_council() -> dict[str, Any]:
    return {
        "format_version": PROFILE_FORMAT_VERSION,
        "name": "Credibility Council",
        "description": "Source-backed factual answers: members research the web, "
        "submit claim packets, peer-review each other's citations (credidation), "
        "and the leader writes a report citing only verified sources.",
        "members": [
            _member("researcher-1", "Researcher 1", "member",
                    "Research the question with web_search and web_fetch, then "
                    "emit a JSON claim packet citing the URLs you fetched. In "
                    "later rounds, review peers' claims against their cited sources."),
            _member("researcher-2", "Researcher 2", "member",
                    "Research independently, emit a JSON claim packet citing "
                    "fetched URLs, then peer-review the other members' claims."),
            _member("leader", "Leader", "finalizer",
                    "Synthesize the verified, admitted claims into a clear answer "
                    "with a source map. Cite only admitted sources."),
        ],
        "topology": {
            "kind": "credibility",
            # Headroom for research (tool turns) + claim packet + credidation.
            "max_rounds": 4,
            "max_messages_per_member": 4,
        },
        "finalization_policy": {"mode": "credibility_report",
                                "finalizer_member_id": "leader"},
        # Internet tools are REQUIRED for this mode and still gated by F041
        # consent at runtime.
        "tool_policy": {
            "web_search": {"enabled": True},
            "web_fetch": {"enabled": True},
            "require_first_use_consent": True,
        },
        "credibility_policy": {
            "enabled": True,
            "strictness": "normal",
            "leader_member_id": "leader",
            "require_search": True,
            "require_fetch": True,
        },
        "child_run_policy": {"enabled": False},
    }


def all_examples() -> dict[str, dict[str, Any]]:
    return {
        "brainstorm-council": brainstorm_council(),
        "coding-council": coding_council(),
        "credibility-council": credibility_council(),
    }
