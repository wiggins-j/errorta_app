"""Background poller — the client-side event synthesizer (F147 §4.4).

There is no SSE; the app polls, so the CLI does too. To make the live view feel
real-time, the poller **tail-diffs** the append-only ledgers (team-log, decisions,
turns, tool-events, prs, attention) and diffs the snapshot reads (run state,
usage-summary, runtime profiles), emitting **synthetic events** to the active
view(s). Because the ledgers are append-only with stable ids, "what's new since
cursor X" is a cheap set difference; snapshot sources emit only when their content
hash changes (primed silently on the first poll so start isn't a spurious change).

**Golden invariant #4 — the poller only ever hits the CLI's OWN sidecar.** It uses
the injected :class:`~errorta_cli.client.SidecarClient` exclusively (bound to
``sidecar.resolve()``'s base url); it never probes a port, never constructs its own
transport, never talks to a foreign sidecar. ``test_poller_tail_diff`` locks this.

Cadence mirrors the app (run/main 2.5s, team-log/usage 4s, runtime 2s) via
per-source intervals, with a ``--poll-interval`` override and adaptive backoff when
idle. ``poll_once`` (used by tests + a single ``--watch`` frame) ignores the
per-source timers and polls everything once.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .errors import CliError


@dataclass(frozen=True)
class Source:
    """One pollable ledger/snapshot and the verbosity channel it feeds."""

    name: str
    path: str  # ``str.format(project_id=...)`` template
    channel: str
    mode: str  # "append" (list tail-diff) | "snapshot" (hash-diff)
    key: str = ""  # the list key inside the payload (append mode)
    interval: float = 2.5
    id_field: str | None = None  # stable-id field (append mode); else a content hash
    params: dict[str, Any] | None = None


# The default source set (spec §4.4). Intervals mirror the app's poll cadence.
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source("run", "/coding/projects/{project_id}/run", "poll", "snapshot", interval=2.5),
    Source("team-log", "/coding/projects/{project_id}/team-log", "team-log", "append",
           key="entries", interval=4.0),
    Source("decisions", "/coding/projects/{project_id}/decisions", "decisions", "append",
           key="decisions", id_field="decision_id", interval=2.5),
    Source("turns", "/coding/projects/{project_id}/turns", "turns", "append",
           key="turns", id_field="turn_id", interval=2.5),
    Source("tool-events", "/coding/projects/{project_id}/tool-events", "tools", "append",
           key="tool_events", id_field="event_id", interval=2.5),
    Source("prs", "/coding/projects/{project_id}/prs", "prs", "append",
           key="prs", interval=2.5),
    Source("attention", "/coding/projects/{project_id}/attention", "attention", "append",
           key="signals", interval=2.5),
    Source("tokens", "/coding/projects/{project_id}/usage-summary", "tokens", "snapshot",
           interval=4.0),
    Source("runtime", "/coding/projects/{project_id}/runtime/profiles", "runtime", "snapshot",
           interval=2.0),
)


@dataclass
class Event:
    """A synthetic event: one new ledger entry (append) or a changed snapshot."""

    seq: int
    channel: str
    source: str
    kind: str  # "append" | "changed"
    item: Any


# Cap on remembered append-ledger ids per source. ``last_id`` is the high-water
# mark for full chronological ledgers; this bounded set is the fallback dedupe
# window for bounded-tail routes where the previous high-water id has fallen out
# of the returned payload.
_SEEN_CAP = 4096


@dataclass
class _Cursor:
    seen: set[str] = field(default_factory=set)
    order: list[str] = field(default_factory=list)
    last_id: str | None = None
    snapshot_hash: str | None = None
    next_due: float = 0.0

    def remember(self, iid: str, cap: int | None = None) -> None:
        """Record a freshly-seen id, evicting the oldest once past ``cap``."""
        cap = _SEEN_CAP if cap is None else cap
        self.seen.add(iid)
        self.order.append(iid)
        overflow = len(self.order) - cap
        if overflow > 0:
            for old in self.order[:overflow]:
                self.seen.discard(old)
            del self.order[:overflow]


def _stable_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()  # noqa: S324 — cursor id, not security


class Poller:
    """Tail-diff the append-only ledgers + snapshots into synthetic events."""

    def __init__(
        self,
        client: Any,
        project_id: str,
        *,
        verbosity: Any | None = None,
        sources: Iterable[Source] = DEFAULT_SOURCES,
        interval_override: float | None = None,
        backoff_max: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # The ONLY sidecar handle the poller ever touches (invariant #4). No port
        # probe, no self-built transport — everything routes through this client,
        # which is bound to the CLI's own sidecar via sidecar.resolve().
        self.client = client
        self.project_id = project_id
        self.verbosity = verbosity
        self._clock = clock
        self._seq = 0
        self._backoff = 1.0
        self._backoff_max = max(1.0, backoff_max)
        srcs = list(sources)
        if interval_override is not None:
            srcs = [Source(**{**s.__dict__, "interval": interval_override}) for s in srcs]
        self.sources: dict[str, Source] = {s.name: s for s in srcs}
        self.cursors: dict[str, _Cursor] = {s.name: _Cursor() for s in srcs}
        self.base_interval = (
            interval_override
            if interval_override is not None
            else min((s.interval for s in srcs), default=2.5)
        )

    @property
    def base_url(self) -> str | None:
        """The sidecar base url the poller talks to — always the client's own."""
        return getattr(self.client, "base_url", None)

    # -- diffing -------------------------------------------------------------
    def _id_of(self, source: Source, item: Any) -> str:
        if source.id_field and isinstance(item, dict) and item.get(source.id_field):
            return str(item[source.id_field])
        return _stable_hash(item)

    def _emit(self, source: Source, kind: str, item: Any) -> Event:
        self._seq += 1
        return Event(self._seq, source.channel, source.name, kind, item)

    def poll_source(self, source: Source) -> list[Event]:
        """Fetch one source and return its new events (append) / change (snapshot)."""
        cur = self.cursors[source.name]
        try:
            payload = self.client.get_json(
                source.path.format(project_id=self.project_id), params=source.params
            )
        except CliError:
            # Observability must never crash the view; a transient error is just
            # "no new events this tick".
            return []
        events: list[Event] = []
        if source.mode == "append":
            items = (payload or {}).get(source.key) or []
            indexed = [(self._id_of(source, item), item) for item in items]
            ids = [iid for iid, _item in indexed]
            if cur.last_id is None:
                candidates = indexed
            else:
                try:
                    last_index = ids.index(cur.last_id)
                    candidates = indexed[last_index + 1:]
                except ValueError:
                    # Bounded-tail route where our previous high-water id fell
                    # off the returned window. Fall back to the recent dedupe
                    # set and emit only entries not seen in that retained window.
                    candidates = [(iid, item) for iid, item in indexed if iid not in cur.seen]
            for iid, item in candidates:
                if iid in cur.seen:
                    continue
                cur.remember(iid)
                events.append(self._emit(source, "append", item))
            if ids:
                cur.last_id = ids[-1]
        else:  # snapshot
            digest = _stable_hash(payload)
            if cur.snapshot_hash is None:
                cur.snapshot_hash = digest  # prime silently; start isn't a change
            elif digest != cur.snapshot_hash:
                cur.snapshot_hash = digest
                events.append(self._emit(source, "changed", payload))
        return events

    def poll_once(self, *, channels: set[str] | None = None) -> list[Event]:
        """Poll every source once (ignoring per-source timers). Test/frame entry."""
        out: list[Event] = []
        for source in self.sources.values():
            if channels is not None and source.channel not in channels:
                continue
            out.extend(self.poll_source(source))
        return out

    # -- the live loop -------------------------------------------------------
    def run(
        self,
        on_event: Callable[[Event], None],
        *,
        should_stop: Callable[[], bool] | None = None,
        channels: set[str] | None = None,
        iterations: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Poll due sources on their cadence, emitting events until stopped.

        Adaptive backoff: when a tick produces no events the sleep grows (capped at
        ``base_interval * backoff_max``); any event resets it. ``iterations`` bounds
        the loop for tests; production passes ``should_stop`` (Ctrl-C / cancel).
        """
        count = 0
        while True:
            if should_stop is not None and should_stop():
                return
            now = self._clock()
            tick: list[Event] = []
            for source in self.sources.values():
                if channels is not None and source.channel not in channels:
                    continue
                cur = self.cursors[source.name]
                if now < cur.next_due:
                    continue
                cur.next_due = now + source.interval
                tick.extend(self.poll_source(source))
            for event in sorted(tick, key=lambda e: e.seq):
                on_event(event)
            self._backoff = 1.0 if tick else min(self._backoff * 1.5, self._backoff_max)
            count += 1
            if iterations is not None and count >= iterations:
                return
            sleep(self.base_interval * self._backoff)


def events_for_view(events: Iterable[Event], verbosity: Any) -> list[Event]:
    """Filter events to the channels the current verbosity dial would stream."""
    if verbosity is None:
        return list(events)
    return [e for e in events if verbosity.should_emit(e.channel)]
