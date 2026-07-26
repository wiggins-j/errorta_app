"""F088-09 — dev context requests (typed, read-only mid-run retrieval)."""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.schemas import (
    DeveloperContextRequestIntent,
    DeveloperToolPlanIntent,
    TurnParseError,
    parse_coding_turn,
)
from errorta_project_grounding import retrieval
from errorta_project_grounding.adapter import GroundingHit
from errorta_project_grounding.memory_store import (
    MemoryItem,
    MemoryQuery,
    MemorySourceRef,
    ProjectMemoryStore,
)


def _turn(intent: dict, *, task_id="t1") -> str:
    return json.dumps({"schema_version": "coding_turn.v1", "role": "dev",
                       "task_id": task_id, "intent": intent})


# --- schema dispatch --------------------------------------------------------


def test_context_request_parses_to_its_intent() -> None:
    out = parse_coding_turn("dev", "t1", _turn({
        "kind": "context_request", "reason": "missing_api_contract",
        "question": "What does divide do on zero?",
        "scope": {"corpus_query": "divide by zero", "sources": ["memory", "corpus"]},
        "needed_for": "implementation", "max_items": 4}))
    assert not isinstance(out, TurnParseError)
    assert isinstance(out.intent, DeveloperContextRequestIntent)
    assert out.intent.scope.sources == ["memory", "corpus"]


def test_tool_plan_still_parses_to_tool_intent() -> None:
    out = parse_coding_turn("dev", "t1", _turn({
        "kind": "tool_plan", "task_type": "implementation",
        "tool_calls": [{"tool": "code_write", "args": {"path": "a.py", "content": "x=1"}}]}))
    assert not isinstance(out, TurnParseError)
    assert isinstance(out.intent, DeveloperToolPlanIntent)


def test_context_request_without_question_fails_closed() -> None:
    out = parse_coding_turn("dev", "t1", _turn({"kind": "context_request", "question": "  "}))
    assert isinstance(out, TurnParseError)


# --- read-only answer -------------------------------------------------------


def _store(tmp: Path, pid: str) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _durable(mem, mid, content):
    mem.put(MemoryItem(project_id=mem.project_id, authority="durable_truth",
                       source_type="pm_decision", source_ref=MemorySourceRef(task_id="t"),
                       content=content, memory_id=mid, created_at="2026-01-01T00:00:00Z"))


