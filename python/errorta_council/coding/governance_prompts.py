"""F100 prompt builders for PM governance artifacts and planning reviews."""
from __future__ import annotations

import json

from .governance import (
    GovernanceArtifact,
    GovernanceFinding,
    GovernanceReview,
    GovernanceStore,
)
from .ledger import LedgerStore, format_focus_lines


def _current_focus_block(store: LedgerStore) -> str:
    """F137: the ordered active Current Focus set. When present, it is the
    operative SCOPE for the artifact under draft/review; the North Star is
    demoted to reference-only. Empty string when there is no active focus (a
    pure North-Star run behaves exactly as pre-F137)."""
    try:
        active = store.active_focuses()
    except Exception:
        active = []
    if not active:
        return ""
    return (
        "CURRENT FOCUS — scope this artifact to ONLY the following, in order. Do "
        "NOT write a whole-product spec/plan; plan just this focus set:\n"
        + "\n".join(format_focus_lines(active))
        + "\n"
    )


def _project_context(store: LedgerStore) -> str:
    try:
        project = store.get_project()
    except Exception:
        return "Project context unavailable.\n"
    focus = _current_focus_block(store)
    if focus:
        # F137: focus is the scope; the North Star is a guardrail for HOW to
        # build (consistency), NOT a list of things to build now.
        return (
            f"{focus}"
            "North Star (REFERENCE ONLY — a guardrail for consistency, not the "
            f"scope; do NOT expand this artifact to cover it): {project.north_star}\n"
            f"Definition of done (reference only): {project.definition_of_done}\n"
            f"Status: {project.status}\n"
        )
    return (
        f"North Star: {project.north_star}\n"
        f"Definition of done: {project.definition_of_done}\n"
        f"Status: {project.status}\n"
    )


def _pending_user_direction(store: LedgerStore) -> str:
    try:
        pending = store.list_unconsumed_interjections()
    except Exception:
        pending = []
    if not pending:
        return ""
    lines = "\n".join(f"- {p.get('message', '')}" for p in pending)
    return "AUTHORITATIVE USER DIRECTION:\n" + lines + "\n\n"


