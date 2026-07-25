"""GL02 — grounded review: the two-lane invariant + the per-head veto cap.

Extends the committed SPEC-14 (grounded reviewer) with the INVARIANT its pieces
jointly imply and one decision:

* Items 1-2 (the two-lane invariant / the bar): review has a MACHINE lane (does it
  load / run / render — decided by the executor's gate + GL01 probe evidence, never
  the LLM) and a JUDGMENT lane (design / clarity / spec conformance — the only
  surface a diff can evidence). A blocking finding that reasons about runtime with
  NO executor evidence to back it may not bounce to a DEV who also cannot run it —
  it ROUTES. Backed by a red gate/probe it is a normal cited defect (actionable).
* Item 3 (the per-head veto cap): the same PR head rejected twice escalates the
  disagreement to the PM, composing UNDER Spec 16's depth-3 lineage cap.

The regression lock (Specs 14/15/16 green) lives in those specs' own test files;
here we assert the net-new behaviour and its composition with Spec 16.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import runner
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.testing import (
    TestRunResult as RunResult,
)
from errorta_council.coding.testing import (
    TestRunSession as RunSession,
)
from errorta_council.coding.workspace import CodingWorkspace


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"l-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _pr_and_review(s, ws, *, seed="src/mod.js"):
    dev_task = s.add_task(title="add module", role="dev")
    branch = ws.start_task_branch(dev_task.task_id)
    ws.write_file(seed, "export const x = 1\n", task_id=dev_task.task_id)
    pr = s.record_pr(task_id=dev_task.task_id, branch=branch,
                     head=ws.branch_head(branch), dev_member="m-dev")
    review = s.add_task(title=f"review PR: {dev_task.title}", role="reviewer",
                        pr_id=pr["pr_id"], depends_on=[dev_task.task_id])
    return pr, review


def _record_red_probe(s, head: str) -> None:
    """A red GL01 web:probe run bound to ``head`` (a black canvas / console error) —
    the machine lane's evidence that a runtime claim is real."""
    result = RunResult(
        command_id="web:probe", argv_sha256="", status="failed", exit_code=1,
        passed=False, duration_ms=0, stdout_sha256="", stdout_preview="",
        stderr_preview="canvas is entirely black", reason="web probe failed")
    session = RunSession(command_ids=["web:probe"], results=[result],
                             unknown_ids=[], passed=False, sandbox="")
    s.record_test_run(session, task_id="web-probe", head=head)


def _revises(s):
    return [t for t in s.list_tasks() if t.title.startswith("revise:")]


# --------------------------------------------------------------------------- #
# The classifier — the machine/judgment lane split (the contract table).
# --------------------------------------------------------------------------- #

def _f(title, body=""):
    return {"title": title, "body": body, "blocking": True}


def test_is_execution_claim_table() -> None:
    machine = [
        _f("this will render black at runtime"),
        _f("black canvas", "the first canvas paints nothing"),
        _f("no evidence the tests were run"),
        _f("no evidence that the acceptance tests were actually run"),
        _f("crashes on start", "the app throws before first paint"),
        _f("race condition in the init sequence"),
        _f("the page won't load", "blank on navigate"),
        _f("fails to launch", "the runtime never comes up"),
        _f("the level is untested"),
    ]
    judgment = [
        # A precise, diff-evidenced defect is NOT machine-lane just because a bug
        # crashes — the citation reasons about the diff, not the run.
        _f("null deref in init", "src/mod.js:1 crashes"),
        _f("this variable name is unclear"),
        _f("does not conform to the DoD's stroke-count requirement"),
        _f("stale gate", ""),                     # Spec 16 fixture — must stay judgment
        _f("brand new defect in the solver"),
        _f("missing null check on the config path"),
        _f("off-by-one in the scoring loop"),
    ]
    for finding in machine:
        assert runner.is_execution_claim(finding), finding
    for finding in judgment:
        assert not runner.is_execution_claim(finding), finding


# --------------------------------------------------------------------------- #
# Item 1-2 — the invariant: an unverifiable machine-lane rejection ROUTES.
# --------------------------------------------------------------------------- #