def test_answer_is_read_only_and_grounded(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request
    s = _store(tmp_path, "cr1")
    mem = ProjectMemoryStore("cr1", root=tmp_path)
    _durable(mem, "d1", "divide raises ValueError on zero")
    before = len(mem.query(MemoryQuery(authorities=("durable_truth",), limit=200)))

    monkeypatch.setattr(retrieval, "retrieve_project_corpus",
                        lambda *a, **k: [GroundingHit(content="README: divide -> ValueError",
                                                      corpus_id="c", chunk_id="c9", score=0.8)])
    task = s.add_task(title="impl divide", role="dev")
    intent = DeveloperContextRequestIntent(
        kind="context_request", question="zero behavior?",
        scope={"sources": ["memory", "corpus"], "corpus_query": "divide by zero"})
    answer = _answer_dev_context_request(s, task, intent)

    assert answer["schema_version"] == "context_response.v1"
    assert answer["corpus_evidence"][0]["ref"] == "hit:c:c9"
    assert answer["memory"][0]["ref"] == "mem:d1"
    # recorded for audit (read-only ledger metadata)
    assert any(d["choice"] == "context_request" for d in s.list_decisions())
    # NO durable mutation — memory only queried
    after = len(mem.query(MemoryQuery(authorities=("durable_truth",), limit=200)))
    assert after == before


def test_answer_caps_results(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request
    s = _store(tmp_path, "cr2")
    mem = ProjectMemoryStore("cr2", root=tmp_path)
    for i in range(10):
        _durable(mem, f"d{i}", f"fact {i}")
    monkeypatch.setattr(retrieval, "retrieve_project_corpus",
                        lambda *a, **k: [GroundingHit(content=f"h{i}", corpus_id="c",
                                                      chunk_id=f"c{i}") for i in range(10)])
    task = s.add_task(title="t", role="dev")
    intent = DeveloperContextRequestIntent(kind="context_request", question="q",
                                           scope={"sources": ["memory", "corpus"]}, max_items=3)
    answer = _answer_dev_context_request(s, task, intent)
    assert len(answer["corpus_evidence"]) == 3 and len(answer["memory"]) == 3


# --- P1 fix: the answer is reliably delivered back to the dev ----------------


def test_dev_prompt_delivers_prior_context_response(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request, _dev_prompt
    s = _store(tmp_path, "cr3")
    ProjectMemoryStore("cr3", root=tmp_path)  # create the memory db
    monkeypatch.setattr(retrieval, "retrieve_project_corpus",
                        lambda *a, **k: [GroundingHit(
                            content="divide raises", corpus_id="c", chunk_id="c1")])
    task = s.add_task(title="impl divide", role="dev")
    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="zero?", scope={"sources": ["corpus"]}))
    prompt = _dev_prompt(task, s)
    assert "context_response.v1" in prompt
    assert "Context response to YOUR earlier request" in prompt


def test_context_request_writes_wip_memory_row(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request
    s = _store(tmp_path, "cr4")
    mem = ProjectMemoryStore("cr4", root=tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    task = s.add_task(title="t", role="dev")
    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="what API?", scope={"sources": ["memory"]}))
    wip = mem.query(MemoryQuery(authorities=("wip",), limit=200))
    assert any(i.source_type == "context_request" for i in wip)  # surfaces in PM briefing


# --- Spec 20: the context channel is bounded --------------------------------
#
# Before Spec 20 the DEV dispatch branch answered a context request, requeued the
# task to `todo`, and returned a plain `noop` — the ONLY dev dead-end that did not
# set `unproductive=True`. Nothing counted, so the F127 escalate-up ladder never
# engaged, and because only the latest answer was threaded back the next prompt
# was identical: same prompt -> same model output -> an infinite re-dispatch.


def _ctx_env(task_id: str, question: str, *, kind: str = "context_request") -> str:
    return _turn({"kind": kind, "question": question,
                  "scope": {"sources": ["memory", "corpus"]}}, task_id=task_id)


def _ctx_runner(store, ws, caller):
    from errorta_council.coding.runner import build_run_turn, members_by_coding_role
    return build_run_turn(store, ws, members_by_coding_role([
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}}]),
        caller, guardrail_enabled=True)


def _ctx_project(tmp_path, pid: str):
    """A ledger + real worktree wired for the DEV dispatch path."""
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def _task_extras(store, task_id: str) -> dict:
    task = next(t for t in store.list_tasks() if t.task_id == task_id)
    return getattr(task, "_extras", {}) or {}


def _context_key(question: str) -> str:
    from errorta_council.coding.runner import _context_question_key
    return _context_question_key(question)


