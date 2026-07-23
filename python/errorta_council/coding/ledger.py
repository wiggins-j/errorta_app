"""F087-01 — project ledger store. Pure persistence; no egress (Council inv. 3).

One project per directory under
``${ERRORTA_HOME}/council/coding-projects/<id>/`` holding:

* ``project.json``   — North Star + status (full-rewrite projection).
* ``backlog.jsonl``  — append-only task events; ``list_tasks`` replays them.
* ``decisions.jsonl``— append-only ADR-style decision log.
* ``artifacts.json`` — file/artifact index (last write per path).
* ``skills.jsonl``   — append-only skills-used log.
* ``digest.json``    — rolling compact state digest.

All writes are atomic (temp + rename, mode 0600). The module imports no member,
gateway, MCP, HTTP, or subprocess machinery.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .usage_rollup import rollup_turns  # pure sibling; no egress (Council inv. 3)


class LedgerError(Exception):
    """Base class for ledger failures."""


class ProjectNotFound(LedgerError):
    """Raised when a project directory has no project.json."""


class FocusNotFound(LedgerError):
    """Raised when a Current Focus id does not exist."""


class FocusTransitionError(LedgerError):
    """Raised when a Current Focus lifecycle transition is not allowed."""


_VALID_ROLES = ("pm", "dev", "reviewer", "tester")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_focus_id() -> str:
    return f"focus-{uuid.uuid4().hex[:12]}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# F087-14 WS-2: bound append-only log growth. Cheap st_size check on every append;
# the expensive keep-the-tail rewrite runs only when a log exceeds the byte cap.
# The kept tail preserves recent semantics (incl. the latest review/test verdict
# the merge gate reads).
_LOG_MAX_BYTES = 8 * 1024 * 1024
_LOG_KEEP_LINES = 20_000


def _append_capped_jsonl(path: Path, payload: dict[str, Any]) -> None:
    _append_jsonl(path, payload)
    try:
        if path.stat().st_size <= _LOG_MAX_BYTES:
            return
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return
    if len(lines) > _LOG_KEEP_LINES:
        kept = lines[-_LOG_KEEP_LINES:]
        _atomic_write_text(path, "\n".join(kept) + "\n")


def _clean_token(value: int | None) -> int | None:
    """A persisted token count is a non-negative int or absent. Reject bool
    (an int subclass), negatives, and non-ints so a bad value is dropped rather
    than written as a misleading number."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _turn_usage_block(*, input_tokens: int | None, output_tokens: int | None,
                      cache_read_input_tokens: int | None,
                      cache_write_input_tokens: int | None,
                      measured: bool,
                      provenance: str | None = None,
                      measured_input: int | None = None,
                      measured_output: int | None = None,
                      estimated_input: int | None = None,
                      estimated_output: int | None = None,
                      estimated_input_raw: int | None = None,
                      cli_overhead_tokens: int | None = None,
                      estimator_method: str | None = None,
                      calibration_factor: float | None = None,
                      ) -> dict[str, Any] | None:
    """Build the compact per-turn ``usage`` sub-dict (F143 / F143-01 Slice C).

    ``input_tokens``/``output_tokens`` are the EFFECTIVE values the rollup sums
    (measured-where-present-else-estimated); ``measured`` stays provider-reported-
    only (unchanged F143 semantics) so the current ``rollup_turns`` and its tests
    keep passing. The extended fields — ``provenance``, ``measured_*``,
    ``estimated_*``, ``cli_overhead_tokens``, ``estimator_method``,
    ``calibration_factor`` — drive labeling/tooltips/calibration (Slice D+ read
    them). All are additive and omitted when absent.

    Returns ``None`` ONLY when there is genuinely nothing to record — no measured
    AND no estimated values (the legacy/unreported safety case). An estimated-only
    turn (F143-01) now DOES get a block with ``provenance="estimated"`` — that is the
    deliberate change from the old F143 behavior of dropping it."""
    inp = _clean_token(input_tokens)
    out = _clean_token(output_tokens)
    mi = _clean_token(measured_input)
    mo = _clean_token(measured_output)
    ei = _clean_token(estimated_input)
    eo = _clean_token(estimated_output)
    # Genuinely-nothing safety case: no effective ints and no estimates → no block
    # (legacy/unreported). This keeps a bare ``measured=False`` turn with no numbers
    # (e.g. a fake caller) off the record, exactly as before.
    if inp is None and out is None and ei is None and eo is None:
        return None
    if provenance is None:
        # Derived default for direct callers (tests) that don't pass provenance.
        if measured and (inp is not None or out is not None):
            provenance = "measured" if (inp is not None and out is not None) \
                else "measured_partial"
        elif ei is not None or eo is not None:
            provenance = "estimated"
        else:
            provenance = "unreported"
    # F143-01 Slice D: keep the persisted block self-consistent for the rollup's
    # coverage math. A ``measured`` turn's effective input/output ARE its measured
    # values by definition, so backfill ``measured_input``/``measured_output`` from
    # the effective ints when a direct caller passed the effective numbers +
    # measured=True but no explicit measured_*. (The real runner always passes them;
    # this only helps direct-caller/test paths.) Never overwrite an explicit value.
    if provenance == "measured":
        if mi is None and inp is not None:
            mi = inp
        if mo is None and out is not None:
            mo = out
    usage: dict[str, Any] = {"measured": bool(measured)}
    usage["provenance"] = str(provenance)
    if inp is not None:
        usage["input_tokens"] = inp
    if out is not None:
        usage["output_tokens"] = out
    if mi is not None:
        usage["measured_input"] = mi
    if mo is not None:
        usage["measured_output"] = mo
    if ei is not None:
        usage["estimated_input"] = ei
    if eo is not None:
        usage["estimated_output"] = eo
    # F143-01 calibration: the RAW (uncalibrated) Layer-1 input estimate, persisted
    # ONLY when calibration actually moved the top-line (eir != ei) so factor-1.0 turns
    # stay byte-identical. It is the honest ``cli_overhead`` basis (measured − raw), so
    # the vendor-overhead band survives factor convergence.
    eir = _clean_token(estimated_input_raw)
    if eir is not None and eir != ei:
        usage["estimated_input_raw"] = eir
    cr = _clean_token(cache_read_input_tokens)
    cw = _clean_token(cache_write_input_tokens)
    if cr is not None:
        usage["cache_read_input_tokens"] = cr
    if cw is not None:
        usage["cache_write_input_tokens"] = cw
    co = _clean_token(cli_overhead_tokens)
    if co is not None:
        usage["cli_overhead_tokens"] = co
    if isinstance(estimator_method, str) and estimator_method:
        usage["estimator_method"] = estimator_method
    if isinstance(calibration_factor, (int, float)) and not isinstance(
            calibration_factor, bool):
        usage["calibration_factor"] = float(calibration_factor)
    return usage


def _turn_composition_block(
        composition: dict[str, Any] | None) -> dict[str, Any] | None:
    """F143-01 Slice F: normalize the per-turn Layer-1 ``composition`` block for
    persistence (parallel to ``_turn_usage_block``). Keeps it compact + self-
    consistent: each category is ``{"class": str, "tokens": non-neg int}``; drops
    malformed entries; recomputes ``sent_total`` as the sum of retained category
    tokens so the persisted block always reconciles. Returns ``None`` when there is
    nothing to record (absent/empty). Additive/backward-compatible — legacy turns
    simply lack it."""
    if not isinstance(composition, dict):
        return None
    raw_categories = composition.get("categories")
    categories: list[dict[str, Any]] = []
    if isinstance(raw_categories, list):
        for entry in raw_categories:
            if not isinstance(entry, dict):
                continue
            cls = entry.get("class")
            tokens = _clean_token(entry.get("tokens"))
            if isinstance(cls, str) and cls and tokens is not None:
                categories.append({"class": cls, "tokens": tokens})
    if not categories:
        return None
    block: dict[str, Any] = {
        "sent_total": sum(c["tokens"] for c in categories),
        "categories": categories,
    }
    method = composition.get("estimator_method")
    if isinstance(method, str) and method:
        block["estimator_method"] = method
    return block


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # F087-13 WS-3: a crash mid-append can leave a torn final line. Skip a
        # malformed line rather than wedging every reader (list_tasks / recovery
        # / status) on one corrupt record. The append path writes whole lines, so
        # a torn record is only ever the trailing one.
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL ledger without the torn-line recovery used by normal views.

    Completion is a fail-closed boundary: skipping a malformed record could hide
    the only open task and incorrectly prove the project done. Callers that make
    completion decisions use this strict view; routine status/recovery readers
    keep the tolerant ``_read_jsonl`` behavior above.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise LedgerError(
                f"invalid ledger record in {path.name} at line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise LedgerError(
                f"invalid ledger record in {path.name} at line {line_number}"
            )
        out.append(value)
    return out