def test_runtime_claim_without_evidence_routes_even_when_cited(
        tmp_errorta_home, tmp_path) -> None:
    # The gravity-golf pathology: a reviewer cites a real file AND rejects on a
    # runtime claim a diff cannot evidence. With NO gate/probe evidence it must NOT
    # bounce to a DEV — it routes. The citation does not rescue it (Spec 14 Item 3
    # checks it points somewhere; GL02 checks the diff can DECIDE it).
    s = _store("gl02a", tmp_path)
    ws = _ws("gl02a", s)
    pr, review = _pr_and_review(s, ws)
    runner._handle_review_rejection(
        s, ws, pr=pr, task=review, source="reviewer",
        findings=[{"severity": "blocking", "blocking": True, "cited": True,
                   "path": "src/mod.js",
                   "title": "renders black at runtime",
                   "body": "the canvas will paint black once served"}])
    assert not _revises(s)                                    # no DEV bounce
    assert any(t.role == "pm" for t in s.list_tasks())        # routed instead
    assert s.get_pr(pr["pr_id"])["status"] == "changes_requested"  # fail-closed
    # The LLM never DECIDED the executable question: no revise, PR not merged.
    assert s.get_pr(pr["pr_id"])["status"] != "merged"


def test_runtime_claim_backed_by_red_probe_is_actionable(
        tmp_errorta_home, tmp_path) -> None:
    # Same claim, but now a red web:probe (GL01) at this head BACKS it — the runtime
    # failure is real and documented, so a DEV can fix it: today's cited-defect path.
    s = _store("gl02b", tmp_path)
    ws = _ws("gl02b", s)
    pr, review = _pr_and_review(s, ws)
    _record_red_probe(s, pr["head"])
    runner._handle_review_rejection(
        s, ws, pr=pr, task=review, source="reviewer",
        findings=[{"severity": "blocking", "blocking": True, "cited": True,
                   "path": "src/mod.js", "title": "renders black at runtime",
                   "body": "the canvas paints black"}])
    assert _revises(s)                                        # actionable -> revise
    assert s.get_pr(pr["pr_id"])["status"] == "changes_requested"


def test_green_probe_does_not_back_the_claim(tmp_errorta_home, tmp_path) -> None:
    # A GREEN probe CONTRADICTS the runtime claim (Spec 14 Item 5) and does not back
    # it — so an unbacked machine-lane rejection still routes, never bounces.
    s = _store("gl02c", tmp_path)
    ws = _ws("gl02c", s)
    pr, review = _pr_and_review(s, ws)
    green = RunResult(command_id="web:probe", argv_sha256="", status="completed",
                          exit_code=0, passed=True, duration_ms=0, stdout_sha256="",
                          stdout_preview="", stderr_preview="", reason="")
    s.record_test_run(
        RunSession(command_ids=["web:probe"], results=[green], unknown_ids=[],
                       passed=True, sandbox=""), task_id="web-probe", head=pr["head"])
    runner._handle_review_rejection(
        s, ws, pr=pr, task=review, source="reviewer",
        findings=[{"severity": "blocking", "blocking": True, "cited": True,
                   "path": "src/mod.js", "title": "renders black at runtime"}])
    assert not _revises(s)


def test_judgment_finding_behaves_as_spec14_today(tmp_errorta_home, tmp_path) -> None:
    # A pure design/clarity finding is judgment-lane and is entirely unaffected: it
    # spawns a revise exactly as before GL02.
    s = _store("gl02d", tmp_path)
    ws = _ws("gl02d", s)
    pr, review = _pr_and_review(s, ws)
    runner._handle_review_rejection(
        s, ws, pr=pr, task=review, source="reviewer",
        findings=[{"severity": "blocking", "blocking": True, "cited": True,
                   "path": "src/mod.js", "title": "the module API is confusing",
                   "body": "rename x to a descriptive identifier"}])
    assert _revises(s)


