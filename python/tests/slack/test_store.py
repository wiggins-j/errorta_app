from __future__ import annotations

import stat
from pathlib import Path

import pytest

from errorta_slack import store


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- Bindings ---------------------------------------------------------


def test_binding_for_unbound_channel_returns_none() -> None:
    assert store.binding_for("C123") is None


def test_bind_lookup_unbind_round_trip() -> None:
    store.bind_channel("C123", "errorta")

    binding = store.binding_for("C123")
    assert binding is not None
    assert binding["channel_id"] == "C123"
    assert binding["project_id"] == "errorta"

    store.unbind("C123")

    assert store.binding_for("C123") is None


def test_list_bindings_returns_all() -> None:
    store.bind_channel("C123", "errorta")
    store.bind_channel("C456", "aiar")

    bindings = store.list_bindings()

    channel_ids = {b["channel_id"] for b in bindings}
    assert channel_ids == {"C123", "C456"}


def test_bind_channel_overwrites_existing_binding() -> None:
    store.bind_channel("C123", "errorta")
    store.bind_channel("C123", "aiar")

    binding = store.binding_for("C123")
    assert binding is not None
    assert binding["project_id"] == "aiar"
    assert len(store.list_bindings()) == 1


def test_unbind_unknown_channel_is_a_no_op() -> None:
    store.unbind("C999")  # must not raise


def test_channel_for_project_finds_bound_channel() -> None:
    store.bind_channel("C1", "p1")
    store.bind_channel("C2", "p2")

    assert store.channel_for_project("p1") == "C1"
    assert store.channel_for_project("p2") == "C2"


def test_channel_for_project_returns_none_for_unknown_project() -> None:
    store.bind_channel("C1", "p1")

    assert store.channel_for_project("nope") is None


def test_channel_for_project_ignores_studio_channel() -> None:
    store.bind_channel("C1", "p1")
    store.set_studio_channel("Cstudio")

    assert store.channel_for_project("p1") == "C1"


def test_channel_for_project_returns_none_after_unbind() -> None:
    store.bind_channel("C1", "p1")
    store.unbind("C1")

    assert store.channel_for_project("p1") is None


def test_bindings_file_written_with_owner_only_permissions() -> None:
    store.bind_channel("C123", "errorta")

    from errorta_slack import config

    path = config.slack_dir() / "bindings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- Outbound cursor ----------------------------------------------------


def test_get_cursor_for_unknown_channel_returns_none() -> None:
    assert store.get_cursor("C123") is None


def test_advance_cursor_sets_and_updates_marker() -> None:
    store.advance_cursor("C123", "marker-1")
    assert store.get_cursor("C123") == "marker-1"

    store.advance_cursor("C123", "marker-2")
    assert store.get_cursor("C123") == "marker-2"


def test_advance_cursor_to_same_marker_is_idempotent() -> None:
    store.advance_cursor("C123", "marker-1")
    store.advance_cursor("C123", "marker-1")

    assert store.get_cursor("C123") == "marker-1"


def test_advance_cursor_is_per_channel() -> None:
    store.advance_cursor("C123", "marker-1")
    store.advance_cursor("C456", "marker-2")

    assert store.get_cursor("C123") == "marker-1"
    assert store.get_cursor("C456") == "marker-2"


# --- Dedupe ---------------------------------------------------------------


def test_seen_event_returns_false_then_true() -> None:
    assert store.seen_event("evt-1") is False
    assert store.seen_event("evt-1") is True


def test_seen_event_distinct_ids_independent() -> None:
    assert store.seen_event("evt-1") is False
    assert store.seen_event("evt-2") is False
    assert store.seen_event("evt-1") is True


def test_seen_event_bounded_to_512_ids() -> None:
    for i in range(600):
        store.seen_event(f"evt-{i}")

    # The oldest ids should have been evicted; the most recent 512 remain.
    assert store.seen_event("evt-599") is True
    assert store.seen_event("evt-0") is False


