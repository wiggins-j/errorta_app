"""F088-04 (durable truth) + F088-05 (WIP) — project memory ingestion.

A deterministic projection from the F087 ledger into the F088-02 memory store.
The ledger remains the single source of truth; every memory row is derived,
idempotent (stable ``memory_id``), and carries full provenance. Authority is
assigned by *evidence*, never by tone:

* PM decisions and reviewed+tested+merged PRs  -> ``durable_truth``
* open PRs / in-flight tasks / failures / findings / blockers / conflicts -> ``wip``
* raw dev/reviewer/tester prose                -> ``claim`` (audit only, excluded
  from default retrieval)

No raw model prose is ever promoted to durable truth, and WIP never outranks
merged master truth (the store ranks ``durable_truth`` before ``wip``).
"""
from __future__ import annotations

import json
from typing import Any

from .memory_store import (
    InvalidMemoryItem,
    MemoryItem,
    MemoryQuery,
    MemorySourceRef,
    MemoryVisibility,
    ProjectMemoryStore,
)
from .source_refs import freshness, is_sensitive_path, memory_id

# Decision choices the runner emits (see runner.py). Only PM decisions are
# durable; rejected/uncertain prose is a claim.
_REJECTED_CHOICES = {
    "dev_turn_rejected",
    "reviewer_turn_rejected",
    "pm_turn_rejected",
    "review_rejected",
    "stale_review_head",
}
_LIVE_PR_STATES = {"open", "changes_requested", "mergeable", "conflict"}
_TERMINAL_PR_STATES = {"merged", "abandoned"}
_CONTENT_CAP = 4000


def _cap(text: object) -> str:
    return str(text or "")[:_CONTENT_CAP]