def test_under_budget_context_request_answers_and_requeues_without_penalty(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """A legitimate single question must still be answered and must NOT be scored
    unproductive — otherwise every context-asking dev starts climbing F127."""
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxone")
    task = store.add_task(title="impl divide", role=DEV)
    rt = _ctx_runner(store, ws, lambda m, p: _ctx_env(task.task_id, "what API?"))

    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert out.kind == "noop"
    assert out.unproductive is False
    assert any(d["choice"] == "context_request" for d in store.list_decisions())
    assert not any(d["choice"] == "context_request_exhausted"
                   for d in store.list_decisions())
    assert {t.task_id: t.state for t in store.list_tasks()}[task.task_id] == "todo"
    extras = _task_extras(store, task.task_id)
    # exact key name asserted: update_task takes **patch and silently persists a
    # typo into _extras, which would read back as 0 forever (the original bug).
    assert extras["context_request_attempts"] == 1
    assert extras["last_context_question_key"] == _context_key("  WHAT   api? ")


def test_context_request_counter_persists_across_turns(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """The counter lives on the task in the ledger (like pm_assist_attempts), not
    in the loop's in-memory unproductive counter — so it survives a re-dispatch,
    an alternating turn pattern, and a process restart."""
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxcount")
    task = store.add_task(title="impl divide", role=DEV)
    questions = iter(["first?", "second?", "third?"])
    rt = _ctx_runner(store, ws, lambda m, p: _ctx_env(task.task_id, next(questions)))

    for expected in (1, 2, 3):
        out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
        assert out.unproductive is False
        assert _task_extras(store, task.task_id)["context_request_attempts"] == expected

    # a FRESH store over the same directory still sees the counter
    reopened = LedgerStore("ctxcount", root=tmp_path / "ledger-ctxcount")
    assert _task_extras(reopened, task.task_id)["context_request_attempts"] == 3


def test_context_request_budget_exhaustion_is_unproductive(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """The 4th distinct ask on one task exhausts the budget: it is NOT answered,
    it records `context_request_exhausted`, it requeues, and it returns
    unproductive=True with that reason so the F127 ladder takes over."""
    from errorta_council.coding.runner import _CONTEXT_REQUEST_LIMIT
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxcap")
    task = store.add_task(title="impl divide", role=DEV)
    asked: list[str] = []

    def caller(_member, _prompt):
        asked.append(f"question number {len(asked)}?")
        return _ctx_env(task.task_id, asked[-1])

    rt = _ctx_runner(store, ws, caller)
    for _ in range(_CONTEXT_REQUEST_LIMIT):
        assert rt(Assign(member_id="m-dev", task_id=task.task_id,
                         role=DEV), store).unproductive is False

    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert out.kind == "noop"
    assert out.unproductive is True
    assert out.reason == "context_request_exhausted"
    assert out.member_id == "m-dev" and out.member_role == DEV
    assert {t.task_id: t.state for t in store.list_tasks()}[task.task_id] == "todo"
    # the over-budget ask was NOT answered — still only LIMIT context responses
    answered = [d for d in store.list_decisions() if d["choice"] == "context_request"]
    assert len(answered) == _CONTEXT_REQUEST_LIMIT
    exhausted = [d for d in store.list_decisions()
                 if d["choice"] == "context_request_exhausted"]
    assert len(exhausted) == 1
    assert asked[-1] in exhausted[0]["rationale"]
    assert f"of {_CONTEXT_REQUEST_LIMIT}" in exhausted[0]["rationale"]


def test_verbatim_repeat_short_circuits_the_budget(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """Re-asking the SAME question proves the answer did not help; re-answering
    retrieves the same hits and is guaranteed to loop, so it exhausts the budget
    at once (turn 2) instead of burning one slot per wasted model call. Casing and
    whitespace are normalised, so cosmetic jitter cannot dodge the guard."""
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxrepeat")
    task = store.add_task(title="impl divide", role=DEV)
    variants = iter(["What does divide do on zero?",
                     "  what   does DIVIDE do on   zero?  "])
    rt = _ctx_runner(store, ws, lambda m, p: _ctx_env(task.task_id, next(variants)))

    assert rt(Assign(member_id="m-dev", task_id=task.task_id,
                     role=DEV), store).unproductive is False
    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert out.unproductive is True
    assert out.reason == "context_request_exhausted"
    assert len([d for d in store.list_decisions()
                if d["choice"] == "context_request"]) == 1  # repeat not answered
    rationale = next(d["rationale"] for d in store.list_decisions()
                     if d["choice"] == "context_request_exhausted")
    assert "verbatim repeat" in rationale


def test_long_shared_preamble_does_not_fake_a_verbatim_repeat(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """Two DIFFERENT asks that share a long preamble must not collide. The repeat
    key hashes the WHOLE normalised question; truncating the text before comparing
    would classify a legitimate, progressing follow-up as a dead end and fire the
    F127 ladder on it."""
    from errorta_council.coding.runner import _CONTEXT_QUESTION_CAP
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxpreamble")
    task = store.add_task(title="impl divide", role=DEV)
    preamble = "I am implementing the divide task. " * 40   # >> the quote cap
    assert len(preamble) > _CONTEXT_QUESTION_CAP
    variants = iter([preamble + "What is the return type?",
                     preamble + "What does it raise on zero?"])
    rt = _ctx_runner(store, ws, lambda m, p: _ctx_env(task.task_id, next(variants)))

    assert rt(Assign(member_id="m-dev", task_id=task.task_id,
                     role=DEV), store).unproductive is False
    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert out.unproductive is False        # answered, not short-circuited
    assert len([d for d in store.list_decisions()
                if d["choice"] == "context_request"]) == 2
    assert _task_extras(store, task.task_id)["context_request_attempts"] == 2


def test_room_change_rescue_rearms_the_rendered_budget(tmp_path, monkeypatch) -> None:
    """`resolve_stale_worker_unproductive` zeroes the counter when a room change
    rescues an exhausted task — deliberately re-arming the channel for the NEW
    route. The recorded answers are append-only, so the prompt must read the
    counter and NOT floor it at the answer count, or the rescued dev is told
    "0 remain - do NOT ask again" about a budget the runner would happily serve."""
    from errorta_council.coding.runner import _answer_dev_context_request, _dev_prompt
    s = _store(tmp_path, "cr8")
    ProjectMemoryStore("cr8", root=tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    task = s.add_task(title="impl divide", role="dev")
    for q in ("alpha?", "beta?", "gamma?"):
        _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
            kind="context_request", question=q, scope={"sources": ["memory"]}))
    s.update_task(task.task_id, context_request_attempts=4,
                  last_context_question_key=_context_key("gamma?"))
    task = next(t for t in s.list_tasks() if t.task_id == task.task_id)
    assert "0 remain" in _dev_prompt(task, s)

    # the rescue path's reset, verbatim (attention.resolve_stale_worker_unproductive)
    s.update_task(task.task_id, context_request_attempts=0,
                  last_context_question_key="")
    task = next(t for t in s.list_tasks() if t.task_id == task.task_id)

    prompt = _dev_prompt(task, s)
    assert "You have used 0 of 3 context requests" in prompt
    assert "3 remain" in prompt
    assert "do NOT ask again" not in prompt


def test_exhausted_counter_saturates_at_the_limit(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """An exhausted task is re-dispatched repeatedly while the F127 ladder walks
    its rungs (worker_unproductive_limit is 2, so the same member gets at least
    one more turn). The persisted counter must SATURATE at the limit rather than
    grow, or the very next prompt reads "You have used 4 of 3 context requests" —
    a self-contradictory instruction handed to the model on exactly the turns
    where it must stop asking and implement."""
    from errorta_council.coding.runner import _CONTEXT_REQUEST_LIMIT, _dev_prompt
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxsat")
    task = store.add_task(title="impl divide", role=DEV)
    n = iter(range(100))
    rt = _ctx_runner(store, ws,
                     lambda m, p: _ctx_env(task.task_id, f"question {next(n)}?"))

    for _ in range(_CONTEXT_REQUEST_LIMIT):        # asks 1..3 are answered
        rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    for _ in range(3):                             # asks 4,5,6 all exhaust
        out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
        assert out.unproductive is True
        assert (_task_extras(store, task.task_id)["context_request_attempts"]
                == _CONTEXT_REQUEST_LIMIT)

    fresh = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    prompt = _dev_prompt(fresh, store)
    assert f"You have used {_CONTEXT_REQUEST_LIMIT} of {_CONTEXT_REQUEST_LIMIT}" in prompt
    assert "0 remain" in prompt


def test_non_numeric_persisted_counter_does_not_break_the_dev_turn(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """`update_task` takes **patch and writes unknown keys into `_extras` with no
    validation, so this counter is an unvalidated passthrough. Prompt composition
    runs on EVERY dev turn and was total before Spec 20 — a junk value must
    degrade, not raise a ValueError out of the DEV path."""
    from errorta_council.coding.runner import (
        _answer_dev_context_request,
        _dev_prompt,
        build_run_turn,
        members_by_coding_role,
    )
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxjunk")
    ProjectMemoryStore("ctxjunk", root=tmp_path / "ledger-ctxjunk")
    task = store.add_task(title="impl divide", role=DEV)
    _answer_dev_context_request(store, task, DeveloperContextRequestIntent(
        kind="context_request", question="alpha?", scope={"sources": ["memory"]}))
    store.update_task(task.task_id, context_request_attempts="three")
    task = next(t for t in store.list_tasks() if t.task_id == task.task_id)

    # rendering falls back to the answer count instead of exploding
    prompt = _dev_prompt(task, store)
    assert "You have used 1 of 3 context requests" in prompt

    # and the dispatch guard reads it as "no attempts yet" rather than raising
    rt = build_run_turn(store, ws, members_by_coding_role([
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}}]),
        lambda m, p: _ctx_env(task.task_id, "beta?"), guardrail_enabled=True)
    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    assert out.unproductive is False
    assert _task_extras(store, task.task_id)["context_request_attempts"] == 1


def test_relabelled_unknown_dev_kind_also_hits_the_guard(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """schemas.py relabels ANY unknown dev intent kind carrying a non-empty
    `question` into context_request, so a schema-confused dev is funnelled into
    this branch. The guard lives on the runner, so it catches that too."""
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxrelabel")
    task = store.add_task(title="impl divide", role=DEV)
    rt = _ctx_runner(store, ws, lambda m, p: _ctx_env(
        task.task_id, "what is the API?", kind="ask_question"))

    assert rt(Assign(member_id="m-dev", task_id=task.task_id,
                     role=DEV), store).unproductive is False
    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    assert out.unproductive is True and out.reason == "context_request_exhausted"


# --- Spec 20: prompt threading ----------------------------------------------


def test_dev_prompt_threads_multiple_context_responses_and_budget(
        tmp_path, monkeypatch) -> None:
    """A follow-up ask must see a DIFFERENT prompt than its predecessor — the
    single-latest-answer threading was why the loop had a fixed point. The last N
    answers are carried oldest-first, plus the remaining-ask budget."""
    from errorta_council.coding.runner import (
        _CONTEXT_RESPONSE_THREAD_N,
        _answer_dev_context_request,
        _dev_prompt,
        _latest_context_response_text,
    )
    s = _store(tmp_path, "cr5")
    ProjectMemoryStore("cr5", root=tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    task = s.add_task(title="impl divide", role="dev")

    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="alpha?", scope={"sources": ["memory"]}))
    first = _dev_prompt(task, s)
    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="beta?", scope={"sources": ["memory"]}))
    second = _dev_prompt(task, s)

    assert second != first                       # the fixed point is broken
    assert "Context response to YOUR earlier request" in second
    assert "You have used 2 of 3 context requests" in second
    assert "1 remain" in second
    # assert on the threaded segment itself: the grounding packet quotes the same
    # decisions elsewhere in the prompt, so a whole-prompt substring proves nothing.
    seg = _latest_context_response_text(s, task.task_id, task=task)
    assert seg in second
    assert '"question": "alpha?"' in seg and '"question": "beta?"' in seg
    assert seg.index("alpha?") < seg.index("beta?")          # oldest-first

    # only the last N are threaded
    for q in ("gamma?", "delta?"):
        _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
            kind="context_request", question=q, scope={"sources": ["memory"]}))
    task = next(t for t in s.list_tasks() if t.task_id == task.task_id)
    seg = _latest_context_response_text(s, task.task_id, task=task)
    assert '"question": "alpha?"' not in seg
    assert seg.count('"question": ') == _CONTEXT_RESPONSE_THREAD_N
    assert "0 remain" in seg
    assert "do NOT ask again" in seg
    assert seg in _dev_prompt(task, s)


