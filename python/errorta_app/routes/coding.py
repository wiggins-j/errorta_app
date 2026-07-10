"""F087-01 — Coding Mode project ledger routes (Tauri-origin, loopback).

Thin CRUD over the F087-01 ledger so the workspace viewer (F087-06) and the
user-intervention path (F087-03) can read/edit project state. Mutations apply
the F086-E fail-closed-under-remote-residency guard.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errorta_app import settings
from errorta_council.coding.autonomy import (
    load_policy,
    policy_from_dict,
    policy_to_dict,
    save_policy,
)
from errorta_council.coding.ledger import (
    FocusNotFound,
    FocusTransitionError,
    LedgerError,
    LedgerStore,
    ProjectNotFound,
    _atomic_write_json,
    _now,
    list_projects,
)
from errorta_council.coding.governance import (
    GovernanceMode,
    GovernancePhase,
    HumanCodeApproval,
)
from errorta_council.coding.orientation import build_orientation_packet
from errorta_council.coding.project_status import derive_project_list_status
from errorta_council.coding.skills import (
    SkillsGuardrailPolicy,
    load_guardrail,
    save_guardrail,
)
from errorta_tools.runner.apply_workspace import ApplyWorkspaceError

from ._residency_proxy import refuse_local_dataplane_if_remote

router = APIRouter(prefix="/coding", tags=["coding"])


def _alpha_enforce_not_locked() -> None:
    from errorta_alpha.state import enforce_not_locked

    enforce_not_locked()


def _require_tauri_origin(request: Request) -> None:
    """F087-07-D: coding mutations (esp. merge-back to the real repo) must come
    from the Tauri webview, mirroring the Council/settings guard."""
    if request.headers.get("x-errorta-origin", "").lower() != "tauri-ui":
        raise HTTPException(status_code=403, detail="origin_not_authorized")


def _validate_repo_path(repo_path: Optional[str]) -> None:
    """F087-13 WS-4: an ``existing``-target ``repo_path`` is the merge-back WRITE
    destination. Validate it at create time so the team can never be pointed at
    ~/.ssh, /etc, the Errorta home, etc. Must be an existing git repo and must
    not resolve under a sensitive root or inside a hidden home dotdir."""
    from pathlib import Path
    if not repo_path:
        raise HTTPException(status_code=422, detail="existing target needs repo_path")
    try:
        real = Path(repo_path).expanduser().resolve()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid repo_path: {exc}") from exc
    if not real.is_dir():
        raise HTTPException(status_code=422, detail="repo_path is not a directory")
    if not (real / ".git").exists():
        raise HTTPException(status_code=422, detail="repo_path is not a git repository")

    home = Path.home().resolve()
    denied: list[Path] = [Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"),
                          Path("/System"), Path("/Library"), Path("/private/etc")]
    try:
        from errorta_app.paths import errorta_home
        denied.append(errorta_home().resolve())
    except Exception:
        pass
    if real == home or real == real.anchor or real == Path(real.anchor):
        raise HTTPException(status_code=422, detail="repo_path too broad")
    for root in denied:
        try:
            if real == root or real.is_relative_to(root):
                raise HTTPException(status_code=422,
                                    detail=f"repo_path resolves under a protected root: {root}")
        except AttributeError:  # pragma: no cover - py<3.9
            if str(real).startswith(str(root)):
                raise HTTPException(status_code=422, detail="repo_path under protected root")
    # reject anything inside a hidden dotdir under the home (e.g. ~/.ssh, ~/.aws)
    try:
        if real.is_relative_to(home):
            rel_parts = real.relative_to(home).parts
            if rel_parts and rel_parts[0].startswith("."):
                raise HTTPException(status_code=422,
                                    detail="repo_path is inside a hidden home directory")
    except AttributeError:  # pragma: no cover
        pass


def _validate_delivery_root(delivery_root: Optional[str]) -> Optional[str]:
    """F105: validate the user-selected greenfield delivery PARENT directory.

    The accepted MVP is exported into ``<delivery_root>/<project_id>``, so the
    root is a filesystem WRITE boundary — validate fail-closed (mirrors
    ``_validate_repo_path``). ``None``/blank means the default
    (~/Errorta Projects) and is allowed. Otherwise the root must be an absolute,
    existing directory that is not a filesystem/OS/protected root, not the home
    dir itself, not ERRORTA_HOME (or under it), and not a hidden dot-directory
    directly under home. Returns the normalized absolute path (or None for the
    default). Raises HTTPException 422 on rejection."""
    from pathlib import Path

    if delivery_root is None or not str(delivery_root).strip():
        return None
    try:
        real = Path(delivery_root).expanduser().resolve()
    except Exception as exc:
        raise HTTPException(status_code=422,
                            detail=f"invalid delivery_root: {exc}") from exc
    if not real.is_absolute():
        raise HTTPException(status_code=422, detail="delivery_root must be absolute")
    if not real.is_dir():
        raise HTTPException(status_code=422, detail="delivery_root is not a directory")

    home = Path.home().resolve()
    # Filesystem root (POSIX "/" or a Windows drive root like "C:\\").
    if real == Path(real.anchor) or str(real) == real.anchor:
        raise HTTPException(status_code=422, detail="delivery_root is a filesystem root")
    if real == home:
        raise HTTPException(status_code=422, detail="delivery_root cannot be the home directory")

    denied: list[Path] = [
        # POSIX protected roots. NOTE: ``/private`` is narrowed to ``/private/etc``
        # (matching the existing _validate_repo_path precedent) because the macOS
        # per-user temp dir lives under ``/private/var/folders`` — blocking bare
        # ``/private`` would reject every legitimate temp-rooted directory while
        # still leaving the truly-sensitive ``/private/etc`` covered.
        Path("/System"), Path("/Library"), Path("/usr"), Path("/bin"),
        Path("/sbin"), Path("/etc"), Path("/var"), Path("/private/etc"),
        Path("/Applications"),
        # Windows protected roots (no-ops on POSIX).
        Path("C:\\"), Path("C:\\Windows"), Path("C:\\Program Files"),
        Path("C:\\Program Files (x86)"), Path("C:\\ProgramData"),
    ]
    try:
        from errorta_app.paths import errorta_home
        denied.append(errorta_home().resolve())
    except Exception:
        pass
    for root in denied:
        try:
            if real == root or real.is_relative_to(root):
                raise HTTPException(
                    status_code=422,
                    detail=f"delivery_root resolves under a protected root: {root}")
        except AttributeError:  # pragma: no cover - py<3.9
            if str(real).startswith(str(root)):
                raise HTTPException(status_code=422,
                                    detail="delivery_root under protected root")

    # Reject a hidden dot-directory directly under home (~/.ssh, ~/.config, ...).
    try:
        if real.is_relative_to(home):
            rel_parts = real.relative_to(home).parts
            if rel_parts and rel_parts[0].startswith("."):
                raise HTTPException(
                    status_code=422,
                    detail="delivery_root is inside a hidden home directory")
    except AttributeError:  # pragma: no cover
        pass
    return str(real)


class _NewProject(BaseModel):
    # Slug only (rejects '/', '\\', etc.). Pure-dot traversal ('.'/'..') passes
    # this charset but is rejected by the LedgerStore safe_segment backstop,
    # caught in create_project below -> 422. (Pydantic v2's regex engine has no
    # lookahead, so the '.'/'..' exclusion lives in the store, not here.)
    project_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    north_star: str = ""
    definition_of_done: str = ""
    target: str = "new"
    repo_path: Optional[str] = None
    # F105: parent directory a greenfield project is delivered into. None/blank =
    # the default (~/Errorta Projects). Validated by _validate_delivery_root.
    delivery_root: Optional[str] = None
    # F135: current-focus directive ("what to work on right now"). Optional at
    # create; also editable later via PUT .../work-request.
    work_request: str = ""
    grounding: Optional[dict[str, Any]] = None


class _UpdateProjectFile(BaseModel):
    # F105: a human edit to a merged text file on master. expected_sha256 is the
    # full-blob SHA-256 the editor loaded (exactly 64 hex chars) — optimistic
    # concurrency, a mismatch is a stale write (409).
    content: str
    expected_sha256: str = Field(min_length=64, max_length=64)


class _NewTask(BaseModel):
    title: str = Field(min_length=1)
    role: str
    detail: str = ""
    depends_on: Optional[list[str]] = None


class _CorpusBindingBody(BaseModel):
    mode: str = "none"
    corpus_id: Optional[str] = None
    source_root: Optional[str] = None


class _BootstrapBody(BaseModel):
    corpus_id: str
    source_root: Optional[str] = None


class _GovernanceSettingsBody(BaseModel):
    mode: Optional[GovernanceMode] = None
    phase: Optional[GovernancePhase] = None
    human_code_approval: Optional[HumanCodeApproval] = None
    max_review_rounds: Optional[int] = None
    # F117: showstopper toggle + Progress Monitor thresholds.
    block_on_problems: Optional[bool] = None
    monitor: Optional[dict] = None


class _ResolveSignalBody(BaseModel):
    action: str
    suggestion_id: Optional[str] = None
    correction_text: Optional[str] = None


# F118 Director.
class _DirectorCreateBody(BaseModel):
    name: str
    agent: Optional[dict] = None
    project_ids: Optional[list[str]] = None


class _DirectorUpdateBody(BaseModel):
    name: Optional[str] = None
    agent: Optional[dict] = None
    project_ids: Optional[list[str]] = None


class _GovernanceApprovalBody(BaseModel):
    feedback: str = ""
    actor: str = "user"


class _GovernanceAcceptBody(BaseModel):
    confirm: bool = False


class _GovernanceExportTaskBody(BaseModel):
    target_path: str
    title: Optional[str] = None


def _coding_phase(proj_obj: Any, store: LedgerStore) -> str:
    """F141 WS-I (+ backfill): "north_star" while a project builds toward its
    initial North Star, "steering" once that North Star is MET — i.e. the project
    has reached ``done``. Only then does the Current Focus panel appear (it's the
    post-completion steering control); until then the frontend shows "Building
    toward <North Star>". The durable ``north_star_met_at`` marker is stamped
    forward at ``done`` (new) / North-Star accept (existing). Backfill for projects
    predating the marker so they aren't wrongly stuck hiding the panel:
      - an imported (``existing``) project ships a real foundation, so once its
        North Star is set it is in the steering phase;
      - a project already marked ``done`` is in the steering phase.
    A new project still building toward its North Star (PRs merging, not yet done)
    stays "north_star" — the Current Focus panel does not appear until completion.
    ``store`` is retained for signature stability (no longer read)."""
    if getattr(proj_obj, "phase", "north_star") == "steering":
        return "steering"
    if getattr(proj_obj, "north_star", "") and getattr(proj_obj, "target", "new") == "existing":
        return "steering"
    if str(getattr(proj_obj, "status", "")) == "done":
        return "steering"
    return "north_star"


def _project_out(store: LedgerStore) -> dict[str, Any]:
    from errorta_project_grounding.corpus_binding import binding_status, load_binding

    proj_obj = store.get_project()
    project = proj_obj.to_dict()
    # F141 WS-I: the computed steering-phase flag ("north_star" | "steering")
    # the frontend gates the Current Focus panel on.
    project["phase"] = _coding_phase(proj_obj, store)
    project["grounding"] = binding_status(load_binding(store)).to_dict()
    # F105: surface the (validated, stored) delivery root and the computed planned
    # delivery directory so the UI can show where a greenfield project will land.
    # Existing-target projects deliver by merging into their repo — no planned dir.
    if proj_obj.target == "existing":
        project["planned_delivery_dir"] = None
    else:
        from errorta_council.coding.deliverable import deliverable_dir
        project["planned_delivery_dir"] = str(
            deliverable_dir(store.project_id, proj_obj.delivery_root))
    # F101 S4: surface the latest runtime test verdict per (profile, kind) in the
    # F093 completion projection, head-bound so a stale pass reads as not-fresh.
    try:
        from errorta_council.coding.runtime import (
            RuntimeProfileStore,
            latest_runtime_evidence,
        )
        rstore = RuntimeProfileStore.for_ledger(store)
        project["runtime_evidence"] = latest_runtime_evidence(
            rstore, current_head=_runtime_workspace_head(store.project_id))
    except Exception:  # never let evidence surfacing break the project read
        project["runtime_evidence"] = {
            "results": [], "any_fresh_pass": False, "current_head": ""}
    # F102 RC2: surface the accept/delivered marker so the frontend can gate the
    # GitHub publish buttons (delivered AND no open tasks). Best-effort.
    try:
        from errorta_council.coding.publish_gate import delivered_at, is_project_delivered
        project["delivered"] = is_project_delivered(store)
        project["delivered_at"] = delivered_at(store)
    except Exception:
        project["delivered"] = False
        project["delivered_at"] = None
    return project


def _project_store_or_404(project_id: str) -> LedgerStore:
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    return store


def _safe_governance_target_path(path: str) -> str:
    cleaned = path.replace("\\", "/").strip()
    if (
        not cleaned
        or cleaned.startswith("/")
        or cleaned.startswith("~")
        or "\x00" in cleaned
        or any(part in {"", ".", ".."} for part in cleaned.split("/"))
    ):
        raise HTTPException(status_code=422, detail="invalid target_path")
    return cleaned


def _apply_grounding_payload(
    store: LedgerStore,
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not payload:
        return None
    from pathlib import Path

    from errorta_project_grounding.bootstrap import start_project_bootstrap
    from errorta_project_grounding.corpus_binding import (
        CorpusBindingError,
        ProjectCorpusBinding,
        save_binding,
    )

    mode = str(payload.get("mode") or "none")
    corpus_id = payload.get("corpus_id")
    source_root = payload.get("source_root")
    if mode == "build_from_repo":
        proj = store.get_project()
        root = str(source_root or proj.repo_path or "")
        if not root:
            raise HTTPException(
                status_code=422,
                detail="build_from_repo requires source_root or repo_path",
            )
        # Reuse the existing merge-back target validation for repo/source roots.
        _validate_repo_path(root)
        job = start_project_bootstrap(store, corpus_id=str(corpus_id), source_root=Path(root))
        from errorta_project_grounding.corpus_binding import binding_status, load_binding

        return {
            "bootstrap": job.to_dict(),
            "binding": binding_status(load_binding(store)).to_dict(),
        }
    # A corpus's residency (local vs remote AIAR) is a property of the
    # deployment, not the editor. `build_from_project` ingests the team's own
    # code into whatever AIAR is configured; when a remote AIAR (e.g. example-host
    # over the SSH tunnel) is configured the corpus lives THERE, so the binding
    # must be `remote` or its health would be probed against a local manifest
    # that will never exist. Carry forward any prior adapter_source, else infer
    # from the configured remote AIAR — so a manual editor save never silently
    # downgrades a remote build_from_project corpus to local.
    from errorta_project_grounding.corpus_binding import load_binding
    adapter_source = "local"
    if mode in ("existing", "build_from_project"):
        prior = load_binding(store)
        if prior.mode == mode and prior.adapter_source == "remote":
            adapter_source = "remote"
        else:
            try:
                from errorta_project_grounding.remote_adapter import active_remote_adapter
                if active_remote_adapter() is not None:
                    adapter_source = "remote"
            except Exception:
                adapter_source = "local"
    try:
        binding = save_binding(
            store,
            ProjectCorpusBinding(
                project_id=store.project_id,
                mode=mode,
                corpus_id=corpus_id,
                source_root=source_root,
                adapter_source=adapter_source,
            ),
        )
    except CorpusBindingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"binding": binding.to_dict()}


@router.get("/grounding/corpora")
def list_grounding_corpora() -> dict[str, Any]:
    # F095: delegate to the single residency-aware corpus catalog so the Coding
    # Team grounding picker, the Knowledge -> Corpus panel, and the Council room
    # editor all list the SAME corpora (with normalized counts + a source marker).
    # Retained for backward compatibility; new callers use GET /corpora.
    from errorta_app.corpus_catalog import list_all_corpora

    return list_all_corpora()


def _validate_grounding_payload(payload: dict[str, Any] | None, *, repo_path: str | None) -> None:
    """F088: validate a grounding payload WITHOUT side effects, so an invalid
    payload is rejected (422) BEFORE any project state is written — no partial
    project left behind."""
    if not payload:
        return
    from errorta_project_grounding.corpus_binding import VALID_BINDING_MODES
    mode = str(payload.get("mode") or "none")
    if mode not in VALID_BINDING_MODES:
        raise HTTPException(status_code=422, detail=f"invalid grounding mode: {mode}")
    corpus_id = payload.get("corpus_id")
    if mode in ("existing", "build_from_repo"):
        if not corpus_id:
            raise HTTPException(status_code=422, detail=f"{mode} grounding requires corpus_id")
        from errorta_corpus import validate_corpus_name
        try:
            validate_corpus_name(str(corpus_id))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid corpus_id: {exc}") from exc
    if mode == "build_from_repo":
        root = str(payload.get("source_root") or repo_path or "")
        if not root:
            raise HTTPException(status_code=422,
                                detail=(
                                    "build_from_repo grounding requires "
                                    "source_root or repo_path"
                                ))
        _validate_repo_path(root)


@router.get("/projects")
def list_all_projects() -> dict[str, Any]:
    return {"projects": [_project_list_out(project) for project in list_projects()]}


def _project_list_out(project: dict[str, Any]) -> dict[str, Any]:
    project_id = str(project.get("id") or "")
    lifecycle_status = str(project.get("status") or "active")
    state: dict[str, Any] = {}
    running = False
    has_blocking_attention = False
    if project_id:
        try:
            store = LedgerStore(project_id)
            state = _reconcile_run_state(project_id, store)
            running = state.get("status") == "running" and _thread_alive(project_id)
            from errorta_council.coding import attention

            has_blocking_attention = any(
                sig.kind == "problem" and sig.blocking
                for sig in attention.list_open(project_id, store=store)
            )
        except LedgerError:
            state = {}
            running = False
            has_blocking_attention = False
    list_status, list_status_reason = derive_project_list_status(
        lifecycle_status=lifecycle_status,
        run_status=str(state.get("status") or ""),
        running=running,
        stop_reason=str(state.get("stop_reason") or ""),
        has_blocking_attention=has_blocking_attention,
    )
    return {
        **project,
        "list_status": list_status,
        "list_status_reason": list_status_reason,
    }


@router.post("/projects")
def create_project(body: _NewProject, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote("/coding/projects")
    if body.target == "existing":
        _validate_repo_path(body.repo_path)
    # F105: validate the greenfield delivery root (write boundary) before any
    # state is written. Existing targets deliver into their repo -> store None.
    if body.target == "existing":
        delivery_root = None
    else:
        delivery_root = _validate_delivery_root(body.delivery_root)
    # F088: stage/validate grounding BEFORE writing project state, so a bad
    # grounding payload can't leave a half-created project behind.
    _validate_grounding_payload(body.grounding, repo_path=body.repo_path)
    try:
        store = LedgerStore(body.project_id)  # backstop: store re-validates the slug
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.create_project(
        north_star=body.north_star, definition_of_done=body.definition_of_done,
        target=body.target, repo_path=body.repo_path,
        delivery_root=delivery_root,
        work_request=str(body.work_request or "")[:20_000],
    )
    grounding_result = _apply_grounding_payload(store, body.grounding)
    out = {"project": _project_out(store)}
    if grounding_result:
        out.update(grounding_result)
    return out


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    try:
        return {"project": _project_out(LedgerStore(project_id))}
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}")
    store = LedgerStore(project_id)
    try:
        proj = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    if _thread_alive(project_id):
        raise HTTPException(status_code=409, detail="project run is still active")
    from errorta_council.coding.workspace import CodingWorkspace

    ws = CodingWorkspace(project_id, store)
    ws.set_target(proj.target)
    with store.lock:
        if _thread_alive(project_id):
            raise HTTPException(status_code=409, detail="project run is still active")
        ws.destroy()
        store.delete_project()
        _RUNS.pop(project_id, None)
    return {"deleted": True, "project_id": project_id}


@router.get("/projects/{project_id}/grounding/capabilities")
def get_grounding_capabilities(project_id: str) -> dict[str, Any]:
    try:
        LedgerStore(project_id).get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    # When a remote AIAR is configured, report ITS capabilities (honest
    # source:"remote", remote_ingest marker gating ingest) — not the local probe.
    from errorta_project_grounding.remote_adapter import active_remote_adapter
    remote = active_remote_adapter()
    if remote is not None:
        return {"capabilities": remote.capabilities().to_dict()}
    from errorta_project_grounding.capabilities import probe_aiar_grounding_capabilities

    return {"capabilities": probe_aiar_grounding_capabilities().to_dict()}


@router.get("/projects/{project_id}/grounding/retrieve")
def grounding_retrieve(project_id: str, q: str, request: Request, k: int = 6) -> dict[str, Any]:
    """F088 Slice 4: retrieve from the project's bound corpus (the runbook's
    ownership gate — proves the remote AIAR serves the corpus). Tauri-origin
    guarded since it triggers a remote query."""
    _require_tauri_origin(request)
    # If no remote AIAR is configured, retrieval would hit the LOCAL corpus —
    # which under remote residency is a data-plane violation. Fail closed.
    from errorta_project_grounding.remote_adapter import active_remote_adapter
    if active_remote_adapter() is None:
        refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/grounding/retrieve")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    # F088-10: surface the retrieval status alongside the hits so the UI can
    # tell "no corpus bound" / "served but no match" / "adapter unavailable"
    # apart instead of guessing from an empty list. ``hits`` is unchanged.
    from errorta_project_grounding.retrieval import retrieve_with_status
    hits, status = retrieve_with_status(store, query=q, top_k=max(1, min(int(k), 50)))
    return {"hits": [h.to_dict() for h in hits], "status": status}


@router.get("/projects/{project_id}/grounding/corpus-binding")
def get_corpus_binding(project_id: str) -> dict[str, Any]:
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    from errorta_project_grounding.corpus_binding import binding_status, load_binding

    return {"binding": binding_status(load_binding(store)).to_dict()}


@router.get("/projects/{project_id}/pm-working-memory")
def get_pm_working_memory(project_id: str) -> dict[str, Any]:
    """F099: redacted PM working-memory health. Returns refs/status only, never
    the raw PM working-memory document."""
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    from errorta_project_grounding.context_packets import ensure_pm_working_memory
    from errorta_project_grounding.pm_working_memory import pm_working_memory_status

    ensure_pm_working_memory(store)
    return {"pm_working_memory": pm_working_memory_status(store)}


@router.get("/projects/{project_id}/governance")
def get_governance(project_id: str) -> dict[str, Any]:
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore
    from errorta_council.coding.governance_status import governance_status
    from errorta_council.coding.runner import members_by_coding_role

    governance = GovernanceStore.for_ledger(store)
    # F100-01: fold the plain-language governance status (stage + status +
    # stepper) into the existing summary the UI already polls — additive, pure
    # projection. The roster comes from the run the project was started with
    # (persisted run_config), so the actor labels survive outside a live run.
    members = [m for m in (store.get_run_config().get("members") or [])
               if isinstance(m, dict)]
    by_role = members_by_coding_role(members)
    run_state = store.get_run_state()
    run_active = run_state.get("status") == "running" and _thread_alive(project_id)
    status = governance_status(store, by_role, run_active=run_active)
    return {"governance": governance.summary(include_body=False), "status": status}


@router.put("/projects/{project_id}/governance/settings")
def put_governance_settings(
    project_id: str,
    body: _GovernanceSettingsBody,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/governance/settings")
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceState, GovernanceStore

    governance = GovernanceStore.for_ledger(store)
    current = governance.load_state().to_dict()
    if body.mode is not None:
        current["mode"] = body.mode
    if body.phase is not None:
        current["phase"] = body.phase
    elif body.mode is not None and body.mode != "off" and current.get("phase") == "idle":
        current["phase"] = "brainstorming"
    elif body.mode == "off":
        current["phase"] = "idle"
    if body.human_code_approval is not None:
        current["human_code_approval"] = body.human_code_approval
    if body.max_review_rounds is not None:
        current["max_review_rounds"] = body.max_review_rounds
    if body.block_on_problems is not None:
        current["block_on_problems"] = bool(body.block_on_problems)
    if body.monitor is not None:
        current["monitor"] = body.monitor
    saved = governance.save_state(GovernanceState.from_dict(current))
    return {"state": saved.to_dict(), "governance": governance.summary(include_body=False)}


# --- F117 attention signals (Problems + Alerts) ----------------------------
@router.get("/projects/{project_id}/attention")
def list_attention(
    project_id: str, state: Optional[str] = None, kind: Optional[str] = None,
) -> dict[str, Any]:
    store = _project_store_or_404(project_id)
    from errorta_council.coding import attention
    from errorta_council.coding.governance import GovernanceStore

    signals = attention.list_all(project_id, state=state, kind=kind, store=store)
    phase = GovernanceStore.for_ledger(store).load_state().phase
    return {
        "signals": [s.to_dict() for s in signals],
        "blocks_stage": attention.blocks_stage(project_id, phase, store=store),
    }


def _parse_resolve_body(raw: bytes) -> _ResolveSignalBody:
    """Parse the resolve body, tolerating a double-JSON-encoded string body.

    The resolve action is a user-facing button click; it must not hard-fail
    with a 422 just because the body arrived JSON-encoded one layer too deep
    (observed from the desktop webview: the JSON object is sent wrapped in an
    extra JSON-string layer, which a strict ``body: _ResolveSignalBody`` param
    rejects). Unwrap at most one extra layer, then validate.
    """
    import json

    try:
        data: Any = json.loads(raw or b"{}")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="resolve body must be JSON") from exc
    if isinstance(data, str):
        # Double-encoded: a JSON string whose contents are themselves JSON.
        try:
            data = json.loads(data)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=422, detail="resolve body must be a JSON object",
            ) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="resolve body must be a JSON object")
    try:
        return _ResolveSignalBody(**data)
    except Exception as exc:  # pydantic ValidationError -> 422, not 500
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/projects/{project_id}/attention/{signal_id}/resolve")
async def resolve_attention(
    project_id: str, signal_id: str, request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/attention/{signal_id}/resolve")
    body = _parse_resolve_body(await request.body())
    store = _project_store_or_404(project_id)
    from errorta_council.coding import attention

    try:
        signal, created_task_id = attention.resolve(
            project_id, signal_id, body.action,
            suggestion_id=body.suggestion_id, correction_text=body.correction_text,
            store=store,
        )
    except attention.AttentionError as exc:
        msg = str(exc)
        code = 404 if "unknown signal" in msg else 409
        raise HTTPException(status_code=code, detail=msg) from exc
    return {"signal": signal.to_dict(), "created_task_id": created_task_id}


# --- F118 Director (cross-project supervisor) ------------------------------
def _director_error(exc: Exception) -> HTTPException:
    msg = str(exc)
    if "unknown director" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "already supervised" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=422, detail=msg)


@router.get("/directors")
def list_directors(request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding import director
    return {"directors": [d.to_dict() for d in director.list_directors()]}


@router.post("/directors")
def create_director(body: _DirectorCreateBody, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote("/coding/directors")
    from errorta_council.coding import director
    try:
        d = director.create_director(
            name=body.name, agent=body.agent, project_ids=body.project_ids)
    except director.DirectorError as exc:
        raise _director_error(exc) from exc
    return {"director": d.to_dict()}


@router.get("/directors/{director_id}")
def get_director(director_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding import director
    try:
        d = director.get_director(director_id)
    except director.DirectorError as exc:
        raise _director_error(exc) from exc
    if d is None:
        raise HTTPException(status_code=404, detail="director_not_found")
    return {"director": d.to_dict(),
            "attention": director.aggregate_attention(director_id)}


@router.put("/directors/{director_id}")
def update_director(
    director_id: str, body: _DirectorUpdateBody, request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/directors/{director_id}")
    from errorta_council.coding import director
    try:
        d = director.update_director(
            director_id, name=body.name, agent=body.agent,
            project_ids=body.project_ids)
    except director.DirectorError as exc:
        raise _director_error(exc) from exc
    return {"director": d.to_dict()}


@router.delete("/directors/{director_id}")
def delete_director(director_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/directors/{director_id}")
    from errorta_council.coding import director
    try:
        removed = director.delete_director(director_id)
    except director.DirectorError as exc:
        raise _director_error(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="director_not_found")
    return {"deleted": director_id}


@router.get("/directors/{director_id}/attention")
def director_attention(director_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding import director
    try:
        return {"groups": director.aggregate_attention(director_id)}
    except director.DirectorError as exc:
        raise _director_error(exc) from exc


@router.get("/directors/{director_id}/inbox")
def director_inbox(director_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding import director
    try:
        return {"items": director.inbox(director_id)}
    except director.DirectorError as exc:
        raise _director_error(exc) from exc


@router.get("/projects/{project_id}/governance/artifacts")
def get_governance_artifacts(project_id: str, include_body: bool = False) -> dict[str, Any]:
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore

    artifacts = [a.to_dict() for a in GovernanceStore.for_ledger(store).list_artifacts()]
    if not include_body:
        for artifact in artifacts:
            artifact.pop("body_markdown", None)
            artifact.pop("body_json", None)
    return {"artifacts": artifacts}


@router.get("/projects/{project_id}/governance/artifacts/{artifact_id}")
def get_governance_artifact(project_id: str, artifact_id: str) -> dict[str, Any]:
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore

    artifact = GovernanceStore.for_ledger(store).get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {"artifact": artifact.to_dict()}


@router.post("/projects/{project_id}/governance/artifacts/{artifact_id}/accept")
def accept_governance_artifact(
    project_id: str,
    artifact_id: str,
    body: _GovernanceAcceptBody,
    request: Request,
) -> dict[str, Any]:
    """F100-02 D: the human "good enough, move on" override.

    Tauri-origin guarded + explicit confirm — it overrides the AI review gate.
    Force-approves the EXACT viewed artifact and advances governance to the next
    stage. 400 if not confirmed or governance is off; 409 if the artifact is
    stale / already superseded.
    """
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/governance/artifacts/{artifact_id}/accept"
    )
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm required")
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceError, GovernanceStore

    governance = GovernanceStore.for_ledger(store)
    if governance.load_state().mode == "off":
        raise HTTPException(status_code=400, detail="governance is off")
    try:
        artifact = governance.force_accept_artifact(artifact_id, by="human")
    except GovernanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "artifact": artifact.to_dict(),
        "state": governance.load_state().to_dict(),
        "governance": governance.summary(include_body=False),
    }


@router.get("/projects/{project_id}/governance/reviews")
def get_governance_reviews(project_id: str, artifact_id: Optional[str] = None) -> dict[str, Any]:
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore

    return {
        "reviews": [
            r.to_dict()
            for r in GovernanceStore.for_ledger(store).list_reviews(artifact_id=artifact_id)
        ]
    }


@router.get("/projects/{project_id}/governance/approvals")
def get_governance_approvals(project_id: str) -> dict[str, Any]:
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore

    return {"approvals": [a.to_dict() for a in GovernanceStore.for_ledger(store).list_approvals()]}


@router.post("/projects/{project_id}/governance/approvals/{approval_id}/approve")
def approve_governance_approval(
    project_id: str,
    approval_id: str,
    body: _GovernanceApprovalBody,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/governance/approvals/{approval_id}/approve"
    )
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceError, GovernanceStore

    governance = GovernanceStore.for_ledger(store)
    try:
        approval = governance.resolve_approval(
            approval_id,
            approved=True,
            resolved_by=body.actor or "user",
            actor_role=body.actor or "user",
            feedback=body.feedback,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"approval": approval.to_dict(), "governance": governance.summary(include_body=False)}


@router.post("/projects/{project_id}/governance/approvals/{approval_id}/reject")
def reject_governance_approval(
    project_id: str,
    approval_id: str,
    body: _GovernanceApprovalBody,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/governance/approvals/{approval_id}/reject"
    )
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceError, GovernanceStore

    governance = GovernanceStore.for_ledger(store)
    try:
        approval = governance.resolve_approval(
            approval_id,
            approved=False,
            resolved_by=body.actor or "user",
            actor_role=body.actor or "user",
            feedback=body.feedback,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"approval": approval.to_dict(), "governance": governance.summary(include_body=False)}


@router.post("/projects/{project_id}/governance/artifacts/{artifact_id}/export-task")
def export_governance_artifact_task(
    project_id: str,
    artifact_id: str,
    body: _GovernanceExportTaskBody,
    request: Request,
) -> dict[str, Any]:
    """Create a normal DEV task to write an approved governance artifact into
    the repo. This keeps durable planning documents explicit rather than letting
    the PM mutate the user's repo during the approval flow."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/governance/artifacts/{artifact_id}/export-task"
    )
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore

    governance = GovernanceStore.for_ledger(store)
    artifact = governance.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.state != "approved":
        raise HTTPException(status_code=409, detail="artifact must be approved before export")
    target_path = _safe_governance_target_path(body.target_path)
    task = store.add_task(
        title=body.title or f"write {artifact.artifact_kind}: {artifact.title}",
        role="dev",
        detail=(
            f"Write approved governance artifact {artifact.artifact_id} to "
            f"{target_path}.\n\n{artifact.body_markdown}"
        ),
        source_spec_artifact_id=artifact.artifact_id if artifact.artifact_kind == "spec" else None,
        source_plan_artifact_id=(
            artifact.artifact_id if artifact.artifact_kind == "implementation_plan" else None
        ),
        governance_required=governance.load_state().mode == "strict",
    )
    return {"task": task.to_dict()}