def _split_unknown(cls: type, raw: dict[str, Any]) -> tuple[dict, dict]:
    known_fields = {f for f in cls.__dataclass_fields__ if f != "_extras"}
    known = {k: v for k, v in raw.items() if k in known_fields}
    extras = {k: v for k, v in raw.items() if k not in known_fields}
    return known, extras


@dataclass(frozen=True)
class Project:
    id: str
    north_star: str = ""
    definition_of_done: str = ""
    target: str = "new"           # "new" | "existing"
    repo_path: str | None = None
    # F105: for a greenfield ("new") project, the user-selected PARENT directory
    # the accepted MVP is exported into (delivered to `<delivery_root>/<id>`).
    # None means the default (~/Errorta Projects). Ignored / stored None for
    # "existing" targets. Defaulted so old project.json files keep loading.
    delivery_root: str | None = None
    status: str = "active"        # active | paused | done | failed
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    # F093: the PM's written justification for declaring the project done, and
    # when it was declared. Empty until the run reaches definition_of_done.
    completion_summary: str = ""
    completed_at: str = ""
    # F121: whether the user has confirmed the pre-first-run readiness gate
    # ("Run setup"). False on a brand-new project, so the first Start Run opens
    # the gate instead of starting. Defaulted so old project.json files load.
    run_setup_confirmed: bool = False
    # F135 D5: the current-focus directive ("what should the team work on right
    # now?"), distinct from the durable north_star and the whole-project
    # definition_of_done. Surfaced to the PM's first turn via the orientation
    # packet, and via the interjection seam when a run is live. Defaulted so old
    # project.json files keep loading.
    work_request: str = ""
    # F135 D10: import provenance for a project brought in from an existing repo
    # (github_clone | local_folder | local_folder_git_init). Display/audit only,
    # never authoritative for a run. None for hand-created projects.
    import_source: dict[str, Any] | None = None
    # F141 WS-I: forward-only stamp for "the North Star has been met" — the
    # project has crossed from building-its-initial-North-Star into ongoing
    # steering. Set on North-Star accept for imported targets, and on the first
    # foundation-merge for `new` targets. Empty until met. Drives whether the
    # Current Focus panel shows (see the `phase` property).
    north_star_met_at: str = ""
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def phase(self) -> str:
        """F141 WS-I: "north_star" while building the initial North Star,
        "steering" once it's met. The frontend gates the Current Focus panel on
        this (plus "always show if focuses already exist")."""
        return "steering" if self.north_star_met_at else "north_star"

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id, "north_star": self.north_star,
            "definition_of_done": self.definition_of_done, "target": self.target,
            "repo_path": self.repo_path, "delivery_root": self.delivery_root,
            "status": self.status,
            "revision": self.revision, "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completion_summary": self.completion_summary,
            "completed_at": self.completed_at,
            "run_setup_confirmed": self.run_setup_confirmed,
            "work_request": self.work_request,
            "import_source": self.import_source,
            "north_star_met_at": self.north_star_met_at,
        }
        d.update(self._extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Project":
        known, extras = _split_unknown(cls, raw)
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    role: str
    detail: str = ""
    state: str = "todo"           # todo|doing|blocked|done|dropped
    assignee_member_id: str | None = None
    parent_task_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    result_ref: str | None = None
    pr_id: str | None = None           # F087-17: PR this task acts on (review/test)
    # F141 WS-D: a short human "why this was sent back" for rework tasks. The
    # machine title (e.g. "revise: <branch>") stays load-bearing for
    # _supersede_ancestors, so the reason lives here instead of in the title.
    reason_summary: str = ""
    source_spec_artifact_id: str | None = None
    source_plan_artifact_id: str | None = None
    source_slice_id: str | None = None
    governance_required: bool = False
    # F129 PM model-assignment inputs + validated assignment. Defaults keep old
    # ledger rows readable and byte-compatible at the semantic level.
    task_type: str = "implementation"
    difficulty_tier: str = "mid"
    preferred_member_id: str = ""
    preferred_route_id: str = ""
    assignment_rationale: str = ""
    model_assignment: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "task_id": self.task_id, "title": self.title, "role": self.role,
            "detail": self.detail, "state": self.state,
            "assignee_member_id": self.assignee_member_id,
            "parent_task_id": self.parent_task_id,
            "depends_on": list(self.depends_on), "result_ref": self.result_ref,
            "pr_id": self.pr_id,
            "source_spec_artifact_id": self.source_spec_artifact_id,
            "source_plan_artifact_id": self.source_plan_artifact_id,
            "source_slice_id": self.source_slice_id,
            "governance_required": self.governance_required,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
        if self.task_type != "implementation":
            d["task_type"] = self.task_type
        if self.difficulty_tier != "mid":
            d["difficulty_tier"] = self.difficulty_tier
        if self.preferred_member_id:
            d["preferred_member_id"] = self.preferred_member_id
        if self.preferred_route_id:
            d["preferred_route_id"] = self.preferred_route_id
        if self.assignment_rationale:
            d["assignment_rationale"] = self.assignment_rationale
        if self.reason_summary:
            d["reason_summary"] = self.reason_summary
        if self.model_assignment:
            d["model_assignment"] = dict(self.model_assignment)
        d.update(self._extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        known, extras = _split_unknown(cls, raw)
        known["depends_on"] = list(known.get("depends_on") or [])
        if known.get("model_assignment") is not None:
            known["model_assignment"] = dict(known["model_assignment"])
        return cls(**known, _extras=extras)


# F137: valid Focus lifecycle states. active -> completed (PM-proposed) ->
# archived (human-accepted). "archived" is also reachable directly when a user
# drops a focus. Once archived, a focus never re-enters planning.
FOCUS_STATES = ("active", "completed", "archived")


@dataclass(frozen=True)
class Focus:
    """F137: a Current Focus — a concrete, bounded increment the team should work
    on *now*, distinct from (and scoped tighter than) the durable North Star. A
    project can hold several active focuses; the PM plans + orders tasks/PRs
    across them. Generalizes the F135 single-string ``work_request`` directive."""
    id: str
    title: str
    body: str = ""
    status: str = "active"        # active | completed | archived
    order: int = 0                # ordering among active focuses (asc)
    origin: str = "user"          # user | work_request_migration | director | pm
    created_at: str = ""
    completed_at: str = ""        # PM-proposed complete (pending human accept)
    accepted_at: str = ""         # human-accept stamp (== archived transition)
    archived_at: str = ""
    completion_summary: str = ""  # PM's justification at completion (like F093)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id, "title": self.title, "body": self.body,
            "status": self.status, "order": self.order, "origin": self.origin,
            "created_at": self.created_at, "completed_at": self.completed_at,
            "accepted_at": self.accepted_at, "archived_at": self.archived_at,
            "completion_summary": self.completion_summary,
        }
        d.update(self._extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Focus":
        known, extras = _split_unknown(cls, raw)
        return cls(**known, _extras=extras)


def format_focus_lines(focuses: list[Focus]) -> list[str]:
    """F137: the single canonical rendering of an ordered focus set as numbered
    ``N. title — body`` lines. Shared by the governance prompt, the PM
    task-planning prompt, and the mid-run interjection text so all three stay in
    sync (add a field once, here)."""
    lines: list[str] = []
    for i, f in enumerate(focuses, start=1):
        line = f"{i}. {f.title}"
        if f.body.strip():
            line += f" — {f.body.strip()}"
        lines.append(line)
    return lines


def list_projects(root: Path | None = None) -> list[dict[str, Any]]:
    """List all coding projects (id + North Star + status) under the root."""
    if root is None:
        from errorta_app.paths import errorta_home
        root = errorta_home() / "council" / "coding-projects"
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        pj = child / "project.json"
        if pj.exists():
            try:
                raw = json.loads(pj.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": raw.get("id", child.name),
                        "north_star": raw.get("north_star", ""),
                        "status": raw.get("status", "active")})
    return out


class LedgerStore:
    """Read/write the on-disk ledger for one coding project."""

    def __init__(self, project_id: str, *, root: Path | None = None) -> None:
        # F087-07-A: project_id is a single path component. Reject ../ /, \, NUL,
        # empty, '.' (reuses the F086-A primitive) so it can never escape the
        # ledger root, then realpath-assert containment as defense-in-depth.
        from errorta_export.safe_path import UnsafePathError, safe_segment
        try:
            safe_segment(project_id)
        except UnsafePathError as exc:
            raise LedgerError(f"invalid project_id: {project_id!r}") from exc
        if root is None:
            from errorta_app.paths import errorta_home
            root = errorta_home() / "council" / "coding-projects"
        root = Path(root)
        self.project_id = project_id
        self.dir = root / project_id
        if not self.dir.resolve().is_relative_to(root.resolve()):
            raise LedgerError(f"project_id escapes the ledger root: {project_id!r}")

    # --- paths -----------------------------------------------------------
    @property
    def _project_path(self) -> Path:
        return self.dir / "project.json"

    @property
    def _backlog_path(self) -> Path:
        return self.dir / "backlog.jsonl"

    @property
    def _decisions_path(self) -> Path:
        return self.dir / "decisions.jsonl"

    @property
    def _turns_path(self) -> Path:
        return self.dir / "turns.jsonl"

    @property
    def _prs_path(self) -> Path:
        return self.dir / "prs.json"

    @property
    def _artifacts_path(self) -> Path:
        return self.dir / "artifacts.json"

    @property
    def _skills_path(self) -> Path:
        return self.dir / "skills.jsonl"

    @property
    def _tool_events_path(self) -> Path:
        return self.dir / "tool-events.jsonl"

    @property
    def _digest_path(self) -> Path:
        return self.dir / "digest.json"

    @property
    def _test_commands_path(self) -> Path:
        return self.dir / "test-commands.json"

    @property
    def _test_runs_path(self) -> Path:
        return self.dir / "test-runs.jsonl"

    # --- project ---------------------------------------------------------
    def create_project(self, *, north_star: str, definition_of_done: str,
                        target: str, repo_path: str | None,
                        delivery_root: str | None = None,
                        work_request: str = "",
                        import_source: dict[str, Any] | None = None) -> Project:
        ts = _now()
        proj = Project(
            id=self.project_id, north_star=north_star,
            definition_of_done=definition_of_done, target=target,
            repo_path=repo_path,
            # An "existing"-target project delivers by merging back into its repo,
            # so a delivery_root is meaningless there — store None.
            delivery_root=(delivery_root if target != "existing" else None),
            status="active", revision=1,
            created_at=ts, updated_at=ts,
            work_request=work_request, import_source=import_source,
        )
        _atomic_write_json(self._project_path, proj.to_dict())
        return proj

    def get_project(self) -> Project:
        if not self._project_path.exists():
            raise ProjectNotFound(self.project_id)
        return Project.from_dict(json.loads(self._project_path.read_text("utf-8")))

    def set_project_status(self, status: str) -> Project:
        """Transition project status (active|paused|done|failed); bumps revision.

        F141 WS-I: reaching ``done`` is when the initial North Star is MET — stamp
        the forward-only ``north_star_met_at`` marker, crossing into the steering
        phase where the Current Focus panel becomes relevant. Forward-only, so a
        later F146 re-open (status back to active) stays in steering."""
        raw = self.get_project().to_dict()
        raw["status"] = status
        raw["revision"] = int(raw.get("revision", 1)) + 1
        raw["updated_at"] = _now()
        if status == "done" and not raw.get("north_star_met_at"):
            raw["north_star_met_at"] = _now()
        _atomic_write_json(self._project_path, raw)
        return Project.from_dict(raw)

    def set_completion(self, summary: str) -> Project:
        """F093: record the PM's completion summary when the run reaches
        definition_of_done. Idempotent — a re-`done` run overwrites with the
        latest summary. Surfaces in the GET project projection via to_dict()."""
        with self.lock:
            raw = self.get_project().to_dict()
            raw["completion_summary"] = summary
            raw["completed_at"] = _now()
            raw["revision"] = int(raw.get("revision", 1)) + 1
            raw["updated_at"] = _now()
            _atomic_write_json(self._project_path, raw)
            return Project.from_dict(raw)

    def delete_project(self) -> bool:
        """Delete this project's Errorta-owned ledger directory.

        External target repositories and delivered project exports are not under
        ``self.dir`` and are intentionally left untouched.
        """
        with self.lock:
            if not self._project_path.exists():
                raise ProjectNotFound(self.project_id)
            root = self.dir.resolve()
            parent = self.dir.parent.resolve()
            try:
                root.relative_to(parent)
            except ValueError as exc:
                raise LedgerError("project directory escapes ledger root") from exc
            from errorta_tools.runner.apply_workspace import resilient_rmtree
            resilient_rmtree(root)   # F157: tolerate a briefly-open file on delete
            return True

    # --- backlog ---------------------------------------------------------
    def _projected_tasks(self) -> dict[str, Task]:
        proj: dict[str, Task] = {}
        for raw in _read_jsonl(self._backlog_path):
            t = Task.from_dict(raw)
            proj[t.task_id] = t  # last event per task_id wins
        return proj

    def add_task(self, *, title: str, role: str, detail: str = "",
                 assignee_member_id: str | None = None,
                 parent_task_id: str | None = None,
                 depends_on: list[str] | None = None,
                 pr_id: str | None = None,
                 reason_summary: str = "",
                 source_spec_artifact_id: str | None = None,
                 source_plan_artifact_id: str | None = None,
                 source_slice_id: str | None = None,
                 governance_required: bool = False,
                 task_type: str = "implementation",
                 difficulty_tier: str = "mid",
                 preferred_member_id: str = "",
                 preferred_route_id: str = "",
                 assignment_rationale: str = "",
                 model_assignment: dict[str, Any] | None = None,
                 target_files: list[str] | None = None) -> Task:
        if role not in _VALID_ROLES:
            raise LedgerError(f"invalid role: {role!r}")
        ts = _now()
        # F159: an optional declared touched-files list rides in _extras (no schema
        # migration; round-trips via to_dict/_split_unknown). The hot-file gate
        # prefers it over prose inference.
        extras = {"target_files": [str(p) for p in target_files if p]} \
            if target_files else {}
        t = Task(
            task_id=f"t-{uuid.uuid4().hex[:12]}", title=title, role=role,
            detail=detail, state="todo", assignee_member_id=assignee_member_id,
            parent_task_id=parent_task_id, depends_on=list(depends_on or []),
            pr_id=pr_id, reason_summary=reason_summary,
            source_spec_artifact_id=source_spec_artifact_id,
            source_plan_artifact_id=source_plan_artifact_id,
            source_slice_id=source_slice_id,
            governance_required=bool(governance_required),
            task_type=task_type, difficulty_tier=difficulty_tier,
            preferred_member_id=preferred_member_id,
            preferred_route_id=preferred_route_id,
            assignment_rationale=assignment_rationale,
            model_assignment=dict(model_assignment) if model_assignment else None,
            created_at=ts, updated_at=ts, _extras=extras,
        )
        # F087-14 WS-2: backlog mutations take the per-project lock so a
        # concurrent compaction (which rewrites the file) can't lose an append.
        with self.lock:
            _append_jsonl(self._backlog_path, t.to_dict())
        return t

    def update_task(self, task_id: str, **patch: Any) -> Task:
        with self.lock:
            tasks = self._projected_tasks()
            if task_id not in tasks:
                raise LedgerError(f"unknown task: {task_id}")
            prior = tasks[task_id]
            cur = prior.to_dict()
            cur.update(patch)
            cur["updated_at"] = _now()
            # F129 Contract #7: flush buffered pending performance attempts on
            # task-boundary transitions BEFORE we serialize the new row, so the
            # persisted task no longer carries `_f129_pending`.
            new_state = str(cur.get("state") or "")
            new_assignment = cur.get("model_assignment") or {}
            prior_assignment = prior.model_assignment or {}
            done_transition = prior.state != new_state and new_state == "done"
            drop_transition = prior.state != new_state and new_state == "dropped"
            assignment_swap = (
                str(new_assignment.get("assignment_id") or "")
                != str(prior_assignment.get("assignment_id") or "")
            )
            # Cross-member reassignment (F127) clears assignee without touching
            # model_assignment. A prior productive turn on the failed member
            # didn't carry the task to done — flush as rejected.
            reassign_clear = (
                prior.assignee_member_id
                and cur.get("assignee_member_id") is None
            )
            if done_transition:
                outcome = "accepted"
            elif drop_transition or assignment_swap or reassign_clear:
                outcome = "rejected"
            else:
                outcome = None
            if outcome is not None:
                try:
                    from .performance_corpus import flush_pending_attempts
                    _, cleaned = flush_pending_attempts(cur, outcome)
                    cur = cleaned
                except Exception:  # noqa: BLE001 - telemetry must not break writes
                    pass
            updated = Task.from_dict(cur)
            _append_jsonl(self._backlog_path, updated.to_dict())
            self._maybe_compact_backlog()
            return updated

    # F087-14 WS-2: the backlog is version-append (every update appends a full
    # record) and is replayed on every list_tasks. Compact it to the current task
    # per id so the replay cost stays bounded over a long run.
    _BACKLOG_COMPACT_MIN = 256

    def _maybe_compact_backlog(self) -> None:
        try:
            raw = _read_jsonl(self._backlog_path)
        except OSError:
            return
        ntasks = len({r.get("task_id") for r in raw if r.get("task_id")})
        if len(raw) >= self._BACKLOG_COMPACT_MIN and len(raw) > 3 * max(ntasks, 1):
            self.compact_backlog()

    def compact_backlog(self) -> int:
        """Rewrite the backlog to one record per task (current version), in
        first-seen order. Returns the number of superseded records dropped."""
        with self.lock:
            if not self._backlog_path.exists():
                return 0
            order: list[str] = []
            seen: set[str] = set()
            raw_count = 0
            for raw in _read_jsonl(self._backlog_path):
                raw_count += 1
                tid = raw.get("task_id")
                if tid and tid not in seen:
                    seen.add(tid)
                    order.append(tid)
            proj = self._projected_tasks()
            lines = [
                json.dumps(proj[tid].to_dict(), ensure_ascii=False, sort_keys=True)
                for tid in order if tid in proj
            ]
            _atomic_write_text(self._backlog_path,
                               ("\n".join(lines) + "\n") if lines else "")
            return raw_count - len(lines)

    def list_tasks(self, *, state: str | None = None,
                   role: str | None = None) -> list[Task]:
        order: list[str] = []
        seen: set[str] = set()
        for raw in _read_jsonl(self._backlog_path):
            tid = raw["task_id"]
            if tid not in seen:
                seen.add(tid)
                order.append(tid)
        proj = self._projected_tasks()
        out = [proj[tid] for tid in order]
        if state is not None:
            out = [t for t in out if t.state == state]
        if role is not None:
            out = [t for t in out if t.role == role]
        return out

    def list_tasks_strict(self) -> list[Task]:
        """Completion-only task view that raises on any malformed record."""
        raw = _read_jsonl_strict(self._backlog_path)
        order: list[str] = []
        projected: dict[str, Task] = {}
        for record in raw:
            try:
                task = Task.from_dict(record)
            except (KeyError, TypeError, ValueError) as exc:
                raise LedgerError("invalid task record in backlog.jsonl") from exc
            if not task.task_id:
                raise LedgerError("task record is missing task_id")
            if task.task_id not in projected:
                order.append(task.task_id)
            projected[task.task_id] = task
        return [projected[task_id] for task_id in order]

    def next_task(self, role: str) -> Task | None:
        proj = self._projected_tasks()
        done_ids = {tid for tid, t in proj.items() if t.state == "done"}
        for t in self.list_tasks(role=role, state="todo"):
            if all(dep in done_ids for dep in t.depends_on):
                return t
        return None

    def next_tasks(self, role: str, n: int, *,
                   exclude: set[str] | None = None) -> list[Task]:
        """Up to ``n`` ready (deps satisfied) ``todo`` tasks for ``role``, in
        backlog order, skipping any id in ``exclude``. Same readiness rule as
        ``next_task``; used by ``plan_next_batch`` (F087 Slice 1) to hand
        distinct tasks to several idle members without double-assigning — the
        caller adds each chosen id to ``exclude`` as it builds the batch.
        Read-only (no lock)."""
        if n <= 0:
            return []
        skip = set(exclude or ())
        proj = self._projected_tasks()
        done_ids = {tid for tid, t in proj.items() if t.state == "done"}
        out: list[Task] = []
        for t in self.list_tasks(role=role, state="todo"):
            if t.task_id in skip:
                continue
            if all(dep in done_ids for dep in t.depends_on):
                out.append(t)
                if len(out) >= n:
                    break
        return out

    # --- decisions / artifacts / skills ----------------------------------
    def record_decision(self, *, title: str, context: str, choice: str,
                        rationale: str, alternatives: list[str] | None = None,
                        related_task_ids: list[str] | None = None,
                        extra: dict[str, Any] | None = None) -> dict[str, Any]:
        rec = {
            "decision_id": f"d-{uuid.uuid4().hex[:12]}", "title": title,
            "context": context, "choice": choice, "rationale": rationale,
            "alternatives": list(alternatives or []),
            "related_task_ids": list(related_task_ids or []), "at": _now(),
        }
        # F087-15 H1: callers may stamp structured fields (e.g. reviewed_head) so
        # downstream readers (the merge gate) can bind a verdict to a head.
        if extra:
            rec.update(extra)
        # F087 Slice 0: serialize the append (concurrent worker turns) — RLock,
        # so nesting inside an already-locked caller is safe.
        with self.lock:
            _append_capped_jsonl(self._decisions_path, rec)
        return rec

    def list_decisions(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._decisions_path)

    # --- run transcript (F087-16: verbose per-turn audit) --------------------
    _TURN_FIELD_CAP = 20_000

    def record_turn(self, *, role: str, member_id: str, task_id: str,
                    prompt: str, response: str, outcome: str, reason: str = "",
                    parse_ok: bool = True, duration_ms: int = 0,
                    model_assignment: dict[str, Any] | None = None,
                    route_id: str | None = None,
                    input_tokens: int | None = None,
                    output_tokens: int | None = None,
                    cache_read_input_tokens: int | None = None,
                    cache_write_input_tokens: int | None = None,
                    measured: bool = False,
                    provenance: str | None = None,
                    composition: dict[str, Any] | None = None,
                    measured_input: int | None = None,
                    measured_output: int | None = None,
                    estimated_input: int | None = None,
                    estimated_output: int | None = None,
                    estimated_input_raw: int | None = None,
                    cli_overhead_tokens: int | None = None,
                    estimator_method: str | None = None,
                    calibration_factor: float | None = None) -> dict[str, Any]:
        """Append one member turn's verbatim transcript: the exact prompt the
        member received, its RAW model response, and the resulting outcome — so a
        run can be reviewed after the fact ("did each member do its job?"). Both
        text fields are capped (bounded ledger growth) but otherwise verbatim.

        F143: the gateway result's per-turn token counts are persisted (additive)
        under a compact ``usage`` sub-dict when the provider reported them. The token
        ints are tiny and NOT subject to ``_TURN_FIELD_CAP`` (that cap guards the big
        prompt/response text only). ``measured`` mirrors the gateway's
        ``raw_usage_available`` — a turn whose provider reported nothing is written
        without a ``usage`` block and rolls up as ``unreported`` (never zero-cost).
        """
        rec: dict[str, Any] = {
            "turn_id": f"trn-{uuid.uuid4().hex[:12]}", "role": role,
            "member_id": member_id, "task_id": task_id,
            "prompt": str(prompt)[: self._TURN_FIELD_CAP],
            "response": str(response)[: self._TURN_FIELD_CAP],
            "outcome": outcome, "reason": reason, "parse_ok": bool(parse_ok),
            "duration_ms": int(duration_ms), "at": _now(),
        }
        if model_assignment:
            rec["model_assignment"] = dict(model_assignment)
        # F143-01 Slice A: stamp the resolved route the gateway dispatched to as a
        # first-class field so PM/review/test turns (which skip the F129 assignment
        # gate) no longer roll up as route ``unknown``. Additive/backward-compatible:
        # older records simply lack it. Only written when non-empty.
        if isinstance(route_id, str) and route_id.strip():
            rec["route_id"] = route_id.strip()
        usage = _turn_usage_block(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens, measured=measured,
            provenance=provenance,
            measured_input=measured_input, measured_output=measured_output,
            estimated_input=estimated_input, estimated_output=estimated_output,
            estimated_input_raw=estimated_input_raw,
            cli_overhead_tokens=cli_overhead_tokens,
            estimator_method=estimator_method,
            calibration_factor=calibration_factor)
        if usage is not None:
            rec["usage"] = usage
        # F143-01 Slice F: the Layer-1 per-segment composition (what Errorta sent,
        # by category). Additive sibling of ``usage``; omitted when absent (legacy
        # turns / coarse-fallback builders / corrective-retry-only turns).
        comp_block = _turn_composition_block(composition)
        if comp_block is not None:
            rec["composition"] = comp_block
        with self.lock:  # F087 Slice 0: serialize concurrent turn appends
            _append_capped_jsonl(self._turns_path, rec)
        return rec

    def list_turns(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        turns = _read_jsonl(self._turns_path)
        if limit is None:
            return turns
        if limit <= 0:
            return []
        return turns[-int(limit):]

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        """F143-01 Slice D: fetch a single recorded turn by its ``turn_id``, or
        ``None`` if unknown. Used by the per-turn composition endpoint."""
        if not turn_id:
            return None
        for turn in _read_jsonl(self._turns_path):
            if isinstance(turn, dict) and turn.get("turn_id") == turn_id:
                return turn
        return None

    # --- pull requests (F087-17: branch-per-task -> PM-approved merge) --------
    def _prs(self) -> dict[str, Any]:
        if not self._prs_path.exists():
            return {}
        try:
            return json.loads(self._prs_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def record_pr(self, *, task_id: str, branch: str, head: str,
                  dev_member: str, base: str = "master") -> dict[str, Any]:
        with self.lock:
            prs = self._prs()
            pr = {
                "pr_id": f"pr-{uuid.uuid4().hex[:12]}", "task_id": task_id,
                "branch": branch, "base": base, "dev_member": dev_member,
                "head": head, "status": "open",
                "reviewed_head": None, "reviewer_approved": None,
                # F100 PR-B: the PM's PR review (strict-mode dual gate), distinct
                # from the reviewer's. Seeded so every PR record is shape-complete.
                "pm_reviewed_head": None, "pm_reviewer_approved": None,
                "tested_head": None, "tests_passed": None,
                "conflicts": [],
                # F159: the git repo-relative files this branch actually touched vs
                # base, persisted at PR-open (refreshed at merge). The OBSERVED
                # touched-files signal — reliable where prose/`target_files` are
                # silent — that hot-file ownership keys off. Seeded so every PR
                # record is shape-complete from birth.
                "changed_paths": [],
                # F091: when a revise PR merges, the PR(s) it superseded get
                # status="superseded" + this back-pointer to the merged PR. Seeded
                # here so every PR record is shape-complete from birth.
                "superseded_by_pr_id": None,
                "created_at": _now(), "updated_at": _now(),
            }
            prs[pr["pr_id"]] = pr
            _atomic_write_json(self._prs_path, prs)
            return pr

    def update_pr(self, pr_id: str, **patch: Any) -> dict[str, Any]:
        with self.lock:
            prs = self._prs()
            if pr_id not in prs:
                raise LedgerError(f"unknown pr: {pr_id}")
            prs[pr_id].update(patch)
            prs[pr_id]["updated_at"] = _now()
            _atomic_write_json(self._prs_path, prs)
            return prs[pr_id]

    def get_pr(self, pr_id: str) -> dict[str, Any] | None:
        return self._prs().get(pr_id)

    def open_pr_for_task(self, task_id: str) -> dict[str, Any] | None:
        """The most recent non-terminal PR for a task (open/changes/mergeable)."""
        # F091: "superseded" (like "merged"/"abandoned") is terminal and
        # intentionally excluded from this live allow-list.
        live = {"open", "changes_requested", "mergeable"}
        out = [p for p in self._prs().values()
               if p.get("task_id") == task_id and p.get("status") in live]
        out.sort(key=lambda p: p.get("created_at", ""))
        return out[-1] if out else None

    def list_prs(self) -> list[dict[str, Any]]:
        return sorted(self._prs().values(), key=lambda p: p.get("created_at", ""))

    def list_prs_strict(self) -> list[dict[str, Any]]:
        """Completion-only PR view that raises instead of hiding corruption."""
        if not self._prs_path.exists():
            return []
        try:
            raw = json.loads(self._prs_path.read_text("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise LedgerError("invalid pull-request ledger") from exc
        if not isinstance(raw, dict):
            raise LedgerError("pull-request ledger must be an object")
        prs: list[dict[str, Any]] = []
        for value in raw.values():
            if not isinstance(value, dict) or not value.get("pr_id"):
                raise LedgerError("invalid pull-request record")
            prs.append(value)
        return sorted(prs, key=lambda p: str(p.get("created_at", "")))

    def pr_state_summary(self) -> dict[str, Any]:
        """F087-19 #1: a compact PR/test snapshot for the orientation packet —
        counts by status, the still-open PRs (branch + status), and the latest
        green (merged / tests-passed) head so the PM always sees integration
        state directly, not just via recent decisions."""
        counts: dict[str, int] = {}
        open_prs: list[dict[str, str]] = []
        latest_merged_head = ""
        for p in self.list_prs():
            s = str(p.get("status", ""))
            counts[s] = counts.get(s, 0) + 1
            # F091: "superseded" is deliberately NOT in this open-set — once a
            # revise PR merges and supersedes a stale PR, the PM must stop seeing
            # that PR as outstanding work (the bug F091 fixes).
            if s in ("open", "changes_requested", "mergeable", "conflict"):
                open_prs.append({"branch": str(p.get("branch", "")), "status": s})
            if s == "merged" and p.get("head"):
                latest_merged_head = str(p["head"])
        latest_green_head = latest_merged_head
        for r in self.list_test_runs():
            if r.get("passed") and r.get("head"):
                latest_green_head = str(r["head"])
        return {
            "counts": counts, "open_prs": open_prs,
            "latest_merged_head": latest_merged_head[:12],
            "latest_green_head": latest_green_head[:12],
        }

    # --- episode summaries (F087-19 #5: durable cross-merge memory) -----------
    @property
    def _episodes_path(self) -> Path:
        return self.dir / "episodes.jsonl"

    def record_episode(self, *, title: str, summary: str, head: str = "",
                       related_task_ids: list[str] | None = None) -> dict[str, Any]:
        """A durable milestone summary (e.g. a merged PR): survives turn-log
        capping so old reasoning doesn't silently fall out of context."""
        rec = {
            "episode_id": f"ep-{uuid.uuid4().hex[:12]}", "title": title,
            "summary": str(summary)[:2000], "head": str(head)[:12],
            "related_task_ids": list(related_task_ids or []), "at": _now(),
        }
        with self.lock:  # F087 Slice 0: serialize concurrent episode appends
            _append_capped_jsonl(self._episodes_path, rec)
        return rec

    def list_episodes(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        eps = _read_jsonl(self._episodes_path)
        if limit is None:
            return eps
        if limit <= 0:
            return []
        return eps[-int(limit):]

    def upsert_artifact(self, *, path: str, status: str, last_task_id: str,
                        content_sha256: str, summary: str = "") -> dict[str, Any]:
        # F087 Slice 0: the whole read-modify-write must be atomic under
        # concurrent worker turns, not just the final write.
        with self.lock:
            index: dict[str, Any] = {}
            if self._artifacts_path.exists():
                index = json.loads(self._artifacts_path.read_text("utf-8"))
            index[path] = {
                "path": path, "status": status, "last_task_id": last_task_id,
                "content_sha256": content_sha256, "summary": summary,
                "updated_at": _now(),
            }
            _atomic_write_json(self._artifacts_path, index)
            return index[path]

    def list_artifacts(
        self, *, scope: str = "all", merged_paths: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Artifact provenance records.

        ``scope="all"`` (default) returns every recorded artifact — including work
        that only ever lived on a task branch that was later abandoned/superseded.
        This is the forensic view; it is NOT what the project actually contains.

        ``scope="merged"`` returns only artifacts whose path is currently on
        ``master``. The ledger never decides mergedness itself (that would re-create
        the F139 ledger↔git drift bug) — the caller MUST pass ``merged_paths`` from
        ``workspace.list_files(scope="master")`` (git truth). Passing
        ``scope="merged"`` without ``merged_paths`` is a programming error.
        """
        artifacts = (
            list(json.loads(self._artifacts_path.read_text("utf-8")).values())
            if self._artifacts_path.exists() else []
        )
        if scope == "all":
            return artifacts
        if scope == "merged":
            if merged_paths is None:
                raise ValueError(
                    "list_artifacts(scope='merged') requires merged_paths "
                    "(git truth from workspace.list_files(scope='master'))")
            keep = set(merged_paths)
            return [a for a in artifacts if a.get("path") in keep]
        raise ValueError(f"unknown artifacts scope: {scope!r}")

    # --- tool events (F087-08: model intent -> tool fact ledger) ---------------
    def record_tool_event(
        self,
        *,
        turn_id: str,
        task_id: str,
        member_id: str,
        role: str,
        tool: str,
        status: str,
        intent: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        rec = {
            "event_id": f"te-{uuid.uuid4().hex[:12]}",
            "turn_id": turn_id,
            "task_id": task_id,
            "member_id": member_id,
            "role": role,
            "tool": tool,
            "intent": dict(intent or {}),
            "status": status,
            "result": dict(result or {}),
            "error": error,
            "at": _now(),
        }
        with self.lock:
            _append_capped_jsonl(self._tool_events_path, rec)
        return rec

    def list_tool_events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        events = _read_jsonl(self._tool_events_path)
        if limit is None:
            return events
        if limit <= 0:
            return []
        return events[-int(limit):]

    # --- F087-10 test-command registry + grounded run records ----------------
    def get_test_commands(self) -> dict[str, Any]:
        if not self._test_commands_path.exists():
            return {}
        return json.loads(self._test_commands_path.read_text("utf-8"))

    def set_test_commands(self, commands: Any) -> dict[str, Any]:
        """Validate + persist the per-project test-command registry. Argv-only
        (no shell), slug-safe ids, bounded timeouts, worktree-relative cwd. Any
        violation raises LedgerError (fail-closed: a malformed registry is never
        partially stored)."""
        from errorta_export.safe_path import UnsafePathError, safe_segment
        if not isinstance(commands, dict):
            raise LedgerError("test commands must be an object")
        clean: dict[str, Any] = {}
        for cmd_id, spec in commands.items():
            try:
                safe_segment(str(cmd_id))
            except UnsafePathError as exc:
                raise LedgerError(f"unsafe command_id: {cmd_id!r}") from exc
            if len(str(cmd_id)) > 64:
                raise LedgerError(f"command_id too long: {cmd_id!r}")
            if not isinstance(spec, dict):
                raise LedgerError(f"command {cmd_id!r} must be an object")
            argv = spec.get("argv")
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(a, str) for a in argv)):
                raise LedgerError(f"command {cmd_id!r} argv must be a non-empty "
                                  "list of strings")
            timeout = spec.get("timeout_seconds", 120)
            if not isinstance(timeout, (int, float)) or not (1 <= timeout <= 600):
                raise LedgerError(f"command {cmd_id!r} timeout_seconds must be "
                                  "in [1, 600]")
            cwd = str(spec.get("cwd", "."))
            if cwd.startswith("/") or cwd.startswith("\\") or ".." in Path(cwd).parts:
                raise LedgerError(f"command {cmd_id!r} cwd must be worktree-relative")
            clean[str(cmd_id)] = {
                "argv": [str(a) for a in argv], "cwd": cwd,
                "timeout_seconds": int(timeout),
                "label": str(spec.get("label", cmd_id)),
            }
        _atomic_write_json(self._test_commands_path, clean)
        return clean

    def record_test_run(self, session: Any, *, task_id: str,
                        head: str = "") -> dict[str, Any]:
        """Append one grounded test-run session (the audit spine: command_ids +
        per-command exit codes + argv/stdout hashes + passed). F087-15 H1: the
        worktree ``head`` the run executed against is persisted so the merge gate
        can bind the verdict to the exact head; F087-15 M4: the actual sandbox
        backend is recorded for honest reporting."""
        rec = {
            "test_run_id": f"tr-{uuid.uuid4().hex[:12]}", "task_id": task_id,
            "command_ids": list(session.command_ids),
            "unknown_ids": list(session.unknown_ids),
            "passed": bool(session.passed),
            "results": [r.to_dict() for r in session.results],
            "head": str(head),
            "sandbox": str(getattr(session, "sandbox", "") or ""),
            "at": _now(),
        }
        with self.lock:  # F087 Slice 0: serialize concurrent test-run appends
            _append_capped_jsonl(self._test_runs_path, rec)
        return rec

    def list_test_runs(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._test_runs_path)

    # --- test-execution settings (F087-15 M4) --------------------------------
    @property
    def _settings_path(self) -> Path:
        return self.dir / "settings.json"

    def _settings(self) -> dict[str, Any]:
        if not self._settings_path.exists():
            return {}
        try:
            return json.loads(self._settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def get_require_sandbox(self) -> bool:
        """Whether test runs must fail closed when no OS sandbox is available."""
        return bool(self._settings().get("require_sandbox", False))

    def set_require_sandbox(self, value: bool) -> bool:
        with self.lock:
            s = self._settings()
            s["require_sandbox"] = bool(value)
            _atomic_write_json(self._settings_path, s)
        return bool(value)

    def get_assembled_run_required(self) -> bool:
        """Spec 05 Phase A: whether the merge gate refuses a web/app deliverable
        that has NOTHING verifying it actually runs — no runnable runtime profile
        and no registered assembled/acceptance test command (the "vacuous 12/12
        PASS on a broken app" case). Off by default globally: ``Project`` carries
        no project-kind field to auto-enable this from, so an operator opts in and
        the ``assembled_run_unverified`` blocker is additionally scoped to a
        web/app deliverable (index.html / web|static runtime profile) inside
        ``gather_merge_evidence``. Mirrors ``get_require_sandbox``."""
        return bool(self._settings().get("assembled_run_required", False))

    def set_assembled_run_required(self, value: bool) -> bool:
        with self.lock:
            s = self._settings()
            s["assembled_run_required"] = bool(value)
            _atomic_write_json(self._settings_path, s)
        return bool(value)

    # --- F104 S5: implementer grounding signal + spec-conformance policy ------
    _GROUNDING_POLICIES = ("off", "warn", "required_when_corpus_bound", "required")

    def get_grounding_policy(self) -> str:
        """How strictly the merge gate treats implementer grounding (F104 S5).
        Default ``warn`` — surfaced, never blocks (mirrors F101 D5)."""
        val = str(self._settings().get("grounding_policy", "warn"))
        return val if val in self._GROUNDING_POLICIES else "warn"

    def set_grounding_policy(self, value: str) -> str:
        if value not in self._GROUNDING_POLICIES:
            raise LedgerError(f"invalid grounding_policy: {value!r}")
        with self.lock:
            s = self._settings()
            s["grounding_policy"] = value
            _atomic_write_json(self._settings_path, s)
        return value

    @property
    def _grounding_signals_path(self) -> Path:
        return self.dir / "grounding-signals.json"

    def _grounding_signals(self) -> dict[str, Any]:
        if not self._grounding_signals_path.exists():
            return {}
        try:
            raw = json.loads(self._grounding_signals_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def record_implementer_grounding(self, *, task_id: str,
                                     corpus_evidence_count: int) -> None:
        """Record that an implementer turn for ``task_id`` saw N corpus-evidence
        items (F104 S5). Keeps the MAX seen so a single ungrounded retry can't
        erase a prior grounded turn. Deterministic merge-gate input."""
        with self.lock:
            data = self._grounding_signals()
            prev = int(data.get(task_id, {}).get("corpus_evidence_count", 0))
            data[task_id] = {
                "task_id": task_id,
                "corpus_evidence_count": max(prev, int(corpus_evidence_count)),
                "updated_at": _now(),
            }
            _atomic_write_json(self._grounding_signals_path, data)

    def implementer_grounding(self, task_id: str) -> int:
        return int(self._grounding_signals().get(task_id, {}).get(
            "corpus_evidence_count", 0))

    def any_implementer_grounded(self) -> bool:
        return any(int(v.get("corpus_evidence_count", 0)) > 0
                   for v in self._grounding_signals().values())

    # --- run lifecycle (F087-07-F: durable across a sidecar restart) ---------
    @property
    def _run_state_path(self) -> Path:
        return self.dir / "run_state.json"

    def get_run_state(self) -> dict[str, Any]:
        default = {"status": "idle", "stop_reason": None, "started_at": None,
                   "ended_at": None, "cancel_requested": False,
                   "last_error": None, "counters": None}
        if not self._run_state_path.exists():
            return default
        try:
            raw = json.loads(self._run_state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return default
        default.update(raw)
        return default

    @property
    def lock(self):
        """F087-13 WS-3: the process-wide per-project lock (shared across every
        LedgerStore instance for this project's dir). Serializes the run-state
        read-modify-write, the start-run critical section, and recovery."""
        from .locks import lock_for_dir
        return lock_for_dir(self.dir)

    def set_run_state(self, **patch: Any) -> dict[str, Any]:
        # F087-13 WS-3: read-modify-write of one JSON doc under the per-project
        # lock so a worker write racing a cancel/recovery write can't silently
        # revert the other field (last-writer-wins on the whole document).
        with self.lock:
            state = self.get_run_state()
            state.update(patch)
            _atomic_write_json(self._run_state_path, state)
            return state

    # --- run config (F097: the team a run was started with, so Resume can
    # reconstruct it without the caller re-supplying members/room_id) ----------
    @property
    def _run_config_path(self) -> Path:
        return self.dir / "run_config.json"

    def get_run_config(self) -> dict[str, Any]:
        """The persisted start-time team config, or ``{}`` when absent."""
        if not self._run_config_path.exists():
            return {}
        try:
            raw = json.loads(self._run_config_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def set_run_config(self, **patch: Any) -> dict[str, Any]:
        """Merge-write the run config under the per-project lock (mirrors
        ``set_run_state``). Stores member dicts (routes/roles/prompts — never keys)
        + the source room id + a timestamp."""
        with self.lock:
            cfg = self.get_run_config()
            cfg.update(patch)
            _atomic_write_json(self._run_config_path, cfg)
            return cfg

    # --- F141 WS-J: synchronous PM chat thread ("pull the PM aside") ---------
    @property
    def _pm_chat_path(self) -> Path:
        return self.dir / "pm_chat.jsonl"

    def append_pm_chat(self, *, role: str, message: str,
                       thread_id: str = "main") -> dict[str, Any]:
        """Append one turn (user or pm) to the PM chat thread. Lock-held so a
        concurrent read/append can't interleave. Distinct from an interjection:
        this is a conversation, not an authoritative next-turn directive."""
        rec = {"role": str(role), "message": str(message)[: self._TURN_FIELD_CAP],
               "thread_id": str(thread_id or "main"), "at": _now()}
        with self.lock:
            # Capped like every other transcript ledger (turns/decisions/…) so a
            # long-lived chat can't grow pm_chat.jsonl without bound.
            _append_capped_jsonl(self._pm_chat_path, rec)
        return rec

    def list_pm_chat(self, *, thread_id: str = "main") -> list[dict[str, Any]]:
        """The PM chat thread in order (empty when none). Best-effort read."""
        return [r for r in _read_jsonl(self._pm_chat_path)
                if str(r.get("thread_id", "main")) == str(thread_id or "main")]

    # --- interjections (F087-07-E: the F049 pinned authoritative contract) ---
    @property
    def _interjections_path(self) -> Path:
        return self.dir / "interjections.jsonl"

    @property
    def _interject_cursor_path(self) -> Path:
        return self.dir / "interject_cursor.json"

    def record_interjection(
        self,
        message: str,
        *,
        pm_reply: dict[str, Any] | None = None,
        artifact_id: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Record an authoritative user directive for the PM's next plan turn.

        ``artifact_id`` (F100-02 C) tags a comment to the governance artifact it
        was made on, so it threads into the audit trail / Team Log; the PM prompt
        already injects unconsumed interjections, so no new delivery path.
        ``kind`` (F135) tags the interjection's origin (e.g. ``"work_request"``)
        so a later edit can supersede a stale one of the same kind.
        """
        rec: dict[str, Any] = {"message": str(message)[: self._TURN_FIELD_CAP],
                               "at": _now()}
        if pm_reply is not None:
            rec["pm_reply"] = pm_reply
        if artifact_id is not None:
            rec["artifact_id"] = str(artifact_id)
        if kind is not None:
            rec["kind"] = str(kind)
        # F135 review #1: hold the per-project lock so an append can't interleave
        # with supersede_work_request_interjection's full-file rewrite (which would
        # silently clobber a concurrently-appended record).
        with self.lock:
            _append_capped_jsonl(self._interjections_path, rec)
        return rec

    def _supersede_interjection(self, message: str, *,
                                kind: str) -> dict[str, Any]:
        """F135 D5 (generalized by F137): record a fresh ``kind``-tagged
        interjection and drop any prior UNCONSUMED one of the same kind so only
        the newest applies.

        Consumed records are always a prefix (indices < cursor), so rewriting the
        unconsumed suffix keeps the cursor valid. Unrelated interjections in the
        suffix are preserved and still stack, matching existing behaviour. Held
        under the per-project lock + written atomically (temp + os.replace) so a
        concurrent append (a live PM turn) can neither be lost nor read a
        truncated file (F135 review #1).
        """
        with self.lock:
            records = _read_jsonl(self._interjections_path)
            cursor = self._interject_cursor()
            consumed, pending = records[:cursor], records[cursor:]
            pending = [r for r in pending if r.get("kind") != kind]
            rec: dict[str, Any] = {"message": str(message)[: self._TURN_FIELD_CAP],
                                   "at": _now(), "kind": kind}
            kept = consumed + pending + [rec]
            body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
            _atomic_write_text(self._interjections_path, body)
        return rec

    def supersede_work_request_interjection(self, message: str) -> dict[str, Any]:
        """F135 D5 back-compat shim — see ``_supersede_interjection``."""
        return self._supersede_interjection(message, kind="work_request")

    def supersede_current_focus_interjection(self, message: str) -> dict[str, Any]:
        """F137: deliver a mid-run Current-Focus change to the running PM as an
        authoritative ``current_focus`` interjection, superseding any prior
        unconsumed one so the PM re-plans against the newest focus set."""
        return self._supersede_interjection(message, kind="current_focus")

    # --- F135: current-focus directive (work_request) --------------------
    def set_work_request(self, work_request: str) -> Project:
        """Persist the current-focus directive; bumps revision. Capped to the
        turn-field cap so an oversized directive can't blow the packet budget.
        Held under the per-project lock so a concurrent run-state / completion
        write can't lose-update the same project.json (F135 review #2).

        F137: the Focus ledger is now the source of truth for scoping, so this
        also upserts the *primary* active focus (the first active one, or a new
        one) to keep the legacy field and the ledger coherent. An empty string
        clears the legacy field without touching the ledger."""
        with self.lock:
            raw = self.get_project().to_dict()
            text = str(work_request)[: self._TURN_FIELD_CAP]
            raw["work_request"] = text
            raw["revision"] = int(raw.get("revision", 1)) + 1
            raw["updated_at"] = _now()
            _atomic_write_json(self._project_path, raw)
            if text.strip():
                self._upsert_primary_focus(text)
            return Project.from_dict(raw)

    # --- F137: Current Focus goals (multi-item, lifecycle-managed) --------
    _ACTIVE_FOCUS_SOFT_CAP = 10

    @property
    def _focus_path(self) -> Path:
        return self.dir / "focus.jsonl"

    def _read_focuses(self) -> list[Focus]:
        return [Focus.from_dict(r) for r in _read_jsonl(self._focus_path)]

    def _write_focuses(self, focuses: list[Focus]) -> None:
        body = "".join(
            json.dumps(f.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for f in focuses
        )
        _atomic_write_text(self._focus_path, body)

    def _ensure_focus_migrated(self) -> list[Focus] | None:
        """F137 D8: a one-time migration seeding the ledger from the legacy F135
        ``work_request`` string. Idempotent — guarded on the focus file's
        absence — and lock-held so a concurrent add can't double-seed. Callers
        hold the lock already OR call this outside a lock; ``lock`` is reentrant.

        Returns the seeded list when it just migrated, else ``None`` (so a read
        can use the in-memory result even if persistence fails). Persisting is
        **best-effort**: a read on a read-only / remote-residency mount must never
        crash — it simply doesn't durably migrate there (a later writable call
        will). Mutating callers still surface the write failure via their own
        ``_write_focuses``."""
        with self.lock:
            if self._focus_path.exists():
                return None
            try:
                wr = (self.get_project().work_request or "").strip()
            except Exception:
                wr = ""
            if not wr:
                return None
            seed = [Focus(
                id=_new_focus_id(), title=wr[: self._TURN_FIELD_CAP],
                status="active", order=0, origin="work_request_migration",
                created_at=_now(),
            )]
            try:
                self._write_focuses(seed)
            except OSError:
                pass  # best-effort; the read still returns the in-memory seed
            return seed

    @staticmethod
    def _focus_sort_key(f: Focus) -> tuple[int, str]:
        return (f.order, f.created_at)

    def list_focuses(self, *, status: str | None = None) -> list[Focus]:
        """All focuses (optionally filtered by status), ordered by (order,
        created_at). Runs the legacy migration first so callers see a coherent
        ledger even on a project that predates F137 — and is a pure read from the
        caller's view (migration persistence is best-effort, never fatal)."""
        seeded = self._ensure_focus_migrated()
        raw = seeded if seeded is not None else self._read_focuses()
        focuses = sorted(raw, key=self._focus_sort_key)
        if status is not None:
            focuses = [f for f in focuses if f.status == status]
        return focuses

    def active_focuses(self) -> list[Focus]:
        """The active focus set — the operative scope for a run, in PM/user
        order. Empty is valid (a pure North-Star run behaves as pre-F137)."""
        return self.list_focuses(status="active")

    def add_focus(self, *, title: str, body: str = "",
                  origin: str = "user") -> Focus:
        """Append a new active focus at the end of the active order. Returns the
        created focus; ``over_soft_cap`` is advisory (the route surfaces it)."""
        title = str(title).strip()[: self._TURN_FIELD_CAP]
        if not title:
            raise LedgerError("focus title is required")
        with self.lock:
            self._ensure_focus_migrated()
            focuses = self._read_focuses()
            max_order = max(
                (f.order for f in focuses if f.status == "active"), default=-1)
            focus = Focus(
                id=_new_focus_id(), title=title,
                body=str(body)[: self._TURN_FIELD_CAP], status="active",
                order=max_order + 1, origin=origin, created_at=_now(),
            )
            self._write_focuses(focuses + [focus])
            return focus

    def _upsert_primary_focus(self, title: str) -> Focus:
        """F137 back-compat helper for ``set_work_request``: retitle the first
        active focus, or add one when there is none. Caller holds the lock."""
        self._ensure_focus_migrated()
        focuses = self._read_focuses()
        active = sorted(
            (f for f in focuses if f.status == "active"), key=self._focus_sort_key)
        if active:
            primary = active[0]
            updated = replace(primary, title=str(title)[: self._TURN_FIELD_CAP])
            self._write_focuses(
                [updated if f.id == primary.id else f for f in focuses])
            return updated
        max_order = max((f.order for f in focuses if f.status == "active"),
                        default=-1)
        focus = Focus(id=_new_focus_id(), title=str(title)[: self._TURN_FIELD_CAP],
                      status="active", order=max_order + 1,
                      origin="work_request_migration", created_at=_now())
        self._write_focuses(focuses + [focus])
        return focus

    def _get_focus(self, focus_id: str) -> Focus:
        for f in self._read_focuses():
            if f.id == focus_id:
                return f
        raise FocusNotFound(f"focus not found: {focus_id!r}")

    def update_focus(self, focus_id: str, **patch: Any) -> Focus:
        """Edit a focus's mutable fields (title/body/status). Status is validated
        against FOCUS_STATES. Lock-held full-rewrite."""
        allowed = {"title", "body", "status"}
        bad = set(patch) - allowed
        if bad:
            raise LedgerError(f"cannot patch focus fields: {sorted(bad)}")
        if "status" in patch and patch["status"] not in FOCUS_STATES:
            raise LedgerError(f"invalid focus status: {patch['status']!r}")
        with self.lock:
            focuses = self._read_focuses()
            target = next((f for f in focuses if f.id == focus_id), None)
            if target is None:
                raise FocusNotFound(f"focus not found: {focus_id!r}")
            if target.status == "archived":
                raise FocusTransitionError(
                    f"archived focus is read-only: {focus_id!r}")
            clean = dict(patch)
            for key in ("title", "body"):
                if key in clean:
                    clean[key] = str(clean[key]).strip()[: self._TURN_FIELD_CAP]
            if "title" in clean and not clean["title"]:
                raise LedgerError("focus title is required")
            requested_status = clean.get("status")
            if requested_status not in (None, target.status, "completed", "archived"):
                raise FocusTransitionError(
                    f"invalid focus transition: {target.status} -> {requested_status}")
            if target.status == "completed" and requested_status == "archived":
                raise FocusTransitionError(
                    "completed focus must be archived through the accept gate")
            if clean.get("status") == "archived" and not target.archived_at:
                clean["archived_at"] = _now()
            if clean.get("status") == "completed" and not target.completed_at:
                clean["completed_at"] = _now()
            updated = replace(target, **clean)
            self._write_focuses(
                [updated if f.id == focus_id else f for f in focuses])
            return updated

    def reorder_focuses(self, ordered_ids: list[str]) -> list[Focus]:
        """Set the active-focus order to ``ordered_ids``. Ids not present are
        ignored; active ids omitted from the list keep their relative order after
        the explicitly-ordered ones. Non-active focuses are untouched."""
        with self.lock:
            self._ensure_focus_migrated()
            focuses = self._read_focuses()
            by_id = {f.id: f for f in focuses}
            active = sorted(
                (f for f in focuses if f.status == "active"),
                key=self._focus_sort_key)
            ranked = [fid for fid in ordered_ids if by_id.get(fid) is not None
                      and by_id[fid].status == "active"]
            tail = [f.id for f in active if f.id not in ranked]
            new_order = {fid: i for i, fid in enumerate(ranked + tail)}
            rewritten = [
                replace(f, order=new_order[f.id]) if f.id in new_order else f
                for f in focuses
            ]
            self._write_focuses(rewritten)
            return sorted(
                (f for f in rewritten if f.status == "active"),
                key=self._focus_sort_key)

    def propose_focus_complete(self, focus_id: str,
                               completion_summary: str) -> Focus:
        """PM path: mark a focus completed (pending the human-accept gate). Sets
        completed_at + a completion_summary; does NOT archive."""
        with self.lock:
            focuses = self._read_focuses()
            target = next((f for f in focuses if f.id == focus_id), None)
            if target is None:
                raise FocusNotFound(f"focus not found: {focus_id!r}")
            # F137 D5: an archived focus is terminal history — never resurrect it
            # back into the active/completed lifecycle.
            if target.status == "archived":
                raise FocusTransitionError(f"focus is archived: {focus_id!r}")
            updated = replace(
                target, status="completed", completed_at=_now(),
                completion_summary=str(completion_summary)[: self._TURN_FIELD_CAP])
            self._write_focuses(
                [updated if f.id == focus_id else f for f in focuses])
            return updated

    def accept_focus(self, focus_id: str) -> Focus:
        """Human-accept gate: archive a completed focus. Stamps
        accepted_at + archived_at. Does NOT change project status (F137 D6)."""
        with self.lock:
            focuses = self._read_focuses()
            target = next((f for f in focuses if f.id == focus_id), None)
            if target is None:
                raise FocusNotFound(f"focus not found: {focus_id!r}")
            # Already archived (dropped or previously accepted) — terminal; a
            # re-accept must not re-stamp accepted_at over the original.
            if target.status == "archived":
                raise FocusTransitionError(f"focus is archived: {focus_id!r}")
            if target.status != "completed":
                raise FocusTransitionError(
                    f"focus must be completed before accept: {focus_id!r}")
            ts = _now()
            updated = replace(
                target, status="archived", accepted_at=ts,
                archived_at=target.archived_at or ts)
            self._write_focuses(
                [updated if f.id == focus_id else f for f in focuses])
            return updated

    def current_focus_directive_text(self) -> str:
        """Render the ordered active focus set for a ``current_focus`` interjection
        (mid-run steering). Empty string when there is no active focus."""
        active = self.active_focuses()
        if not active:
            return ""
        return "Current Focus updated. The team's operative scope is now:\n" + \
            "\n".join(format_focus_lines(active))

    def promote_north_star(self, north_star: str, definition_of_done: str) -> Project:
        """F135 review #2: lock-held read-modify-write of the authoritative North
        Star + Definition of Done (used by proposal-accept and put-north-star) so a
        concurrent completion/run-state write can't lose-update project.json."""
        with self.lock:
            raw = self.get_project().to_dict()
            raw["north_star"] = str(north_star)
            raw["definition_of_done"] = str(definition_of_done)
            raw["revision"] = int(raw.get("revision", 1)) + 1
            raw["updated_at"] = _now()
            # F141 WS-I: an imported project ships a real, already-built
            # foundation — accepting its inferred North Star puts it straight into
            # the steering phase (Current Focus becomes relevant). Forward-only.
            if raw.get("target") == "existing" and not raw.get("north_star_met_at"):
                raw["north_star_met_at"] = _now()
            _atomic_write_json(self._project_path, raw)
            return Project.from_dict(raw)

    def mark_north_star_met(self) -> Project:
        """F141 WS-I: forward-only stamp that the project has crossed into the
        steering phase (its initial North Star is met). Idempotent — a second call
        never moves the timestamp. Called on the first foundation-merge for `new`
        targets; imported targets are stamped at North-Star accept."""
        with self.lock:
            raw = self.get_project().to_dict()
            if raw.get("north_star_met_at"):
                return Project.from_dict(raw)
            raw["north_star_met_at"] = _now()
            raw["updated_at"] = _now()
            _atomic_write_json(self._project_path, raw)
            return Project.from_dict(raw)

    def update_import_source(self, patch: dict[str, Any]) -> Project:
        """F138: lock-held merge into ``import_source`` (e.g. record a refresh's
        ``refreshed_at`` / new ``cloned_ref``). Display/audit only; never
        authoritative for a run. No-op-safe when ``import_source`` is None (a
        hand-created project) — starts a fresh dict."""
        with self.lock:
            raw = self.get_project().to_dict()
            current = raw.get("import_source")
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(patch)
            raw["import_source"] = merged
            raw["revision"] = int(raw.get("revision", 1)) + 1
            raw["updated_at"] = _now()
            _atomic_write_json(self._project_path, raw)
            return Project.from_dict(raw)

    # --- F135: North Star inference proposal (non-authoritative) ---------
    @property
    def _orientation_proposal_path(self) -> Path:
        return self.dir / "north-star-proposal.json"

    def save_orientation_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        _atomic_write_json(self._orientation_proposal_path, proposal)
        return proposal

    def get_orientation_proposal(self) -> dict[str, Any] | None:
        if not self._orientation_proposal_path.exists():
            return None
        try:
            raw = json.loads(self._orientation_proposal_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def _interject_cursor(self) -> int:
        if not self._interject_cursor_path.exists():
            return 0
        try:
            raw = json.loads(self._interject_cursor_path.read_text("utf-8"))
            return int(raw.get("consumed", 0))
        except (OSError, ValueError):
            return 0

    def list_unconsumed_interjections(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._interjections_path)[self._interject_cursor():]

    def mark_interjections_consumed(self) -> None:
        # F135 review #1: serialize the count-then-write against a concurrent
        # supersede rewrite so the cursor can't be set from a stale line count.
        with self.lock:
            total = len(_read_jsonl(self._interjections_path))
            _atomic_write_json(self._interject_cursor_path, {"consumed": total})

    def record_skill_use(self, *, member_id: str, task_id: str, skill: str,
                         phase: str) -> dict[str, Any]:
        rec = {"member_id": member_id, "task_id": task_id, "skill": skill,
               "phase": phase, "at": _now()}
        _append_capped_jsonl(self._skills_path, rec)
        return rec

    def list_skill_uses(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._skills_path)

    # --- digest ----------------------------------------------------------
    def regenerate_digest(self) -> dict[str, Any]:
        doing = self.list_tasks(state="doing")
        open_count = len(self.list_tasks(state="todo")) + len(doing)
        digest = {
            # NOTE: "current_focus" here is the TASK currently in `doing` — a
            # long-standing digest field. The F137 Current Focus GOALS (the
            # user/PM steering wheel) are the separate "current_focus_goals" key
            # below; the two are intentionally distinct (spec F137 Risks).
            "current_focus": doing[0].title if doing else None,
            "current_focus_goals": [f.title for f in self.active_focuses()],
            "open_task_count": open_count,
            "recent_decisions": self.list_decisions()[-5:],
            "recent_artifacts": self.list_artifacts()[-5:],
            "active_blockers": [t.to_dict() for t in self.list_tasks(state="blocked")],
            # F143: cache the project-wide token total so the overview headline is
            # O(1) to read. Rebuilt from the full turn list every regeneration
            # (never incremented), so it cannot drift or double-count.
            "token_usage": rollup_turns(self.list_turns())["total"],
            "regenerated_at": _now(),
        }
        _atomic_write_json(self._digest_path, digest)
        return digest
