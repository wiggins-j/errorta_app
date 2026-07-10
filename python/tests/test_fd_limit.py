"""The sidecar raises its RLIMIT_NOFILE soft limit at startup so a concurrent
Coding run doesn't crash with EMFILE (Too many open files)."""
from __future__ import annotations

import resource

from errorta_app import server


def test_raises_soft_limit_toward_target(monkeypatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 1_048_576))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    server._raise_fd_limit()
    assert calls == [(resource.RLIMIT_NOFILE, (server._TARGET_FD_SOFT_LIMIT, 1_048_576))]


def test_caps_target_at_hard_limit(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 4096))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append(limits))
    server._raise_fd_limit()
    assert calls == [(4096, 4096)]  # never request more than the hard limit


def test_noop_when_already_high(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        resource, "getrlimit", lambda which: (server._TARGET_FD_SOFT_LIMIT, 1_048_576))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append(limits))
    server._raise_fd_limit()
    assert calls == []  # don't touch an already-sufficient (or higher) soft limit


def test_never_raises_on_error(monkeypatch) -> None:
    def boom(which):  # noqa: ANN001
        raise OSError("nope")

    monkeypatch.setattr(resource, "getrlimit", boom)
    server._raise_fd_limit()  # must not propagate