# --- Confirmations ----------------------------------------------------


def test_stage_confirmation_returns_pending_record() -> None:
    cid = store.stage_confirmation("deploy", {"target": "prod"}, "1699999999.000100")

    record = store.get_confirmation(cid)
    assert record is not None
    assert record["id"] == cid
    assert record["verb"] == "deploy"
    assert record["args"] == {"target": "prod"}
    assert record["thread_ts"] == "1699999999.000100"
    assert record["state"] == "pending"
    assert "created_at" in record


def test_get_confirmation_unknown_id_returns_none() -> None:
    assert store.get_confirmation("nope") is None


def test_resolve_confirmation_sets_state_and_returns_record() -> None:
    cid = store.stage_confirmation("deploy", {"target": "prod"}, "ts-1")

    resolved, claimed = store.resolve_confirmation(cid, "approved")

    assert claimed is True
    assert resolved["state"] == "approved"
    assert store.get_confirmation(cid)["state"] == "approved"


def test_resolve_confirmation_second_call_does_not_reclaim() -> None:
    """Task 9 carry-over: resolution is a CLAIM, not a blind state write --
    only the FIRST resolver of a given confirmation gets claimed=True. A
    second resolve on the same (already-resolved) id must report
    claimed=False and must not stomp the first decision, so a caller can
    safely gate firing an effect on the return value alone."""
    cid = store.stage_confirmation("deploy", {"target": "prod"}, "ts-1")

    first_record, first_claimed = store.resolve_confirmation(cid, "approved")
    second_record, second_claimed = store.resolve_confirmation(cid, "declined")

    assert first_claimed is True
    assert first_record["state"] == "approved"
    assert second_claimed is False
    assert second_record["state"] == "approved"  # untouched by the losing call
    assert store.get_confirmation(cid)["state"] == "approved"


def test_resolve_confirmation_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        store.resolve_confirmation("nope", "approved")


def test_stage_confirmation_records_channel_id() -> None:
    cid = store.stage_confirmation(
        "deploy", {"target": "prod"}, "ts-1", channel_id="C123",
    )

    record = store.get_confirmation(cid)
    assert record is not None
    assert record["channel_id"] == "C123"


def test_stage_confirmation_channel_id_defaults_to_empty_string() -> None:
    cid = store.stage_confirmation("deploy", {"target": "prod"}, "ts-1")

    record = store.get_confirmation(cid)
    assert record is not None
    assert record["channel_id"] == ""


def test_stage_confirmation_ids_are_unique() -> None:
    cid1 = store.stage_confirmation("deploy", {}, "ts-1")
    cid2 = store.stage_confirmation("deploy", {}, "ts-2")

    assert cid1 != cid2


def test_pop_pending_older_than_returns_and_resolves_stale() -> None:
    cid_old = store.stage_confirmation("deploy", {}, "ts-1", now=1000.0)
    cid_new = store.stage_confirmation("deploy", {}, "ts-2", now=1900.0)

    popped = store.pop_pending_older_than(500.0, now=2000.0)

    popped_ids = {r["id"] for r in popped}
    assert popped_ids == {cid_old}
    assert store.get_confirmation(cid_old)["state"] == "timed_out"
    assert store.get_confirmation(cid_new)["state"] == "pending"


def test_pop_pending_older_than_ignores_already_resolved() -> None:
    cid = store.stage_confirmation("deploy", {}, "ts-1", now=1000.0)
    store.resolve_confirmation(cid, "approved")

    popped = store.pop_pending_older_than(0.0, now=2000.0)

    assert popped == []


# --- Prefs --------------------------------------------------------------


def test_get_prefs_for_unknown_channel_returns_empty_dict() -> None:
    assert store.get_prefs("C123") == {}


def test_set_pref_then_get_prefs_round_trips() -> None:
    store.set_pref("C123", "quiet_hours", True)
    store.set_pref("C123", "digest", "daily")

    prefs = store.get_prefs("C123")

    assert prefs == {"quiet_hours": True, "digest": "daily"}


