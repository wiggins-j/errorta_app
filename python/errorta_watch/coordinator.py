"""Singleton coordinator for per-corpus folder watchers."""
from __future__ import annotations

import datetime as _dt
import threading
from typing import Iterable

from .ingest_bridge import ingest_via_pipeline
from .poller import FolderPoller
from .state import (
    WatchState,
    list_persisted_corpora,
    load_state,
    save_state,
    state_path,
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# F005-PROD: zombie threshold — heartbeat older than this means watcher stale.
HEARTBEAT_STALE_SECONDS = 90.0


def _heartbeat_age_seconds(last_heartbeat: str | None) -> float | None:
    if not last_heartbeat:
        return None
    try:
        ts = _dt.datetime.fromisoformat(last_heartbeat)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    return max(0.0, (now - ts).total_seconds())


class WatchCoordinator:
    """Manages the set of running ``FolderPoller`` threads."""

    def __init__(self) -> None:
        self._pollers: dict[str, FolderPoller] = {}
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------

    def restore_from_disk(self) -> list[str]:
        """Restart pollers for every corpus with a persisted ``watch.json``."""
        restored: list[str] = []
        for corpus in list_persisted_corpora():
            state = load_state(corpus)
            if state is None:
                continue
            with self._lock:
                if corpus in self._pollers:
                    continue
                poller = FolderPoller(state, ingest_hook=ingest_via_pipeline)
                self._pollers[corpus] = poller
            # initial_scan=False — restored state already has a manifest;
            # the next polling tick will reconcile.
            poller.start(initial_scan=False)
            restored.append(corpus)
        return restored

    def shutdown(self) -> None:
        with self._lock:
            pollers = list(self._pollers.values())
            self._pollers.clear()
        for p in pollers:
            p.stop()

    # ---- per-corpus operations ------------------------------------------

    def start(
        self,
        corpus: str,
        watched_path: str,
        *,
        deletion_policy: str = "remove",
        type_filter: Iterable[str] = (),
        extra_ignores: Iterable[str] = (),
    ) -> dict:
        """Begin watching ``watched_path`` for ``corpus``.

        Raises ValueError if a watcher is already running for that corpus —
        the caller should ``change_path`` instead.
        """
        with self._lock:
            if corpus in self._pollers and self._pollers[corpus].is_alive():
                raise ValueError(f"corpus '{corpus}' is already being watched")

            state = WatchState(
                corpus=corpus,
                watched_path=watched_path,
                started_at=_now_iso(),
                deletion_policy=deletion_policy,  # type: ignore[arg-type]
                type_filter=list(type_filter),
                extra_ignores=list(extra_ignores),
            )
            save_state(state)
            poller = FolderPoller(state, ingest_hook=ingest_via_pipeline)
            self._pollers[corpus] = poller

        poller.start(initial_scan=True)
        return self.status(corpus)

    def stop(self, corpus: str) -> bool:
        with self._lock:
            poller = self._pollers.pop(corpus, None)
        if poller is None:
            return False
        poller.stop()
        # Stop is durable: remove the persisted state so the next sidecar
        # restart does not silently re-spawn this watcher via restore_from_disk.
        try:
            state_path(corpus).unlink()
        except FileNotFoundError:
            pass
        return True

    def pause(self, corpus: str) -> bool:
        with self._lock:
            poller = self._pollers.get(corpus)
        if poller is None:
            return False
        poller.pause()
        return True

    def resume(self, corpus: str) -> bool:
        with self._lock:
            poller = self._pollers.get(corpus)
        if poller is None:
            return False
        poller.resume()
        return True

    def change_path(self, corpus: str, new_path: str) -> dict:
        """Stop the current watcher and start a fresh one at ``new_path``.

        Per the spec, this does not delete already-ingested files — the new
        watcher's manifest starts empty, so anything ingested under the old
        path is left in place.
        """
        old = load_state(corpus)
        if old is None:
            raise ValueError(f"no watch state for corpus '{corpus}'")

        self.stop(corpus)
        return self.start(
            corpus,
            new_path,
            deletion_policy=old.deletion_policy,
            type_filter=old.type_filter,
            extra_ignores=old.extra_ignores,
        )

    def set_deletion_policy(self, corpus: str, policy: str) -> dict:
        if policy not in ("remove", "mark_missing"):
            raise ValueError(f"unknown deletion policy: {policy}")
        with self._lock:
            poller = self._pollers.get(corpus)
        if poller is None:
            state = load_state(corpus)
            if state is None:
                raise ValueError(f"no watch state for corpus '{corpus}'")
            state.deletion_policy = policy  # type: ignore[assignment]
            save_state(state)
            return _state_summary(state, alive=False)
        poller.state.deletion_policy = policy  # type: ignore[assignment]
        save_state(poller.state)
        return _state_summary(poller.state, alive=poller.is_alive())

    def status(self, corpus: str) -> dict:
        with self._lock:
            poller = self._pollers.get(corpus)
        state = poller.state if poller else load_state(corpus)
        if state is None:
            return {"corpus": corpus, "watching": False}
        return _state_summary(state, alive=bool(poller and poller.is_alive()))

    def status_all(self) -> list[dict]:
        with self._lock:
            corpora = list(self._pollers.keys())
        return [self.status(c) for c in corpora]

    def force_rescan(self, corpus: str) -> dict:
        """Trigger an immediate ``run_once`` for ``corpus`` and return status.

        Non-blocking with respect to an in-flight scan: if the poller is
        already mid-scan (which may include up to 6s of backoff sleep per
        failing file), this returns the current status immediately rather
        than queueing behind the running scan. The status dict carries a
        ``rescan_started`` boolean so callers can tell the difference.
        """
        with self._lock:
            poller = self._pollers.get(corpus)
        if poller is None:
            raise ValueError(f"not watching: {corpus}")
        ran, _ = poller.try_run_once()
        status = self.status(corpus)
        status["rescan_started"] = ran
        return status


def _state_summary(state: WatchState, *, alive: bool) -> dict:
    age = _heartbeat_age_seconds(state.last_heartbeat)
    # Stale only meaningful for live, unpaused watchers with a heartbeat.
    stale = bool(
        alive
        and not state.paused
        and age is not None
        and age > HEARTBEAT_STALE_SECONDS
    )
    return {
        "corpus": state.corpus,
        "watching": True,
        "alive": alive,
        "watched_path": state.watched_path,
        "started_at": state.started_at,
        "deletion_policy": state.deletion_policy,
        "type_filter": list(state.type_filter),
        "extra_ignores": list(state.extra_ignores),
        "last_scan_at": state.last_scan_at,
        "last_scan_ok": state.last_scan_ok,
        "last_error": state.last_error,
        "last_heartbeat": state.last_heartbeat,
        "heartbeat_age_seconds": age,
        "stale": stale,
        "paused": state.paused,
        "file_count": len(state.manifest),
    }


# ---- module-level singleton ---------------------------------------------

_coordinator: WatchCoordinator | None = None
_singleton_lock = threading.Lock()


def get_coordinator() -> WatchCoordinator:
    global _coordinator
    with _singleton_lock:
        if _coordinator is None:
            _coordinator = WatchCoordinator()
        return _coordinator
