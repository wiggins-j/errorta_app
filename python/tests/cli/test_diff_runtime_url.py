"""`diff` stat-by-default (+ `--full`) and `runtime run` served-URL surfacing.

Two demo papercuts:
* `errorta diff` dumped the entire delivered diff (thousands of lines) — now the
  default is a per-file stat summary and `--full` prints the whole thing.
* `errorta runtime run --go` launched a web app but never printed the URL nor
  opened the browser — now it surfaces `http://localhost:PORT` and (on a TTY)
  opens it once the dev server answers.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from errorta_cli import registry
from errorta_cli.client import SidecarClient
from errorta_cli.commands import files, runtime

PID = "proj-1"
RT = f"/coding/projects/{PID}/runtime"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# diff: stat summary by default; --full for the whole diff.
# --------------------------------------------------------------------------- #

_RAW_DIFF = ("diff --git a/app/page.tsx b/app/page.tsx\n"
             "@@ -0,0 +1,3 @@\n+one\n+two\n+three\n")
_FILE_DIFFS = [
    {"path": "app/page.tsx", "oldPath": None, "changeType": "added",
     "addedLines": 120, "removedLines": 0, "hunks": []},
    {"path": "package.json", "oldPath": None, "changeType": "modified",
     "addedLines": 8, "removedLines": 2, "hunks": []},
]


def test_call_stashes_full_flag() -> None:
    class C:
        def get_json(self, *_a: Any, **_k: Any) -> Any:
            return {"diff": _RAW_DIFF, "file_diffs": _FILE_DIFFS}

    class Ctx:
        project_id = PID

    assert files._diff_call(C(), Ctx(), {"full": True})["_full"] is True
    assert files._diff_call(C(), Ctx(), {})["_full"] is False


def test_default_renders_stat_not_full_diff() -> None:
    payload = {"_kind": "diff", "_full": False, "diff": _RAW_DIFF,
               "file_diffs": _FILE_DIFFS}
    text = files._diff_render(payload, None, json_mode=False)
    assert "app/page.tsx" in text and "package.json" in text
    assert "+120" in text and "-2" in text
    assert "2 files changed" in text
    # The whole unified diff must NOT be dumped in the default view.
    assert "+one" not in text and "@@ -0,0" not in text


def test_full_flag_prints_whole_diff() -> None:
    payload = {"_kind": "diff", "_full": True, "diff": _RAW_DIFF,
               "file_diffs": _FILE_DIFFS}
    text = files._diff_render(payload, None, json_mode=False)
    # delta may or may not be on PATH; either way the hunk body is present.
    assert "+one" in text and "+three" in text


def test_no_changes_renders_empty() -> None:
    payload = {"_kind": "diff", "_full": False, "diff": "", "file_diffs": []}
    assert "no worktree changes" in files._diff_render(payload, None, json_mode=False)


def test_full_with_no_file_diffs_falls_back_to_raw() -> None:
    # Server returned a diff blob but no structured file_diffs — still printable.
    payload = {"_kind": "diff", "_full": False, "diff": _RAW_DIFF, "file_diffs": []}
    text = files._diff_render(payload, None, json_mode=False)
    assert "+one" in text  # nothing to summarize -> show the diff


# --------------------------------------------------------------------------- #
# runtime run: served URL.
# --------------------------------------------------------------------------- #

def test_served_url_for_server_modality() -> None:
    run = {"plan": {"modality": "server"},
           "session": {"session_id": "rs-1", "allocated_ports": [3000]}}
    assert runtime._served_url(run) == "http://localhost:3000"


def test_served_url_none_for_cli_modality() -> None:
    run = {"plan": {"modality": "cli"},
           "session": {"session_id": "rs-1", "allocated_ports": [3000]}}
    assert runtime._served_url(run) is None


def test_served_url_none_without_ports() -> None:
    run = {"plan": {"modality": "server"}, "session": {"allocated_ports": []}}
    assert runtime._served_url(run) is None


# --------------------------------------------------------------------------- #
# runtime run: browser-open gating (seams, no real browser/network).
# --------------------------------------------------------------------------- #

def _fake_stdout(is_tty: bool):
    class S:
        def isatty(self) -> bool:
            return is_tty
    return S()


def test_should_open_respects_no_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.sys, "stdout", _fake_stdout(True))
    assert runtime._should_open({"no-open": True}) is False


def test_should_open_forces_open_off_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.sys, "stdout", _fake_stdout(False))
    assert runtime._should_open({"open": True}) is True


def test_should_open_default_follows_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.sys, "stdout", _fake_stdout(True))
    assert runtime._should_open({}) is True
    monkeypatch.setattr(runtime.sys, "stdout", _fake_stdout(False))
    assert runtime._should_open({}) is False


def test_surface_opens_after_waiting() -> None:
    calls: list[str] = []
    payload: dict[str, Any] = {}
    run = {"plan": {"modality": "server"},
           "session": {"allocated_ports": [3000]}}
    runtime._surface_served_url(
        {"open": True}, run, payload,
        opener=lambda u: calls.append(f"open:{u}") or True,
        waiter=lambda u: calls.append(f"wait:{u}") or True,
        echo=lambda _m: None,
    )
    assert payload["_url"] == "http://localhost:3000"
    assert payload["_opened"] is True
    assert calls == ["wait:http://localhost:3000", "open:http://localhost:3000"]


def test_surface_no_open_still_reports_url() -> None:
    calls: list[str] = []
    payload: dict[str, Any] = {}
    run = {"plan": {"modality": "server"},
           "session": {"allocated_ports": [8000]}}
    runtime._surface_served_url(
        {"no-open": True}, run, payload,
        opener=lambda u: calls.append(u) or True,
        waiter=lambda u: calls.append(u) or True,
        echo=lambda _m: None,
    )
    assert payload["_url"] == "http://localhost:8000"
    assert "_opened" not in payload
    assert calls == []  # neither waited nor opened


def test_surface_noop_for_non_server() -> None:
    payload: dict[str, Any] = {}
    runtime._surface_served_url({"open": True},
                                {"plan": {"modality": "cli"}, "session": {}}, payload,
                                opener=lambda u: True, waiter=lambda u: True,
                                echo=lambda _m: None)
    assert payload == {}


def test_await_http_returns_true_once_server_answers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def fake_get(url: str, timeout: float = 0) -> Any:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert runtime._await_http("http://localhost:3000", sleep=lambda _s: None) is True
    assert attempts["n"] == 3


def test_await_http_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    assert runtime._await_http("http://localhost:3000", attempts=4,
                               sleep=lambda _s: None) is False


# --------------------------------------------------------------------------- #
# runtime run: through dispatch, the rendered launch shows the URL.
# --------------------------------------------------------------------------- #

def test_run_go_renders_served_url(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "resolved": True, "runnable": True, "plan": {"modality": "server"},
            "session": {"session_id": "rs-1", "state": "starting",
                        "allocated_ports": [3000]}})

    with _mock_client(handler) as client:
        payload, text = registry.dispatch(
            "runtime", client, make_ctx(project_id=PID),
            ["run", "--go", "--yes", "--no-open"])
    assert payload["_url"] == "http://localhost:3000"
    assert "http://localhost:3000" in text
    assert "_opened" not in payload  # --no-open