def test_dev_prompt_budget_reads_the_persisted_counter(tmp_path, monkeypatch) -> None:
    """A short-circuited verbatim repeat records NO answer, so the answer count
    understates the spend — the persisted counter is authoritative and the prompt
    must report the exhausted budget, not a stale "2 remain"."""
    from errorta_council.coding.runner import _answer_dev_context_request, _dev_prompt
    s = _store(tmp_path, "cr6")
    ProjectMemoryStore("cr6", root=tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    task = s.add_task(title="impl divide", role="dev")
    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="alpha?", scope={"sources": ["memory"]}))
    s.update_task(task.task_id, context_request_attempts=3,
                  last_context_question_key=_context_key("alpha?"))
    task = next(t for t in s.list_tasks() if t.task_id == task.task_id)

    prompt = _dev_prompt(task, s)
    assert "You have used 3 of 3 context requests" in prompt
    assert "0 remain" in prompt


def test_no_context_response_leaves_the_dev_prompt_untouched(tmp_path) -> None:
    """The Spec 20 threading segment must render "" for a task that never asked —
    that is what keeps the DEV prompt golden lock
    (tests/coding/test_prompt_segments_golden.py) byte-identical."""
    from errorta_council.coding.runner import _dev_prompt, _latest_context_response_text
    s = _store(tmp_path, "cr7")
    task = s.add_task(title="impl divide", role="dev")
    assert _latest_context_response_text(s, task.task_id, task=task) == ""
    prompt = _dev_prompt(task, s)
    assert "Context response to YOUR earlier request" not in prompt
    assert "context requests on this task" not in prompt