def test_set_pref_is_per_channel() -> None:
    store.set_pref("C123", "digest", "daily")
    store.set_pref("C456", "digest", "weekly")

    assert store.get_prefs("C123") == {"digest": "daily"}
    assert store.get_prefs("C456") == {"digest": "weekly"}


def test_concurrent_stage_confirmation_loses_no_records() -> None:
    """Task 8 review finding: store.py's read-modify-write was unguarded, so
    two threads staging a confirmation at the same time could race -- both
    load the same stale confirmations.json, and the second writer clobbers
    the first's record entirely (not just a field -- the whole entry, and
    its Approve button, vanish). Guards against a regression of the
    threading.RLock fix in every mutating function."""
    import concurrent.futures

    n = 64

    def stage(i: int) -> str:
        return store.stage_confirmation("spend_cloud", {"amount": i}, f"thread-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        cids = list(pool.map(stage, range(n)))

    assert len(cids) == len(set(cids)) == n
    for cid in cids:
        record = store.get_confirmation(cid)
        assert record is not None, f"confirmation {cid} was lost to a race"
        assert record["state"] == "pending"


def test_concurrent_resolve_confirmation_loses_no_updates() -> None:
    """Same lost-update race, on the resolve path: many threads resolving
    DIFFERENT confirmations concurrently must not clobber each other."""
    import concurrent.futures

    n = 64
    cids = [store.stage_confirmation("spend_cloud", {"amount": i}, f"t-{i}") for i in range(n)]

    def resolve(cid: str) -> None:
        store.resolve_confirmation(cid, "approved")

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(resolve, cids))

    for cid in cids:
        record = store.get_confirmation(cid)
        assert record is not None
        assert record["state"] == "approved", f"confirmation {cid} update was lost to a race"


# --- Studio channel (singleton) -----------------------------------------


def test_studio_channel_unset_returns_none() -> None:
    assert store.studio_channel() is None
    assert store.is_studio("C1") is False


def test_set_studio_channel_then_studio_channel_and_is_studio() -> None:
    store.set_studio_channel("C1")

    assert store.studio_channel() == "C1"
    assert store.is_studio("C1") is True
    assert store.is_studio("C2") is False


def test_set_studio_channel_overwrites_singleton() -> None:
    store.set_studio_channel("C1")
    store.set_studio_channel("C2")

    assert store.studio_channel() == "C2"
    assert store.is_studio("C1") is False
    assert store.is_studio("C2") is True


def test_clear_studio_channel_resets_to_none() -> None:
    store.set_studio_channel("C1")
    store.clear_studio_channel()

    assert store.studio_channel() is None
    assert store.is_studio("C1") is False


def test_clear_studio_channel_when_unset_is_a_no_op() -> None:
    store.clear_studio_channel()  # must not raise

    assert store.studio_channel() is None


def test_studio_channel_independent_of_bindings() -> None:
    store.bind_channel("Cproj", "p1")
    store.set_studio_channel("Cstudio")

    binding = store.binding_for("Cproj")
    assert binding is not None
    assert binding["project_id"] == "p1"
    assert store.studio_channel() == "Cstudio"


def test_studio_file_written_with_owner_only_permissions() -> None:
    store.set_studio_channel("C1")

    from errorta_slack import config

    path = config.slack_dir() / "studio.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_module_does_not_import_slack_sdk() -> None:
    """store.py must not pull in slack_sdk at module load.

    Order-independent by construction: this checks store.py's OWN bound
    names and source text, not process-global ``sys.modules`` — a sibling
    test module (e.g. test_connection.py, whose connection.py guard-imports
    slack_sdk when it's actually installed) importing slack_sdk earlier in
    the same pytest session must not make this assertion false-fail.
    """
    import inspect

    assert "slack_sdk" not in vars(store)
    source = inspect.getsource(store)
    assert "import slack_sdk" not in source