def _artifact_block(artifact: GovernanceArtifact | None) -> str:
    if artifact is None:
        return "None.\n"
    payload = {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.artifact_kind,
        "version": artifact.version,
        "state": artifact.state,
        "title": artifact.title,
        "body_markdown": artifact.body_markdown[:8000],
        "body_json": artifact.body_json,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _review_block(reviews: list[GovernanceReview]) -> str:
    if not reviews:
        return "None.\n"
    return json.dumps(
        [r.to_dict() for r in reviews[-3:]],
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _pm_context_packet(store: LedgerStore) -> str:
    try:
        from .runner import _grounding_packet_text
        return _grounding_packet_text("pm", store)
    except Exception:
        return ""


def _unresolved_findings_block(
    governance: GovernanceStore,
    artifact: GovernanceArtifact | None,
) -> str:
    """F100-02 A3 (RC7): when revising after ``changes_requested``, surface the
    latest review's specific findings (blocking first) so the PM addresses each
    one or explicitly defers it to the spec — without this the redraft prompt
    shows the body but never tells the PM what the reviewer objected to."""
    if artifact is None:
        return ""
    by_role = governance.latest_review_by_role(artifact.artifact_id)
    findings: list[GovernanceFinding] = []
    for review in by_role.values():
        if review.verdict != "approved":
            findings.extend(review.findings)
    if not findings:
        return ""
    # Blocking findings first so the PM resolves the load-bearing ones.
    ordered = sorted(findings, key=lambda f: (not f.blocking,))
    lines = "\n".join(
        f"- [{'BLOCKING' if f.blocking else 'non-blocking'}] {f.title}: {f.body}"
        for f in ordered
    )
    label = {
        "brainstorm": "brainstorm",
        "spec": "spec",
        "implementation_plan": "implementation plan",
    }.get(artifact.artifact_kind, artifact.artifact_kind.replace("_", " "))
    defer_note = {
        "brainstorm": '"deferred to spec: <reason>"',
        "spec": '"deferred to plan: <reason>"',
        "implementation_plan": '"deferred to implementation: <reason>"',
    }.get(artifact.artifact_kind, '"deferred: <reason>"')
    return (
        "UNRESOLVED REVIEW FINDINGS on the latest version — for EACH finding, "
        f"either revise the {label} to address it OR add a one-line "
        f"{defer_note} note. A round must change the document or "
        "record why not:\n" + lines + "\n\n"
    )


def build_pm_governance_prompt(
    *,
    store: LedgerStore,
    governance: GovernanceStore,
    phase: str,
) -> str:
    """Prompt the PM for the next governance artifact or revision."""
    try:
        from errorta_project_grounding.context_packets import ensure_pm_working_memory
        ensure_pm_working_memory(store)
    except Exception:
        pass
    latest_brainstorm = governance.latest_artifact("brainstorm")
    latest_spec = governance.latest_artifact("spec")
    latest_plan = governance.latest_artifact("implementation_plan")
    state = governance.load_state()
    redraft_findings = ""
    if phase in {"idle", "brainstorming"}:
        skill = "brainstorming"
        # F100-02 A3 (RC7): if this is a redraft after changes_requested, inject
        # the latest brainstorm review's findings + the address-or-defer rule.
        if (
            latest_brainstorm is not None
            and latest_brainstorm.state == "changes_requested"
        ):
            redraft_findings = _unresolved_findings_block(
                governance, latest_brainstorm)
        instruction = (
            "Create a brainstorm artifact. Clarify problem, audience, constraints, "
            "non-goals, open questions, and recommended direction. Do not create "
            "developer tasks yet."
        )
        schema = (
            '{"schema_version":"governance_turn.v1","role":"pm","intent":'
            '{"kind":"brainstorm_draft","title":"...","problem":"...",'
            '"audience":"...","constraints":[],"non_goals":[],"open_questions":[],'
            '"recommended_direction":"...","body_markdown":"...","source_refs":[]}}'
        )
    elif phase == "drafting_spec":
        skill = "writing-specs"
        # F100 bugfix (2026-06-25): a spec redraft after changes_requested must
        # also surface the reviewer's specific findings (same as brainstorm),
        # otherwise the PM re-drives the loop seeing only the old body — not what
        # the reviewer objected to — and the "continue" path can't actually
        # address the review.
        if latest_spec is not None and latest_spec.state == "changes_requested":
            redraft_findings = _unresolved_findings_block(governance, latest_spec)
        instruction = (
            "Create or revise a feature spec from the approved brainstorm. Include "
            "testable acceptance criteria. Do not create developer tasks yet."
        )
        schema = (
            '{"schema_version":"governance_turn.v1","role":"pm","intent":'
            '{"kind":"spec_draft","title":"...","body_markdown":"...",'
            '"acceptance_criteria":["..."],"source_refs":[]}}'
        )
    elif phase == "drafting_plan":
        skill = "writing-plans"
        # F100 bugfix (2026-06-25): same for a plan redraft after changes_requested.
        if latest_plan is not None and latest_plan.state == "changes_requested":
            redraft_findings = _unresolved_findings_block(governance, latest_plan)
        instruction = (
            "Create or revise an implementation plan from the approved spec. Split "
            "work into small independently reviewable slices. Each slice must have "
            "done_when, tests, and review_focus."
        )
        schema = (
            '{"schema_version":"governance_turn.v1","role":"pm","intent":'
            '{"kind":"plan_draft","title":"Implementation plan","body_markdown":"...",'
            '"slices":[{"slice_id":"S1","title":"...","detail":"...",'
            '"depends_on":[],"files":[],"done_when":["..."],"tests":["..."],'
            '"review_focus":["..."]}],"source_refs":[]}}'
        )
    elif phase == "awaiting_slice_approval":
        skill = "subagent-driven-development"
        instruction = (
            "Review the completed slice evidence and decide whether to accept the "
            "slice, request revision, or require a plan amendment."
        )
        schema = (
            '{"schema_version":"governance_turn.v1","role":"pm","intent":'
            '{"kind":"slice_acceptance","source_slice_id":"S1",'
            '"accepted":true,"rationale":"..."}}'
        )
    else:
        skill = "subagent-driven-development"
        instruction = "Propose the next governance action for this project."
        schema = (
            '{"schema_version":"governance_turn.v1","role":"pm","intent":'
            '{"kind":"brainstorm_draft","title":"...","problem":"...",'
            '"recommended_direction":"...","source_refs":[]}}'
        )

    return (
        f"{_pending_user_direction(store)}"
        f"{redraft_findings}"
        f"Operate under the '{skill}' superpowers discipline.\n"
        "You are the PM for a Coding Mode project. You drive the project through "
        "brainstorm -> spec -> approval -> plan -> approval -> development, but "
        "you cannot self-approve required gates.\n\n"
        f"Current governance state:\n{json.dumps(state.to_dict(), sort_keys=True)}\n\n"
        f"Project:\n{_project_context(store)}\n"
        f"{_pm_context_packet(store)}"
        f"Latest brainstorm:\n{_artifact_block(latest_brainstorm)}\n\n"
        f"Latest spec:\n{_artifact_block(latest_spec)}\n\n"
        f"Latest plan:\n{_artifact_block(latest_plan)}\n\n"
        f"Instruction: {instruction}\n\n"
        "Reply with ONLY this JSON shape, no prose:\n"
        f"{schema}"
    )


def build_governance_review_prompt(
    *,
    store: LedgerStore,
    governance: GovernanceStore,
    artifact: GovernanceArtifact,
    reviewer_role: str = "reviewer",
) -> str:
    """Prompt a reviewer (or, in strict mode, the PM) to review a
    brainstorm/spec/plan artifact. ``reviewer_role`` drives the envelope role
    the model must echo so the strict dual-review path can route PM reviews."""
    reviews = governance.list_reviews(artifact_id=artifact.artifact_id)
    role = "pm" if reviewer_role == "pm" else "reviewer"
    role_note = (
        "You are the PM performing the second, independent governance review "
        "(strict mode dual review). "
        if role == "pm"
        else ""
    )
    # F100-02 A2: calibrate the rubric to the artifact stage. A brainstorm is
    # high-level — holding it to spec/implementation-readiness standards (and
    # treating open questions as defects) is exactly what looped the real run 37
    # times. Spec/plan keep the original rigor.
    if artifact.artifact_kind == "brainstorm":
        rubric = (
            "This is a BRAINSTORM — a HIGH-LEVEL artifact. Open questions, "
            "undecided options, and \"TBD in the spec\" are EXPECTED and are NOT "
            "blocking; the draft step deliberately asks the PM to include open "
            "questions, so their presence is never a defect. Block ONLY on a "
            "genuinely missing brainstorm essential: a problem statement, the "
            "audience, a recommended direction, or non-goals. Do NOT demand "
            "acceptance criteria, decision owners, deadlines, verification "
            "methods, or measurable targets — those belong to the SPEC stage, "
            "not here. Keep findings to the few that actually matter.\n\n"
        )
    else:
        rubric = (
            "Review for ambiguity, missing acceptance criteria, unsafe "
            "assumptions, unowned dependencies, missing tests, oversize slices, "
            "and scope creep.\n\n"
        )
    return (
        "Operate under the 'requesting-code-review' discipline, but review this "
        "planning artifact instead of a code diff. Be concrete and actionable.\n"
        f"{role_note}\n"
        f"Project:\n{_project_context(store)}\n"
        f"Artifact under review:\n{_artifact_block(artifact)}\n\n"
        f"Prior reviews:\n{_review_block(reviews)}\n\n"
        f"{rubric}"
        'The "verdict" MUST be exactly one of: "approved" | "request_changes" '
        '| "blocked".\n'
        "Each finding MUST be an object of this exact shape:\n"
        '{"severity":"low|medium|high|critical","title":"...","body":"...",'
        '"blocking":true|false}\n'
        'A non-"approved" verdict (request_changes or blocked) REQUIRES at least '
        "one finding.\n\n"
        "Reply with ONLY this JSON shape, no prose:\n"
        '{"schema_version":"governance_turn.v1","role":"' + role + '","intent":'
        '{"kind":"artifact_review","artifact_id":"'
        f'{artifact.artifact_id}","verdict":"request_changes","findings":'
        '[{"severity":"medium","title":"Short finding title",'
        '"body":"What is wrong and how to fix it.","blocking":false}]}}'
    )


__all__ = ["build_pm_governance_prompt", "build_governance_review_prompt"]
