"""The poller tail-diffs append-only ledgers into events with stable cursors.

Locks: appending to a source surfaces exactly the new entries (no dupes, stable
ids); snapshots emit only on change (primed silently); and the golden invariant #4
— the poller only ever hits the CLI's OWN sidecar (the injected client), never a
probed foreign one.
"""
from __future__ import annotations

from errorta_cli.poller import DEFAULT_SOURCES, Event, Poller, Source, events_for_view
from errorta_cli.verbosity import Level, Verbosity

PID = "proj-1"


class GrowingClient:
    """A fake sidecar whose ledgers grow between polls. Records every path so we
    can assert the poller talks ONLY to this client (its own sidecar)."""

    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1:59999"
        self.calls: list[str] = []
        self.state: dict[str, object] = {}

    def get_json(self, path: str, *, params=None):
        self.calls.append(path)
        for key, payload in self.state.items():
            if key in path:
                return payload
        return {}


def _decisions(*ids):
    return {"decisions": [{"decision_id": i, "choice": "pr_opened", "title": i} for i in ids]}


def test_append_source_emits_only_new_entries_with_stable_ids():
    client = GrowingClient()
    src = Source("decisions", "/coding/projects/{project_id}/decisions", "decisions",
                 "append", key="decisions", id_field="decision_id")
    poller = Poller(client, PID, sources=[src])

    client.state["/decisions"] = _decisions("d1", "d2")
    first = poller.poll_source(src)
    assert [e.item["decision_id"] for e in first] == ["d1", "d2"]

    # Re-poll with no change → no dupes.
    assert poller.poll_source(src) == []

    # Append d3 → exactly one new event; cursor is stable (d1/d2 not re-emitted).
    client.state["/decisions"] = _decisions("d1", "d2", "d3")
    third = poller.poll_source(src)
    assert [e.item["decision_id"] for e in third] == ["d3"]
    assert poller.cursors["decisions"].order == ["d1", "d2", "d3"]


def test_append_falls_back_to_content_hash_without_id_field():
    client = GrowingClient()
    # team-log entries have no id field → stable content hash keeps dedupe correct.
    src = Source("team-log", "/coding/projects/{project_id}/team-log", "team-log",
                 "append", key="entries")
    poller = Poller(client, PID, sources=[src])
    client.state["/team-log"] = {"entries": [{"at": "t", "message": "a"}]}
    assert len(poller.poll_source(src)) == 1
    assert poller.poll_source(src) == []  # same entry, no dupe
    client.state["/team-log"] = {"entries": [{"at": "t", "message": "a"},
                                             {"at": "t", "message": "b"}]}
    new = poller.poll_source(src)
    assert [e.item["message"] for e in new] == ["b"]


def test_snapshot_source_primes_silently_then_emits_on_change():
    client = GrowingClient()
    src = Source("run", "/coding/projects/{project_id}/run", "poll", "snapshot")
    poller = Poller(client, PID, sources=[src])
    client.state["/run"] = {"state": {"status": "running"}}
    assert poller.poll_source(src) == []  # first poll primes, no spurious event
    assert poller.poll_source(src) == []  # unchanged → nothing
    client.state["/run"] = {"state": {"status": "stopped", "stop_reason": "done"}}
    changed = poller.poll_source(src)
    assert len(changed) == 1 and changed[0].kind == "changed"


def test_poll_once_covers_all_sources_and_assigns_monotonic_seq():
    client = GrowingClient()
    client.state["/decisions"] = _decisions("d1")
    client.state["/team-log"] = {"entries": [{"at": "t", "message": "m"}]}
    poller = Poller(client, PID, sources=[
        Source("decisions", "/coding/projects/{project_id}/decisions", "decisions",
               "append", key="decisions", id_field="decision_id"),
        Source("team-log", "/coding/projects/{project_id}/team-log", "team-log",
               "append", key="entries"),
    ])
    events = poller.poll_once()
    assert {e.source for e in events} == {"decisions", "team-log"}
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_poller_only_hits_own_sidecar_base_url():
    client = GrowingClient()
    poller = Poller(client, PID)  # full DEFAULT_SOURCES
    # Invariant #4: the poller's base url is the injected client's own.
    assert poller.base_url == client.base_url
    poller.poll_once()
    # Every request went through the injected client (no foreign probe).
    assert client.calls, "poller made no requests"
    assert all(p.startswith("/coding/") or p == "/healthz" for p in client.calls)
    # DEFAULT_SOURCES cover the append-only ledgers named in the spec.
    channels = {s.channel for s in DEFAULT_SOURCES}
    assert {"team-log", "decisions", "turns", "prs", "attention", "tokens"} <= channels


def test_run_loop_is_bounded_and_backs_off_when_idle():
    client = GrowingClient()
    client.state["/decisions"] = _decisions("d1")
    poller = Poller(
        client, PID,
        sources=[Source("decisions", "/coding/projects/{project_id}/decisions",
                        "decisions", "append", key="decisions", id_field="decision_id")],
        interval_override=1.0, clock=lambda: 0.0,
    )
    slept: list[float] = []
    seen: list[Event] = []
    poller.run(seen.append, iterations=3, sleep=slept.append)
    # First tick emits d1; later ticks are idle → sleeps grow (adaptive backoff).
    assert [e.item["decision_id"] for e in seen] == ["d1"]
    assert len(slept) == 2  # iterations-1 sleeps (no sleep after the final frame)
    assert slept[-1] > slept[0]  # backoff grew after idle ticks


def test_events_for_view_filters_by_verbosity_channel():
    events = [Event(1, "team-log", "team-log", "append", {}),
              Event(2, "turns", "turns", "append", {})]
    v = Verbosity(level=Level.DEFAULT)  # team-log on (L1), turns off (needs L3)
    filtered = events_for_view(events, v)
    assert [e.channel for e in filtered] == ["team-log"]