def test_mixed_rejection_with_a_real_defect_still_spawns_a_revise(
        tmp_errorta_home, tmp_path) -> None:
    # A rejection that ALSO names a real citable defect is not all-unactionable, so
    # the revise fires (addressing that defect) — the machine-lane finding rides along.
    s = _store("gl02e", tmp_path)
    ws = _ws("gl02e", s)
    pr, review = _pr_and_review(s, ws)
    runner._handle_review_rejection(
        s, ws, pr=pr, task=review, source="reviewer",
        findings=[{"severity": "blocking", "blocking": True, "cited": True,
                   "path": "src/mod.js", "title": "renders black at runtime"},
                  {"severity": "blocking", "blocking": True, "cited": True,
                   "path": "src/mod.js", "title": "off-by-one in the scoring loop"}])
    assert _revises(s)


def test_lane_tag_persists_next_to_cited(tmp_errorta_home, tmp_path) -> None:
    # Item 1: the lane tag is persisted next to Spec 14's ``cited`` flag; a
    # non-runtime finding is tagged judgment (the conservative default).
    s = _store("gl02f", tmp_path)
    ws = _ws("gl02f", s)
    pr, _ = _pr_and_review(s, ws)
    marked = runner._mark_finding_citations(
        [{"severity": "blocking", "blocking": True, "path": "src/mod.js",
          "title": "renders black at runtime", "body": ""},
         {"severity": "blocking", "blocking": True, "path": "src/mod.js",
          "title": "the API is confusing", "body": ""}],
        workspace=ws, pr=pr)
    assert marked[0]["lane"] == "machine"
    assert marked[1]["lane"] == "judgment"


def test_pre_gl02_record_with_no_lane_reads_as_judgment(
        tmp_errorta_home, tmp_path) -> None:
    # A pre-GL02 finding carries no lane tag; the classifier treats absence as
    # judgment (no runtime phrase -> not machine-lane).
    assert not runner.is_execution_claim({"title": "looks wrong", "body": ""})


# --------------------------------------------------------------------------- #
# Item 3 — the per-head veto cap (2), composing with Spec 16's depth-3 cap.
# --------------------------------------------------------------------------- #

_JUDGMENT = [{"severity": "blocking", "blocking": True, "cited": True,
              "path": "src/mod.js", "title": "the API is confusing"}]


def test_two_rejections_of_one_head_escalate_to_pm(tmp_errorta_home, tmp_path) -> None:
    s = _store("gl02g", tmp_path)
    ws = _ws("gl02g", s)
    pr, review = _pr_and_review(s, ws)
    # First veto of this head: a normal revise.
    runner._handle_review_rejection(s, ws, pr=pr, task=review,
                                    findings=_JUDGMENT, source="reviewer")
    assert len(_revises(s)) == 1
    assert not any(d["choice"] == "reviewer_veto_escalated" for d in s.list_decisions())
    # Second veto of the SAME head: escalate, NO second revise.
    review2 = s.add_task(title="review PR: recheck", role="reviewer",
                         pr_id=pr["pr_id"], depends_on=[review.task_id])
    runner._handle_review_rejection(s, ws, pr=pr, task=review2,
                                    findings=_JUDGMENT, source="reviewer")
    assert len(_revises(s)) == 1                              # NO revise on the 2nd
    assert any(t.role == "pm" and t.title.startswith("reviewer disagreement:")
               for t in s.list_tasks())
    assert any(d["choice"] == "reviewer_veto_escalated" for d in s.list_decisions())
    # The verdict is surfaced, never flipped: the PR is not merged/blocked by GL02.
    assert s.get_pr(pr["pr_id"])["status"] == "changes_requested"


def test_a_different_head_starts_fresh(tmp_errorta_home, tmp_path) -> None:
    s = _store("gl02h", tmp_path)
    ws = _ws("gl02h", s)
    pr1, r1 = _pr_and_review(s, ws, seed="src/a.js")
    runner._handle_review_rejection(s, ws, pr=pr1, task=r1,
                                    findings=_JUDGMENT, source="reviewer")
    pr2, r2 = _pr_and_review(s, ws, seed="src/b.js")           # a DIFFERENT head
    runner._handle_review_rejection(s, ws, pr=pr2, task=r2,
                                    findings=_JUDGMENT, source="reviewer")
    # Each distinct head was vetoed once -> two revises, no escalation.
    assert len(_revises(s)) == 2
    assert not any(d["choice"] == "reviewer_veto_escalated" for d in s.list_decisions())


