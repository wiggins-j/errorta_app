"""F143 — a coding turn threads the gateway result's token usage into the ledger.

Covers the thread-local usage-sink seam: ``gateway_member_caller`` writes usage,
the ``build_run_turn`` capture wrapper reads it, and ``record_turn`` persists it —
while a fake caller that never writes the sink yields an unreported turn.
"""
import json
import threading
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _merge_call_usage,
    _usage_sink,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_PM_ENV = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": False, "tasks": []}})


def _pm_turn() -> str:
    return _PM_ENV


def _new_store(tmp_path: Path, pid: str) -> LedgerStore:
    store = LedgerStore(pid, root=tmp_path)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def test_measured_turn_persists_tokens(tmp_path: Path) -> None:
    store = _new_store(tmp_path, "cptok")

    # A caller that behaves like gateway_member_caller: writes the usage sink, then
    # returns the response text. (A remote/CLI/reporting provider.) A single turn
    # may make several gateway calls; set the measured usage exactly once so the
    # assertion is robust to the internal call count.
    seen = {"set": False}

    def caller(member, prompt):
        if not seen["set"]:
            seen["set"] = True
            _usage_sink.last = {
                "input_tokens": 123, "output_tokens": 45,
                "cache_read_input_tokens": 9, "cache_write_input_tokens": None,
                "measured": True,
            }
        return _pm_turn()

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)

    turns = store.list_turns()
    assert len(turns) == 1
    usage = turns[0].get("usage")
    # F143-01 Slice C/D: the block now carries provenance + the split measured/
    # effective fields + estimator metadata. This fake caller writes only measured
    # usage (no estimate), so effective == measured and provenance == "measured".
    # The accumulator's estimated_* default to 0 for a fake caller that bypasses the
    # real estimator (the production caller always stamps a nonzero byte estimate).
    # The estimator method/factor come from the shared singleton (default 1.0).
    assert usage == {
        "measured": True, "provenance": "measured",
        "input_tokens": 123, "output_tokens": 45,
        "measured_input": 123, "measured_output": 45,
        "estimated_input": 0, "estimated_output": 0,
        "cache_read_input_tokens": 9,
        "estimator_method": "calibrated_heuristic_v1",
        "calibration_factor": 1.0,
    }


def test_unreported_provider_turn_has_no_usage_block(tmp_path: Path) -> None:
    store = _new_store(tmp_path, "cpunrep")

    # A caller that never touches the sink (a fake, or a provider like cursor_cli
    # that reports nothing). The turn must persist WITHOUT a usage block.
    def caller(member, prompt):
        return _pm_turn()

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)

    turns = store.list_turns()
    assert len(turns) == 1
    assert "usage" not in turns[0]


