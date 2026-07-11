"""Shared helpers for the errorta_cli test suite.

These tests exercise the CLI as a pure sidecar *client* — no real sidecar, no
engine, no AIAR. HTTP is mocked (``httpx.MockTransport``) or replaced by a
recording double, so the whole suite runs in a minimal env with only
``typer``/``rich``/``prompt_toolkit``/``httpx``/``pytest`` installed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from errorta_cli.session import Context
from errorta_cli.verbosity import Verbosity


class RecordingClient:
    """A stand-in for :class:`~errorta_cli.client.SidecarClient`.

    Records every ``(method, path)`` so a test can assert two invocation
    surfaces (argv vs slash) hit the identical route sequence, and returns a
    benign JSON-ish payload so renderers don't blow up.
    """

    def __init__(self, response: Any | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._response = response if response is not None else {}

    def _record(self, method: str, path: str) -> Any:
        self.calls.append((method, path))
        return self._response

    def get_json(self, path: str, *, params: dict | None = None) -> Any:
        return self._record("GET", path)

    def post_json(self, path: str, *, json: Any | None = None, params: dict | None = None) -> Any:
        return self._record("POST", path)

    def put_json(self, path: str, *, json: Any | None = None, params: dict | None = None) -> Any:
        return self._record("PUT", path)

    def delete_json(self, path: str, *, params: dict | None = None) -> Any:
        return self._record("DELETE", path)


@pytest.fixture
def make_ctx(tmp_path: Path):
    """Factory for a :class:`Context` rooted at an isolated tmp home."""

    def _factory(project_id: str | None = None) -> Context:
        return Context(
            home=tmp_path,
            verbosity=Verbosity(),
            project_id=project_id,
        )

    return _factory