# --- Spec 20: the budget is a ladder counter, so the F127 route-around rungs
#     must re-arm it. Without this, the fresh/stronger route the ladder just
#     installed inherits a spent channel and its FIRST well-formed turn scores
#     `context_request_exhausted` — the rungs exist precisely to give it a fair
#     attempt, and on this failure mode it would get none of the channel it needs.

_LADDER_MEMBERS = [("m-dev", "dev"), ("m-dev2", "dev"), ("m-pm", "pm")]


def _exhausted_ladder_task(tmp_path, pid: str):
    """A task whose context budget is already saturated, as the dispatch branch
    leaves it after an exhaustion turn."""
    from errorta_council.coding.runner import _CONTEXT_REQUEST_LIMIT
    s = _store(tmp_path, pid)
    task = s.add_task(title="impl divide", role="dev")
    s.update_task(task.task_id, context_request_attempts=_CONTEXT_REQUEST_LIMIT,
                  last_context_question_key=_context_key("alpha?"))
    return s, task


def _drive_ladder(store, task_id, member_id, policy, members, turns=2,
                  reason="context_request_exhausted"):
    from errorta_council.coding.autonomy import LoopCounters, TurnOutcome, _handle_unproductive
    from errorta_council.coding.topology import DEV, Assign
    counters = LoopCounters()
    action = Assign(member_id=member_id, task_id=task_id, role=DEV)
    outcome = TurnOutcome(kind="noop", unproductive=True, member_id=member_id,
                          member_role=DEV, member_route="anthropic.haiku",
                          reason=reason)
    stops = [_handle_unproductive(store, action, outcome, counters, policy, members)
             for _ in range(turns)]
    return counters, stops


