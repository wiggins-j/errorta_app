from pathlib import Path
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding import skills as sk
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_role_skill_mapping() -> None:
    assert sk.primary_skill(DEV) == "test-driven-development"
    assert sk.primary_skill(PM) == "brainstorming"
    assert "verification-before-completion" in sk.skills_for_role(TESTER)


def test_frame_turn_on_and_off() -> None:
    on = sk.frame_turn(DEV, enabled=True)
    assert on["skill"] == "test-driven-development" and on["directive"]
    off = sk.frame_turn(DEV, enabled=False)
    assert off["skill"] is None and off["directive"] == ""


def test_tdd_gate_dev_requires_test() -> None:
    ok, _ = sk.tdd_gate(role=DEV, task_type="implementation",
                        has_passing_test=False, enabled=True)
    assert ok is False
    ok2, _ = sk.tdd_gate(role=DEV, task_type="implementation",
                         has_passing_test=True, enabled=True)
    assert ok2 is True


def test_tdd_gate_exempts_docs_and_non_dev_and_off() -> None:
    assert sk.tdd_gate(role=DEV, task_type="docs", has_passing_test=False, enabled=True)[0]
    assert sk.tdd_gate(role=REVIEWER, task_type="implementation",
                       has_passing_test=False, enabled=True)[0]
    assert sk.tdd_gate(role=DEV, task_type="implementation",
                       has_passing_test=False, enabled=False)[0]


def test_cli_skill_prompt_names_skill() -> None:
    frag = sk.cli_skill_prompt(DEV)
    assert "test-driven-development" in frag


def test_record_turn_skill_writes_to_ledger(tmp_path: Path) -> None:
    s = _store(tmp_path)
    sk.record_turn_skill(s, member_id="m-dev", task_id="t1", role=DEV)
    uses = s.list_skill_uses()
    assert uses[0]["skill"] == "test-driven-development"


def test_guardrail_defaults_on_and_persists(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert sk.load_guardrail(s).enabled is True  # default ON
    sk.save_guardrail(s, sk.SkillsGuardrailPolicy(enabled=False))
    assert sk.load_guardrail(LedgerStore("p", root=tmp_path)).enabled is False


def test_enforce_dev_completion_gate_blocks_untested(tmp_path: Path) -> None:
    from errorta_council.coding.topology import CodingReconciler
    s = _store(tmp_path)
    rec = CodingReconciler(s)
    t = s.add_task(title="impl parser", role=DEV)
    # deliberate violation: claim done with no passing test
    outcome = sk.enforce_dev_completion(rec, t, task_type="implementation",
                                        has_passing_test=False, enabled=True)
    assert outcome == "needs_test"
    # task NOT done; a test task was spawned and impl re-queued depending on it
    impl = next(x for x in s.list_tasks() if x.task_id == t.task_id)
    assert impl.state == "todo"
    test_tasks = [x for x in s.list_tasks() if x.title.startswith("write a failing test")]
    assert len(test_tasks) == 1 and impl.depends_on == [test_tasks[0].task_id]
    # no review task spawned (work isn't actually done)
    assert not any(x.title.startswith("review:") for x in s.list_tasks())


def test_enforce_dev_completion_passes_with_test(tmp_path: Path) -> None:
    from errorta_council.coding.topology import CodingReconciler
    s = _store(tmp_path)
    rec = CodingReconciler(s)
    t = s.add_task(title="impl parser", role=DEV)
    outcome = sk.enforce_dev_completion(rec, t, task_type="implementation",
                                        has_passing_test=True, enabled=True)
    assert outcome == "done"
    assert next(x for x in s.list_tasks() if x.task_id == t.task_id).state == "done"
    assert any(x.title.startswith("review:") for x in s.list_tasks())