@router.put("/projects/{project_id}/grounding/corpus-binding")
def put_corpus_binding(
    project_id: str,
    body: _CorpusBindingBody,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/grounding/corpus-binding")
    store = LedgerStore(project_id)
    try:
        proj = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    # F088-10: the PUT path must validate too (it previously skipped this, so an
    # empty corpus_id reached start_project_bootstrap and built a corpus named
    # "None"). Fail closed with 422 BEFORE any bootstrap/state write.
    payload = body.model_dump()
    _validate_grounding_payload(payload, repo_path=proj.repo_path)
    result = _apply_grounding_payload(store, payload)
    return result or {"binding": get_corpus_binding(project_id)["binding"]}


@router.post("/projects/{project_id}/grounding/bootstrap")
def post_grounding_bootstrap(
    project_id: str,
    body: _BootstrapBody,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/grounding/bootstrap")
    from pathlib import Path

    from errorta_project_grounding.bootstrap import start_project_bootstrap

    store = LedgerStore(project_id)
    try:
        proj = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    root = body.source_root or proj.repo_path
    if not root:
        raise HTTPException(status_code=422, detail="bootstrap requires source_root or repo_path")
    _validate_repo_path(root)
    return {
        "job": start_project_bootstrap(
            store,
            corpus_id=body.corpus_id,
            source_root=Path(root),
        ).to_dict()
    }


@router.get("/projects/{project_id}/grounding/bootstrap/{job_id}")
def get_grounding_bootstrap_job(project_id: str, job_id: str) -> dict[str, Any]:
    from errorta_project_grounding.bootstrap import load_job

    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    job = load_job(store, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="bootstrap job not found")
    return {"job": job.to_dict()}


class _MemorySyncBody(BaseModel):
    mode: str = "from_ledger"  # "from_ledger" | "from_repo"


@router.post("/projects/{project_id}/grounding/memory/sync")
def post_grounding_memory_sync(project_id: str, request: Request) -> dict[str, Any]:
    """F088-06: re-project the F087 ledger into the project memory store
    (idempotent). Tauri-origin + residency guarded (no local writes in remote
    mode)."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/grounding/memory/sync")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    from errorta_project_grounding.update_pipeline import sync_from_ledger
    return {"counts": sync_from_ledger(store, workspace=_optional_workspace(project_id))}


@router.post("/projects/{project_id}/grounding/memory/rebuild")
def post_grounding_memory_rebuild(
    project_id: str,
    body: _MemorySyncBody,
    request: Request,
) -> dict[str, Any]:
    """F088-06 repair path: rebuild the memory index from the ledger
    (``from_ledger``) or re-ingest merged master files into the bound corpus
    (``from_repo``). Guarded."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/grounding/memory/rebuild")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    from errorta_project_grounding.update_pipeline import (
        rebuild_from_ledger,
        rebuild_from_repo,
    )
    if body.mode == "from_repo":
        ws = _workspace(project_id)
        return {"result": rebuild_from_repo(store, ws)}
    return {"counts": rebuild_from_ledger(store, workspace=_optional_workspace(project_id))}


class _BuildFromProjectBody(BaseModel):
    corpus_id: str | None = None


@router.post("/projects/{project_id}/grounding/build-from-project")
def post_grounding_build_from_project(
    project_id: str, body: _BuildFromProjectBody, request: Request,
) -> dict[str, Any]:
    """F088-03: build a project corpus from the project's OWN coding workspace
    (the team's merged ``master`` tree) — the trusted-internal source the external
    ``build_from_repo`` path can't reach (its repo-path guard rejects the
    workspace). Creates the corpus on the (remote) AIAR, ingests the master tree,
    binds it ``build_from_project``, and the PM/dev retrieval then pulls the
    project's own code. Tauri-origin + residency guarded; idempotent (re-running
    re-ingests)."""
    import re
    import shutil
    import tempfile
    from dataclasses import replace
    from pathlib import Path

    from errorta_corpus import validate_corpus_name
    from errorta_project_grounding.bootstrap import (
        CODE_EXTENSIONS,
        start_project_bootstrap,
    )
    from errorta_project_grounding.corpus_binding import (
        default_binding,
        load_binding,
        save_binding,
    )

    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/grounding/build-from-project")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    ws = _workspace(project_id)  # 409 if the project has no worktree yet

    corpus_id = (body.corpus_id or "").strip()
    if not corpus_id:
        slug = re.sub(r"[^a-z0-9-]+", "-", project_id.lower()).strip("-") or "project"
        corpus_id = f"project-{slug}"
    try:
        validate_corpus_name(corpus_id)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid corpus id: {exc}") from exc

    # Export master to a temp dir (trusted internal source — NOT a user path, so
    # the external repo-path guard is correctly bypassed) and bootstrap from it.
    tmp = tempfile.mkdtemp(prefix=f"f088-build-{project_id}-")
    try:
        try:
            ws.export(tmp)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail=f"could not export project workspace: {exc}") from exc
        job = start_project_bootstrap(
            store, corpus_id=corpus_id, source_root=Path(tmp),
            extra_extensions=CODE_EXTENSIONS)
        no_eligible = not (job.plan and job.plan.included)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if no_eligible or job.status != "done":
        # The build couldn't index anything — most often because the team hasn't
        # merged any source to `master` yet (a corpus can't index code that
        # doesn't exist there; the remote path reports "failed", the local path
        # reports "done" with an empty plan). Reset the binding to `none` rather
        # than leaving it pointing at a corpus that was never created (which
        # surfaces as a confusing 404), and tell the operator clearly why.
        save_binding(store, default_binding(project_id))
        if no_eligible:
            detail = ("nothing to index yet — no source files are merged to the "
                      "project's master branch. Build a corpus after at least one "
                      "PR merges.")
        else:
            detail = "corpus build failed: " + "; ".join(job.errors or ["unknown error"])
        raise HTTPException(status_code=409, detail=detail)

    # start_project_bootstrap binds `build_from_repo` with the (now-deleted) temp
    # source_root; re-bind as `build_from_project` so the merge-refresh + UI treat
    # it as the project's own corpus (source = the workspace, no external path).
    b = load_binding(store)
    save_binding(store, replace(b, mode="build_from_project", source_root=None))
    return {"job": job.to_dict(), "binding": get_corpus_binding(project_id)["binding"]}


def _optional_workspace(project_id: str):
    """The project worktree if it exists, else None (sync still indexes the
    ledger-backed records without a worktree)."""
    try:
        return _workspace(project_id)
    except HTTPException:
        return None


@router.get("/projects/{project_id}/backlog")
def get_backlog(project_id: str) -> dict[str, Any]:
    return {"tasks": [t.to_dict() for t in LedgerStore(project_id).list_tasks()]}


@router.get("/model-learning")
def get_model_learning() -> dict[str, Any]:
    """F135: global, cross-project PM learning digest (read-only, fail-open).

    Not scoped by project — the performance corpus is shared across every project
    and PM, so a new project starts from every prior project's attempts.
    """
    from errorta_council.coding.performance_corpus import learning_digest

    try:
        return {"learning": learning_digest()}
    except Exception:  # observability must never 5xx a panel
        from errorta_council.coding.model_selector import (
            DEMOTION_ACCEPTED_RATE,
            MIN_ATTEMPTS_FOR_SIGNAL,
            PREFERRED_ACCEPTED_RATE,
        )

        return {"learning": {
            "summary": {"total_attempts": 0, "distinct_routes": 0,
                        "window_days": 90, "generated_at": "",
                        "corpus_available": False},
            "thresholds": {"min_attempts": MIN_ATTEMPTS_FOR_SIGNAL,
                           "demotion_rate": DEMOTION_ACCEPTED_RATE,
                           "preferred_rate": PREFERRED_ACCEPTED_RATE},
            "routes": [],
        }}