def test_member_exclusion_rung_rearms_the_context_budget(tmp_path) -> None:
    """The exclusion rung hands the task to a DIFFERENT member. That member has
    never asked anything on this task, so it must start with a full budget and a
    cleared repeat key — otherwise the very rung that exists to give it a fair
    attempt hands it a channel that is already spent."""
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    s, task = _exhausted_ladder_task(tmp_path, "ctxrung1")
    policy = CodingAutonomyPolicy(worker_unproductive_limit=2)

    _, stops = _drive_ladder(s, task.task_id, "m-dev", policy, _LADDER_MEMBERS)

    assert stops == [None, None]
    assert any(d["choice"] == "worker_excluded" for d in s.list_decisions())
    extras = _task_extras(s, task.task_id)
    assert extras["excluded_member_ids"] == ["m-dev"]   # the rung really fired
    assert extras["context_request_attempts"] == 0
    assert extras["last_context_question_key"] == ""


def test_model_escalation_rung_rearms_the_context_budget(tmp_path, monkeypatch) -> None:
    """Δ review: the escalation rung keeps the SAME member on a stronger route, so
    it must NOT re-arm a budget that context-exhaustion just burned — a stronger
    model asking the same corpus the same question gets the same answers, and
    re-arming here multiplied the cap by the rung count (~25 dev calls of pure
    asking on one task, the same order as the unbounded pathology this bounds).
    It still re-arms for any OTHER unproductive reason, where a stronger route is
    a genuinely fresh attempt. Exclusion/reassignment (a DIFFERENT member) still
    re-arms unconditionally — see the test above."""
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    from errorta_council.coding.model_assignment import make_assignment
    from errorta_council.coding.model_availability import RouteAvailability
    from errorta_council.coding.runner import _CONTEXT_REQUEST_LIMIT
    monkeypatch.setattr(
        "errorta_council.coding.model_availability.resolve_route_availability",
        lambda routes: {r: RouteAvailability(r, r.split(".", 1)[0], True, "")
                        for r in routes})
    monkeypatch.setattr("errorta_council.coding.performance_corpus.digest", lambda: {})
    s, task = _exhausted_ladder_task(tmp_path, "ctxrung2")
    assignment = make_assignment(
        task_id=task.task_id, member_id="m-dev", route_id="anthropic.haiku",
        task_type="implementation", difficulty_tier="light", rationale="cheap",
        source="selector")
    s.update_task(task.task_id, model_assignment=assignment.to_dict(),
                  model_pool_snapshot=["anthropic.haiku", "openai.gpt-5",
                                       "anthropic.opus"])
    policy = CodingAutonomyPolicy(worker_unproductive_limit=2,
                                  model_escalation_limit=2)

    counters, stops = _drive_ladder(s, task.task_id, "m-dev", policy, _LADDER_MEMBERS)

    assert stops == [None, None] and counters.model_escalations == 1
    fresh = next(t for t in s.list_tasks() if t.task_id == task.task_id)
    assert fresh.model_assignment["route_id"] == "openai.gpt-5"   # the rung fired
    assert not (fresh._extras.get("excluded_member_ids") or [])   # exclusion did NOT
    # The rung fired, but the budget stays SPENT: the reason was exhaustion.
    assert fresh._extras["context_request_attempts"] == _CONTEXT_REQUEST_LIMIT
    assert fresh._extras["last_context_question_key"] != ""

    # …and the same rung DOES re-arm for a different unproductive reason.
    s2, task2 = _exhausted_ladder_task(tmp_path, "ctxrung2b")
    s2.update_task(task2.task_id, model_assignment=assignment.to_dict(),
                   model_pool_snapshot=["anthropic.haiku", "openai.gpt-5",
                                        "anthropic.opus"])
    _drive_ladder(s2, task2.task_id, "m-dev", policy, _LADDER_MEMBERS,
                  reason="unparseable")
    fresh2 = next(t for t in s2.list_tasks() if t.task_id == task2.task_id)
    assert fresh2._extras["context_request_attempts"] == 0
    assert fresh2._extras["last_context_question_key"] == ""


