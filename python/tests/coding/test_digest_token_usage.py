"""F143 — the project digest caches the token total, idempotently."""
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.usage_rollup import rollup_turns


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("cdig", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d",
                     target="new", repo_path=None)
    return s


def test_digest_token_usage_matches_rollup_and_is_idempotent(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.record_turn(role="dev", member_id="m1", task_id="t1", prompt="p", response="r",
                  outcome="applied", input_tokens=10, output_tokens=4, measured=True)
    s.record_turn(role="dev", member_id="m1", task_id="t1", prompt="p", response="r",
                  outcome="applied", input_tokens=20, output_tokens=6, measured=True)
    # an unreported turn (provider reported nothing) still counts toward coverage
    s.record_turn(role="dev", member_id="m2", task_id="t2", prompt="p", response="r",
                  outcome="applied", measured=False)

    expected = rollup_turns(s.list_turns())["total"]
    d1 = s.regenerate_digest()
    assert d1["token_usage"] == expected
    assert d1["token_usage"]["input"] == 30 and d1["token_usage"]["output"] == 10
    assert d1["token_usage"]["unreported_turns"] == 1

    # regenerating again must not drift or double-count (rebuilt from the full list)
    d2 = s.regenerate_digest()
    assert d2["token_usage"] == d1["token_usage"]


def test_digest_token_usage_zeroed_when_no_turns(tmp_path: Path) -> None:
    s = _store(tmp_path)
    d = s.regenerate_digest()
    assert d["token_usage"]["input"] == 0 and d["token_usage"]["turns"] == 0