def test_stale_sink_does_not_leak_across_turns(tmp_path: Path) -> None:
    store = _new_store(tmp_path, "cpleak")

    calls = {"n": 0}

    def caller(member, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            _usage_sink.last = {"input_tokens": 500, "output_tokens": 50,
                                "measured": True}
        # second call writes nothing — its turn must not inherit the first's usage
        return _pm_turn()

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    rt(Plan(member_id="m-pm"), store)

    turns = store.list_turns()
    assert len(turns) == 2
    assert turns[0].get("usage", {}).get("input_tokens") == 500
    assert "usage" not in turns[1]


def test_gateway_member_caller_writes_usage_sink() -> None:
    class StubResult:
        content = '{"schema_version": "coding_turn.v1"}'
        input_tokens = 77
        output_tokens = 11
        cache_read_input_tokens = 4
        cache_write_input_tokens = None
        raw_usage_available = True

    class StubGateway:
        async def call(self, req):
            return StubResult()

    _usage_sink.last = None
    caller = gateway_member_caller(StubGateway())
    caller({"id": "m", "gateway_route_id": "r", "provider_kind": "local"}, "hi")
    # F143-01 Slice C: the real caller now also stamps its own byte estimate +
    # provider identity onto the sink (StubResult has empty provider_class/model).
    last = _usage_sink.last
    assert last["input_tokens"] == 77
    assert last["output_tokens"] == 11
    assert last["cache_read_input_tokens"] == 4
    assert last["cache_write_input_tokens"] is None
    assert last["measured"] is True
    # Estimated fields are always present (computed from prompt + result.content).
    assert isinstance(last["estimated_input"], int) and last["estimated_input"] >= 1
    assert isinstance(last["estimated_output"], int) and last["estimated_output"] >= 1
    assert last["provider_class"] == ""
    assert last["model"] == ""


def test_concurrent_turns_do_not_clobber_usage(tmp_path: Path) -> None:
    # The concurrent loop shares ONE run_turn closure across worker threads. Its
    # capture scratch (_cap) must be per-thread, or two overlapping turns clobber
    # each other's usage. Force overlap with a barrier: each thread sets its own
    # usage, then both are held mid-turn before either records.
    store = _new_store(tmp_path, "cpconc")
    values: dict[int, int] = {}
    barrier = threading.Barrier(2)
    seen: set[int] = set()

    def caller(member, prompt):
        tid = threading.get_ident()
        # Set the sink + hit the barrier ONCE per thread: once so usage doesn't
        # accumulate over the turn's multiple calls, barrier so both threads are
        # provably mid-turn (would clobber a shared _cap) before either records.
        if tid not in seen:
            seen.add(tid)
            _usage_sink.last = {"input_tokens": values[tid],
                                "output_tokens": values[tid], "measured": True}
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        return _pm_turn()

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)

    def worker(val: int):
        values[threading.get_ident()] = val
        rt(Plan(member_id="m-pm"), store)

    threads = [threading.Thread(target=worker, args=(v,)) for v in (1000, 2000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    got = sorted(t["usage"]["input_tokens"] for t in store.list_turns() if t.get("usage"))
    assert got == [1000, 2000]  # each thread's usage preserved, neither clobbered


def test_merge_call_usage_sums_measured_calls() -> None:
    acc = _merge_call_usage(None, {"measured": True, "input_tokens": 10,
                                   "output_tokens": 4, "cache_read_input_tokens": 2})
    acc = _merge_call_usage(acc, {"measured": True, "input_tokens": 20,
                                  "output_tokens": 6})
    # F143-01 Slice D: the accumulator now tracks per-call effective + measured/
    # estimated splits + call counts (see _merge_call_usage). Two measured calls,
    # no estimate → effective == measured, estimated sums stay 0.
    assert acc == {"estimated_input": 0, "estimated_output": 0,
                   "effective_input": 30, "effective_output": 10,
                   "cache_read": 2,
                   "measured_calls": 2, "total_calls": 2,
                   "measured_input": 30, "measured_output": 10}


def test_merge_call_usage_ignores_unmeasured_and_bad_values() -> None:
    base = {"measured": True, "input_tokens": 5}
    # unmeasured call contributes nothing
    assert _merge_call_usage(dict(base), {"measured": False, "input_tokens": 99}) == base
    # None call leaves acc untouched (and None acc stays None)
    assert _merge_call_usage(dict(base), None) == base
    assert _merge_call_usage(None, None) is None
    # measured flag but no usable numbers -> no block created
    assert _merge_call_usage(None, {"measured": True}) is None
    # F143-01 Slice D: negative / bool token values are dropped; a call left with
    # only a cache count (no valid in/out, no estimate, no provider meta) is
    # "nothing usable" and leaves the accumulator untouched — the cache would be
    # dropped at the block level anyway (D4: cache is detail, never a headline token).
    merged = _merge_call_usage(None, {"measured": True, "input_tokens": -3,
                                      "output_tokens": True, "cache_read_input_tokens": 7})
    assert merged is None


def test_gateway_member_caller_result_without_usage_marks_unmeasured() -> None:
    class BareResult:
        content = "{}"

    class StubGateway:
        async def call(self, req):
            return BareResult()

    _usage_sink.last = None
    caller = gateway_member_caller(StubGateway())
    caller({"id": "m", "gateway_route_id": "r", "provider_kind": "local"}, "hi")
    assert _usage_sink.last["measured"] is False
    assert _usage_sink.last["input_tokens"] is None