class MemoryIngestor:
    """Projects one project's F087 ledger into its ProjectMemoryStore.

    All methods are idempotent and return the number of rows written. Construct
    with the live ``LedgerStore`` so the memory store binds to the same project
    directory; pass a ``workspace`` only for repo-backed chunk promotion.
    """

    def __init__(self, ledger: Any, *, memory: ProjectMemoryStore | None = None,
                 workspace: Any = None) -> None:
        self.ledger = ledger
        self.workspace = workspace
        self.memory = memory or ProjectMemoryStore(
            ledger.project_id, root=ledger.dir.parent
        )
        self.project_id = ledger.project_id

    # --- helpers -----------------------------------------------------------
    def _put(self, *, mem_id: str, authority: str, source_type: str,
             source_ref: MemorySourceRef, content: str,
             summary: str | None = None, source_ids: tuple[str, ...] = (),
             head: str | None = None, metadata: dict[str, Any] | None = None,
             visibility: MemoryVisibility | None = None) -> bool:
        item = MemoryItem(
            project_id=self.project_id,
            authority=authority,
            source_type=source_type,
            source_ref=source_ref,
            content=content,
            memory_id=mem_id,
            summary=summary,
            source_ids=source_ids,
            freshness=freshness(head),
            visibility=visibility or MemoryVisibility(),
            metadata=metadata or {},
        )
        try:
            self.memory.put(item)
            return True
        except InvalidMemoryItem:
            # Fail closed on a single bad row; never abort the whole sync.
            return False

    # --- F088-04: durable truth --------------------------------------------
    def admit_pm_decisions(self) -> int:
        """PM decisions (``choice="pm_decision"``) -> ``durable_truth``."""
        n = 0
        for d in self.ledger.list_decisions():
            if d.get("choice") != "pm_decision":
                continue
            tasks = d.get("related_task_ids") or []
            ref = MemorySourceRef(task_id=(tasks[0] if tasks else "plan"))
            content = _cap(f"{d.get('title', '')}: {d.get('rationale', '')}".strip(": "))
            if not content:
                continue
            if self._put(
                mem_id=memory_id("pmdecision", d.get("decision_id", "")),
                authority="durable_truth", source_type="pm_decision",
                source_ref=ref, content=content,
                metadata={"decision_id": d.get("decision_id", "")},
            ):
                n += 1
        return n

    def admit_pm_working_memory(self) -> int:
        """The PM's durable cross-turn working memory -> ``durable_truth``.

        This row is derived from the ledger, overwritten idempotently, and
        visible only to the PM by default.
        """
        from .pm_working_memory import (
            SCHEMA_VERSION,
            SOURCE_TYPE,
            build_pm_working_memory_snapshot,
            summarize_pm_working_memory,
        )

        snapshot = build_pm_working_memory_snapshot(self.ledger)
        corpus_id = self._bound_corpus_id()
        snapshot.setdefault("freshness", {})["bound_corpus_id"] = corpus_id
        content = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        return int(
            self._put(
                mem_id=memory_id(SOURCE_TYPE, self.project_id),
                authority="durable_truth",
                source_type=SOURCE_TYPE,
                source_ref=MemorySourceRef(task_id="pm", corpus_id=corpus_id),
                content=content,
                summary=summarize_pm_working_memory(snapshot),
                metadata={
                    "schema_version": SCHEMA_VERSION,
                    "generated_at": snapshot.get("generated_at"),
                    "aiar_mirror_status": "not_attempted",
                    "warning_count": len(snapshot.get("warnings") or []),
                },
                visibility=MemoryVisibility(
                    default_pm=True,
                    default_dev=False,
                    default_reviewer=False,
                    default_tester=False,
                ),
            )
        )

    def promote_merged_prs(self) -> int:
        """Reviewed + tested + merged PRs -> durable code-chunk anchors + test
        evidence + a merge episode whose ``source_ids`` link its evidence."""
        n = 0
        prs = self.ledger.list_prs()
        merged = [p for p in prs if self._is_evidence_backed_merge(p)]
        tasks = self.ledger.list_tasks()
        artifacts = self.ledger.list_artifacts()
        test_runs = self.ledger.list_test_runs()
        episodes = self.ledger.list_episodes()
        binding_corpus = self._bound_corpus_id()
        for pr in merged:
            task_id = pr.get("task_id", "")
            head = str(pr.get("head") or "")
            tested_head = str(pr.get("tested_head") or "")
            pr_id = pr.get("pr_id", "")
            evidence_task_ids = self._pr_evidence_task_ids(pr, tasks)
            passing_runs = [
                r for r in test_runs
                if r.get("task_id") in evidence_task_ids
                and r.get("passed")
                and str(r.get("head") or "") == tested_head
            ]
            if not passing_runs:
                continue
            chunk_ids: list[str] = []
            # touched files for THIS merge = artifacts last written by its task;
            # path-based, never task-title-based (F088-04 risk note).
            for art in artifacts:
                if art.get("last_task_id") != task_id:
                    continue
                path = str(art.get("path") or "")
                if not path or is_sensitive_path(path):
                    continue
                mem = memory_id("chunk", binding_corpus or "nocorpus", path, head)
                ref = MemorySourceRef(path=path, commit=head, head=head,
                                      corpus_id=binding_corpus, task_id=task_id)
                if self._put(
                    mem_id=mem, authority="durable_truth", source_type="code_chunk",
                    source_ref=ref,
                    content=_cap(f"merged file {path} @ {head[:12]}"),
                    head=head,
                    metadata={"sha256": art.get("content_sha256", ""),
                              "pr_id": pr_id},
                ):
                    chunk_ids.append(mem)
                    n += 1
            # test evidence: passing runs for this task bound to the merge head.
            ev_ids: list[str] = []
            for r in passing_runs:
                tr_id = r.get("test_run_id", "")
                mem = memory_id("testevidence", tr_id)
                ref = MemorySourceRef(test_run_id=tr_id, task_id=str(r.get("task_id") or task_id),
                                      head=str(r.get("head") or ""), pr_id=pr_id)
                if self._put(
                    mem_id=mem, authority="durable_truth", source_type="test_evidence",
                    source_ref=ref,
                    content=_cap("tests passed: " + ", ".join(r.get("command_ids") or [])
                                 + f" @ {str(r.get('head') or '')[:12]}"
                                 + (f" [{r.get('sandbox')}]" if r.get("sandbox") else "")),
                    head=str(r.get("head") or ""),
                    metadata={"sandbox": r.get("sandbox", "")},
                ):
                    ev_ids.append(mem)
                    n += 1
            # merge episode: derived summary, source_ids link its evidence.
            for ep in episodes:
                if task_id not in (ep.get("related_task_ids") or []):
                    continue
                mem = memory_id("episode", ep.get("episode_id", ""))
                ref = MemorySourceRef(pr_id=pr_id, task_id=task_id,
                                      head=str(ep.get("head") or ""),
                                      commit=str(ep.get("head") or ""))
                src_ids = tuple(chunk_ids + ev_ids)
                # A derived summary with no source_ids fails validation; only
                # promote the episode once it has linked evidence.
                if not src_ids:
                    continue
                if self._put(
                    mem_id=mem, authority="durable_truth", source_type="merge_episode",
                    source_ref=ref, content=_cap(ep.get("summary", "")),
                    summary=_cap(ep.get("title", "")), source_ids=src_ids,
                    head=str(ep.get("head") or ""),
                    metadata={"derived_summary": True,
                              "episode_id": ep.get("episode_id", "")},
                ):
                    n += 1
        return n

    # --- F088-05: WIP -------------------------------------------------------
    def index_wip(self) -> int:
        """Open PRs, in-flight tasks, failures, findings, blockers, conflicts ->
        ``wip``. Terminal PRs' WIP rows are superseded (lifecycle, F088-06)."""
        n = 0
        decisions = self.ledger.list_decisions()
        artifacts = self.ledger.list_artifacts()
        # open PRs / active branches + touched-file ownership
        for pr in self.ledger.list_prs():
            status = str(pr.get("status") or "")
            pr_id = pr.get("pr_id", "")
            task_id = pr.get("task_id", "")
            mem = memory_id("wip_pr", pr_id)
            if status in _TERMINAL_PR_STATES:
                self.memory.supersede(mem)  # no-op if absent
                self._retire_touched_files(pr_id)
                continue
            if status not in _LIVE_PR_STATES:
                continue
            ref = MemorySourceRef(pr_id=pr_id, task_id=task_id,
                                  head=str(pr.get("head") or ""))
            conflicts = pr.get("conflicts") or []
            if self._put(
                mem_id=mem, authority="wip", source_type="open_pr", source_ref=ref,
                content=_cap(f"PR {pr.get('branch', '')} (task {task_id}) "
                             f"status={status}"
                             + (f"; conflicts: {', '.join(conflicts)}" if conflicts else "")),
                head=str(pr.get("head") or ""),
                metadata={"status": status, "branch": pr.get("branch", ""),
                          "lower_authority": True},
            ):
                n += 1
            # F088-05: per-file ownership so overlap is discoverable by path —
            # a MemoryQuery(path=...) returns every active WIP touching that file.
            # The PR DIFF is the authoritative per-PR file set (the artifacts
            # index only records the latest writer of each path, so two PRs
            # touching one file collapse there); fall back to artifacts-by-task
            # only when no workspace/diff is available.
            for path in self._pr_touched_paths(pr, artifacts):
                if is_sensitive_path(path):
                    continue
                if self._put(
                    mem_id=memory_id("wip_file", pr_id, path), authority="wip",
                    source_type="touched_file",
                    source_ref=MemorySourceRef(path=path, pr_id=pr_id, task_id=task_id,
                                               head=str(pr.get("head") or "")),
                    content=_cap(f"{path} owned by PR {pr.get('branch', '')} "
                                 f"(task {task_id})"),
                    head=str(pr.get("head") or ""),
                    metadata={"branch": pr.get("branch", ""), "lower_authority": True},
                ):
                    n += 1
        # in-flight tasks + blockers
        for t in self.ledger.list_tasks():
            if t.state in ("todo", "doing"):
                kind = "task_in_flight"
            elif t.state == "blocked":
                kind = "blocker"
            else:
                continue
            mem = memory_id("wip_task", t.task_id)
            if self._put(
                mem_id=mem, authority="wip", source_type=kind,
                source_ref=MemorySourceRef(task_id=t.task_id),
                content=_cap(f"{t.role} task '{t.title}' state={t.state}"),
                metadata={"state": t.state, "role": t.role, "lower_authority": True},
            ):
                n += 1
        # failed tests
        for r in self.ledger.list_test_runs():
            if r.get("passed"):
                continue
            tr_id = r.get("test_run_id", "")
            mem = memory_id("wip_testfail", tr_id)
            if self._put(
                mem_id=mem, authority="wip", source_type="failed_test",
                source_ref=MemorySourceRef(test_run_id=tr_id, task_id=r.get("task_id", ""),
                                           head=str(r.get("head") or "")),
                content=_cap("tests FAILED: " + ", ".join(r.get("command_ids") or [])),
                head=str(r.get("head") or ""),
                metadata={"lower_authority": True},
            ):
                n += 1
        # review findings + conflicts (from decisions)
        for d in decisions:
            choice = d.get("choice")
            if choice in ("review_rejected", "stale_review_head"):
                kind = "review_finding"
            elif choice == "pr_conflict":
                kind = "merge_conflict"
            else:
                continue
            did = d.get("decision_id", "")
            mem = memory_id("wip_finding", did)
            ref = MemorySourceRef(pr_id=str(d.get("pr_id") or ""),
                                  head=str(d.get("reviewed_head") or ""),
                                  task_id=(d.get("related_task_ids") or [""])[0])
            if self._put(
                mem_id=mem, authority="wip", source_type=kind, source_ref=ref,
                content=_cap(f"{d.get('title', '')}: {d.get('rationale', '')}"),
                head=str(d.get("reviewed_head") or ""),
                metadata={"choice": choice, "lower_authority": True},
            ):
                n += 1
        return n

    # --- claims (audit only) -----------------------------------------------
    def admit_claims(self) -> int:
        """Raw dev/reviewer/tester prose -> ``claim`` (excluded from default
        retrieval). This is the boundary that keeps an unproven model assertion
        from ever reading back as project truth."""
        n = 0
        for trn in self.ledger.list_turns():
            role = trn.get("role", "")
            if role not in ("dev", "reviewer", "tester"):
                continue
            response = _cap(trn.get("response", ""))
            if not response.strip():
                continue
            turn_id = trn.get("turn_id", "")
            ref = MemorySourceRef(task_id=trn.get("task_id", "") or "plan")
            if self._put(
                mem_id=memory_id("claim_turn", turn_id),
                authority="claim", source_type=f"{role}_turn", source_ref=ref,
                content=response,
                metadata={"member_id": trn.get("member_id", ""),
                          "outcome": trn.get("outcome", ""), "turn_id": turn_id},
            ):
                n += 1
        return n

    # --- internal ----------------------------------------------------------
    def _pr_touched_paths(self, pr: dict[str, Any], artifacts: list) -> list[str]:
        """Files this PR touches. The PR diff is authoritative (per-PR); the
        artifacts index (latest-writer-per-path) is only a fallback."""
        branch = pr.get("branch") or ""
        if self.workspace is not None and branch:
            try:
                from errorta_council.coding.diff_review import parse_unified_diff
                diff = self.workspace.pr_diff(branch)
                paths = [fd.path for fd in parse_unified_diff(diff) if fd.path]
                if paths:
                    return paths
            except Exception:
                pass
        return [str(a["path"]) for a in artifacts
                if a.get("last_task_id") == pr.get("task_id") and a.get("path")]

    def _is_evidence_backed_merge(self, pr: dict[str, Any]) -> bool:
        if pr.get("status") != "merged":
            return False
        if pr.get("reviewer_approved") is not True or pr.get("tests_passed") is not True:
            return False
        reviewed_head = str(pr.get("reviewed_head") or "")
        tested_head = str(pr.get("tested_head") or "")
        return bool(reviewed_head and tested_head and reviewed_head == tested_head)

    def _pr_evidence_task_ids(self, pr: dict[str, Any], tasks: list[Any]) -> set[str]:
        pr_id = str(pr.get("pr_id") or "")
        ids = {str(pr.get("task_id") or "")}
        for task in tasks:
            if getattr(task, "pr_id", None) == pr_id:
                ids.add(str(task.task_id))
        return {task_id for task_id in ids if task_id}

    def _retire_touched_files(self, pr_id: str) -> None:
        """Supersede a terminal PR's per-file WIP ownership records."""
        try:
            owned = self.memory.query(MemoryQuery(authorities=("wip",),
                                                  source_type="touched_file", limit=500))
        except Exception:
            return
        for item in owned:
            if item.source_ref.pr_id == pr_id:
                self.memory.supersede(item.memory_id)

    def _bound_corpus_id(self) -> str | None:
        try:
            from .corpus_binding import load_binding
            b = load_binding(self.ledger)
            if b.mode in ("existing", "build_from_repo", "build_from_project") and b.corpus_id:
                return b.corpus_id
        except Exception:
            pass
        return None


__all__ = ["MemoryIngestor"]
