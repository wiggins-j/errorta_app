"""F087-04 — superpowers-skills guardrail.

Makes the obra/superpowers skills the default-on working discipline for Coding
Mode: maps each role to its skills, frames each member's turn with the role's
skill directive, gates dev completion on TDD, and names the role's skill for CLI
coding agents that load skills natively. The guardrail is a per-project checkbox
that defaults ON whenever Coding Mode is selected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .topology import DEV, PM, REVIEWER, TESTER

# Role -> superpowers skills (first entry is the role's primary discipline).
ROLE_SKILLS: dict[str, list[str]] = {
    PM: ["brainstorming", "writing-plans", "dispatching-parallel-agents",
         "subagent-driven-development"],
    DEV: ["test-driven-development", "executing-plans", "systematic-debugging"],
    REVIEWER: ["requesting-code-review", "receiving-code-review"],
    TESTER: ["verification-before-completion"],
    # workspace/merge discipline (applied by F087-05, not a turn-taking role).
    "workspace": ["using-git-worktrees", "finishing-a-development-branch"],
}

# Distilled one-line directives injected into a member's turn framing.
SKILL_DIRECTIVES: dict[str, str] = {
    "test-driven-development":
        "Write a failing test FIRST, then the minimal code to pass it. A task is "
        "not done until its test passes.",
    "brainstorming":
        "Explore intent and constraints before designing; present a design and "
        "get approval before implementation.",
    "writing-plans":
        "Turn the goal into bite-sized, independently-testable tasks before "
        "writing code.",
    "requesting-code-review":
        "Produce a structured verdict (approve / request_changes) with concrete, "
        "actionable findings — never a vague 'looks fine'.",
    "receiving-code-review":
        "Address each finding explicitly; concede a point only when the evidence "
        "is airtight.",
    "verification-before-completion":
        "Run the code and confirm it actually does what it should before marking "
        "anything complete — observe behavior, don't assume.",
    "systematic-debugging":
        "Reproduce, isolate, and fix root cause — not symptoms.",
    "executing-plans": "Implement one task at a time; commit frequently.",
    "dispatching-parallel-agents":
        "Decompose into independent work items and assign them to the right role.",
    "subagent-driven-development":
        "Direct workers task-by-task; verify each deliverable before moving on.",
    "using-git-worktrees": "Work in an isolated worktree; never touch the user's tree.",
    "finishing-a-development-branch":
        "Produce a clean, reviewable diff for human accept at the milestone.",
}

# Task types that legitimately need no test (the TDD gate exempts them).
_NO_TEST_TASK_TYPES = ("docs", "chore", "spec", "plan", "research")


@dataclass(frozen=True)
class SkillsGuardrailPolicy:
    enabled: bool = True  # default ON when Coding Mode is selected


def skills_for_role(role: str) -> list[str]:
    return list(ROLE_SKILLS.get(role, []))


def primary_skill(role: str) -> Optional[str]:
    skills = ROLE_SKILLS.get(role)
    return skills[0] if skills else None


def frame_turn(role: str, *, enabled: bool, phase: Optional[str] = None) -> dict[str, Any]:
    """The skill framing injected into a member's turn. Empty when the guardrail
    is off (escape hatch)."""
    if not enabled:
        return {"skill": None, "phase": None, "directive": ""}
    skill = phase if (phase and phase in SKILL_DIRECTIVES) else primary_skill(role)
    return {
        "skill": skill,
        "phase": phase or skill,
        "directive": SKILL_DIRECTIVES.get(skill or "", ""),
    }


def tdd_gate(*, role: str, task_type: str, has_passing_test: bool,
             enabled: bool) -> tuple[bool, str]:
    """Can a task transition to done? With the guardrail on, a dev implementation
    task requires a passing test; docs/chore/etc. are exempt; non-dev roles are
    not TDD-gated."""
    if not enabled:
        return True, "guardrail off"
    if role != DEV:
        return True, "not a dev task"
    if task_type in _NO_TEST_TASK_TYPES:
        return True, f"no test required for {task_type}"
    if has_passing_test:
        return True, "test passes"
    return False, "dev task requires a passing test (TDD)"


def cli_skill_prompt(role: str) -> str:
    """Prompt fragment naming the role's skills for a CLI coding agent that
    loads the superpowers skills natively (claude_cli / codex_cli)."""
    skills = skills_for_role(role)
    if not skills:
        return ""
    return (
        f"Operate under the superpowers skill discipline. Primary skill: "
        f"'{skills[0]}'. Available skills for your role: {', '.join(skills)}. "
        f"Load and follow them."
    )


def enforce_dev_completion(reconciler: Any, task: Any, *, task_type: str,
                           has_passing_test: bool, enabled: bool) -> str:
    """Apply the TDD gate when a dev claims a task done.

    Gate passes -> complete the task (spawns the review task). Gate fails ->
    do NOT mark done; spawn a 'write a failing test' task and re-queue the impl
    task to depend on it. Returns 'done' or 'needs_test'."""
    ok, _reason = tdd_gate(role=DEV, task_type=task_type,
                           has_passing_test=has_passing_test, enabled=enabled)
    if ok:
        reconciler.complete_dev_task(task)
        return "done"
    test_task = reconciler.ledger.add_task(
        title=f"write a failing test for: {task.title}", role=DEV,
        detail=f"TDD gate: task {task.task_id} needs a failing test first.",
    )
    reconciler.ledger.update_task(task.task_id, state="todo",
                                  depends_on=[test_task.task_id])
    return "needs_test"


def record_turn_skill(store: Any, *, member_id: str, task_id: str, role: str,
                      phase: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Record which skill/phase a member operated under (ledger skills.jsonl)."""
    skill = phase or primary_skill(role)
    if not skill:
        return None
    return store.record_skill_use(member_id=member_id, task_id=task_id,
                                  skill=skill, phase=phase or skill)


# --- persistence (per-project checkbox, default ON) -------------------------

def load_guardrail(store: Any) -> SkillsGuardrailPolicy:
    path = store.dir / "skills_guardrail.json"
    if not path.exists():
        return SkillsGuardrailPolicy()  # default ON
    import json
    raw = json.loads(path.read_text("utf-8"))
    return SkillsGuardrailPolicy(enabled=bool(raw.get("enabled", True)))


def save_guardrail(store: Any, policy: SkillsGuardrailPolicy) -> SkillsGuardrailPolicy:
    from .ledger import _atomic_write_json
    _atomic_write_json(store.dir / "skills_guardrail.json", {"enabled": policy.enabled})
    return policy