@router.get("/projects/{project_id}/model-usage")
def get_model_usage(project_id: str) -> dict[str, Any]:
    """F135: per-project model-assignment usage rollup (read-only, fail-open).

    ``multi_members`` is derived from the run config (a configured multi-model
    member appears even with zero assignments); the per-route/tier/source
    distribution is aggregated over tasks that carry a ``model_assignment``.
    """
    from collections import defaultdict

    store = _project_store_or_404(project_id)
    try:
        members = store.get_run_config().get("members") or []
        tasks = store.list_tasks()
    except Exception:
        return {"usage": {"multi_members": [], "single_members": []}}

    agg: dict[str, dict[tuple, dict[str, int]]] = defaultdict(dict)
    escalations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        ma = getattr(task, "model_assignment", None)
        if not isinstance(ma, dict) or not ma.get("route_id"):
            continue
        member_id = str(ma.get("member_id") or "")
        esc = int(ma.get("escalation_count") or 0)
        key = (ma.get("route_id"), ma.get("difficulty_tier"), ma.get("source"))
        cell = agg[member_id].setdefault(key, {"count": 0, "max_escalation": 0})
        cell["count"] += 1
        cell["max_escalation"] = max(cell["max_escalation"], esc)
        if esc > 0:
            escalations[member_id].append({
                "task_id": ma.get("task_id"),
                "route_id": ma.get("route_id"),
                "escalation_count": esc,
                "attempted_route_ids": list(ma.get("attempted_route_ids") or []),
            })

    multi_members: list[dict[str, Any]] = []
    single_members: list[dict[str, Any]] = []
    for member in members:
        member_id = str(member.get("id") or "")
        mode = str(member.get("model_mode") or "single")
        if mode == "multi":
            assignments = [
                {"route_id": key[0], "difficulty_tier": key[1], "source": key[2],
                 "count": cell["count"], "max_escalation": cell["max_escalation"]}
                for key, cell in agg.get(member_id, {}).items()
            ]
            assignments.sort(key=lambda a: (a["difficulty_tier"] or "", a["route_id"] or ""))
            multi_members.append({
                "member_id": member_id,
                "role": str(member.get("role") or ""),
                "model_mode": "multi",
                "pool": [str(route) for route in (member.get("model_pool") or [])],
                "assignments": assignments,
                "escalations": escalations.get(member_id, []),
            })
        else:
            single_members.append({
                "member_id": member_id,
                "route_id": str(member.get("gateway_route_id") or ""),
            })
    return {"usage": {"multi_members": multi_members, "single_members": single_members}}