def test_pm_assist_and_terminal_rungs_do_not_rearm_the_budget(tmp_path) -> None:
    """Boundedness guard. Only the two rungs that install a NEW dev route re-arm.
    PM assist re-scopes the task into fresh tasks (which get fresh budgets of their
    own) and the terminal rung raises the blocking Problem — re-arming at either
    would re-open the loop Spec 20 closed, so the counter must stay saturated."""
    from errorta_council.coding.autonomy import WORKER_UNPRODUCTIVE, CodingAutonomyPolicy
    from errorta_council.coding.runner import _CONTEXT_REQUEST_LIMIT
    policy = CodingAutonomyPolicy(worker_unproductive_limit=2)

    # No eligible dev left -> PM assist rung.
    s, task = _exhausted_ladder_task(tmp_path, "ctxrung3")
    _drive_ladder(s, task.task_id, "m-dev", policy, [("m-dev", "dev"), ("m-pm", "pm")])
    extras = _task_extras(s, task.task_id)
    assert extras["pm_assist_pending"] is True
    assert extras["context_request_attempts"] == _CONTEXT_REQUEST_LIMIT

    # No PM either -> terminal rung, blocking Problem.
    s2, task2 = _exhausted_ladder_task(tmp_path, "ctxrung4")
    _, stops = _drive_ladder(s2, task2.task_id, "m-dev", policy, [("m-dev", "dev")])
    assert stops[-1] == WORKER_UNPRODUCTIVE
    assert (_task_extras(s2, task2.task_id)["context_request_attempts"]
            == _CONTEXT_REQUEST_LIMIT)


def test_reassigned_member_gets_a_real_answer_on_its_first_ask(
        tmp_errorta_home, tmp_path, monkeypatch) -> None:
    """End-to-end over the REAL dispatch branch + REAL ladder: m-dev burns the
    budget and is excluded; m-dev2's first turn on the task is a schema-valid
    context request and must be ANSWERED, not scored unproductive on arrival."""
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    from errorta_council.coding.runner import (
        _CONTEXT_REQUEST_LIMIT,
        build_run_turn,
        members_by_coding_role,
    )
    from errorta_council.coding.topology import DEV, Assign
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    store, ws = _ctx_project(tmp_path, "ctxrung5")
    task = store.add_task(title="impl divide", role=DEV)
    n = iter(range(100))
    rt = build_run_turn(store, ws, members_by_coding_role([
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
        {"id": "m-dev2", "enabled": True, "metadata": {"coding_role": "dev"}}]),
        lambda m, p: _ctx_env(task.task_id, f"question {next(n)}?"),
        guardrail_enabled=True)

    for _ in range(_CONTEXT_REQUEST_LIMIT):
        rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)
    assert rt(Assign(member_id="m-dev", task_id=task.task_id,
                     role=DEV), store).unproductive is True
    _drive_ladder(store, task.task_id, "m-dev",
                  CodingAutonomyPolicy(worker_unproductive_limit=2), _LADDER_MEMBERS)
    assert _task_extras(store, task.task_id)["excluded_member_ids"] == ["m-dev"]

    out = rt(Assign(member_id="m-dev2", task_id=task.task_id, role=DEV), store)

    assert out.unproductive is False, "the rescued member was born exhausted"
    assert _task_extras(store, task.task_id)["context_request_attempts"] == 1
    answered = [d for d in store.list_decisions() if d["choice"] == "context_request"]
    assert len(answered) == _CONTEXT_REQUEST_LIMIT + 1