def test_veto_cap_escalation_is_deduped_per_head(tmp_errorta_home, tmp_path) -> None:
    s = _store("gl02i", tmp_path)
    ws = _ws("gl02i", s)
    pr, review = _pr_and_review(s, ws)
    for i in range(3):
        rt = s.add_task(title=f"review PR: r{i}", role="reviewer", pr_id=pr["pr_id"])
        runner._handle_review_rejection(s, ws, pr=pr, task=rt,
                                        findings=_JUDGMENT, source="reviewer")
    # 2nd and 3rd vetoes both escalate but the PM task is deduped to one.
    pm = [t for t in s.list_tasks()
          if t.role == "pm" and t.title.startswith("reviewer disagreement:")]
    assert len(pm) == 1


# --- composition with Spec 16's depth-3 lineage cap ------------------------- #

def _revise(s, *, branch, prev_pr, depth):
    t = s.add_task(title=f"revise: {branch}", role="dev", pr_id=prev_pr["pr_id"],
                   revise_depth=depth, finding_class=["stale", "gate"])
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=f"h-{branch}",
                     dev_member="m")
    return t, pr


def _three_deep(s):
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1)
    _r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2)
    r3, p3 = _revise(s, branch="b3", prev_pr=p2, depth=3)
    return r3, p3


def test_lineage_cap_fires_on_distinct_heads_without_per_head_escalation(
        tmp_errorta_home, tmp_path) -> None:
    # A depth-3 lineage of DISTINCT heads (each vetoed once) is exactly Spec 16's
    # grain: the lineage breaker fires; the per-head cap never does. They compose —
    # one detector fires at its grain, the other stays silent — and neither flips
    # ``approved``.
    s = _store("gl02j", tmp_path)
    r3, p3 = _three_deep(s)
    runner._handle_review_rejection(
        s, None, pr=p3, task=r3, source="reviewer",
        findings=[{"blocking": True, "title": "stale gate"}])   # same class as r3
    assert any(d["choice"] == "revise_chain_broken" for d in s.list_decisions())
    assert not any(d["choice"] == "reviewer_veto_escalated"     # per-head silent
                   for d in s.list_decisions())
    assert s.get_pr(p3["pr_id"])["status"] == "blocked"          # Spec 16 terminal
    assert s.get_pr(p3["pr_id"]).get("reviewer_approved") is not True  # not flipped
    assert len(_revises(s)) == 3                                 # no 4th revise


def test_per_head_cap_fires_first_and_short_circuits_the_lineage(
        tmp_errorta_home, tmp_path) -> None:
    # The SAME head vetoed twice trips the per-head cap FIRST (finer grain), before
    # the lineage can reach depth 3 — the composition the spec's Decision describes.
    # Spec 16's breaker never fires (no depth-3 same-class lineage was built).
    s = _store("gl02k", tmp_path)
    ws = _ws("gl02k", s)
    pr, review = _pr_and_review(s, ws)
    runner._handle_review_rejection(s, ws, pr=pr, task=review,
                                    findings=_JUDGMENT, source="reviewer")
    review2 = s.add_task(title="review PR: recheck", role="reviewer", pr_id=pr["pr_id"])
    runner._handle_review_rejection(s, ws, pr=pr, task=review2,
                                    findings=_JUDGMENT, source="reviewer")
    assert any(d["choice"] == "reviewer_veto_escalated" for d in s.list_decisions())
    assert not any(d["choice"] == "revise_chain_broken"        # lineage cap silent
                   for d in s.list_decisions())
    assert s.get_pr(pr["pr_id"])["status"] == "changes_requested"  # not blocked/merged
