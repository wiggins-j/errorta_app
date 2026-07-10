"""F146 Slice D — merge-back gate consistency for the delivery tests gate.

A genuinely test-less, non-runnable project has nothing a delivery test/launch
could run, so ``tests_missing`` must not block its accept gate forever. When
registered test commands OR a runnable runtime DO exist, the delivery test/launch
verdict is required as before.
"""
from pathlib import Path

from errorta_council.coding.evidence import _has_runnable_runtime, _tests_required
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d",
                     target="new", repo_path=None)
    return s


def test_tests_not_required_when_no_tests_and_no_runtime(tmp_path: Path) -> None:
    s = _store("d-none", tmp_path)
    assert _tests_required(s) is False
    assert _has_runnable_runtime(s) is False


def test_tests_required_with_registered_commands(tmp_path: Path) -> None:
    s = _store("d-tests", tmp_path)
    s.set_test_commands({"unit": {"argv": ["python", "-c", "pass"], "cwd": ".",
                                  "timeout_seconds": 30}})
    assert _tests_required(s) is True


def test_tests_required_with_runnable_runtime(tmp_path: Path) -> None:
    # A runtime profile with a `start` argv is runnable -> the delivery launch
    # gate applies even without registered unit-test commands.
    s = _store("d-runtime", tmp_path)
    RuntimeProfileStore.for_ledger(s).upsert_profile(RuntimeProfile(
        profile_id="p1", project_id="d-runtime", kind="desktop",
        runtime_mode="managed_local", start=["python", "game.py"]))
    assert _has_runnable_runtime(s) is True
    assert _tests_required(s) is True


def test_runtime_without_start_is_not_runnable(tmp_path: Path) -> None:
    # A profile with an empty `start` cannot be launched -> not a runnable runtime.
    s = _store("d-norun", tmp_path)
    RuntimeProfileStore.for_ledger(s).upsert_profile(RuntimeProfile(
        profile_id="p1", project_id="d-norun", kind="unknown", start=[]))
    assert _has_runnable_runtime(s) is False
    assert _tests_required(s) is False