@router.post("/projects/{project_id}/tasks")
def add_task(project_id: str, body: _NewTask, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/tasks")
    try:
        t = LedgerStore(project_id).add_task(
            title=body.title, role=body.role, detail=body.detail,
            depends_on=body.depends_on,
        )
    except LedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": t.to_dict()}


@router.patch("/projects/{project_id}/tasks/{task_id}")
def patch_task(
    project_id: str,
    task_id: str,
    patch: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/tasks/{task_id}")
    try:
        return {"task": LedgerStore(project_id).update_task(task_id, **patch).to_dict()}
    except LedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/decisions")
def get_decisions(project_id: str) -> dict[str, Any]:
    return {"decisions": LedgerStore(project_id).list_decisions()}


@router.get("/projects/{project_id}/team-log")
def get_team_log(project_id: str) -> dict[str, Any]:
    """A human-readable, chronological narrative of what the team did
    (North Star → specs/plans/approvals → tasks → dev/review/test/merge)."""
    from errorta_council.coding.team_log import build_team_log
    store = _project_store_or_404(project_id)
    return {"entries": build_team_log(store)}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _looks_like_progress_question(message: str) -> bool:
    text = message.lower()
    direct = (
        "how close",
        "how far",
        "what's left",
        "whats left",
        "where are we",
        "status",
        "progress",
        "remaining",
        "left to do",
        "done yet",
        "finished yet",
    )
    if any(marker in text for marker in direct):
        return True
    return "done" in text and ("?" in text or "are we" in text or "when" in text)


def _task_titles(tasks: list[Any], state: str, *, limit: int = 3) -> list[str]:
    return [str(t.title) for t in tasks if t.state == state][:limit]


def _pm_reply_for_message(store: LedgerStore, message: str) -> dict[str, Any]:
    tasks = [t for t in store.list_tasks() if t.state != "dropped"]
    counts = {
        "todo": sum(1 for t in tasks if t.state == "todo"),
        "doing": sum(1 for t in tasks if t.state == "doing"),
        "blocked": sum(1 for t in tasks if t.state == "blocked"),
        "done": sum(1 for t in tasks if t.state == "done"),
    }
    total = sum(counts.values())
    percent = round((counts["done"] / total) * 100) if total else 0
    progress = {
        "total": total,
        "done": counts["done"],
        "doing": counts["doing"],
        "todo": counts["todo"],
        "blocked": counts["blocked"],
        "percent": percent,
    }
    source_ids = [t.task_id for t in tasks if t.state in {"todo", "doing", "blocked", "done"}]
    if total == 0:
        body = (
            "Got it — the PM will read and act on this on its next turn. "
            "No tasks have been planned yet, so I can't calculate completion from the board."
        )
        kind = "queued_directive"
    else:
        board = (
            f"{percent}% done by task count: {_plural(counts['done'], 'done task')}, "
            f"{_plural(counts['doing'], 'active task')}, {_plural(counts['todo'], 'todo task')}, "
            f"{_plural(counts['blocked'], 'blocked task')} out of {_plural(total, 'planned task')}."
        )
        details: list[str] = []
        doing = _task_titles(tasks, "doing")
        blocked = _task_titles(tasks, "blocked")
        todo = _task_titles(tasks, "todo")
        if doing:
            details.append(f"Doing: {', '.join(doing)}.")
        if blocked:
            details.append(f"Blocked: {', '.join(blocked)}.")
        if todo:
            details.append(f"Next: {', '.join(todo)}.")
        if _looks_like_progress_question(message):
            body = f"We're {board}"
            kind = "progress_summary"
        else:
            body = (
                "Message sent to the PM — it takes priority and will be read and "
                f"acted on on the PM's next turn. Current board: {board}"
            )
            kind = "queued_directive"
        if details:
            body = f"{body} {' '.join(details)}"
    return {
        "role": "pm",
        "kind": kind,
        "message": body,
        "progress": progress,
        "source": "ledger.backlog.task_states",
        "source_ids": source_ids,
        "at": _now(),
    }


@router.post("/projects/{project_id}/interject")
def interject(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """F087-07-E: record an authoritative user directive the PM consumes on its
    next plan turn (the F049 pinned contract), not a normal backlog task."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/interject")
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty interjection")
    artifact_id = body.get("artifact_id")
    artifact_id = str(artifact_id).strip() if artifact_id else None
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    pm_reply = _pm_reply_for_message(store, message)
    # F145: a directive can also change settings (same access as the AI Wizard).
    # Interpret it into control-actions and apply them (grounded, reviewable as a
    # PM Change). Best-effort: the authoritative run directive is recorded either
    # way, so an interpretation failure never blocks steering the run.
    applied: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    run_started = False
    try:
        from errorta_council.coding import control_actions as _ca
        from errorta_council.coding import pm_reference as _pr

        complete = _pm_complete(store)
        if complete is not None:
            interpreted = _ca.interpret_directive(
                message, context=_pr.build_pm_reference_context(project_id, store=store),
                complete=complete)
            applied, refusals, run_started = _apply_pm_control_actions(
                project_id, store, interpreted.get("actions") or [],
                available=_pr.list_available_routes())
    except Exception:  # noqa: BLE001 — directive recording must not fail on this
        applied, refusals, run_started = [], [], False
    return {
        "ok": True,
        "interjection": store.record_interjection(
            message, pm_reply=pm_reply, artifact_id=artifact_id),
        "applied": applied, "refusals": refusals, "run_started": run_started,
    }


# --- F141 WS-J: synchronous PM chat ("pull the PM into your office") --------

def _resolve_pm_member(store: LedgerStore) -> dict[str, Any] | None:
    """The PM member from the project's persisted run config, or None if the team
    isn't configured yet."""
    from errorta_council.coding.topology import PM, coding_role_of
    members = store.get_run_config().get("members") or []
    for m in members:
        if (isinstance(m, dict) and m.get("enabled", True)
                and coding_role_of(m) == PM):
            return m
    return None


def _build_pm_ask_prompt(store: LedgerStore, project: Any, message: str,
                         thread: list[dict[str, Any]], *, context: str = "") -> str:
    from errorta_council.coding.ledger import format_focus_lines
    lines = []
    if context:
        # F145: give "ask PM" the same capability manual + live state as the AI
        # Wizard, so it knows every setting/option and can act on a request.
        lines.append(context)
        lines.append("")
    lines += [
        "You are the PM of an autonomous coding team. The user has pulled you "
        "aside. Answer their question directly and concisely, grounded in the "
        "project state below and the operator's manual + LIVE STATE above.",
        "",
        "When the user asks you to DO something you can do — create a task, start "
        "the team, or change a setting — respond with a JSON object "
        "{\"reply\": \"<one sentence on what you did>\", \"actions\": [ ... ]}. "
        "Each change is applied and shown to the user to accept or revert. Use "
        "ONLY these action types (never invent a REST call or other shape):",
        "  {\"type\": \"create_task\", \"title\": \"...\", \"detail\": \"...\", "
        "\"role\": \"dev|reviewer|tester|pm\"}",
        "  {\"type\": \"start_run\"}   — kick off the team on the backlog",
        "  {\"type\": \"assign_models\", \"role_routes\": {\"dev\": \"<model "
        "name from LIVE STATE>\"}}",
        "  {\"type\": \"set_autonomy\", \"knobs\": { ... }}",
        "  {\"type\": \"set_governance\", \"fields\": { ... }}",
        "If the user asks you to fix a bug or add something, create_task (and "
        "start_run if they want it worked now). For a plain question needing no "
        "action, just answer in prose — no JSON.",
        "",
        f"North Star: {project.north_star or '(none set yet)'}",
    ]
    if project.definition_of_done:
        lines.append(f"Definition of done: {project.definition_of_done}")
    try:
        focuses = store.active_focuses()
    except Exception:
        focuses = []
    if focuses:
        lines.append("Current Focus:")
        lines.extend("  " + line for line in format_focus_lines(focuses))
    tasks = [t for t in store.list_tasks() if t.state != "dropped"]
    counts = {s: sum(1 for t in tasks if t.state == s)
              for s in ("todo", "doing", "blocked", "done")}
    lines.append(
        f"Task board: {counts['done']} done, {counts['doing']} doing, "
        f"{counts['todo']} todo, {counts['blocked']} blocked "
        f"(of {len(tasks)} planned).")
    try:
        recent = store.list_decisions()[-5:]
        if recent:
            lines.append("Recent decisions:")
            for d in recent:
                lines.append(f"  - {d.get('title', '')}: {d.get('choice', '')}")
    except Exception:
        pass
    if thread:
        lines.append("")
        lines.append("Conversation so far:")
        for turn in thread[-12:]:
            who = "You" if turn.get("role") == "user" else "PM"
            lines.append(f"{who}: {turn.get('message', '')}")
    lines.append("")
    lines.append(f"User: {message}")
    lines.append("PM:")
    return "\n".join(lines)


def _apply_pm_control_actions(
    project_id: str, store: LedgerStore, actions: list[dict[str, Any]],
    *, available: list[dict[str, Any]], surface: str = "pop",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Apply a PM's control-actions. Config mutations (create_task / assign_models /
    set_autonomy / set_governance) go through ``control_actions.apply_actions`` as
    reviewable PM Changes; the one route-level action — ``start_run`` — kicks off the
    team here (the run machinery lives in this module). Returns
    ``(applied_change_dicts, refusals, run_started)``. A start failure is a refusal,
    never a 500."""
    from errorta_council.coding import control_actions as _ca
    config_actions, wants_start = _ca.split_run_actions(actions)
    applied, refusals = _ca.apply_actions(
        store, config_actions, available=available, surface=surface)
    run_started = False
    if wants_start:
        try:
            # continue_=True recovers the saved team from run_config and re-drives
            # the ledger — the right "start the team" semantics for an existing team.
            _start_run(project_id, {}, continue_=True)
            run_started = True
        except HTTPException as exc:
            refusals.append({"code": "start_failed", "reason": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 — a start failure is a refusal, not a 500
            refusals.append({"code": "start_failed", "reason": str(exc)})
    return [c.to_dict() for c in applied], refusals, run_started


@router.post("/projects/{project_id}/pm-ask")
def pm_ask(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """F141 WS-J — a synchronous conversation with the PM. Unlike ``/interject``
    (an authoritative directive the PM reads on its next run turn), this returns
    the PM MODEL's own immediate reply and records a chat thread. It does not
    change project scope. Coexists with a live run (read-only + a bounded model
    call); if the PM model can't be reached, returns a clean retryable error."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/pm-ask")
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")
    thread_id = str(body.get("thread_id") or "main")
    store = LedgerStore(project_id)
    try:
        project = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")

    thread = store.list_pm_chat(thread_id=thread_id)
    pm_member = _resolve_pm_member(store)
    if pm_member is None:
        return {
            "reply": {
                "role": "pm", "kind": "unconfigured",
                "message": ("The team isn't set up yet, so I can't chat as the "
                            "PM. Configure a team and start a run once, then pull "
                            "me aside."),
                "at": _now(),
            },
            "thread_id": thread_id, "answered": False,
        }

    # Record the user turn before calling the model so the thread is durable even
    # if the model call fails.
    store.append_pm_chat(role="user", message=message, thread_id=thread_id)
    # F145: same access as the AI Wizard — the operator's manual + live state.
    from errorta_council.coding import control_actions as _ca
    from errorta_council.coding import pm_reference as _pr
    available = _pr.list_available_routes()
    context = _pr.build_pm_reference_context(project_id, store=store)
    prompt = _build_pm_ask_prompt(store, project, message, thread, context=context)

    try:
        m = dict(pm_member)
        tl = dict(m.get("turn_limits") or {})
        # Interactive chat: bound the wait tighter than a normal (up to 10 min)
        # turn. Guard the parse (a legacy/bad config value shouldn't 500 after the
        # user turn is already recorded — fall back to the 90s default).
        try:
            configured = int(tl.get("timeout_seconds", 90) or 90)
        except (TypeError, ValueError):
            configured = 90
        tl["timeout_seconds"] = min(configured, 120)
        m["turn_limits"] = tl
        from errorta_council.coding.runner import gateway_member_caller
        from errorta_council.gateway_local import LocalGateway
        text = gateway_member_caller(LocalGateway())(m, prompt).strip()
    except Exception:  # noqa: BLE001 — never surface a raw egress error
        return {
            "reply": {
                "role": "pm", "kind": "error",
                "message": ("The PM couldn't be reached just now — it may be "
                            "mid-turn on the run. Try again in a moment."),
                "at": _now(),
            },
            "thread_id": thread_id, "answered": False, "error": "pm_unreachable",
        }
    if not text:
        text = "(the PM had nothing to add)"
    # F145: if the PM chose to DO something — create a task, start the team, or
    # change a setting — apply it (grounded) and surface it as a reviewable PM
    # Change. Same agency as the AI Wizard.
    reply_text, actions = _ca.parse_pm_reply(text)
    applied, refusals, run_started = _apply_pm_control_actions(
        project_id, store, actions, available=available)
    if not reply_text:
        reply_text = "(the PM had nothing to add)"
    store.append_pm_chat(role="pm", message=reply_text, thread_id=thread_id)
    return {
        "reply": {"role": "pm", "kind": "chat", "message": reply_text, "at": _now()},
        "thread_id": thread_id, "answered": True,
        "applied": applied, "refusals": refusals, "run_started": run_started,
    }


@router.get("/projects/{project_id}/pm-chat")
def get_pm_chat(project_id: str, request: Request) -> dict[str, Any]:
    """F141 WS-J — the PM chat thread so the composer can render history."""
    _require_tauri_origin(request)
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    return {"thread": store.list_pm_chat(thread_id=str(
        request.query_params.get("thread_id") or "main"))}


# --- F145: the AI Wizard (conversational project setup) ---------------------

_WIZARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@router.get("/wizard/models")
def wizard_models(request: Request) -> dict[str, Any]:
    """The model routes available to power the AI Wizard (for the picker)."""
    _require_tauri_origin(request)
    from errorta_council.coding import pm_reference

    return {"routes": pm_reference.list_available_routes()}


@router.post("/wizard/start")
def wizard_start(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Begin an AI Wizard conversation with a user-picked model. 422 if the chosen
    route isn't in the live catalog (grounded-or-refuse)."""
    _require_tauri_origin(request)
    from errorta_council.coding import pm_reference, wizard

    route = str((body or {}).get("model_route") or "").strip()
    available = pm_reference.list_available_routes()
    ids = {r["route_id"] for r in available}
    if not route:
        raise HTTPException(status_code=400, detail="model_route required")
    if route not in ids:
        raise HTTPException(status_code=422, detail={
            "code": "model_unavailable", "route": route,
            "available": sorted(ids)})
    session = wizard.new_session(route)
    session.messages.append({"role": "pm", "text": wizard.OPENING})
    wizard._save(session)
    return {"session_id": session.session_id, "reply": wizard.OPENING,
            "available_routes": available}


@router.post("/wizard/{session_id}/message")
def wizard_message(session_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """One Wizard turn: the model replies and refines the runnable charter."""
    _require_tauri_origin(request)
    from errorta_council.coding import pm_reference, wizard

    session = wizard.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="wizard session not found")
    message = str((body or {}).get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")
    context = pm_reference.build_pm_reference_context(None)  # pre-project
    try:
        session = wizard.run_turn(session, message, context=context)
    except wizard.WizardError:
        return {"reply": ("I couldn't reach the model just now — try again in a "
                          "moment."), "ready": False, "charter": session.charter,
                "missing": session.missing, "error": "model_unreachable"}
    return {"reply": session.messages[-1]["text"], "ready": session.ready,
            "charter": session.charter, "missing": session.missing}


@router.post("/wizard/{session_id}/finalize")
def wizard_finalize(session_id: str, request: Request) -> dict[str, Any]:
    """The runnable-by-construction gate: return the charter or 409 with what's
    missing (never a guess)."""
    _require_tauri_origin(request)
    from errorta_council.coding import wizard

    session = wizard.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="wizard session not found")
    try:
        charter = wizard.finalize(session)
    except wizard.WizardError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "charter_incomplete", "reason": str(exc),
            "missing": session.missing})
    return {"charter": charter}


@router.post("/wizard/{session_id}/create")
def wizard_create(session_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Create-on-accept: create a fully-set-up, runnable project from the charter.

    Creates the project, seeds its first brainstorm from the transcript, applies a
    recipe-driven autonomy policy + team (grounded in available routes), marks
    run-setup confirmed when a team was assignable, and discards the ephemeral
    session. Auto-start of the run is a follow-up (F145 S5); the project is left
    ready to run."""
    _require_tauri_origin(request)
    from errorta_council.coding import pm_reference, recipes, wizard
    from errorta_council.coding.autonomy import (
        load_policy,
        policy_from_dict,
        policy_to_dict,
        save_policy,
    )
    from errorta_council.coding.governance import GovernanceStore
    from errorta_council.coding.workspace import CodingWorkspace

    session = wizard.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="wizard session not found")
    try:
        charter = wizard.finalize(session)
    except wizard.WizardError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "charter_incomplete", "reason": str(exc)})

    project_id = str((body or {}).get("project_id") or "").strip()
    if not _WIZARD_ID_RE.match(project_id) or project_id in (".", ".."):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_project_id",
            "reason": "letters, numbers, dot, underscore, hyphen (max 64)"})
    try:
        store = LedgerStore(project_id)
    except LedgerError:
        raise HTTPException(status_code=422, detail={"code": "invalid_project_id"})
    try:
        store.get_project()
        raise HTTPException(status_code=409, detail="project already exists")
    except ProjectNotFound:
        pass

    delivery_root = str((body or {}).get("delivery_root") or "").strip() or None
    recipe, autonomous = charter["team_recipe"], charter["autonomous"]
    members = recipes.resolve_team(recipe, pm_reference.list_available_routes())
    warnings: list[str] = []

    # All-or-nothing: if any setup step fails after create_project, remove the
    # half-created project so the id is reusable (the canonical create route
    # validates-then-writes; the wizard writes across several stores, so it cleans
    # up on failure instead).
    try:
        store.create_project(
            north_star=charter["north_star"],
            definition_of_done=charter["definition_of_done"],
            target="new", repo_path=None, delivery_root=delivery_root)
        CodingWorkspace(project_id, store).setup(target="new", repo_path=None)

        # Seed the first brainstorm from the wizard transcript so governance
        # doesn't re-interview the user.
        transcript = "\n\n".join(
            f"**{m.get('role', '?')}:** {m.get('text', '')}" for m in session.messages)
        gov = GovernanceStore.for_ledger(store)
        gov.append_artifact(
            kind="brainstorm", title=f"AI Wizard charter — {charter['north_star'][:60]}",
            body_markdown=transcript, body_json=charter, state="approved",
            author={"role": "pm", "id": "wizard"})
        # Governance overrides (mode + block_on_problems live here, NOT in autonomy).
        gov.update_state(**recipes.governance_overrides(recipe, autonomous=autonomous))
        # Autonomy from the recipe (real levers only).
        merged = {**policy_to_dict(load_policy(store)),
                  **recipes.autonomy_overrides(recipe, autonomous=autonomous)}
        save_policy(store, policy_from_dict(merged))

        if members:
            store.set_run_config(room_id=None, members=members)
            _set_run_setup_confirmed(store, True)
        else:
            warnings.append(
                "no_models_available: no runnable model routes are connected — "
                "connect a provider and assign a team before running.")
    except HTTPException:
        raise
    except Exception as exc:
        import shutil
        shutil.rmtree(store.dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail={
            "code": "wizard_create_failed", "reason": str(exc)})

    wizard.discard_session(session_id)
    return {
        "project_id": project_id, "created": True,
        "team_size": len(members), "autonomous": charter["autonomous"],
        "run_setup_confirmed": bool(members), "warnings": warnings,
    }


# --- F145: the "PM Changes" consent surface --------------------------------


@router.get("/projects/{project_id}/pm-changes")
def list_pm_changes(project_id: str, request: Request) -> dict[str, Any]:
    """Pending PM Changes awaiting Accept/Decline (+ recent resolved)."""
    _require_tauri_origin(request)
    from errorta_council.coding import pm_changes

    store = _project_store_or_404(project_id)
    return {
        "pending": [c.to_dict() for c in pm_changes.list_changes(store, status="pending")],
        "recent": [c.to_dict() for c in pm_changes.list_changes(store)][-20:],
    }


@router.post("/projects/{project_id}/pm-changes/{change_id}/accept")
def accept_pm_change(project_id: str, change_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding import pm_changes

    store = _project_store_or_404(project_id)
    try:
        change = pm_changes.accept(store, change_id)
    except pm_changes.PmChangeError:
        raise HTTPException(status_code=404, detail="change not found")
    return {"change": change.to_dict()}


@router.post("/projects/{project_id}/pm-changes/{change_id}/decline")
def decline_pm_change(project_id: str, change_id: str, request: Request) -> dict[str, Any]:
    """Revert the change (restore the prior config) and mark it declined."""
    _require_tauri_origin(request)
    from errorta_council.coding import pm_changes

    store = _project_store_or_404(project_id)
    try:
        change = pm_changes.decline(store, change_id)
    except pm_changes.PmChangeError:
        raise HTTPException(status_code=404, detail="change not found")
    return {"change": change.to_dict()}


# --- F145: the control plane (change the team by talking) -------------------


def _pm_complete(store: Any) -> "Any":
    """A ``(prompt) -> text`` completion using the project's PM member, for the
    directive interpreter. Falls back to any enabled member's route."""
    from errorta_council.coding.runner import gateway_member_caller
    from errorta_council.gateway_local import LocalGateway

    member = _resolve_pm_member(store)
    if member is None:
        cfg = store.get_run_config()
        members = [m for m in (cfg.get("members") or []) if m.get("enabled", True)]
        member = members[0] if members else None
    if member is None:
        return None
    # Interactive path: bound the wait like pm-ask (default turns run up to 10 min).
    m = dict(member)
    tl = dict(m.get("turn_limits") or {})
    try:
        configured = int(tl.get("timeout_seconds", 120) or 120)
    except (TypeError, ValueError):
        configured = 120
    tl["timeout_seconds"] = min(configured, 120)
    m["turn_limits"] = tl
    caller = gateway_member_caller(LocalGateway())
    return lambda prompt: caller(m, prompt)


@router.post("/projects/{project_id}/pm-control")
def pm_control(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Apply control-actions to a project — either a structured ``actions`` list or
    a natural-language ``directive`` the PM interprets. Each applied change is a
    reviewable PM Changes set; a grounded refusal (unknown/unavailable model, bad
    knob) is returned per-action, never a 500."""
    _require_tauri_origin(request)
    from errorta_council.coding import control_actions as ca
    from errorta_council.coding import pm_reference

    store = _project_store_or_404(project_id)
    available = pm_reference.list_available_routes()
    surface = "log" if (body or {}).get("surface") == "log" else "pop"

    actions = (body or {}).get("actions")
    refusal: str | None = None
    if not isinstance(actions, list):
        directive = str((body or {}).get("directive") or "").strip()
        if not directive:
            raise HTTPException(status_code=400, detail="actions or directive required")
        complete = _pm_complete(store)
        if complete is None:
            raise HTTPException(status_code=409, detail={
                "code": "no_pm", "reason": "no team is configured to interpret a directive"})
        context = pm_reference.build_pm_reference_context(project_id, store=store)
        try:
            interpreted = ca.interpret_directive(directive, context=context, complete=complete)
        except Exception:
            raise HTTPException(status_code=502, detail={"code": "pm_unreachable"})
        actions = interpreted.get("actions") or []
        refusal = interpreted.get("refusal")

    refusals: list[dict[str, Any]] = []
    if refusal:
        refusals.append({"code": "directive_refused", "reason": refusal})
    # Config actions become reviewable PM Changes; start_run (route-level) kicks off
    # the team. Every per-action failure is a refusal, never a 500.
    applied, action_refusals, run_started = _apply_pm_control_actions(
        project_id, store, actions, available=available, surface=surface)
    refusals.extend(action_refusals)
    return {"applied": applied, "refusals": refusals, "run_started": run_started}


# --- live run management (background thread per project) --------------------

_RUNS: dict[str, dict[str, Any]] = {}


_DEFAULT_ROLE_ORDER = ("pm", "dev", "reviewer", "tester")


def _ensure_coding_roles(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Guarantee a workable coding team. If no member carries a coding_role,
    assign roles by position (pm, dev, reviewer, tester, then all extras = dev) so
    any Council room is usable as a coding team without hand-editing metadata."""
    enabled = [m for m in members if m.get("enabled", True)]
    has_role = any(((m.get("metadata") or {}).get("coding_role")) for m in enabled)
    if has_role or not enabled:
        return members
    for i, m in enumerate(enabled):
        role = _DEFAULT_ROLE_ORDER[i] if i < len(_DEFAULT_ROLE_ORDER) else "dev"
        md = dict(m.get("metadata") or {})
        md["coding_role"] = role
        m["metadata"] = md
    return members


def _resolve_members(body: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(body.get("members"), list):
        return _ensure_coding_roles([m for m in body["members"] if isinstance(m, dict)])
    room_id = body.get("room_id")
    if room_id:
        from errorta_council import paths as council_paths
        from errorta_council.room_store import RoomStore
        store = RoomStore(rooms_dir=council_paths.rooms_dir(),
                          deleted_dir=council_paths.deleted_rooms_dir())
        room = store.get(str(room_id))
        members = room.to_dict().get("members", [])
        return _ensure_coding_roles([m for m in members if isinstance(m, dict)])
    return []


def _validate_member_ids(members: list[dict[str, Any]]) -> None:
    """Every enabled team member must carry a unique, non-empty string ``id`` —
    the runner and topology key speaker order + rank on ``m["id"]`` (runner.py
    ~2833/2839, topology.py ~517). A malformed team (e.g. a member that uses
    ``member_id`` instead of ``id``) must be rejected HERE with a clear 422, not
    crash the run worker thread with an unhandled ``KeyError: 'id'`` mid-run.
    Duplicate ids are rejected too — they'd silently collide in the id-keyed
    maps and break speaker ordering."""
    seen: set[str] = set()
    missing: list[int] = []
    dups: set[str] = set()
    for i, m in enumerate(members):
        if not m.get("enabled", True):
            continue
        mid = m.get("id")
        if not (isinstance(mid, str) and mid.strip()):
            missing.append(i)
            continue
        if mid in seen:
            dups.add(mid)
        seen.add(mid)
    if missing or dups:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_member_ids",
            "message": (
                "Each enabled team member needs a unique, non-empty 'id'."
                + (f" Missing id at position(s): {missing}." if missing else "")
                + (f" Duplicate id(s): {sorted(dups)}." if dups else "")
            ),
        })


def _thread_alive(project_id: str) -> bool:
    rec = _RUNS.get(project_id)
    return bool(rec and rec["thread"] is not None and rec["thread"].is_alive())


def _reconcile_run_state(project_id: str, store: LedgerStore) -> dict[str, Any]:
    from errorta_council.coding.run_recovery import recover_orphaned_run
    recover_orphaned_run(
        store,
        live=_thread_alive(project_id),
        reason="run_status",
    )
    return store.get_run_state()


def _run_result_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("status") == "stopped":
        return {"stop_reason": state.get("stop_reason"),
                "iterations": (state.get("counters") or {}).get("iterations")}
    if state.get("status") == "interrupted":
        return {"stop_reason": "interrupted",
                "iterations": (state.get("counters") or {}).get("iterations"),
                "requeued_task_ids": state.get("requeued_task_ids") or []}
    if state.get("status") == "failed":
        return {"error": state.get("last_error")}
    return None


_BASE_BRANCHES = ("master", "main")


def _fingerprint_matches(persisted: dict[str, Any], current: dict[str, Any],
                         ws: Any = None) -> bool:
    """Resume integrity: does the worktree still hold the SAME work it did when the
    run was interrupted? Compares the work-bearing parts (branch heads + per-task
    worktrees) but IGNORES the volatile ``primary`` pointer — which branch the
    shared root worktree happens to be checked out on. The runner moves that
    pointer around as it works, and a reconcile/restart legitimately leaves it back
    on ``master``, so comparing it caused a spurious ``workspace_integrity_failed``.

    F097: the **base branch** (``master``/``main``) may also legitimately ADVANCE
    while a run is interrupted — every task merge fast-forwards it. So the base
    branch passes when its persisted head is EQUAL TO or an ANCESTOR OF the live
    head (a contains/fast-forward). Every OTHER branch + the per-task ``worktrees``
    map must still match exactly — a rewritten or missing task branch is real
    corruption. When ``ws`` is None or the ancestry probe can't run, the base
    branch falls back to strict equality (fail-closed)."""
    def _work(fp: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in (fp or {}).items() if k != "primary"}

    p, c = _work(persisted), _work(current)
    p_branches = dict(p.pop("branches", {}) or {})
    c_branches = dict(c.pop("branches", {}) or {})

    # Non-branch parts (format + worktrees) must match exactly.
    if p != c:
        return False

    # Branch sets must match key-for-key; heads must match exactly except a base
    # branch that fast-forwarded.
    if set(p_branches) != set(c_branches):
        return False
    for name, p_head in p_branches.items():
        c_head = c_branches[name]
        if p_head == c_head:
            continue
        if name in _BASE_BRANCHES and ws is not None and ws.is_ancestor(p_head, c_head):
            continue  # base branch advanced via merges — legitimate
        return False
    return True


def _is_transient_gateway_error(exc: BaseException) -> bool:
    """A flaky model backend (or a proxy/VPN in the path) can return a body that
    doesn't decompress, drop a connection, or make the bridge time out. Those are
    TRANSIENT wire failures — recoverable by simply restarting the run — not code
    defects. Detect them structurally so the top-level worker records a clean,
    recoverable state instead of a cryptic ``error: Error -3 while decompressing
    data: incorrect header check`` hard crash (``zlib.error.__name__`` is the bare
    ``"error"``). The gateway already normalizes these to RetryableError at its
    egress choke point; this is defense-in-depth for any path that leaks a raw
    decode/transport error past the member-health ladder."""
    import concurrent.futures
    import zlib

    if isinstance(exc, (zlib.error, concurrent.futures.TimeoutError)):
        return True
    try:  # httpx is a heavy import; keep it lazy + never let it break the catch.
        import httpx

        if isinstance(exc, (httpx.DecodingError, httpx.TransportError)):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from errorta_council.gateway_local import RetryableError

        if isinstance(exc, RetryableError):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _start_run(
    project_id: str,
    body: dict[str, Any],
    *,
    resume: bool = False,
    continue_: bool = False,
) -> dict[str, Any]:
    _alpha_enforce_not_locked()
    store = LedgerStore(project_id)
    try:
        project = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")

    # F121-B1: belt-and-suspenders readiness gate. The first fresh Start Run on a
    # project that hasn't completed the readiness gate refuses with a structured
    # `run_setup_required` so a stale client can't bypass the gate. Resume /
    # continue are exempt — they re-drive an already-confirmed run. The frontend
    # gate is the primary path; this is the defense behind it.
    if not (resume or continue_) and not project.run_setup_confirmed:
        raise HTTPException(status_code=409, detail={
            "code": "run_setup_required",
            "message": ("Run setup hasn't been confirmed for this project. "
                        "Open Run setup, review the configuration, and confirm."),
        })

    # Resolve members BEFORE entering the critical section (may do I/O / 4xx).
    # F097: the body (members or room_id) wins. On resume with an empty body,
    # recover the team the run was started with from the persisted run_config, so
    # the Resume button works with no input and reproduces the EXACT start-team.
    #
    # F100 "continue governance": a run that STOPPED at a review/gate (status
    # "stopped", not "interrupted") is continued by a FRESH start over the same
    # ledger — the PM re-drafts the stuck artifact with the user's interjection +
    # the latest review findings in context. That continuation also recovers the
    # saved team from run_config when the body is empty (same as resume), so the
    # drawer's "Send & continue" works with no room selection.
    members = _resolve_members(body)
    recovered = False
    if not members and (resume or continue_):
        cfg = store.get_run_config()
        members = _ensure_coding_roles(
            [m for m in (cfg.get("members") or []) if isinstance(m, dict)])
        recovered = bool(members)
    if not members:
        if resume or continue_:
            action = "resume" if resume else "continue"
            raise HTTPException(status_code=400, detail={
                "code": "run_config_missing",
                "message": (f"This run has no saved team to {action} with. "
                            "Pick a Council room as the team and try again."),
            })
        raise HTTPException(status_code=400, detail="no members (pass members or room_id)")

    # Reject a malformed team (missing/duplicate member ``id``) before spawning a
    # worker — otherwise the runner crashes mid-run with an unhandled KeyError.
    _validate_member_ids(members)

    # F120-04: pre-run preflight. Before any worker thread spawns, probe each
    # distinct CLI/subscription route once; if a provider is logged-out / missing,
    # refuse to start with a structured unhealthy-provider list instead of letting
    # the run spin for minutes. Config-gated (default on); deduped per route.
    # First honor the existing cheap liveness guard so an already-running project
    # never burns an auth/model probe or reports the wrong 409 reason. The full
    # alive-check is repeated in the critical section below after preflight, which
    # keeps the concurrent-start race closed.
    if not (resume or continue_) and settings.member_health_preflight_enabled():
        with store.lock:
            _reconcile_run_state(project_id, store)
            if _thread_alive(project_id):
                raise HTTPException(status_code=409, detail="a run is already in progress")
        from errorta_council.coding.member_health import preflight_members
        unhealthy = preflight_members(members)
        if unhealthy:
            raise HTTPException(status_code=409, detail={
                "code": "member_health_preflight_failed",
                "message": (
                    "Can't start: one or more providers are not ready. "
                    "Fix them in Settings → Providers, then start again."),
                "unhealthy": unhealthy,
            })

    from errorta_council.coding.ledger import _now

    # F087-13 WS-3: the entire alive-check -> set running -> register -> start is
    # one critical section under the per-project lock, so two concurrent POST /run
    # for the same project can never both pass the alive-check and spawn two
    # workers over the same worktree. The loser raises 409.
    with store.lock:
        state = _reconcile_run_state(project_id, store)
        if _thread_alive(project_id):
            raise HTTPException(status_code=409, detail="a run is already in progress")
        if resume and state.get("status") != "interrupted":
            raise HTTPException(status_code=409, detail="run is not recoverable")
        # A governance "continue" is a fresh worker over a STOPPED run. Do not
        # allow it for "interrupted": crash recovery must go through /run/resume
        # so workspace-integrity checks cannot be bypassed.
        if continue_ and state.get("status") != "stopped":
            raise HTTPException(status_code=409, detail="run is not continuable")
        if resume:
            # F087-15 M2: refuse to resume against a deleted/reset worktree. Verify
            # the persisted fingerprint matches the actual worktree before spawning
            # a worker (we only INSPECT here — never recreate the worktree).
            persisted_fingerprint = state.get("workspace_fingerprint")
            persisted_head = state.get("workspace_head")
            if persisted_fingerprint or persisted_head:
                from errorta_council.coding.workspace import CodingWorkspace
                proj = store.get_project()
                ws = CodingWorkspace(project_id, store)
                ws.set_target(proj.target)
                if not ws.exists():
                    raise HTTPException(
                        status_code=409, detail="workspace_integrity_failed")
                if persisted_fingerprint:
                    current = ws.workspace_fingerprint()
                    if not _fingerprint_matches(persisted_fingerprint, current, ws=ws):
                        raise HTTPException(
                            status_code=409, detail="workspace_integrity_failed")
                elif persisted_head and ws.head() != persisted_head:
                    raise HTTPException(
                        status_code=409, detail="workspace_integrity_failed")

        # F097: persist the team for future resume (fresh start, or a resume that
        # carried an explicit override). A pure recovery-from-config doesn't
        # re-write. Do this after liveness/recoverability/integrity checks so a
        # failed start/resume cannot overwrite the last valid start-team.
        if not recovered:
            store.set_run_config(
                members=members,
                room_id=body.get("room_id"),
                saved_at=_now(),
            )

        # F087-07-F/F087-12: persist run lifecycle to the ledger so status/cancel/
        # result survive a sidecar restart. Resume starts a fresh worker over the
        # existing ledger/worktree after recovery requeued in-flight tasks.
        previous = state if resume else {}
        # Clear blocking member-health Problems the current roster has already
        # fixed (e.g. a member switched off a removed Cursor model / off a
        # rate-limited account). They're keyed by (member, reason) and stay open
        # + blocking until resolved, so without this a stale Problem keeps gating
        # the run for a member that no longer uses that route at all.
        try:
            from errorta_council.coding import attention as _attention
            _attention.resolve_stale_member_health(project_id, members, store=store)
            _attention.resolve_stale_worker_unproductive(
                project_id, members, store=store
            )
        except Exception:  # noqa: BLE001 — cleanup is best-effort, never block start
            logging.getLogger("errorta.coding").warning(
                "stale member/worker-health cleanup failed", exc_info=True)
        store.set_run_state(status="running", started_at=_now(), ended_at=None,
                            stop_reason=None, last_error=None, cancel_requested=False,
                            counters=None, recoverable=False, can_resume=False,
                            resumed_from_status=previous.get("status"),
                            resumed_at=_now() if resume else None)
        record: dict[str, Any] = {"thread": None}

        def _should_cancel() -> bool:
            return bool(store.get_run_state().get("cancel_requested"))

        def _worker() -> None:
            try:
                from errorta_council.coding.autonomy import load_policy
                from errorta_council.coding.runner import CodingRunner, gateway_member_caller
                from errorta_council.coding.skills import load_guardrail
                from errorta_council.gateway_local import LocalGateway
                runner = CodingRunner(
                    project_id, members, gateway_member_caller(LocalGateway()),
                    guardrail_enabled=load_guardrail(store).enabled)
                # The route owns lifecycle (cancel/recovery flags) -> tell the
                # runner not to also write running/stopped/failed (F087-19 #4).
                res = runner.run(load_policy(store), should_cancel=_should_cancel,
                                 manage_lifecycle=False)
                store.set_run_state(status="stopped", stop_reason=res.stop_reason,
                                    ended_at=_now(), recoverable=False, can_resume=False,
                                    counters={
                                        "iterations": res.counters.iterations,
                                        "turns_repaired": res.counters.turns_repaired,
                                        "task_reassignments": res.counters.task_reassignments,
                                        "model_escalations": res.counters.model_escalations,
                                        "pm_assists": res.counters.pm_assists,
                                    })
            except BaseException as exc:  # noqa: BLE001
                # MUST be BaseException, not Exception. A SystemExit-class error
                # (SystemExit/KeyboardInterrupt/GeneratorExit) raised deep in a
                # member turn would otherwise escape `except Exception`, silently
                # kill this daemon thread, and leave run_state stuck at "running"
                # with no live worker — so every status poll re-flags the run
                # "interrupted (resumable)" forever (the resume-loop bug). Record
                # it as a terminal failure with the real message + a logged
                # traceback so the cause is visible and the loop can't recur.
                # The run stays recoverable: the next start reclaims the in-flight
                # 'doing' tasks (run_recovery.reclaim_stranded_inflight).
                logging.getLogger("errorta.coding").exception(
                    "coding worker thread exited abnormally: %s", exc)
                # A transient gateway/decode/network failure (a flaky provider or
                # a proxy mangling gzip) must NOT read as a cryptic hard crash: the
                # run stays fully recoverable (the next Start reclaims the in-flight
                # tasks), so record a clean, honest, actionable message instead of
                # a raw ``error: Error -3 …`` string. Genuinely-unexpected
                # exceptions keep the raw type + message so a real defect stays
                # debuggable. ``can_resume`` stays False to match the fatal path —
                # /run/resume only accepts an ``interrupted`` state, so the recovery
                # here is a fresh Start (driven by ``recoverable``), not Resume.
                if _is_transient_gateway_error(exc):
                    store.set_run_state(
                        status="failed",
                        last_error=("transient model-backend error "
                                    "(network/decompression) — retry the run"),
                        ended_at=_now(), recoverable=True, can_resume=False)
                else:
                    store.set_run_state(
                        status="failed", last_error=f"{type(exc).__name__}: {exc}",
                        ended_at=_now(), recoverable=True, can_resume=False)

        t = threading.Thread(target=_worker, daemon=True)
        record["thread"] = t
        _RUNS[project_id] = record
        t.start()
    return {"started": True, "resumed": resume}


def _live_project_ids() -> list[str]:
    """Project ids with a live worker thread (for boot-recovery liveness)."""
    return [pid for pid in list(_RUNS.keys()) if _thread_alive(pid)]


@router.post("/projects/{project_id}/run")
def start_run(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/run")
    return _start_run(project_id, body, resume=False)


@router.post("/projects/{project_id}/run/resume")
def resume_run(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/run/resume")
    return _start_run(project_id, body, resume=True)


@router.post("/projects/{project_id}/run/continue")
def continue_run(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """F100 governance continuation: re-drive a run that STOPPED at a review/gate.

    A review-stopped governance run has status ``stopped`` (not ``interrupted``),
    so ``/run/resume`` (crash-recovery only) rejects it with 409. This endpoint
    starts a FRESH worker over the same ledger so the PM re-drafts the stuck
    artifact with the user's queued interjection + the latest review findings in
    context. The saved team is recovered from run_config when the body is empty.
    """
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/run/continue")
    return _start_run(project_id, body, continue_=True)


@router.get("/projects/{project_id}/run")
def run_status(project_id: str) -> dict[str, Any]:
    store = LedgerStore(project_id)
    state = _reconcile_run_state(project_id, store)
    running = state.get("status") == "running" and _thread_alive(project_id)
    return {"running": running, "result": _run_result_from_state(state),
            "state": state, "recoverable": bool(state.get("recoverable")),
            "can_resume": bool(state.get("can_resume"))}


@router.post("/projects/{project_id}/run/cancel")
def cancel_run(project_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/run/cancel")
    # Persist the cancel so the loop's should_cancel sees it (and it survives a
    # restart). The loop observes it at its next turn boundary.
    LedgerStore(project_id).set_run_state(cancel_requested=True)
    return {"cancelled": True}


# --------------------------------------------------------------------------- #
# F121 — pre-first-run readiness gate ("Run setup")
# --------------------------------------------------------------------------- #
class _RunSetupConfirmBody(BaseModel):
    """The resolved config the readiness gate applies on confirm. Every field is
    optional — the gate sends what it manages; absent fields keep their current
    project value. ``team_room_id`` selects the team; ``members`` is the override
    path. ``grounding`` is the corpus-binding payload (validated as elsewhere)."""

    governance_mode: Optional[GovernanceMode] = None
    block_on_problems: Optional[bool] = None
    human_code_approval: Optional[HumanCodeApproval] = None
    max_review_rounds: Optional[int] = None
    checkpoint_cadence: Optional[str] = None
    checkpoint_n: Optional[int] = None
    guardrail_enabled: Optional[bool] = None
    max_iterations: Optional[int] = None
    max_model_calls: Optional[int] = None
    max_parallel_workers: Optional[int] = None
    member_failure_limit: Optional[int] = None
    preflight_enabled: Optional[bool] = None
    team_room_id: Optional[str] = None
    members: Optional[list[dict[str, Any]]] = None
    grounding: Optional[dict[str, Any]] = None


def _set_run_setup_confirmed(store: LedgerStore, confirmed: bool) -> dict[str, Any]:
    """Persist the project's ``run_setup_confirmed`` flag (mirrors put_north_star
    so the rest of the record + _extras round-trip verbatim)."""
    raw = store.get_project().to_dict()
    raw["run_setup_confirmed"] = bool(confirmed)
    raw["updated_at"] = _now()
    _atomic_write_json(store._project_path, raw)
    return raw


@router.get("/projects/{project_id}/run-setup")
def get_run_setup(project_id: str) -> dict[str, Any]:
    """Return the gate's current state: whether setup is confirmed, the project's
    live config, and the user-level sticky defaults seed for a fresh pre-fill.

    The frontend composes the pre-fill: a never-confirmed project seeds from
    ``defaults`` (the user's last-used config) or the built-in Careful preset;
    an already-confirmed project shows its live config."""
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceStore

    project = store.get_project()
    governance = GovernanceStore.for_ledger(store).load_state().to_dict()
    policy = policy_to_dict(load_policy(store))
    guardrail = load_guardrail(store).enabled
    return {
        "run_setup_confirmed": bool(project.run_setup_confirmed),
        "governance": governance,
        "autonomy": policy,
        "guardrail_enabled": guardrail,
        "member_health_preflight": settings.member_health_preflight_enabled(),
        "defaults": settings.get_coding_run_defaults(),
    }


@router.post("/projects/{project_id}/run-setup/preflight")
def run_setup_preflight(
    project_id: str, body: dict[str, Any], request: Request,
) -> dict[str, Any]:
    """Probe the gate's selected team's distinct provider routes (F120 reuse).

    Returns ``{unhealthy: [...]}`` — one entry per logged-out / unavailable
    provider class with its remediation, exactly as the in-loop preflight. An
    empty list means every required route is ready. Always 200 (the gate renders
    the badges); the *blocking* decision is the frontend's (D4)."""
    _require_tauri_origin(request)
    _project_store_or_404(project_id)
    members = _resolve_members(body)
    if not members:
        raise HTTPException(status_code=400, detail="no members (pass members or room_id)")
    _validate_member_ids(members)
    from errorta_council.coding.member_health import preflight_members
    return {"unhealthy": preflight_members(members)}


@router.post("/projects/{project_id}/run-setup/confirm")
def confirm_run_setup(
    project_id: str, body: _RunSetupConfirmBody, request: Request,
) -> dict[str, Any]:
    """Apply the readiness gate's resolved config, mark setup confirmed, and
    remember it as the user-level sticky default for the next project (F121-B4).

    Composes the EXISTING setters (governance settings, autonomy policy,
    guardrail, room/team via run_config, grounding bind) — no new run semantics.
    Does NOT start the run; the frontend starts it after a successful confirm."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/run-setup/confirm")
    store = _project_store_or_404(project_id)
    from errorta_council.coding.governance import GovernanceState, GovernanceStore

    # Resolve and validate the selected team before applying any of the setup
    # setters below. A malformed team must reject the whole confirmation rather
    # than leave governance, autonomy, or user-level settings partially updated.
    team_room_id = body.team_room_id
    members = _resolve_members({
        "members": body.members,
        "room_id": team_room_id,
    } if (body.members or team_room_id) else {})
    if members:
        _validate_member_ids(members)

    # 1) Governance settings (mode / block_on_problems / approval / review rounds).
    governance = GovernanceStore.for_ledger(store)
    current = governance.load_state().to_dict()
    if body.governance_mode is not None:
        current["mode"] = body.governance_mode
        if body.governance_mode == "off":
            current["phase"] = "idle"
        elif current.get("phase") == "idle":
            current["phase"] = "brainstorming"
    if body.block_on_problems is not None:
        current["block_on_problems"] = bool(body.block_on_problems)
    if body.human_code_approval is not None:
        current["human_code_approval"] = body.human_code_approval
    if body.max_review_rounds is not None:
        current["max_review_rounds"] = body.max_review_rounds
    governance.save_state(GovernanceState.from_dict(current))

    # 2) Autonomy policy (cadence + spend caps + member_failure_limit).
    pol = policy_to_dict(load_policy(store))
    for key in ("checkpoint_cadence", "checkpoint_n", "max_iterations",
                "max_model_calls", "max_parallel_workers", "member_failure_limit"):
        val = getattr(body, key)
        if val is not None:
            pol[key] = val
    save_policy(store, policy_from_dict(pol))

    # 3) Guardrail toggle.
    if body.guardrail_enabled is not None:
        save_guardrail(store, SkillsGuardrailPolicy(enabled=bool(body.guardrail_enabled)))

    # 4) Member-health preflight toggle (user-level, F120).
    if body.preflight_enabled is not None:
        s = settings.load()
        s["member_health_preflight"] = bool(body.preflight_enabled)
        settings.save(s)

    # 5) Team — persist the validated room/members as the run_config so Start Run
    # (and resume) uses it.
    if members:
        store.set_run_config(members=members, room_id=team_room_id, saved_at=_now())

    # 6) Grounding bind (optional; validated + applied as elsewhere).
    if body.grounding is not None:
        proj = store.get_project()
        _validate_grounding_payload(body.grounding, repo_path=proj.repo_path)
        _apply_grounding_payload(store, body.grounding)

    # 7) Mark the gate confirmed (so Start Run proceeds).
    raw = _set_run_setup_confirmed(store, True)

    # 8) Remember the resolved config as the next-project pre-fill seed (D3).
    saved = governance.load_state().to_dict()
    pol_now = policy_to_dict(load_policy(store))
    settings.set_coding_run_defaults({
        "governance_mode": saved.get("mode"),
        "block_on_problems": saved.get("block_on_problems"),
        "human_code_approval": saved.get("human_code_approval"),
        "max_review_rounds": saved.get("max_review_rounds"),
        "checkpoint_cadence": pol_now.get("checkpoint_cadence"),
        "checkpoint_n": pol_now.get("checkpoint_n"),
        "guardrail_enabled": load_guardrail(store).enabled,
        "max_iterations": pol_now.get("max_iterations"),
        "max_model_calls": pol_now.get("max_model_calls"),
        "max_parallel_workers": pol_now.get("max_parallel_workers"),
        "member_failure_limit": pol_now.get("member_failure_limit"),
        "preflight_enabled": settings.member_health_preflight_enabled(),
        "team_room_id": team_room_id,
    })

    return {"run_setup_confirmed": True, "project": raw}


@router.get("/projects/{project_id}/artifacts")
def get_artifacts(project_id: str) -> dict[str, Any]:
    store = LedgerStore(project_id)
    artifacts = store.list_artifacts()
    workspace = None
    try:
        workspace = _workspace(project_id)
    except HTTPException as exc:
        if exc.status_code not in (404, 409):
            raise
    return {
        "artifacts": [
            {**artifact, "on_master": bool(
                workspace and workspace.is_on_master(str(artifact.get("path", "")))
            )}
            for artifact in artifacts
        ]
    }


@router.get("/projects/{project_id}/tool-events")
def get_tool_events(project_id: str, limit: int = 25) -> dict[str, Any]:
    return {"tool_events": LedgerStore(project_id).list_tool_events(limit=limit)}


@router.get("/projects/{project_id}/turns")
def get_turns(project_id: str, limit: int = 100) -> dict[str, Any]:
    # F087-16: the verbose per-turn transcript (prompt + raw response + outcome).
    return {"turns": LedgerStore(project_id).list_turns(limit=limit)}


@router.get("/projects/{project_id}/usage-summary")
def get_usage_summary(project_id: str) -> dict[str, Any]:
    """F143 / F143-01 Slice D: per-project token-usage rollup (read-only) —
    ``by_member`` / ``by_route`` / ``by_role`` / ``total`` summed over the full turn
    ledger. The headline ``input``/``output`` is the GENUINE effective total
    (measured-where-present, estimated otherwise); each bucket carries the
    measured/estimated split, cache detail (never in the headline — D4), four
    provenance counts, and a ``coverage`` share. Additive/backward-compatible — the
    F143 fields are unchanged; the provenance/coverage/by_role fields are new. 404 on
    an unknown project."""
    from errorta_council.coding.usage_rollup import rollup_turns

    store = _project_store_or_404(project_id)
    return {"usage": rollup_turns(store.list_turns(limit=None))}


@router.get(
    "/projects/{project_id}/tasks/{task_id}/turns/{turn_id}/composition")
def get_turn_composition(
        project_id: str, task_id: str, turn_id: str) -> dict[str, Any]:
    """F143-01 Slice F: per-turn Context Report. Returns the turn's Layer-1
    ``composition`` block (what Errorta sent, by category — populated by the segmented
    prompt builders) + ``cli_overhead_tokens`` (the CLI's vendor-managed inner context
    we can't itemize) + a Layer-2 caveat note for CLI members.

    404 on an unknown project, task, or turn."""
    store = _project_store_or_404(project_id)

    # The TURN RECORD is the authority for task membership — not list_tasks(). PM
    # plan turns carry the pseudo-task-id "plan" and governance turns "governance:*"
    # (runner.py), which are real recorded turns with real compositions but are NOT
    # entries in list_tasks(); a task-existence guard would 404 exactly those (a
    # segmented builder's composition the user most wants to see). So we 404 only
    # when the turn itself is missing or its recorded task_id doesn't match the path.
    turn = store.get_turn(turn_id)
    if turn is None or str(turn.get("task_id") or "") != task_id:
        raise HTTPException(status_code=404, detail="turn not found")

    usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else {}
    composition = turn.get("composition")
    if not isinstance(composition, dict):
        composition = {"sent_total": 0, "categories": []}
    else:
        composition.setdefault("sent_total", 0)
        composition.setdefault("categories", [])

    cli_overhead = usage.get("cli_overhead_tokens")
    if not isinstance(cli_overhead, int) or isinstance(cli_overhead, bool):
        cli_overhead = None

    # Layer-2 caveat: a CLI-backed member (provider_class ends "_cli") wraps our
    # piped prompt in its own vendor-managed system prompt/tools/skills that we can't
    # itemize. Detect it from the resolved route (route_ids are "<provider>.<model>",
    # e.g. "claude_cli.sonnet") so the note appears for every CLI member — even one
    # with no measured input (no overhead magnitude to quote). Non-CLI members get no
    # note (their prompt is fully knowable — the categories ARE the whole story).
    route_id = str(turn.get("route_id") or "")
    provider_class = route_id.split(".", 1)[0] if route_id else ""
    # cli_overhead is computed at record time ONLY for a `_cli` provider_class, so a
    # non-null overhead is itself proof of a CLI member — OR the resolved route looks
    # CLI. Combining both sources keeps the note and the quoted overhead consistent
    # even if route_id fell back to a non-CLI-shaped value (review F2).
    is_cli = provider_class.endswith("_cli") or cli_overhead is not None
    note = None
    if is_cli:
        route_label = route_id or "the CLI"
        if cli_overhead is not None:
            note = (
                f"This shows what Errorta sent into the {route_label} CLI. The CLI "
                "adds its own system prompt, tools, and skills on top "
                f"(~{cli_overhead} tokens, vendor-managed) that Errorta can't "
                "itemize (Layer-2, not shown here).")
        else:
            note = (
                f"This shows what Errorta sent into the {route_label} CLI. The CLI "
                "adds its own system prompt, tools, and skills on top "
                "(vendor-managed) that Errorta can't itemize (Layer-2, not shown "
                "here).")

    return {
        "composition": composition,
        "cli_overhead_tokens": cli_overhead,
        "note": note,
    }


@router.get("/projects/{project_id}/prs")
def get_prs(project_id: str) -> dict[str, Any]:
    # F087-17: the branch-per-task pull requests + their review/test/merge state.
    return {"prs": LedgerStore(project_id).list_prs()}


@router.get("/projects/{project_id}/run-log.txt")
def get_run_log(project_id: str):
    """F087-16: a human-readable plaintext transcript of the whole run — North
    Star, every member turn (verbatim prompt + raw response + outcome), the
    decision log, grounded test runs, and files touched — for after-the-fact
    review/export."""
    from fastapi.responses import PlainTextResponse
    store = LedgerStore(project_id)
    try:
        proj = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    text = _render_run_log(store, proj)
    return PlainTextResponse(
        text,
        headers={"Content-Disposition":
                 f'attachment; filename="coding-run-{project_id}.txt"'})


def _render_run_log(store: LedgerStore, proj: Any) -> str:
    lines: list[str] = []
    sep = "=" * 78
    lines += [sep, f"CODING TEAM RUN LOG — {proj.id}", sep,
              f"North Star: {proj.north_star}",
              f"Definition of done: {proj.definition_of_done}",
              f"Status: {proj.status}   Target: {proj.target}", ""]
    state = store.get_run_state()
    lines += [f"Run state: {state.get('status')}  "
              f"stop_reason={state.get('stop_reason')}  "
              f"iterations={(state.get('counters') or {}).get('iterations')}", ""]

    lines += [sep, "TURN-BY-TURN TRANSCRIPT", sep]
    turns = store.list_turns()
    if not turns:
        lines.append("(no turns recorded)")
    for i, t in enumerate(turns, 1):
        lines += [
            "",
            f"--- turn {i}: {t.get('role')} on task {t.get('task_id')} "
            f"-> {t.get('outcome')}"
            + (f" [{t.get('reason')}]" if t.get("reason") else "")
            + f"  ({t.get('duration_ms')}ms, {t.get('at')}) ---",
            f"member: {t.get('member_id')}   parse_ok: {t.get('parse_ok')}",
            (f"model: {(t.get('model_assignment') or {}).get('route_id')}  "
             f"assignment={(t.get('model_assignment') or {}).get('assignment_id')}  "
             f"difficulty={(t.get('model_assignment') or {}).get('difficulty_tier')}"),
            "PROMPT:", str(t.get("prompt", "")),
            "RESPONSE:", str(t.get("response", "")),
        ]

    lines += ["", sep, "DECISION LOG", sep]
    for d in store.list_decisions():
        lines.append(f"[{d.get('at')}] {d.get('choice')}: {d.get('title')} "
                     f"— {d.get('rationale')}")

    lines += ["", sep, "TEST RUNS", sep]
    for r in store.list_test_runs():
        exits = "; ".join(f"{x.get('command_id')}={x.get('status')}/{x.get('exit_code')}"
                          for x in r.get("results", []))
        lines.append(f"[{r.get('at')}] passed={r.get('passed')} "
                     f"head={r.get('head')} sandbox={r.get('sandbox')} "
                     f"cmds={r.get('command_ids')} :: {exits}")

    lines += ["", sep, "FILES TOUCHED", sep]
    for a in store.list_artifacts():
        lines.append(f"{a.get('status'):>8}  {a.get('path')}")

    return "\n".join(lines) + "\n"


# --- F087-10: test-command registry + grounded test-run records --------------
@router.get("/projects/{project_id}/test-commands")
def get_test_commands(project_id: str) -> dict[str, Any]:
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    return {"commands": store.get_test_commands()}


@router.put("/projects/{project_id}/test-commands")
def put_test_commands(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/test-commands")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        saved = store.set_test_commands(body.get("commands", body))
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"commands": saved}


@router.get("/projects/{project_id}/test-runs")
def get_test_runs(project_id: str) -> dict[str, Any]:
    return {"runs": LedgerStore(project_id).list_test_runs()}


@router.get("/projects/{project_id}/test-settings")
def get_test_settings(project_id: str) -> dict[str, Any]:
    return {"require_sandbox": LedgerStore(project_id).get_require_sandbox()}


@router.put("/projects/{project_id}/test-settings")
def put_test_settings(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/test-settings")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    return {"require_sandbox": store.set_require_sandbox(bool(body.get("require_sandbox")))}


# --------------------------------------------------------------------------- #
# F101 — coding runtime preview (S1 read routes; S3 adds the executing routes).
#
# Runtime routes carry the Tauri-origin guard on mutations but are deliberately
# NOT refuse_local_dataplane_if_remote: running a generated project locally is a
# different data plane than AIAR-corpus writes, and is allowed under remote AIAR
# residency (spec D + plan "Locked constraints").
# --------------------------------------------------------------------------- #
def _runtime_store(project_id: str):
    from errorta_council.coding.runtime import RuntimeProfileStore
    store = _project_store_or_404(project_id)
    return RuntimeProfileStore.for_ledger(store)


def _desktop_reaches_reduced_isolation(plan) -> bool:
    """Whether running this desktop plan will actually drop to T2 (no OS sandbox).

    Keyed off the SAME backend the process manager resolves from the profile
    (``resolve_sandbox_backend(profile.sandbox)``) — NOT merely whether this host
    *has* a windowing sandbox — so the run route's consent gate can't disagree
    with the tier the manager stamps (``recorded_backend == "none"`` -> tier 2).
    A desktop run reaches T2 when its profile sets ``sandbox="none"`` OR uses
    ``sandbox="auto"`` on a host with no seatbelt/bwrap; both must be gated behind
    the explicit second consent (F101-03 S3, invariant: never silently exceed the
    minimum trust tier). An explicit-but-unavailable backend raises here — the
    manager will *block* that run (never a silent T2 downgrade), so it needs no
    reduced-isolation consent and this returns False."""
    from errorta_council.coding.runtime_process import resolve_sandbox_backend
    from errorta_tools.runner.sandbox import SANDBOX_NONE, SandboxUnavailable

    profile = plan.source_profile
    sandbox_choice = profile.sandbox if profile is not None else "auto"
    try:
        return resolve_sandbox_backend(sandbox_choice) == SANDBOX_NONE
    except SandboxUnavailable:
        return False


def _runtime_workspace_root(project_id: str):
    """The master worktree path for detection, or None if no worktree exists
    yet. (Detection on a project with no files is an honest empty result, not a
    409 — the panel shows "No runnable demo detected".)"""
    from errorta_council.coding.workspace import CodingWorkspace
    store = _project_store_or_404(project_id)
    ws = CodingWorkspace(project_id, store)
    ws.set_target(store.get_project().target)
    if not ws.exists():
        return None
    return ws.root()


@router.get("/projects/{project_id}/runtime/profiles")
def get_runtime_profiles(project_id: str) -> dict[str, Any]:
    rstore = _runtime_store(project_id)
    return {"profiles": [p.to_dict() for p in rstore.list_profiles()]}


@router.put("/projects/{project_id}/runtime/profiles/{profile_id}")
def put_runtime_profile(
    project_id: str, profile_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding.runtime import RuntimeValidationError, validate_profile
    from errorta_export.safe_path import UnsafePathError, safe_segment
    try:
        safe_segment(profile_id)
    except UnsafePathError as exc:
        raise HTTPException(status_code=422, detail="invalid profile_id") from exc
    if len(profile_id) > 64:
        raise HTTPException(status_code=422, detail="profile_id too long")
    rstore = _runtime_store(project_id)
    payload = body.get("profile", body) if isinstance(body, dict) else body
    try:
        profile = validate_profile(payload, profile_id=profile_id, project_id=project_id)
    except RuntimeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved = rstore.upsert_profile(profile)
    return {"profile": saved.to_dict()}


@router.post("/projects/{project_id}/runtime/detect")
def post_runtime_detect(project_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding.runtime import detect
    root = _runtime_workspace_root(project_id)
    if root is None:
        return {"proposed": []}
    proposed = detect(root, project_id=project_id)
    return {"proposed": [p.to_dict() for p in proposed]}


@router.post("/projects/{project_id}/runtime/run")
def post_runtime_run(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """F101-03 S1 — the universal Run front door.

    Resolves a *grounded* ``LaunchPlan`` (spec D2: never a guessed command — a
    ``start`` whose entrypoint file is absent on master is refused, not run) and
    either returns it as a preview (default) or executes it via the matching
    ``Launcher`` when ``confirm`` is true. An ungrounded / unknown project returns
    200 with a ``looked_for`` checklist — an honest "I don't know how to run this",
    never a 4xx/5xx.

    Like every runtime mutation it carries the Tauri-origin guard but is NOT
    refuse_local_dataplane_if_remote (running a generated project locally is a
    different data plane than AIAR-corpus writes).
    """
    _require_tauri_origin(request)
    from errorta_council.coding.runtime import HostFacts
    from errorta_council.coding.runtime_launchers import can_launch, get_launcher
    from errorta_council.coding.runtime_resolve import (
        Unresolved,
        resolve_launch_plan,
    )

    body = body if isinstance(body, dict) else {}
    confirm = bool(body.get("confirm"))
    confirm_reduced = bool(body.get("confirm_reduced_isolation"))

    # No worktree yet is an honest "nothing to run", not a 409 (the panel shows
    # the checklist). _runtime_workspace_root still 404s an unknown project.
    root = _runtime_workspace_root(project_id)
    if root is None:
        return {
            "resolved": False, "runnable": False, "reason": "no_worktree",
            "plan": None, "session": None,
            "looked_for": ["a worktree with project files (none exists yet)"],
        }

    head = _runtime_workspace_head(project_id)
    rstore = _runtime_store(project_id)
    outcome = resolve_launch_plan(root, head, rstore, project_id)
    if isinstance(outcome, Unresolved):
        return {
            "resolved": False, "runnable": False, "reason": "unresolved",
            "plan": None, "session": None, "looked_for": outcome.looked_for,
        }

    plan = outcome
    launcher = get_launcher(plan.modality)
    if launcher is None:
        # Defensive: the S1 resolver only maps modalities that have a launcher,
        # so this is unreachable today. Report honestly rather than 500.
        return {
            "resolved": True, "runnable": False, "reason": "launcher_unavailable",
            "plan": plan.to_dict(), "session": None, "looked_for": [],
        }

    # F101-03 S5 — host/residency matrix: a modality this host can't run (no
    # display for a GUI, a foreign-arch binary, a remote host) is refused with a
    # structured reason, never executed. Shown in the preview and enforced on
    # confirm.
    host = HostFacts.local()
    host_ok, host_reason = can_launch(plan, host)
    if not host_ok:
        return {
            "resolved": True, "runnable": False, "reason": host_reason,
            "plan": plan.to_dict(), "host": host.to_dict(),
            "session": None, "looked_for": [],
        }

    # F101-03 S3 — trust tier T2 (consent-gated reduced isolation): a desktop app
    # reaches T2 (runs without an OS sandbox) when its profile resolves to no
    # sandbox — either ``sandbox="none"`` or ``sandbox="auto"`` on a host with no
    # windowing sandbox. That widening requires a SECOND explicit consent
    # (confirm_reduced_isolation) — mirrors F101 D2's logged bare-child fallback.
    needs_reduced_consent = (
        plan.modality == "desktop" and _desktop_reaches_reduced_isolation(plan))

    if not confirm:
        # Preview only — the consent step. No execution.
        return {
            "resolved": True, "runnable": True, "reason": None,
            "plan": plan.to_dict(), "host": host.to_dict(),
            "requires_reduced_isolation_consent": needs_reduced_consent,
            "session": None, "looked_for": [],
        }

    if needs_reduced_consent and not confirm_reduced:
        return {
            "resolved": True, "runnable": False,
            "reason": "reduced_isolation_consent_required",
            "plan": plan.to_dict(), "host": host.to_dict(),
            "requires_reduced_isolation_consent": True,
            "session": None, "looked_for": [],
        }

    # Execute. Persist a detector-grounded profile so the manager (which reads
    # profiles from the store) can start it — the same detect -> save -> start the
    # manual flow does, collapsed into one grounded action. A profile-grounded
    # plan is already stored, so no rewrite.
    if plan.source_profile is not None and plan.grounded_by != "profile":
        rstore.upsert_profile(plan.source_profile)
    mgr = _runtime_manager(project_id)
    try:
        session = launcher.launch(mgr, plan)
    except Exception as exc:
        raise _runtime_op_error(exc) from exc
    return {
        "resolved": True, "runnable": True, "reason": None,
        "plan": plan.to_dict(), "session": session.to_dict(), "looked_for": [],
    }


# --- F101 S3 — managed-local runtime execution (sandboxed) ----------------- #
def _runtime_manager(project_id: str):
    from errorta_council.coding.runtime_process import (
        RuntimeProcessError,
        RuntimeProcessManager,
    )
    _project_store_or_404(project_id)  # 404 if the project is unknown
    try:
        return RuntimeProcessManager.for_project(project_id)
    except RuntimeProcessError as exc:
        if str(exc) == "no_worktree":
            raise HTTPException(status_code=409,
                                detail="no worktree for this project yet") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _runtime_op_error(exc: Exception) -> HTTPException:
    from errorta_council.coding.runtime_process import RuntimeProcessError
    if isinstance(exc, RuntimeProcessError) and str(exc) == "profile_not_found":
        return HTTPException(status_code=404, detail="profile not found")
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/projects/{project_id}/runtime/{profile_id}/setup")
def post_runtime_setup(
    project_id: str, profile_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    _require_tauri_origin(request)
    # Setup runs install commands (e.g. npm install runs arbitrary postinstall
    # scripts) — require an explicit confirm, mirroring the worktree-accept gate.
    if not bool(body.get("confirm")):
        raise HTTPException(status_code=400, detail="setup_requires_confirm")
    mgr = _runtime_manager(project_id)
    try:
        session = mgr.setup(profile_id)
    except Exception as exc:
        raise _runtime_op_error(exc) from exc
    return {"session": session.to_dict()}


@router.post("/projects/{project_id}/runtime/{profile_id}/start")
def post_runtime_start(
    project_id: str, profile_id: str, request: Request
) -> dict[str, Any]:
    _require_tauri_origin(request)
    mgr = _runtime_manager(project_id)
    try:
        # The raw Start button keeps setup an explicit step (auto_setup=False):
        # only the confirm-gated Run front door (/runtime/run) auto-installs, so
        # install commands (e.g. npm postinstall) never run from a bare, un-gated
        # click. A venv-pending Start blocks with setup_required, steering the user
        # to "Run setup" / "Run".
        session = mgr.start(profile_id, auto_setup=False)
    except Exception as exc:
        raise _runtime_op_error(exc) from exc
    return {"session": session.to_dict()}


@router.post("/projects/{project_id}/runtime/{profile_id}/run-cli")
def post_runtime_run_cli(
    project_id: str, profile_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """F101-02 — run a CLI/script profile once as a time-boxed transcript run
    (the CLI analog of "open the demo in a browser"). Optional ``extra_args`` is
    parsed argv-style (``shlex.split``, no shell) and appended to ``start``;
    optional ``timeout_seconds`` overrides the per-profile time-box (clamped
    1..600). Returns the (initial) ``RuntimeSession``; the panel polls the
    existing sessions + logs routes for the terminal transcript.

    Like every runtime mutation it carries the Tauri-origin guard but is NOT
    refuse_local_dataplane_if_remote (running a generated project locally is a
    different data plane than AIAR-corpus writes; allowed under remote AIAR
    residency). There is deliberately no /mobile/v1 analog — runtime execution
    is desktop/loopback only.
    """
    import shlex

    _require_tauri_origin(request)
    # Accept either "extra_args" (spec/plan) or the alias "args".
    raw_args = body.get("extra_args", body.get("args"))
    arg_tokens: list[str] = []
    if raw_args not in (None, ""):
        if not isinstance(raw_args, str):
            raise HTTPException(status_code=422, detail="extra_args must be a string")
        if len(raw_args) > 4096:
            raise HTTPException(status_code=422, detail="extra_args too long")
        try:
            arg_tokens = shlex.split(raw_args, posix=True)
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail=f"invalid extra_args: {exc}") from exc
        if len(arg_tokens) > 64:
            raise HTTPException(status_code=422, detail="too many extra_args tokens")

    timeout_seconds = body.get("timeout_seconds")
    if timeout_seconds is not None:
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422,
                                detail="timeout_seconds must be a number") from exc

    mgr = _runtime_manager(project_id)
    try:
        # Raw CLI run keeps setup explicit (auto_setup=False), like the Start
        # route — only the confirm-gated Run front door auto-installs deps.
        session = mgr.run_cli(profile_id, args=arg_tokens,
                              timeout_seconds=timeout_seconds, auto_setup=False)
    except Exception as exc:
        raise _runtime_op_error(exc) from exc
    return {"session": session.to_dict()}


@router.post("/projects/{project_id}/runtime/{profile_id}/stop")
def post_runtime_stop(
    project_id: str, profile_id: str, request: Request
) -> dict[str, Any]:
    _require_tauri_origin(request)
    mgr = _runtime_manager(project_id)
    mgr.stop(profile_id)
    return {"stopped": True}


@router.post("/projects/{project_id}/runtime/{profile_id}/screenshot")
def post_runtime_screenshot(
    project_id: str, profile_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """F101-03 S2 — on-demand capture of a live windowed session's OWN window.

    Best-effort: returns ``{screenshot_ref: null}`` (an honest "no screenshot")
    when window capture isn't available on this host (no display / no Quartz /
    non-macOS), never an error. ``session_id`` may be given; otherwise the most
    recent live session for the profile is used."""
    _require_tauri_origin(request)
    mgr = _runtime_manager(project_id)
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        live = [s for s in mgr.rstore.list_sessions()
                if s.profile_id == profile_id
                and s.state in ("starting", "running", "healthy", "unhealthy")]
        if live:
            session_id = live[-1].session_id
    if not session_id or mgr.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="no live session")
    ref = mgr.capture_screenshot(session_id)
    return {"screenshot_ref": ref, "session_id": session_id}


@router.get("/projects/{project_id}/runtime/sessions/{session_id}")
def get_runtime_session(project_id: str, session_id: str) -> dict[str, Any]:
    mgr = _runtime_manager(project_id)
    session = mgr.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": session.to_dict()}


@router.get("/projects/{project_id}/runtime/sessions/{session_id}/logs")
def get_runtime_session_logs(project_id: str, session_id: str) -> dict[str, Any]:
    mgr = _runtime_manager(project_id)
    if mgr.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return mgr.get_logs(session_id)


@router.post("/projects/{project_id}/runtime/{profile_id}/health-check")
def post_runtime_health_check(
    project_id: str, profile_id: str, request: Request
) -> dict[str, Any]:
    _require_tauri_origin(request)
    mgr = _runtime_manager(project_id)
    try:
        status = mgr.health_check(profile_id)
    except Exception as exc:
        raise _runtime_op_error(exc) from exc
    return {"health_status": status}


def _runtime_workspace_head(project_id: str) -> str:
    """The current master worktree head, for binding runtime evidence (F101 S4).
    Empty string if unavailable — evidence then can never be 'fresh'."""
    from errorta_council.coding.workspace import CodingWorkspace
    try:
        store = LedgerStore(project_id)
        ws = CodingWorkspace(project_id, store)
        ws.set_target(store.get_project().target)
        return ws.head() if ws.exists() else ""
    except Exception:
        return ""


@router.post("/projects/{project_id}/runtime/{profile_id}/test")
def post_runtime_test(
    project_id: str, profile_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding.runtime import RUNTIME_TEST_KINDS
    from errorta_council.coding.testing import run_runtime_test
    kind = str(body.get("kind", ""))
    if kind not in RUNTIME_TEST_KINDS:
        raise HTTPException(status_code=422,
                            detail=f"kind must be one of {list(RUNTIME_TEST_KINDS)}")
    mgr = _runtime_manager(project_id)
    head = _runtime_workspace_head(project_id)
    try:
        result = run_runtime_test(mgr, profile_id, kind, head=head)
    except Exception as exc:
        raise _runtime_op_error(exc) from exc
    # Bind the verdict to profile id + session id + the current head (the F087-10
    # staleness rule). Runtime evidence is a WARN surface (D5) — recorded, not a
    # merge blocker.
    mgr.rstore.record_runtime_test(
        kind=result.kind, profile_id=result.profile_id,
        session_id=result.session_id, passed=result.passed, head=head,
        detail=result.detail)
    return {"result": result.to_dict()}


@router.post("/projects/{project_id}/runtime/{profile_id}/repair")
def post_runtime_repair(
    project_id: str, profile_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """F101 S5 — turn a failed runtime into a Coding Team dev task carrying the
    profile commands + last session outcome + a redacted log tail (so the dev
    can fix it and the reviewer can vet the runtime commands for safety)."""
    _require_tauri_origin(request)
    # Creates a coding-ledger task — same residency posture as POST /tasks.
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/runtime/{profile_id}/repair")
    from errorta_council.coding.runtime import build_repair_brief
    store = _project_store_or_404(project_id)
    mgr = _runtime_manager(project_id)
    profile = mgr.rstore.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    # Bind the named session, else the most recent session for this profile.
    session = None
    requested_sid = body.get("session_id")
    if requested_sid:
        session = mgr.get_session(str(requested_sid))
    else:
        candidates = [s for s in mgr.rstore.list_sessions()
                      if s.profile_id == profile_id]
        session = candidates[-1] if candidates else None
    log_lines = mgr.get_logs(session.session_id)["lines"] if session else []
    title, detail = build_repair_brief(
        profile=profile, session=session, log_lines=log_lines)
    task = store.add_task(title=title, role="dev", detail=detail)
    return {"task": task.to_dict()}


def _workspace(project_id: str):
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    try:
        proj = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    ws = CodingWorkspace(project_id, store)
    ws.set_target(proj.target)
    if not ws.exists():
        raise HTTPException(status_code=409, detail="no worktree for this project yet")
    return ws


@router.get("/projects/{project_id}/worktree")
def get_worktree_preview(project_id: str) -> dict[str, Any]:
    # F087-13 WS-1: the preview now carries the structured per-file diff AND the
    # evidence merge gate (blockers for open tasks / unreviewed / failing tests /
    # conflicts), so the UI can render a readable diff and refuse to enable
    # Accept on incomplete work.
    from errorta_council.coding.evidence import merge_review
    ws = _workspace(project_id)
    preview = ws.preview()
    review = merge_review(LedgerStore(project_id), ws)
    # F104 S5: surface the spec-conformance signal (implementer_grounded) so the
    # merge view can show whether the implementer saw the bound corpus's facts.
    return {**preview, "file_diffs": review["file_diffs"], "gate": review["gate"],
            "grounding": review.get("grounding")}


@router.get("/projects/{project_id}/files")
def get_project_file(project_id: str, path: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    # Coding worktrees are always local apply workspaces; SSH/remote residency
    # only moves AIAR data, so do not reject this local source read in remote mode.
    ws = _workspace(project_id)
    try:
        raw = ws.read_master_file(path)
    except ApplyWorkspaceError as exc:
        raise HTTPException(status_code=400, detail="bad_path") from exc
    if raw is None:
        raise HTTPException(status_code=404, detail={"reason": "not_on_master"})

    original_bytes = len(raw)
    cap = 256 * 1024
    truncated = original_bytes > cap
    shown = raw[:cap]
    content: str | None
    if b"\x00" in shown:
        encoding = "binary"
        content = None
    else:
        try:
            content = shown.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "binary"
            content = None

    out: dict[str, Any] = {
        "path": path,
        "content": content,
        "truncated": truncated,
        "encoding": encoding,
        "bytes": original_bytes,
        "on_master": True,
    }
    # F105: optimistic-concurrency token for the editor. SHA-256 over the FULL raw
    # blob (not the capped display bytes), only for text content the editor can
    # round-trip. The PUT route compares against this exact value.
    if encoding == "utf-8" and content is not None:
        import hashlib
        out["content_sha256"] = hashlib.sha256(raw).hexdigest()
    return out


@router.put("/projects/{project_id}/files")
def put_project_file(project_id: str, path: str, body: _UpdateProjectFile,
                     request: Request) -> dict[str, Any]:
    """F105: save a human edit to a merged text file on the internal ``master``.

    Tauri-origin + loopback only (never under /mobile/v1). Optimistic-concurrency
    via ``expected_sha256``; rejected while a coding run is active (the runner
    owns the worktree then). Fail-closed at every step; writes via git plumbing
    so the shared working-tree branch is never switched mid-run."""
    import hashlib

    _require_tauri_origin(request)
    # Coding worktrees are always local apply workspaces; SSH/remote residency
    # only moves AIAR data, so do not reject this local source write in remote mode
    # (mirrors GET /files + F092's rationale).

    # (1) project exists + (2) a worktree exists for it.
    ws = _workspace(project_id)
    store = LedgerStore(project_id)

    # (3) reject while a run is active — the runner owns the worktree then. Use
    # the SAME authoritative reconciled run-state as GET /run / cancel.
    state = _reconcile_run_state(project_id, store)
    if state.get("status") == "running" and _thread_alive(project_id):
        raise HTTPException(
            status_code=409,
            detail={"reason": "run_active",
                    "message": "cannot edit files while a coding run is active"})

    # (4) safe pathspec (D3) — same helper read_master_file uses.
    from errorta_tools.runner.apply_workspace import _safe_rel_pathspec
    try:
        _safe_rel_pathspec(path)
    except ApplyWorkspaceError as exc:
        raise HTTPException(status_code=400, detail="bad_path") from exc

    # (5) read current master blob.
    try:
        raw = ws.read_master_file(path)
    except ApplyWorkspaceError as exc:
        raise HTTPException(status_code=400, detail="bad_path") from exc
    # (6) reject if absent on master.
    if raw is None:
        raise HTTPException(status_code=404, detail={"reason": "not_on_master"})

    cap = 256 * 1024
    # (7) reject if current is binary.
    if b"\x00" in raw:
        raise HTTPException(status_code=422, detail={"reason": "binary_file"})
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail={"reason": "binary_file"})
    # (8) reject if current exceeds the editable cap (viewer is read-only there).
    if len(raw) > cap:
        raise HTTPException(status_code=422, detail={"reason": "file_too_large"})

    # (9) optimistic concurrency — compare full current SHA vs expected.
    current_sha = hashlib.sha256(raw).hexdigest()
    if current_sha != body.expected_sha256:
        raise HTTPException(
            status_code=409,
            detail={"reason": "stale_file", "content_sha256": current_sha})

    new_bytes = body.content.encode("utf-8")
    # (10) reject if new content exceeds the editable cap.
    if len(new_bytes) > cap:
        raise HTTPException(status_code=422, detail={"reason": "content_too_large"})
    # (11) reject NUL bytes / invalid UTF-8 in the new content (str is already
    # UTF-8 by construction, but a NUL would smuggle a binary file in).
    if "\x00" in body.content:
        raise HTTPException(status_code=400, detail={"reason": "invalid_content"})

    # (12) write to master atomically via git plumbing (no checkout switch).
    head = ws.write_master_file(path, body.content)
    new_sha = hashlib.sha256(new_bytes).hexdigest()

    # (13) update the artifact index so the Files panel + on-master view point at
    # the new content hash.
    store.upsert_artifact(
        path=path, status="modified", last_task_id="human-edit",
        content_sha256=new_sha, summary="human edit")

    # (14) ledger event — surfaces in the decision log + Team Log (D2/D5). path /
    # content_sha256 / head land as top-level fields via record_decision(extra=).
    store.record_decision(
        title=f"human edited file: {path}", context=f"project {project_id}",
        choice="human_file_edit",
        rationale=f"bytes={len(body.content)} sha256={new_sha}",
        related_task_ids=[],
        extra={"path": path, "content_sha256": new_sha, "head": head})

    return {
        "path": path,
        "content_sha256": new_sha,
        "bytes": len(new_bytes),
        "head": head,
        "on_master": True,
    }


@router.post("/projects/{project_id}/worktree/accept")
def accept_worktree(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/worktree/accept")
    if not bool(body.get("confirm")):
        raise HTTPException(status_code=400,
                            detail="merge-back requires confirm:true")
    # F087-13 WS-1: enforce the evidence gate. The merge-back is the last trust
    # boundary before the user's real files — refuse unreviewed / untested /
    # incomplete / conflicting work UNLESS the operator passes an explicit,
    # SEPARATE override:true (confirm:true alone can never bypass the gate).
    from errorta_council.coding.evidence import merge_review
    store = LedgerStore(project_id)
    ws = _workspace(project_id)
    review = merge_review(store, ws)
    gate = review["_gate"]
    override = bool(body.get("override"))
    if not gate.allowed and not override:
        raise HTTPException(
            status_code=409,
            detail={"error": "merge_gate_blocked", "gate": review["gate"]})
    if not gate.allowed and override:
        store.record_decision(
            title="merge gate overridden", context="merge-back",
            choice="merge_gate_override",
            rationale="operator merged despite blockers: "
                      + ", ".join(b.code for b in gate.blockers))
    # F105: re-validate the greenfield delivery root immediately before export
    # (the filesystem may have changed since create time) and refuse to overwrite
    # a non-empty destination. Do this BEFORE accept() mutates anything.
    proj = store.get_project()
    if proj.target != "existing":
        from errorta_council.coding.deliverable import deliverable_dir
        delivery_root = _validate_delivery_root(proj.delivery_root)
        # `deliver()` (export_master) creates the dir if absent and reuses it if
        # empty; refuse only a non-empty existing destination (never overwrite).
        dest = deliverable_dir(project_id, delivery_root)
        if dest.exists():
            try:
                non_empty = any(dest.iterdir())
            except OSError:
                non_empty = True
            if non_empty:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "delivery_destination_not_empty",
                            "delivery_dir": str(dest),
                            "message": "the planned delivery directory already "
                                       "exists and is not empty; delivery will "
                                       "not overwrite it"})
    else:
        delivery_root = None
    result = ws.accept(
        confirm=True, allow_conflicts=bool(body.get("allow_conflicts")))
    # F087-20: deliver the accepted MVP to a real, user-facing location and tell
    # the user where it is + how to run it. (existing target -> their repo;
    # new target -> a clean exported folder.)
    from errorta_council.coding.deliverable import deliver
    try:
        delivery = deliver(project_id, ws, target=proj.target,
                           repo_path=proj.repo_path, delivery_root=delivery_root)
        store.record_decision(
            title="delivered", context="merge-back", choice="delivered",
            rationale=f"delivered to {delivery['delivered_to']}")
    except Exception as exc:  # delivery is best-effort; the merge already happened
        delivery = {"delivered_to": "", "open_url": "", "run_hint": "",
                    "delivery_error": str(exc)}
    return {**result, **delivery}


@router.get("/projects/{project_id}/orientation")
def get_orientation(project_id: str, token_budget: int = 4000) -> dict[str, Any]:
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    return build_orientation_packet(store, token_budget=token_budget).to_dict()


@router.get("/projects/{project_id}/guardrail")
def get_guardrail(project_id: str) -> dict[str, Any]:
    return {"enabled": load_guardrail(LedgerStore(project_id)).enabled}


@router.put("/projects/{project_id}/guardrail")
def put_guardrail(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/guardrail")
    pol = save_guardrail(LedgerStore(project_id),
                         SkillsGuardrailPolicy(enabled=bool(body.get("enabled", True))))
    return {"enabled": pol.enabled}


@router.get("/projects/{project_id}/autonomy")
def get_autonomy(project_id: str) -> dict[str, Any]:
    return {"policy": policy_to_dict(load_policy(LedgerStore(project_id)))}


@router.put("/projects/{project_id}/autonomy")
def put_autonomy(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/autonomy")
    store = LedgerStore(project_id)
    try:
        store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    saved = save_policy(store, policy_from_dict(body))
    return {"policy": policy_to_dict(saved)}


@router.put("/projects/{project_id}/north-star")
def put_north_star(project_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/north-star")
    store = LedgerStore(project_id)
    try:
        proj = store.get_project()
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="project not found")
    raw = proj.to_dict()
    raw["north_star"] = str(body.get("north_star", raw["north_star"]))
    raw["definition_of_done"] = str(body.get("definition_of_done", raw["definition_of_done"]))
    raw["revision"] = int(raw.get("revision", 1)) + 1
    raw["updated_at"] = _now()
    _atomic_write_json(store._project_path, raw)
    return {"project": raw}


# ---------------------------------------------------------------------------
# F135 — import an existing project (GitHub clone / local folder), North Star
# inference, and the current-focus work_request directive.
# ---------------------------------------------------------------------------

# In-process background-job registries for the two long steps (clone, scan),
# mirroring the F095 grounding-bootstrap job pattern. Poll via the GET endpoints.
_IMPORT_JOBS: dict[str, dict[str, Any]] = {}
_SCAN_JOBS: dict[str, dict[str, Any]] = {}
_F135_JOBS_LOCK = threading.Lock()


_JOB_REGISTRY_CAP = 64


def _job_new(registry: dict[str, dict[str, Any]], **fields: Any) -> str:
    jid = uuid.uuid4().hex
    with _F135_JOBS_LOCK:
        # F135 review #6: bound the in-process registry — drop the oldest terminal
        # jobs first so a long-lived sidecar can't leak unbounded job records.
        if len(registry) >= _JOB_REGISTRY_CAP:
            terminal = [k for k, v in registry.items()
                        if v.get("status") in ("done", "error")]
            for k in terminal[: len(registry) - _JOB_REGISTRY_CAP + 1]:
                registry.pop(k, None)
        registry[jid] = {"job_id": jid, **fields}
    return jid


def _job_set(registry: dict[str, dict[str, Any]], jid: str, **patch: Any) -> None:
    with _F135_JOBS_LOCK:
        if jid in registry:
            registry[jid].update(patch)


def _job_get(registry: dict[str, dict[str, Any]], jid: str) -> dict[str, Any]:
    with _F135_JOBS_LOCK:
        return dict(registry.get(jid) or {})


def _reject_new_project_if_exists(project_id: str) -> LedgerStore:
    """Return a LedgerStore for a NOT-yet-existing project (409 if it exists,
    422 on a bad slug)."""
    try:
        store = LedgerStore(project_id)
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        store.get_project()
    except ProjectNotFound:
        return store
    raise HTTPException(status_code=409, detail="project already exists")


def _validate_import_folder(folder_path: Optional[str], *, require_git: bool) -> str:
    """F135: validate a user folder being imported as a project (a read boundary,
    and a write boundary when git_init runs). Mirrors _validate_repo_path's
    protected-root rules but does not require ``.git`` unless ``require_git``."""
    from pathlib import Path
    if not folder_path or not str(folder_path).strip():
        raise HTTPException(status_code=422, detail="folder_path required")
    try:
        real = Path(folder_path).expanduser().resolve()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid folder_path: {exc}") from exc
    if not real.is_dir():
        raise HTTPException(status_code=422, detail="folder_path is not a directory")
    if require_git and not (real / ".git").exists():
        raise HTTPException(status_code=422, detail="folder is not a git repository")
    home = Path.home().resolve()
    if real == home or real == Path(real.anchor) or str(real) == real.anchor:
        raise HTTPException(status_code=422, detail="folder_path too broad")
    denied: list[Path] = [
        Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/System"),
        Path("/Library"), Path("/private/etc"), Path("/var"), Path("/Applications"),
        Path("C:\\"), Path("C:\\Windows"), Path("C:\\Program Files"),
        Path("C:\\Program Files (x86)"), Path("C:\\ProgramData"),
    ]
    try:
        from errorta_app.paths import errorta_home
        denied.append(errorta_home().resolve())
    except Exception:
        pass
    for root in denied:
        try:
            if real == root or real.is_relative_to(root):
                raise HTTPException(
                    status_code=422,
                    detail=f"folder resolves under a protected root: {root}")
        except AttributeError:  # pragma: no cover - py<3.9
            if str(real).startswith(str(root)):
                raise HTTPException(status_code=422, detail="folder under protected root")
    try:
        if real.is_relative_to(home):
            parts = real.relative_to(home).parts
            if parts and parts[0].startswith("."):
                raise HTTPException(
                    status_code=422, detail="folder is inside a hidden home directory")
    except AttributeError:  # pragma: no cover
        pass
    return str(real)


def _connect_github_target(project_id: str, repo_path: str, owner: str, repo: str,
                           default_branch: str | None) -> None:
    """Populate the F102 existing_repo_pr connection target so the UI can show
    'Connected to owner/repo'. Best-effort — never breaks an import."""
    try:
        from errorta_council.coding.publish_ledger import PublishLedger
        ledger = PublishLedger(project_id)
        # F138 M-2: reuse an existing existing_repo_pr target (upsert matches by
        # target_id only), so a repeated connect (every pull-refresh) updates the
        # one target instead of appending a duplicate each time.
        existing = next(
            (t for t in ledger.list_targets() if t.kind == "existing_repo_pr"), None)
        ledger.upsert_target(
            kind="existing_repo_pr",
            target_id=existing.target_id if existing else None,
            repo_path=repo_path,
            github_owner=owner, github_repo=repo, default_branch=default_branch)
    except Exception:  # pragma: no cover - connection is advisory
        logging.getLogger(__name__).warning("F135: could not connect github target")


class _LocalImport(BaseModel):
    project_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    folder_path: str
    git_init: bool = False
    confirm: bool = False


class _GithubClone(BaseModel):
    project_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    repo_url: str
    ref: Optional[str] = None
    destination_root: Optional[str] = None
    shallow: bool = False


@router.post("/projects/import/local")
def import_local(body: _LocalImport, request: Request) -> dict[str, Any]:
    """F135 S1 — register a local folder as an existing-repo project. Offers an
    explicit ``git init`` (confirm-gated) for a non-git folder, and auto-connects
    a GitHub ``origin`` when one is present."""
    _require_tauri_origin(request)
    from pathlib import Path

    from errorta_tools.runner import publish as egress
    store = _reject_new_project_if_exists(body.project_id)
    real = _validate_import_folder(body.folder_path, require_git=False)
    has_git = (Path(real) / ".git").exists()
    kind = "local_folder"
    if not has_git:
        if not body.git_init:
            raise HTTPException(status_code=422,
                                detail={"error": "not_a_git_repo",
                                        "detail": "set git_init=true to initialize"})
        if not body.confirm:
            raise HTTPException(status_code=422, detail={"error": "confirm_required"})
        try:
            egress.git_init(real)
        except egress.PublishEgressError as exc:
            raise HTTPException(status_code=400, detail="git_init_failed") from exc
        kind = "local_folder_git_init"

    origin_url: str | None = None
    owner_repo = None
    try:
        r = egress._git_run(Path(real), "remote", "get-url", "origin")
        if r.returncode == 0:
            origin_url = (r.stdout or "").strip() or None
            owner_repo = egress.parse_github_origin(origin_url)
    except Exception:
        owner_repo = None
    head = egress.git_rev_parse_head(real)
    store.create_project(
        north_star="", definition_of_done="", target="existing",
        repo_path=real,
        import_source={"kind": kind,
                       "origin_url": origin_url if owner_repo else None,
                       "cloned_ref": head, "imported_at": _now()})
    if owner_repo:
        _connect_github_target(body.project_id, real, owner_repo[0], owner_repo[1],
                               egress.detect_default_branch(real))
    return {"project": _project_out(store)}


@router.get("/projects/import/github/auth-status")
def import_github_auth_status(request: Request) -> dict[str, Any]:
    """F135 — project-less GitHub auth detection for the import wizard (the F102
    per-project auth-status route 404s before a project exists). Never returns a
    token."""
    _require_tauri_origin(request)
    from errorta_tools.runner import github_secrets
    from errorta_tools.runner.publish import gh_auth_status
    status = gh_auth_status()
    return {
        "gh_present": bool(status.get("gh_present")),
        "login": status.get("login"),
        "token_in_keychain": github_secrets.has_token(),
    }


class _GithubBranches(BaseModel):
    repo_url: str


@router.post("/projects/import/github/branches")
def import_github_branches(body: _GithubBranches, request: Request) -> dict[str, Any]:
    """F141 WS-C — list a GitHub repo's branches WITHOUT cloning, so the import
    wizard can offer a branch dropdown. Reuses ``git ls-remote`` through the same
    ``gh`` credential helper ``git_clone`` uses (private repos work when authed).
    Never blocks the import: any failure returns ``{ok: false, error}`` (HTTP 200)
    so the UI falls back to the free-text branch field. No token is ever
    returned."""
    _require_tauri_origin(request)
    from errorta_tools.runner.publish import (
        PublishEgressError,
        list_remote_branches,
        parse_github_origin,
    )
    if parse_github_origin(body.repo_url) is None:
        return {"ok": False, "error": "invalid_repo_url"}
    try:
        result = list_remote_branches(body.repo_url)
    except PublishEgressError as exc:
        return {"ok": False, "error": str(exc)[:120]}
    except Exception:  # pragma: no cover - defensive; never block import
        return {"ok": False, "error": "branches_unavailable"}
    return {
        "ok": True,
        "branches": result["branches"],
        "default_branch": result["default_branch"],
    }


def _run_clone_job(job_id: str, project_id: str, repo_url: str,
                   ref: str | None, dest: str, shallow: bool) -> None:

    from errorta_tools.runner import publish as egress
    try:
        egress.git_clone(repo_url, dest, ref=ref, shallow=bool(shallow))
        _validate_import_folder(dest, require_git=True)  # re-validate the write result
        default_branch = egress.detect_default_branch(dest)
        head = egress.git_rev_parse_head(dest)
        owner_repo = egress.parse_github_origin(repo_url)
        cloned_ref = f"{default_branch}@{head}" if head else default_branch
        store = LedgerStore(project_id)
        # F135 review #5: re-check existence under the per-project lock before
        # create so two racing clone jobs for the same id can't double-create.
        with store.lock:
            try:
                store.get_project()
                _job_set(_IMPORT_JOBS, job_id, status="error",
                         message="project already exists")
                return
            except ProjectNotFound:
                pass
            store.create_project(
                north_star="", definition_of_done="", target="existing",
                repo_path=str(dest),
                import_source={"kind": "github_clone", "origin_url": repo_url,
                               "cloned_ref": cloned_ref, "imported_at": _now()})
        if owner_repo:
            _connect_github_target(project_id, str(dest), owner_repo[0],
                                   owner_repo[1], default_branch)
        _job_set(_IMPORT_JOBS, job_id, status="done", project_id=project_id)
    except HTTPException as exc:
        _job_set(_IMPORT_JOBS, job_id, status="error", message=str(exc.detail)[:200])
    except egress.PublishEgressError as exc:
        _job_set(_IMPORT_JOBS, job_id, status="error", message=str(exc)[:120])
    except Exception:  # pragma: no cover - defensive
        _job_set(_IMPORT_JOBS, job_id, status="error", message="import_failed")


@router.post("/projects/import/github/clone")
def import_github_clone(body: _GithubClone, request: Request) -> dict[str, Any]:
    """F135 S2 — start a background clone job. Returns ``{job_id, status}``; poll
    the status endpoint. ``gh``-authed (no token in the URL); destination
    validated; non-GitHub URL rejected."""
    _require_tauri_origin(request)
    from pathlib import Path

    from errorta_tools.runner.publish import get_gh_binary, parse_github_origin
    _reject_new_project_if_exists(body.project_id)
    owner_repo = parse_github_origin(body.repo_url)
    if owner_repo is None:
        raise HTTPException(status_code=400,
                            detail="repo_url must be a GitHub HTTPS/SSH URL")
    if get_gh_binary() is None:
        raise HTTPException(status_code=400, detail={"error": "gh_not_connected"})
    if body.destination_root:
        dest_root = Path(_validate_delivery_root(body.destination_root))
    else:
        dest_root = Path.home() / "Errorta Projects" / "_repos"
        dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{owner_repo[0]}__{owner_repo[1]}"
    if dest.exists() and any(dest.iterdir()):
        raise HTTPException(status_code=409,
                            detail="destination already exists and is not empty")
    jid = _job_new(_IMPORT_JOBS, status="cloning", message=None, project_id=None)
    threading.Thread(
        target=_run_clone_job,
        args=(jid, body.project_id, body.repo_url, body.ref, str(dest), body.shallow),
        daemon=True).start()
    return {"job_id": jid, "status": "cloning"}


@router.get("/projects/import/github/clone/{job_id}")
def import_github_clone_status(job_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    job = _job_get(_IMPORT_JOBS, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# --- North Star inference (orientation scan) ------------------------------ #


class _OrientationScan(BaseModel):
    route_id: Optional[str] = None


def _run_scan_job(job_id: str, project_id: str, member: dict[str, Any]) -> None:
    from errorta_council.coding.orientation_scan import (
        ScanError,
        run_orientation_scan,
    )
    try:
        store = LedgerStore(project_id)
        proj = store.get_project()
        from errorta_council.coding.runner import gateway_member_caller
        from errorta_council.gateway_local import LocalGateway
        caller = gateway_member_caller(LocalGateway())
        run_orientation_scan(store, member=member, caller=caller,
                             repo_path=proj.repo_path)
        _job_set(_SCAN_JOBS, job_id, status="done")
    except ScanError as exc:  # config/route problem — actionable reason
        _job_set(_SCAN_JOBS, job_id, status="error", message=f"scan_error:{exc.reason}")
    except Exception:  # a gateway/provider failure — redacted, no raw detail
        # F135 review #9: distinguish the failure class without echoing a raw
        # error (which could carry a path/token). The reason is enough to act on.
        _job_set(_SCAN_JOBS, job_id, status="error", message="scan_failed:gateway")


@router.post("/projects/{project_id}/orientation-scan")
def orientation_scan(project_id: str, body: _OrientationScan,
                     request: Request) -> dict[str, Any]:
    """F135 S4 — start a North Star inference job. Bounded, read-only; the single
    gateway call sends repo bytes off-box under remote residency, so it is
    residency-guarded. 409 while a run is live; 400 when no model route resolves."""
    _require_tauri_origin(request)
    # F135 review #4: the scan makes a real model call, so honor the alpha lock
    # like the run-start paths do.
    _alpha_enforce_not_locked()
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/orientation-scan")
    store = _project_store_or_404(project_id)
    if _thread_alive(project_id):
        raise HTTPException(status_code=409, detail="project run is still active")
    from errorta_council.coding.orientation_scan import ScanError, resolve_scan_member
    try:
        member = resolve_scan_member(store, route_id=body.route_id)
    except ScanError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": exc.reason,
                    "detail": "no saved team and no route_id given"}) from exc
    jid = _job_new(_SCAN_JOBS, status="scanning", message=None)
    threading.Thread(target=_run_scan_job, args=(jid, project_id, member),
                     daemon=True).start()
    return {"job_id": jid, "status": "scanning"}


@router.get("/projects/{project_id}/orientation-scan/{job_id}")
def orientation_scan_status(project_id: str, job_id: str,
                            request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    _project_store_or_404(project_id)
    job = _job_get(_SCAN_JOBS, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/projects/{project_id}/north-star-proposal")
def get_north_star_proposal(project_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    store = _project_store_or_404(project_id)
    proposal = store.get_orientation_proposal()
    if proposal is None:
        raise HTTPException(status_code=404, detail="no proposal")
    return {"proposal": proposal}


@router.post("/projects/{project_id}/north-star-proposal/accept")
def accept_north_star_proposal(project_id: str, request: Request) -> dict[str, Any]:
    """Promote the proposal's North Star + Definition of Done to authoritative
    (the human-accept gate). Marks the proposal accepted."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/north-star-proposal/accept")
    store = _project_store_or_404(project_id)
    # F135 review #3: accept promotes the run's North Star inputs, so it must not
    # run mid-flight — 409 like every other mutating route (D11).
    if _thread_alive(project_id):
        raise HTTPException(status_code=409, detail="project run is still active")
    proposal = store.get_orientation_proposal()
    if proposal is None:
        raise HTTPException(status_code=404, detail="no proposal")
    # F135 review #2: lock-held promotion so a concurrent completion/run-state
    # write can't lose-update project.json.
    proj = store.promote_north_star(
        str(proposal.get("north_star", "")),
        str(proposal.get("definition_of_done", "")))
    proposal["accepted"] = True
    proposal["accepted_at"] = _now()
    store.save_orientation_proposal(proposal)
    return {"project": proj.to_dict(), "proposal": proposal}


# --- current-focus directive (work_request) ------------------------------- #


class _WorkRequest(BaseModel):
    work_request: str = ""


@router.put("/projects/{project_id}/work-request")
def put_work_request(project_id: str, body: _WorkRequest,
                     request: Request) -> dict[str, Any]:
    """F135 S3 — set the current-focus directive. Persisted on the project and, if
    a run is live, delivered as a ``work_request``-tagged interjection that
    supersedes any prior unconsumed one."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/work-request")
    store = _project_store_or_404(project_id)
    proj = store.set_work_request(body.work_request or "")
    if _thread_alive(project_id):
        store.supersede_work_request_interjection(proj.work_request)
    return {"project": proj.to_dict()}


# --- F137: Current Focus goals (multi-item, lifecycle-managed) ------------- #


class _FocusCreate(BaseModel):
    title: str = Field(..., min_length=1)
    body: str = ""


class _FocusReorder(BaseModel):
    ordered_ids: list[str] = Field(default_factory=list)


class _FocusEdit(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[Literal["active", "completed", "archived"]] = None


def _deliver_focus_interjection(project_id: str, store: LedgerStore) -> None:
    """When a run is live, push the new active-focus set to the running PM as an
    authoritative ``current_focus`` interjection so it re-plans without a restart."""
    if not _thread_alive(project_id):
        return
    text = store.current_focus_directive_text() or (
        "Current Focus updated. No active focuses remain. Stop planning the "
        "previous focus and wait for new user direction; do not expand work to "
        "the whole North Star."
    )
    store.supersede_current_focus_interjection(text)


@router.get("/projects/{project_id}/focus")
def list_focus(project_id: str, request: Request,
               status: Literal["active", "completed", "archived", "all"] =
               "active") -> dict[str, Any]:
    """F137 — list Current Focus goals. ``status`` is one of active | completed |
    archived | all (default active). Runs the one-time legacy work_request
    migration on first read."""
    _require_tauri_origin(request)
    store = _project_store_or_404(project_id)
    want = None if status == "all" else status
    focuses = store.list_focuses(status=want)
    return {"focuses": [f.to_dict() for f in focuses]}


@router.post("/projects/{project_id}/focus")
def create_focus(project_id: str, body: _FocusCreate,
                 request: Request) -> dict[str, Any]:
    """F137 — add an active Current Focus (mid-run allowed: it's steering, not a
    gate). Delivers a ``current_focus`` interjection when a run is live."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/focus")
    store = _project_store_or_404(project_id)
    try:
        focus = store.add_focus(title=body.title, body=body.body or "")
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _deliver_focus_interjection(project_id, store)
    over_cap = len(store.active_focuses()) > LedgerStore._ACTIVE_FOCUS_SOFT_CAP
    return {"focus": focus.to_dict(), "over_soft_cap": over_cap}


@router.put("/projects/{project_id}/focus/reorder")
def reorder_focus(project_id: str, body: _FocusReorder,
                  request: Request) -> dict[str, Any]:
    """F137 — set the active-focus order (drives PM task/PR sequencing)."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(f"/coding/projects/{project_id}/focus/reorder")
    store = _project_store_or_404(project_id)
    focuses = store.reorder_focuses(list(body.ordered_ids or []))
    _deliver_focus_interjection(project_id, store)
    return {"focuses": [f.to_dict() for f in focuses]}


@router.put("/projects/{project_id}/focus/{focus_id}")
def edit_focus(project_id: str, focus_id: str, body: _FocusEdit,
               request: Request) -> dict[str, Any]:
    """F137 — edit a focus (title/body/status). A user may archive a focus
    directly by setting status=archived (dropping it)."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/focus/{focus_id}")
    store = _project_store_or_404(project_id)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=422, detail="no fields to update")
    try:
        focus = store.update_focus(focus_id, **patch)
    except FocusNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FocusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _deliver_focus_interjection(project_id, store)
    return {"focus": focus.to_dict()}


@router.post("/projects/{project_id}/focus/{focus_id}/accept")
def accept_focus(project_id: str, focus_id: str,
                 request: Request) -> dict[str, Any]:
    """F137 — human-accept gate: archive a completed focus.
    Like the North-Star accept, this is a gate — refuse while a run is live so a
    concurrent PM turn can't race the archival."""
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(
        f"/coding/projects/{project_id}/focus/{focus_id}/accept")
    if _thread_alive(project_id):
        raise HTTPException(status_code=409, detail="project run is still active")
    store = _project_store_or_404(project_id)
    try:
        focus = store.accept_focus(focus_id)
    except FocusNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FocusTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"focus": focus.to_dict()}


# ---------------------------------------------------------------------------
# F138 — refresh an imported project from remote (fast-forward pull + re-seed)
# ---------------------------------------------------------------------------

_REFRESH_JOBS: dict[str, dict[str, Any]] = {}


def _ref_sha(cloned_ref: str) -> str:
    """Normalize an ``import_source.cloned_ref`` to just the sha (F138 H1): F135's
    GitHub clone stores ``"<branch>@<sha>"`` but a local import stores a bare
    ``"<sha>"``; ``git_rev_parse_head`` returns a bare sha. Compare normalized."""
    ref = (cloned_ref or "").strip()
    return ref.split("@", 1)[1] if "@" in ref else ref


def _run_refresh_job(job_id: str, project_id: str, pull: bool,
                     discard_workspace: bool) -> None:
    from pathlib import Path

    from errorta_council.coding.workspace import CodingWorkspace
    from errorta_tools.runner import publish as egress
    try:
        store = LedgerStore(project_id)
        proj = store.get_project()
        repo_path = proj.repo_path
        if not repo_path or not Path(repo_path).is_dir():
            _job_set(_REFRESH_JOBS, job_id, status="error", message="repo_path_missing")
            return
        # Serialize the git mutation + re-seed + import_source write against a
        # concurrent completion / another refresh on the same project (F138 L2).
        with store.lock:
            # F138 H-1: a run can START between the route's 409 check and this job
            # acquiring the lock (POST /run does its whole start under the same
            # store.lock). Re-check liveness HERE so a re-seed can never rmtree a
            # live run's worktrees out from under it. `_thread_alive` reads the
            # `_RUNS` registry POST /run populates under the lock, so by now a
            # just-started run is visible.
            if _thread_alive(project_id):
                _job_set(_REFRESH_JOBS, job_id, status="error", message="run_active")
                return
            ws = CodingWorkspace(project_id, store)
            # Gate before fetch/fast-forward so a late-discovered workspace change
            # cannot leave the user's repo advanced while the snapshot stays stale.
            if ws.has_unaccepted_changes() and not discard_workspace:
                _job_set(_REFRESH_JOBS, job_id, status="error",
                         message="unaccepted_changes")
                return
            remote_pulled = False
            default_branch: str | None = None
            if pull and egress.has_origin(repo_path):
                # Dirty / detached / mid-rebase only block the PULL (a fast-forward
                # needs a clean tree on the default branch). A local re-seed just
                # copies the current working tree, so it tolerates all of these.
                status = egress.target_repo_status(repo_path)
                if status.get("in_progress"):
                    _job_set(_REFRESH_JOBS, job_id, status="error",
                             message="repo_rebase_in_progress")
                    return
                if status.get("detached"):
                    _job_set(_REFRESH_JOBS, job_id, status="error", message="repo_detached")
                    return
                if not status.get("clean"):
                    _job_set(_REFRESH_JOBS, job_id, status="error", message="repo_dirty")
                    return
                egress.git_fetch(repo_path, unshallow=egress.git_is_shallow(repo_path))
                # ``fetch`` leaves origin/HEAD stale when the remote's default
                # branch changes. Refresh it before validating the checked-out
                # branch or updating the F102 publish target.
                egress.git_refresh_remote_head(repo_path)
                default_branch = egress.detect_default_branch(repo_path)
                current = egress.git_current_branch(repo_path)
                if current != default_branch:
                    _job_set(_REFRESH_JOBS, job_id, status="error",
                             message="not_on_default_branch",
                             detail={"current": current, "default": default_branch})
                    return
                ab = egress.git_ahead_behind(repo_path, "HEAD", f"origin/{default_branch}")
                if ab is not None and ab[0] > 0:
                    _job_set(_REFRESH_JOBS, job_id, status="error",
                             message="branch_diverged",
                             detail={"ahead": ab[0], "behind": ab[1]})
                    return
                if ab is None or ab[1] > 0:
                    egress.git_fast_forward(repo_path, f"origin/{default_branch}")
                    remote_pulled = True
                # M3: keep the F102 publish target's default branch consistent.
                new_default = egress.detect_default_branch(repo_path)
                r = egress._git_run(Path(repo_path), "remote", "get-url", "origin")
                owner_repo = egress.parse_github_origin(
                    (r.stdout or "").strip() if r.returncode == 0 else None)
                if owner_repo:
                    _connect_github_target(project_id, repo_path, owner_repo[0],
                                           owner_repo[1], new_default)
                default_branch = new_default

            ws.reseed(repo_path)

            head = egress.git_rev_parse_head(repo_path)
            branch = default_branch or egress.git_current_branch(repo_path)
            cloned_ref = f"{branch}@{head}" if (head and branch and branch != "HEAD") else head
            store.update_import_source({"cloned_ref": cloned_ref,
                                        "refreshed_at": _now(),
                                        "remote_pulled": remote_pulled})
        _job_set(_REFRESH_JOBS, job_id, status="done", remote_pulled=remote_pulled)
    except egress.PublishEgressError as exc:
        _job_set(_REFRESH_JOBS, job_id, status="error", message=str(exc)[:120])
    except Exception:  # pragma: no cover - defensive; no raw detail
        _job_set(_REFRESH_JOBS, job_id, status="error", message="refresh_failed")


class _RefreshRequest(BaseModel):
    pull: bool = True
    discard_workspace: bool = False


@router.post("/projects/{project_id}/refresh")
def refresh_project(project_id: str, body: _RefreshRequest,
                    request: Request) -> dict[str, Any]:
    """F138 — start a background refresh job: (optionally) fast-forward the imported
    repo to its remote default branch, then re-seed the Coding Team's snapshot.
    Tauri-origin guarded; 409 while a run is (or may be) live; not alpha-locked (a
    git op, no model call — matches import_github_clone)."""
    _require_tauri_origin(request)
    store = _project_store_or_404(project_id)
    proj = store.get_project()
    if proj.target != "existing":
        raise HTTPException(status_code=422,
                            detail="refresh applies only to imported (existing) projects")
    _validate_repo_path(proj.repo_path)  # 422 if the imported repo is gone/invalid
    state = _reconcile_run_state(project_id, store)
    if state.get("status") == "running" and _thread_alive(project_id):
        raise HTTPException(status_code=409, detail="project run is still active")
    jid = _job_new(_REFRESH_JOBS, status="refreshing", message=None,
                   remote_pulled=False, project_id=project_id)
    threading.Thread(
        target=_run_refresh_job,
        args=(jid, project_id, bool(body.pull), bool(body.discard_workspace)),
        daemon=True).start()
    return {"job_id": jid, "status": "refreshing"}


@router.get("/projects/{project_id}/refresh/{job_id}")
def refresh_project_status(project_id: str, job_id: str,
                           request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    _project_store_or_404(project_id)
    job = _job_get(_REFRESH_JOBS, job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/projects/{project_id}/refresh-preview")
def refresh_preview(project_id: str, request: Request) -> dict[str, Any]:
    """F138 — read-only staleness projection, fail-open (never 5xx). Reports whether
    the snapshot is behind the repo / the remote so the UI can show a badge without
    the user committing to a refresh."""
    from pathlib import Path

    from errorta_council.coding.workspace import CodingWorkspace
    from errorta_tools.runner import publish as egress
    _require_tauri_origin(request)
    store = _project_store_or_404(project_id)
    proj = store.get_project()
    repo_path = proj.repo_path or ""
    out: dict[str, Any] = {
        "target": proj.target,
        "repo_path_exists": bool(repo_path) and Path(repo_path).is_dir(),
        "snapshot_ref": _ref_sha(str((proj.import_source or {}).get("cloned_ref") or "")) or None,
        "repo_head": None, "repo_dirty": None, "repo_differs": None,
        "workspace_has_unaccepted_changes": False,
        "origin_present": False, "default_branch": None, "shallow": False,
        "local_ahead": None, "remote_ahead": None,
    }
    if proj.target != "existing" or not out["repo_path_exists"]:
        return {"preview": out}
    try:
        out["repo_head"] = egress.git_rev_parse_head(repo_path) or None
        out["repo_dirty"] = not egress.target_repo_status(repo_path).get("clean")
    except Exception:
        pass
    # Uncommitted edits do not move HEAD, but they still make the imported folder
    # differ from the snapshot and must surface the pre-run badge even when an old
    # project has no recorded snapshot ref.
    if out["repo_dirty"] is True:
        out["repo_differs"] = True
    elif out["snapshot_ref"] and out["repo_head"]:
        out["repo_differs"] = out["snapshot_ref"] != out["repo_head"]
    try:
        out["workspace_has_unaccepted_changes"] = (
            CodingWorkspace(project_id, store).has_unaccepted_changes())
    except Exception:
        out["workspace_has_unaccepted_changes"] = False
    try:  # remote is best-effort — offline / no-origin / shallow just leave it null
        if egress.has_origin(repo_path):
            out["origin_present"] = True
            out["shallow"] = egress.git_is_shallow(repo_path)
            out["default_branch"] = egress.detect_default_branch(repo_path)
            # F138 M-3: the preview must never hang the panel — bound the fetch to
            # 10s and DON'T `--unshallow` here (a potentially large download); a
            # shallow repo just reports ahead/behind as null (the real refresh job
            # unshallows). Fail-open on timeout/offline.
            if not out["shallow"]:
                egress.git_fetch(repo_path, timeout=10.0)
                egress.git_refresh_remote_head(repo_path, timeout=10.0)
                out["default_branch"] = egress.detect_default_branch(repo_path)
                comparison_ref = out["snapshot_ref"] or "HEAD"
                ab = egress.git_ahead_behind(
                    repo_path, comparison_ref, f"origin/{out['default_branch']}")
                if ab is not None:
                    out["local_ahead"], out["remote_ahead"] = ab[0], ab[1]
    except Exception:
        pass
    return {"preview": out}


# ---------------------------------------------------------------------------
# F102 — publishing (P1 manual export + P2 auth-status detection)
# ---------------------------------------------------------------------------


class _ManualExport(BaseModel):
    kind: str = Field(..., description="zip | patch | git_apply | open_folder")
    dest: Optional[str] = None


_MANUAL_EXPORT_KINDS = {"zip", "patch", "git_apply", "open_folder"}


def _publish_ledger(project_id: str):
    from errorta_council.coding.publish_ledger import PublishLedger
    return PublishLedger(project_id)


def _manual_export_target(project_id: str):
    """Find or create the project's single ``manual_export`` publish target so
    every manual-export event has a stable target_id."""
    ledger = _publish_ledger(project_id)
    for t in ledger.list_targets():
        if t.kind == "manual_export":
            return ledger, t
    return ledger, ledger.upsert_target(kind="manual_export")


@router.get("/projects/{project_id}/publish/targets")
def list_publish_targets(project_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    _project_store_or_404(project_id)
    ledger = _publish_ledger(project_id)
    return {"targets": [t.to_dict() for t in ledger.list_targets()]}


@router.get("/projects/{project_id}/publish/events")
def list_publish_events(project_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    _project_store_or_404(project_id)
    ledger = _publish_ledger(project_id)
    return {"events": [e.to_dict() for e in ledger.list_events()]}


@router.post("/projects/{project_id}/publish/manual-export")
def manual_export(project_id: str, body: _ManualExport, request: Request) -> dict[str, Any]:
    """F102 P1 — no-auth manual export: a zip / patch / ``git apply`` command /
    open-folder hand-off. Always available; never touches GitHub. Records a
    redacted ``manual_export`` publish event."""
    _require_tauri_origin(request)
    kind = (body.kind or "").strip()
    if kind not in _MANUAL_EXPORT_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown manual-export kind: {kind!r}")

    # Coding worktrees are always local apply workspaces; SSH/remote residency
    # only moves AIAR data, so these local source operations stay allowed.
    store = _project_store_or_404(project_id)
    ws = _workspace(project_id)  # 409 if no worktree yet
    proj = store.get_project()

    from errorta_council.coding.deliverable import deliverable_dir, run_hint
    from errorta_tools.runner.publish import (
        PublishEgressError,
        build_patch,
        build_zip_export,
    )

    ledger, target = _manual_export_target(project_id)
    delivery_root = proj.delivery_root if proj.target != "existing" else None
    dest_dir = deliverable_dir(project_id, delivery_root)

    try:
        if kind == "zip":
            zip_path = dest_dir / f"{project_id}.zip"
            built = build_zip_export(ws.root(), zip_path, ref="master")
            ledger.append_event(
                target_id=target.target_id, kind="manual_export",
                state="committed", commit_sha=ws.head())
            return {"kind": "zip", "path": str(built),
                    "run_hint": f"unzip {built}"}

        if kind == "patch":
            patch_text = build_patch(ws.root(), ref="master")
            patch_path: str | None = None
            if patch_text.strip():
                dest_dir.mkdir(parents=True, exist_ok=True)
                p = dest_dir / f"{project_id}.patch"
                p.write_text(patch_text, encoding="utf-8")
                patch_path = str(p)
            ledger.append_event(
                target_id=target.target_id, kind="manual_export",
                state="committed", commit_sha=ws.head())
            return {"kind": "patch", "patch": patch_text, "path": patch_path}

        if kind == "git_apply":
            patch_text = build_patch(ws.root(), ref="master")
            dest_dir.mkdir(parents=True, exist_ok=True)
            p = dest_dir / f"{project_id}.patch"
            p.write_text(patch_text, encoding="utf-8")
            ledger.append_event(
                target_id=target.target_id, kind="manual_export",
                state="committed", commit_sha=ws.head())
            return {"kind": "git_apply", "path": str(p),
                    "command": f"git apply {p}"}

        # open_folder: materialize the master tree to the delivered folder.
        delivered = ws.export(str(dest_dir))
        ledger.append_event(
            target_id=target.target_id, kind="manual_export",
            state="committed", commit_sha=ws.head())
        return {"kind": "open_folder", "path": str(delivered),
                "run_hint": run_hint(delivered)}
    except (PublishEgressError, ApplyWorkspaceError) as exc:
        ledger.append_event(
            target_id=target.target_id, kind="manual_export",
            state="failed", error=str(exc))
        raise HTTPException(status_code=400, detail="manual_export_failed") from exc


@router.get("/projects/{project_id}/publish/auth-status")
def publish_auth_status(project_id: str, request: Request) -> dict[str, Any]:
    """F102 P2 — read-only auth detection. Reports whether ``gh`` is present, the
    logged-in GitHub login, and whether a device-flow token is in the OS
    keychain. NEVER returns a token."""
    _require_tauri_origin(request)
    _project_store_or_404(project_id)
    from errorta_tools.runner import github_secrets
    from errorta_tools.runner.publish import gh_auth_status
    status = gh_auth_status()
    return {
        "gh_present": bool(status.get("gh_present")),
        "login": status.get("login"),
        "token_in_keychain": github_secrets.has_token(),
    }


# --- P3/P4 GitHub publishing ---------------------------------------------- #


class _ExistingRepoPr(BaseModel):
    override: bool = False
    # F135: optional PM-drafted PR fields. When omitted, the deterministic F102
    # defaults (branch errorta/<id>, default title, generated body) are used.
    branch: Optional[str] = None
    title: Optional[str] = None
    body_override: Optional[str] = None


class _NewGithubRepo(BaseModel):
    repo_name: str
    private: bool = True
    local_only: bool = False
    override: bool = False


@router.post("/projects/{project_id}/publish/existing-repo-pr")
def publish_existing_repo_pr(
    project_id: str, body: _ExistingRepoPr, request: Request,
) -> dict[str, Any]:
    """F102 P3 — branch + commit + push the accepted changes + open a PR on the
    user's existing repo. Tauri-origin only. Gated on delivered + no open tasks;
    refuses to clobber unrelated user changes; never direct-pushes the default
    branch; blocks (409) on a secret-scan hit unless ``override:true``."""
    _require_tauri_origin(request)
    store = _project_store_or_404(project_id)
    ws = _workspace(project_id)  # 409 if no worktree yet
    from errorta_council.coding.publish_github import (
        PublishGateError,
    )
    from errorta_council.coding.publish_github import (
        publish_existing_repo_pr as _orchestrate,
    )
    try:
        return _orchestrate(
            store, ws, override=bool(body.override),
            branch=body.branch, title=body.title, body_override=body.body_override)
    except PublishGateError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"error": exc.reason, "detail": exc.detail}) from exc


@router.post("/projects/{project_id}/publish/new-github-repo")
def publish_new_github_repo(
    project_id: str, body: _NewGithubRepo, request: Request,
) -> dict[str, Any]:
    """F102 P4 — create a new (private-by-default) GitHub repo from the delivered
    tree + push an initial commit, or stop at a local git repo when
    ``local_only:true``. Tauri-origin only. Gated on delivered + no open tasks;
    blocks (409) on a secret-scan hit unless ``override:true``."""
    _require_tauri_origin(request)
    store = _project_store_or_404(project_id)
    ws = _workspace(project_id)  # 409 if no worktree yet
    from errorta_council.coding.publish_github import (
        PublishGateError,
    )
    from errorta_council.coding.publish_github import (
        publish_new_github_repo as _orchestrate,
    )
    try:
        return _orchestrate(
            store, ws, repo_name=body.repo_name, private=bool(body.private),
            local_only=bool(body.local_only), override=bool(body.override))
    except PublishGateError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"error": exc.reason, "detail": exc.detail}) from exc
