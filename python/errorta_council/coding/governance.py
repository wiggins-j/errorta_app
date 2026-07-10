"""F100 - durable Coding PM governance artifacts and approvals.

The governance layer is deliberately ledger-local and provider-free. It stores
PM-authored brainstorm/spec/plan artifacts, reviewer findings, and user approval
gates beside the existing Coding project ledger. Runner code can then decide
whether to materialize approved plan slices into normal DEV tasks.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from .ledger import (
    LedgerError,
    LedgerStore,
    _append_jsonl,
    _atomic_write_json,
    _now,
    _read_jsonl,
)

STATE_SCHEMA = "coding_governance_state.v1"
ARTIFACT_SCHEMA = "coding_governance_artifact.v1"

GovernanceMode = Literal["off", "light", "strict"]
GovernancePhase = Literal[
    "idle",
    "brainstorming",
    "reviewing_brainstorm",
    "awaiting_brainstorm_approval",
    "drafting_spec",
    "reviewing_spec",
    "awaiting_spec_approval",
    "drafting_plan",
    "reviewing_plan",
    "awaiting_plan_approval",
    "development",
    "awaiting_slice_approval",
    "awaiting_final_approval",
    "complete",
]
HumanCodeApproval = Literal["none", "per_slice", "per_milestone", "final_only"]
ArtifactKind = Literal[
    "brainstorm",
    "spec",
    "implementation_plan",
    "plan_amendment",
    "slice_acceptance",
    "completion_summary",
]
ArtifactState = Literal[
    "draft",
    "under_review",
    "changes_requested",
    "awaiting_approval",
    "approved",
    "rejected",
    "superseded",
]
ReviewVerdict = Literal["approved", "request_changes", "blocked"]
ApprovalState = Literal["pending", "approved", "rejected", "cancelled"]
ApprovalKind = Literal[
    "brainstorm_approval",
    "spec_approval",
    "plan_approval",
    "slice_approval",
    "milestone_approval",
    "final_approval",
]


class GovernanceError(LedgerError):
    """Raised for invalid governance state transitions."""


def _normalized_body_hash(body: str) -> str:
    """Stable hash of an artifact body for no-progress detection.

    Normalizes trailing whitespace per line + surrounding blank lines so a
    cosmetic reflow doesn't read as progress, while any real content change
    flips the hash.
    """
    import hashlib

    normalized = "\n".join(line.rstrip() for line in (body or "").splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# F100-02 A1: default cap on governance revision rounds before the loop stops
# and asks the human ("after the 2nd or 3rd you'd be on the same page").
DEFAULT_MAX_REVIEW_ROUNDS = 3


@dataclass(frozen=True)
class GovernanceState:
    schema_version: str = STATE_SCHEMA
    mode: GovernanceMode = "off"
    phase: GovernancePhase = "idle"
    human_code_approval: HumanCodeApproval = "final_only"
    active_artifact_ids: dict[str, str] = field(default_factory=dict)
    max_review_rounds: int = DEFAULT_MAX_REVIEW_ROUNDS
    # F117: when on (default), a stage halts while an open blocking Problem exists;
    # when off, the PM auto-resolves Problems (still recorded + shown). `monitor`
    # holds the Progress Monitor thresholds (the monitor itself lands in F117-03).
    block_on_problems: bool = True
    monitor: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GovernanceState":
        return cls(
            schema_version=str(raw.get("schema_version") or STATE_SCHEMA),
            mode=_mode(raw.get("mode")),
            phase=_phase(raw.get("phase")),
            human_code_approval=_code_approval(raw.get("human_code_approval")),
            active_artifact_ids=dict(raw.get("active_artifact_ids") or {}),
            # RC5: from_dict builds fields by hand — a new field is silently
            # dropped without this manual line, breaking the settings round-trip.
            max_review_rounds=_max_review_rounds(raw.get("max_review_rounds")),
            # F117: absent => default on (migration-safe for pre-F117 projects).
            block_on_problems=bool(raw.get("block_on_problems", True)),
            monitor=dict(raw.get("monitor") or {}),
            updated_at=str(raw.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GovernanceFinding:
    severity: str
    title: str
    body: str = ""
    blocking: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GovernanceFinding":
        return cls(
            severity=str(raw.get("severity") or "medium"),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            blocking=bool(raw.get("blocking", False)),
        )


@dataclass(frozen=True)
class GovernanceArtifact:
    artifact_id: str
    project_id: str
    artifact_kind: ArtifactKind
    version: int
    state: ArtifactState
    title: str
    body_markdown: str = ""
    body_json: dict[str, Any] = field(default_factory=dict)
    source_refs: list[str] = field(default_factory=list)
    supersedes_artifact_id: str | None = None
    author: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    schema_version: str = ARTIFACT_SCHEMA

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GovernanceArtifact":
        return cls(
            artifact_id=str(raw.get("artifact_id") or ""),
            project_id=str(raw.get("project_id") or ""),
            artifact_kind=_artifact_kind(raw.get("artifact_kind")),
            version=int(raw.get("version") or 1),
            state=_artifact_state(raw.get("state")),
            title=str(raw.get("title") or ""),
            body_markdown=str(raw.get("body_markdown") or ""),
            body_json=dict(raw.get("body_json") or {}),
            source_refs=[str(r) for r in raw.get("source_refs") or []],
            supersedes_artifact_id=(
                str(raw.get("supersedes_artifact_id"))
                if raw.get("supersedes_artifact_id") else None
            ),
            author=dict(raw.get("author") or {}),
            created_at=str(raw.get("created_at") or ""),
            schema_version=str(raw.get("schema_version") or ARTIFACT_SCHEMA),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GovernanceReview:
    review_id: str
    artifact_id: str
    reviewer_member_id: str
    verdict: ReviewVerdict
    findings: list[GovernanceFinding] = field(default_factory=list)
    reviewer_role: str = "reviewer"
    created_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GovernanceReview":
        return cls(
            review_id=str(raw.get("review_id") or ""),
            artifact_id=str(raw.get("artifact_id") or ""),
            reviewer_member_id=str(raw.get("reviewer_member_id") or ""),
            verdict=_review_verdict(raw.get("verdict")),
            findings=[
                GovernanceFinding.from_dict(f)
                for f in raw.get("findings") or []
                if isinstance(f, dict)
            ],
            reviewer_role=str(raw.get("reviewer_role") or "reviewer"),
            created_at=str(raw.get("created_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["findings"] = [asdict(f) for f in self.findings]
        return out


@dataclass(frozen=True)
class GovernanceApproval:
    approval_id: str
    kind: ApprovalKind
    artifact_id: str
    required_actor: str = "user"
    state: ApprovalState = "pending"
    requested_by_member_id: str = ""
    resolved_by: str | None = None
    feedback: str = ""
    created_at: str = ""
    resolved_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GovernanceApproval":
        return cls(
            approval_id=str(raw.get("approval_id") or ""),
            kind=_approval_kind(raw.get("kind")),
            artifact_id=str(raw.get("artifact_id") or ""),
            required_actor=str(raw.get("required_actor") or "user"),
            state=_approval_state(raw.get("state")),
            requested_by_member_id=str(raw.get("requested_by_member_id") or ""),
            resolved_by=(
                str(raw.get("resolved_by")) if raw.get("resolved_by") else None
            ),
            feedback=str(raw.get("feedback") or ""),
            created_at=str(raw.get("created_at") or ""),
            resolved_at=str(raw.get("resolved_at")) if raw.get("resolved_at") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanSlice:
    slice_id: str
    title: str
    detail: str = ""
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    review_focus: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlanSlice":
        sid = str(raw.get("slice_id") or raw.get("id") or "")
        title = str(raw.get("title") or sid)
        return cls(
            slice_id=sid,
            title=title,
            detail=str(raw.get("detail") or raw.get("description") or ""),
            depends_on=[str(v) for v in raw.get("depends_on") or []],
            files=[str(v) for v in raw.get("files") or []],
            done_when=[str(v) for v in raw.get("done_when") or []],
            tests=[str(v) for v in raw.get("tests") or []],
            review_focus=[str(v) for v in raw.get("review_focus") or []],
        )

    def task_detail(self) -> str:
        lines: list[str] = []
        if self.detail:
            lines.append(self.detail)
        if self.files:
            lines.append("Files: " + ", ".join(self.files))
        if self.done_when:
            lines.append("Done when: " + "; ".join(self.done_when))
        if self.tests:
            lines.append("Tests: " + "; ".join(self.tests))
        if self.review_focus:
            lines.append("Review focus: " + "; ".join(self.review_focus))
        return "\n".join(lines)


class GovernanceStore:
    """Durable governance state for one Coding project."""

    def __init__(self, project_id: str, *, root: Path | None = None) -> None:
        self.ledger = LedgerStore(project_id, root=root)
        self.project_id = project_id
        self.dir = self.ledger.dir
        self._state_path = self.dir / "governance.json"
        self._artifact_path = self.dir / "governance_artifacts.jsonl"
        self._review_path = self.dir / "governance_reviews.jsonl"
        self._approval_path = self.dir / "governance_approvals.jsonl"

    @classmethod
    def for_ledger(cls, ledger: LedgerStore) -> "GovernanceStore":
        return cls(ledger.project_id, root=ledger.dir.parent)

    def load_state(self) -> GovernanceState:
        if not self._state_path.exists():
            return GovernanceState(updated_at=_now())
        try:
            import json
            raw = json.loads(self._state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return GovernanceState(mode="strict", phase="idle", updated_at=_now())
        return GovernanceState.from_dict(raw)

    def save_state(self, state: GovernanceState) -> GovernanceState:
        updated = replace(state, schema_version=STATE_SCHEMA, updated_at=_now())
        _atomic_write_json(self._state_path, updated.to_dict())
        return updated

    def update_state(self, **patch: Any) -> GovernanceState:
        state = self.load_state()
        values = state.to_dict()
        values.update(patch)
        return self.save_state(GovernanceState.from_dict(values))

    def _artifact_events(self, *, kind: str | None = None) -> list[GovernanceArtifact]:
        out = [
            GovernanceArtifact.from_dict(raw)
            for raw in _read_jsonl(self._artifact_path)
        ]
        if kind:
            out = [a for a in out if a.artifact_kind == kind]
        return out

    def list_artifacts(self, *, kind: str | None = None) -> list[GovernanceArtifact]:
        order: list[str] = []
        projected: dict[str, GovernanceArtifact] = {}
        for artifact in self._artifact_events(kind=kind):
            if artifact.artifact_id not in projected:
                order.append(artifact.artifact_id)
            projected[artifact.artifact_id] = artifact
        return [projected[artifact_id] for artifact_id in order]

    def get_artifact(self, artifact_id: str) -> GovernanceArtifact | None:
        for artifact in reversed(self._artifact_events()):
            if artifact.artifact_id == artifact_id:
                return artifact
        return None

    def latest_artifact(self, kind: str) -> GovernanceArtifact | None:
        artifacts = self.list_artifacts(kind=kind)
        return artifacts[-1] if artifacts else None

    def latest_approved_artifact(self, kind: str) -> GovernanceArtifact | None:
        artifact = self.latest_artifact(kind)
        if artifact is not None and artifact.state == "approved":
            return artifact
        return None

    def append_artifact(
        self,
        *,
        kind: ArtifactKind,
        title: str,
        body_markdown: str = "",
        body_json: dict[str, Any] | None = None,
        state: ArtifactState = "draft",
        source_refs: list[str] | None = None,
        supersedes_artifact_id: str | None = None,
        author: dict[str, str] | None = None,
    ) -> GovernanceArtifact:
        if supersedes_artifact_id and self.get_artifact(supersedes_artifact_id) is None:
            raise GovernanceError("superseded artifact does not exist")
        version = 1 + len(self.list_artifacts(kind=kind))
        artifact = GovernanceArtifact(
            artifact_id=f"ga_{kind}_{uuid.uuid4().hex[:10]}",
            project_id=self.project_id,
            artifact_kind=kind,
            version=version,
            state=state,
            title=title,
            body_markdown=body_markdown,
            body_json=dict(body_json or {}),
            source_refs=list(source_refs or []),
            supersedes_artifact_id=supersedes_artifact_id,
            author=dict(author or {}),
            created_at=_now(),
        )
        _append_jsonl(self._artifact_path, artifact.to_dict())
        state_obj = self.load_state()
        active = dict(state_obj.active_artifact_ids)
        active[kind] = artifact.artifact_id
        self.save_state(replace(state_obj, active_artifact_ids=active))
        return artifact

    def set_artifact_state(
        self,
        artifact_id: str,
        state: ArtifactState,
    ) -> GovernanceArtifact:
        artifact = self.get_artifact(artifact_id)
        if artifact is None:
            raise GovernanceError(f"unknown artifact: {artifact_id}")
        updated = replace(artifact, state=state)
        _append_jsonl(self._artifact_path, updated.to_dict())
        return updated

    def list_reviews(self, *, artifact_id: str | None = None) -> list[GovernanceReview]:
        out = [GovernanceReview.from_dict(raw) for raw in _read_jsonl(self._review_path)]
        if artifact_id:
            out = [r for r in out if r.artifact_id == artifact_id]
        return out

    def append_review(
        self,
        *,
        artifact_id: str,
        reviewer_member_id: str,
        verdict: ReviewVerdict,
        findings: list[GovernanceFinding] | list[dict[str, Any]] | None = None,
        reviewer_role: str = "reviewer",
    ) -> GovernanceReview:
        if self.get_artifact(artifact_id) is None:
            raise GovernanceError(f"unknown artifact: {artifact_id}")
        parsed_findings = [
            f if isinstance(f, GovernanceFinding) else GovernanceFinding.from_dict(f)
            for f in (findings or [])
        ]
        if verdict != "approved" and not parsed_findings:
            raise GovernanceError("non-approved governance review requires findings")
        review = GovernanceReview(
            review_id=f"gr_{uuid.uuid4().hex[:12]}",
            artifact_id=artifact_id,
            reviewer_member_id=reviewer_member_id,
            verdict=verdict,
            findings=parsed_findings,
            reviewer_role=str(reviewer_role or "reviewer"),
            created_at=_now(),
        )
        _append_jsonl(self._review_path, review.to_dict())
        return review

    def latest_review_by_role(self, artifact_id: str) -> dict[str, GovernanceReview]:
        """Most-recent review per reviewer_role for one artifact (last wins)."""
        latest: dict[str, GovernanceReview] = {}
        for review in self.list_reviews(artifact_id=artifact_id):
            latest[review.reviewer_role] = review
        return latest

    def settle_artifact_after_review(self, artifact_id: str, mode: str) -> str:
        """Single source of truth for what happens after a review lands.

        Returns the resolved artifact state: ``changes_requested`` (any reviewer
        rejected), ``approved`` (every required reviewer approved), or
        ``under_review`` (more required reviews still pending).
        """
        art = self.get_artifact(artifact_id)
        if art is None:
            return ""
        by_role = self.latest_review_by_role(artifact_id)
        if any(r.verdict != "approved" for r in by_role.values()):
            self.set_artifact_state(art.artifact_id, "changes_requested")
            self.update_state(phase=_revision_phase_for_kind(art.artifact_kind))
            return "changes_requested"
        required = required_reviewer_roles(mode, art.artifact_kind)
        if all(
            role in by_role and by_role[role].verdict == "approved"
            for role in required
        ):
            self.set_artifact_state(art.artifact_id, "approved")
            self.update_state(phase=next_phase_after_kind(art.artifact_kind))
            return "approved"
        self.set_artifact_state(art.artifact_id, "under_review")
        self.update_state(phase=reviewing_phase_for_kind(art.artifact_kind))
        return "under_review"

    def _resolved_review_state(self, artifact_id: str) -> str:
        """Read-only resolved review state of one artifact (no mutation).

        Mirrors ``settle_artifact_after_review``'s rejection test: any latest
        per-role review that is not ``approved`` resolves to ``changes_requested``.
        Used by the convergence helpers and the status projection.
        """
        by_role = self.latest_review_by_role(artifact_id)
        if any(r.verdict != "approved" for r in by_role.values()):
            return "changes_requested"
        return "" if not by_role else "reviewed"

    def review_round_count(self, kind: str) -> int:
        """F100-02 A1 (RC1): number of ``changes_requested`` rounds for ``kind``
        in the current stage.

        Brainstorm redrafts append a NEW version of the same kind (they chain to
        ``supersedes_artifact_id=None``), so per-kind counting is the correct
        unit: count the versions of ``kind`` whose resolved review is
        ``changes_requested``. Counting per-kind auto-resets per stage (each kind
        is drafted once per project), so spec/plan never inherit brainstorm's
        count.
        """
        return sum(
            1
            for art in self.list_artifacts(kind=kind)
            if self._resolved_review_state(art.artifact_id) == "changes_requested"
        )

    def no_progress_streak(self, kind: str) -> int:
        """F100-02 A1 (RC1): trailing run of versions of ``kind`` whose normalized
        ``body_markdown`` hash equals the immediately prior version's.

        The observed failure was byte-identical resubmission (2 distinct bodies
        across 37 versions); an exact normalized-body hash catches it.
        """
        artifacts = self.list_artifacts(kind=kind)
        if len(artifacts) < 2:
            return 0
        hashes = [_normalized_body_hash(a.body_markdown) for a in artifacts]
        streak = 0
        for i in range(len(hashes) - 1, 0, -1):
            if hashes[i] == hashes[i - 1]:
                streak += 1
            else:
                break
        return streak

    def force_accept_artifact(
        self,
        artifact_id: str,
        *,
        by: str = "human",
    ) -> GovernanceArtifact:
        """F100-02 D (RC3): human override of the AI dual review.

        Force-approves the given artifact and advances governance to the next
        stage, bypassing the per-role reviewer requirement (it's the human's
        "good enough, move on" call). It does NOT fabricate reviewer approvals
        and does NOT use ``resolve_approval`` (the artifact flow no longer creates
        a pending ``GovernanceApproval``). The scheduler advances purely on
        ``latest_approved_artifact(kind)``, so writing ``approved`` + advancing
        the phase is sufficient; ``settle_artifact_after_review`` only runs on a
        new review, so this state write is never overwritten.

        Raises ``GovernanceError`` if ``artifact_id`` is not the current latest
        artifact of its kind (stale / already superseded) — the human must accept
        exactly the version they are viewing.
        """
        artifact = self.get_artifact(artifact_id)
        if artifact is None:
            raise GovernanceError(f"unknown artifact: {artifact_id}")
        latest = self.latest_artifact(artifact.artifact_kind)
        if latest is None or latest.artifact_id != artifact_id:
            raise GovernanceError("artifact is not the current latest version")
        updated = self.set_artifact_state(artifact_id, "approved")
        self.update_state(phase=next_phase_after_kind(artifact.artifact_kind))
        # The human override (by="human") and the PM-as-final-authority path
        # (by="pm", used in light/off mode when the reviewer deadlocks) both
        # force-approve, but they are honestly distinct decisions in the log.
        is_human = by == "human"
        self.ledger.record_decision(
            title=(
                "human accepted governance artifact" if is_human
                else "PM finalized governance artifact"
            ),
            context=f"governance:{artifact_id}",
            choice="human_artifact_accept" if is_human else "governance_pm_finalized",
            rationale=(
                f"{'human override' if is_human else 'PM final decision'}: "
                f"{artifact.artifact_kind} v{artifact.version} force-approved by {by}"
            ),
            extra={"artifact_id": artifact_id,
                   "artifact_kind": artifact.artifact_kind,
                   "accepted_by": by},
        )
        return updated

    def list_approvals(
        self,
        *,
        state: str | None = None,
        artifact_id: str | None = None,
    ) -> list[GovernanceApproval]:
        out = [
            GovernanceApproval.from_dict(raw)
            for raw in _read_jsonl(self._approval_path)
        ]
        if state:
            out = [a for a in out if a.state == state]
        if artifact_id:
            out = [a for a in out if a.artifact_id == artifact_id]
        return out

    def get_approval(self, approval_id: str) -> GovernanceApproval | None:
        for approval in reversed(self.list_approvals()):
            if approval.approval_id == approval_id:
                return approval
        return None

    def pending_approval(self) -> GovernanceApproval | None:
        pending = self.list_approvals(state="pending")
        return pending[-1] if pending else None

    def create_approval(
        self,
        *,
        kind: ApprovalKind,
        artifact_id: str,
        requested_by_member_id: str,
        required_actor: str = "user",
    ) -> GovernanceApproval:
        if self.get_artifact(artifact_id) is None:
            raise GovernanceError(f"unknown artifact: {artifact_id}")
        for approval in self.list_approvals(artifact_id=artifact_id):
            if approval.kind == kind and approval.state == "pending":
                return approval
        approval = GovernanceApproval(
            approval_id=f"gap_{uuid.uuid4().hex[:12]}",
            kind=kind,
            artifact_id=artifact_id,
            required_actor=required_actor,
            state="pending",
            requested_by_member_id=requested_by_member_id,
            created_at=_now(),
        )
        _append_jsonl(self._approval_path, approval.to_dict())
        return approval

    def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolved_by: str,
        actor_role: str = "user",
        feedback: str = "",
    ) -> GovernanceApproval:
        current = self.get_approval(approval_id)
        if current is None:
            raise GovernanceError(f"unknown approval: {approval_id}")
        if current.state != "pending":
            return current
        if current.required_actor == "user" and actor_role == "pm":
            raise GovernanceError("pm cannot approve user-required governance gate")
        resolved = replace(
            current,
            state="approved" if approved else "rejected",
            resolved_by=resolved_by,
            feedback=feedback,
            resolved_at=_now(),
        )
        _append_jsonl(self._approval_path, resolved.to_dict())
        self._apply_approval_resolution(resolved)
        return resolved

    def _apply_approval_resolution(self, approval: GovernanceApproval) -> None:
        artifact = self.get_artifact(approval.artifact_id)
        if artifact is None:
            return
        if approval.state == "rejected":
            self.set_artifact_state(artifact.artifact_id, "rejected")
            phase = {
                "brainstorm": "brainstorming",
                "spec": "drafting_spec",
                "implementation_plan": "drafting_plan",
            }.get(artifact.artifact_kind, "development")
            self.update_state(phase=phase)
            return
        self.set_artifact_state(artifact.artifact_id, "approved")
        next_phase = {
            "brainstorm_approval": "drafting_spec",
            "spec_approval": "drafting_plan",
            "plan_approval": "development",
            "slice_approval": "development",
            "milestone_approval": "development",
            "final_approval": "complete",
        }.get(approval.kind, "development")
        self.update_state(phase=next_phase)

    def plan_slices(self, artifact: GovernanceArtifact | None = None) -> list[PlanSlice]:
        plan = artifact or self.latest_approved_artifact("implementation_plan")
        if plan is None:
            return []
        raw_slices = plan.body_json.get("slices") if isinstance(plan.body_json, dict) else []
        return [
            PlanSlice.from_dict(raw)
            for raw in raw_slices or []
            if isinstance(raw, dict) and (raw.get("slice_id") or raw.get("id"))
        ]

    def summary(self, *, include_body: bool = True) -> dict[str, Any]:
        artifacts = [a.to_dict() for a in self.list_artifacts()]
        if not include_body:
            for artifact in artifacts:
                artifact.pop("body_markdown", None)
                artifact.pop("body_json", None)
        return {
            "state": self.load_state().to_dict(),
            "artifacts": artifacts,
            "reviews": [r.to_dict() for r in self.list_reviews()],
            "approvals": [a.to_dict() for a in self.list_approvals()],
            "plan_slices": [asdict(s) for s in self.plan_slices()],
        }


def governance_store_for(ledger: LedgerStore) -> GovernanceStore:
    return GovernanceStore.for_ledger(ledger)


def approved_gate_for_kind(kind: str) -> ApprovalKind:
    if kind == "brainstorm":
        return "brainstorm_approval"
    if kind == "spec":
        return "spec_approval"
    if kind == "implementation_plan":
        return "plan_approval"
    raise GovernanceError(f"no approval gate for artifact kind: {kind}")


def required_reviewer_roles(mode: str, kind: str) -> tuple[str, ...]:
    """Which reviewer roles must approve an artifact before it advances.

    * ``strict`` — both the reviewer AND the PM review every artifact kind.
    * ``light`` — only the reviewer, and only for spec/plan (brainstorm skipped).
    * ``off`` — no reviews at all.
    """
    if mode == "strict":
        return ("reviewer", "pm")
    if mode == "light" and kind in ("spec", "implementation_plan"):
        return ("reviewer",)
    return ()


def next_phase_after_kind(kind: str) -> GovernancePhase:
    return {
        "brainstorm": "drafting_spec",
        "spec": "drafting_plan",
        "implementation_plan": "development",
    }.get(kind, "development")  # type: ignore[return-value]


def reviewing_phase_for_kind(kind: str) -> GovernancePhase:
    return {
        "brainstorm": "reviewing_brainstorm",
        "spec": "reviewing_spec",
        "implementation_plan": "reviewing_plan",
    }.get(kind, "reviewing_spec")  # type: ignore[return-value]


def _revision_phase_for_kind(kind: str) -> GovernancePhase:
    return {
        "brainstorm": "brainstorming",
        "spec": "drafting_spec",
        "implementation_plan": "drafting_plan",
    }.get(kind, "development")  # type: ignore[return-value]


def next_phase_after_artifact(kind: str, mode: str, reviewer_approved: bool) -> GovernancePhase:
    if mode == "strict":
        return {
            "brainstorm": "awaiting_brainstorm_approval",
            "spec": "awaiting_spec_approval",
            "implementation_plan": "awaiting_plan_approval",
        }.get(kind, "development")  # type: ignore[return-value]
    if mode == "light" and reviewer_approved:
        return {
            "brainstorm": "drafting_spec",
            "spec": "drafting_plan",
            "implementation_plan": "development",
        }.get(kind, "development")  # type: ignore[return-value]
    return "development"


def _mode(value: object) -> GovernanceMode:
    text = str(value or "off")
    return text if text in {"off", "light", "strict"} else "off"  # type: ignore[return-value]


def _phase(value: object) -> GovernancePhase:
    text = str(value or "idle")
    allowed = {
        "idle", "brainstorming", "reviewing_brainstorm",
        "awaiting_brainstorm_approval",
        "drafting_spec", "reviewing_spec", "awaiting_spec_approval",
        "drafting_plan", "reviewing_plan", "awaiting_plan_approval",
        "development", "awaiting_slice_approval", "awaiting_final_approval",
        "complete",
    }
    return text if text in allowed else "idle"  # type: ignore[return-value]


def _max_review_rounds(value: object) -> int:
    """Coerce the governance review-round cap; default 3, floor 1."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_REVIEW_ROUNDS
    return n if n >= 1 else DEFAULT_MAX_REVIEW_ROUNDS


