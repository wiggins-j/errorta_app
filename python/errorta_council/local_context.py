"""LocalContextBuilder — sealed per-member payload (invariant 5 prep)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from errorta_council.schema import CouncilEvent, EventType, RunMeta


_ROLE_PROMPTS: dict[str, str] = {
    "scholar": "You are a careful scholar. Cite reasoning step-by-step.",
    "skeptic": "You are a critical skeptic. Challenge assumptions in the prior answers.",
    "member": "You are a Council member. Give a concise, grounded answer.",
    "answerer": "You are a Council answerer. Provide a short, direct answer.",
}


@dataclass(frozen=True)
class LocalContextBuilder:
    max_input_chars: int = 8_000

    async def build(
        self, *, run_meta: RunMeta, member: dict, transcript: Iterable[CouncilEvent]
    ) -> dict:
        role = member.get("role", "member")
        system_prompt = _ROLE_PROMPTS.get(role, _ROLE_PROMPTS["member"])
        messages: list[dict[str, str]] = [
            {"role": "system", "content": f"[role: {role}] {system_prompt}"},
        ]
        prior_member_msgs = [
            e for e in transcript if e.type == EventType.MEMBER_MESSAGE
        ]
        included: list[dict[str, str]] = []
        budget = self.max_input_chars - len(system_prompt) - len(run_meta.prompt)
        for ev in reversed(prior_member_msgs):
            content = (ev.payload or {}).get("content", "")
            if not content:
                continue
            speaker = ev.member_id or "unknown"
            line = f"[{speaker}]: {content}"
            if len(line) > budget:
                continue
            included.append({"role": "assistant", "content": line})
            budget -= len(line)
        messages.extend(reversed(included))
        messages.append({"role": "user", "content": run_meta.prompt})

        payload_bytes = json.dumps(messages, sort_keys=True).encode("utf-8")
        sha = hashlib.sha256(payload_bytes).hexdigest()[:16]
        return {
            "context_id": f"ctx-{run_meta.id}-{member['id']}-{sha}",
            "messages": messages,
        }