def _code_approval(value: object) -> HumanCodeApproval:
    text = str(value or "final_only")
    allowed = {"none", "per_slice", "per_milestone", "final_only"}
    return text if text in allowed else "final_only"  # type: ignore[return-value]


def _artifact_kind(value: object) -> ArtifactKind:
    text = str(value or "brainstorm")
    allowed = {
        "brainstorm", "spec", "implementation_plan", "plan_amendment",
        "slice_acceptance", "completion_summary",
    }
    return text if text in allowed else "brainstorm"  # type: ignore[return-value]


def _artifact_state(value: object) -> ArtifactState:
    text = str(value or "draft")
    allowed = {
        "draft", "under_review", "changes_requested", "awaiting_approval",
        "approved", "rejected", "superseded",
    }
    return text if text in allowed else "draft"  # type: ignore[return-value]


def _review_verdict(value: object) -> ReviewVerdict:
    text = str(value or "request_changes")
    allowed = {"approved", "request_changes", "blocked"}
    return text if text in allowed else "request_changes"  # type: ignore[return-value]


def _approval_kind(value: object) -> ApprovalKind:
    text = str(value or "spec_approval")
    allowed = {
        "brainstorm_approval", "spec_approval", "plan_approval",
        "slice_approval", "milestone_approval", "final_approval",
    }
    return text if text in allowed else "spec_approval"  # type: ignore[return-value]


def _approval_state(value: object) -> ApprovalState:
    text = str(value or "pending")
    allowed = {"pending", "approved", "rejected", "cancelled"}
    return text if text in allowed else "pending"  # type: ignore[return-value]


__all__ = [
    "ARTIFACT_SCHEMA",
    "STATE_SCHEMA",
    "GovernanceApproval",
    "GovernanceArtifact",
    "GovernanceError",
    "GovernanceFinding",
    "GovernanceReview",
    "GovernanceState",
    "GovernanceStore",
    "PlanSlice",
    "approved_gate_for_kind",
    "governance_store_for",
    "next_phase_after_artifact",
    "next_phase_after_kind",
    "required_reviewer_roles",
    "reviewing_phase_for_kind",
]
