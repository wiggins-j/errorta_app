"""Spec 18 — `errorta status` from an unbound directory.

Phase 1: when nothing is bound, `_call` also fetches `GET /coding/projects` so
the render can surface what the sidecar is actually doing — and does NOT make the
side-effecting run call. The bound branch is untouched (exactly one run call). A
failing project-list call degrades to the health-only payload.

Phase 2: the render lists running projects first, then blocking attention, then
the rest, capped at 5 with a `+N more` tail and the exact next-command hints;
with no projects it prints only the existing no-project message.
"""
from __future__ import annotations

from typing import Any

from errorta_cli import registry
from errorta_cli.commands.status import _call
from errorta_cli.render import NO_PROJECT_MSG
from errorta_cli.render.status import render_status
from errorta_cli.verbosity import Verbosity

from .conftest import RouteClient

# --------------------------------------------------------------------------- #
# Phase 1 — the call.
# --------------------------------------------------------------------------- #

def _projects_resp(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"/healthz": {"service": "e"}, "/coding/projects": {"projects": items}}


def test_unbound_call_fetches_health_and_project_list_and_no_run(make_ctx) -> None:
    client = RouteClient(responses=_projects_resp([{"id": "a", "list_status": "running"}]))
    payload = _call(client, make_ctx(), {})
    assert client.calls == [("GET", "/healthz"), ("GET", "/coding/projects")]
    assert payload["project_id"] is None
    assert payload["projects"] == [{"id": "a", "list_status": "running"}]
    assert payload["run"] is None


def test_bound_call_is_unchanged(make_ctx) -> None:
    client = RouteClient(default={"health": {}, "run": {}})
    payload = _call(client, make_ctx(project_id="p"), {})
    assert client.calls == [("GET", "/healthz"), ("GET", "/coding/projects/p/run")]
    assert payload["projects"] is None


def test_failing_project_list_degrades_to_health_only(make_ctx) -> None:
    class _Raising(RouteClient):
        def get_json(self, path: str, *, params: dict | None = None) -> Any:
            self.calls.append(("GET", path))
            if path == "/coding/projects":
                raise RuntimeError("sidecar hiccup")
            return {"service": "e"}

    client = _Raising()
    payload = _call(client, make_ctx(), {})
    assert ("GET", "/coding/projects") in client.calls
    assert payload["projects"] is None
    # Render must not blow up on the degraded payload.
    out = render_status(payload, Verbosity())
    assert "no project bound to this directory" in out


# --------------------------------------------------------------------------- #
# Phase 2 — the render.
# --------------------------------------------------------------------------- #

def _payload(projects: Any) -> dict[str, Any]:
    return {
        "project_id": None,
        "health": {"service": "errorta", "version": "1", "python": "3.14"},
        "run": None,
        "projects": projects,
    }


def _flat(out: str) -> str:
    return " ".join(out.split())


def test_no_projects_prints_only_the_message() -> None:
    out = render_status(_payload([]), Verbosity())
    assert NO_PROJECT_MSG.split(" — ")[0] in out
    # No table rows, no hint block when there is nothing to target.
    assert "errorta open <id>" not in out
    assert "errorta watch" not in out


def test_none_projects_also_prints_only_the_message() -> None:
    out = render_status(_payload(None), Verbosity())
    assert "no project bound to this directory" in out
    assert "errorta open <id>" not in out


def test_running_first_then_hints() -> None:
    projects = [
        {"id": "idle-a", "list_status": "active", "list_status_reason": "lifecycle"},
        {"id": "live-1", "list_status": "running", "list_status_reason": "live_run"},
        {"id": "idle-b", "list_status": "paused", "list_status_reason": "lifecycle"},
    ]
    out = render_status(_payload(projects), Verbosity())
    flat = _flat(out)
    # Running project sorts ahead of the two idle ones.
    assert flat.index("live-1") < flat.index("idle-a")
    assert flat.index("live-1") < flat.index("idle-b")
    # Both next-command hint lines are present, verbatim.
    assert "errorta open <id>" in out
    assert "bind this directory" in out
    assert "errorta watch" in out
    assert "live dashboard for the bound project" in out


def test_attention_sorts_ahead_of_idle() -> None:
    projects = [
        {"id": "idle-a", "list_status": "active", "list_status_reason": "lifecycle"},
        {"id": "attn-1", "list_status": "needs attention",
         "list_status_reason": "auth_failed"},
    ]
    flat = _flat(render_status(_payload(projects), Verbosity()))
    assert flat.index("attn-1") < flat.index("idle-a")
    # The reason explains something → it is surfaced.
    assert "auth_failed" in flat


def test_reason_hidden_when_uninformative() -> None:
    projects = [{"id": "live-1", "list_status": "running", "list_status_reason": "live_run"}]
    flat = _flat(render_status(_payload(projects), Verbosity()))
    assert "live-1" in flat
    assert "live_run" not in flat  # redundant with the running status


def test_caps_at_five_with_more_tail() -> None:
    projects = [
        {"id": f"p{i}", "list_status": "active", "list_status_reason": "lifecycle"}
        for i in range(8)
    ]
    flat = _flat(render_status(_payload(projects), Verbosity()))
    shown = [f"p{i}" for i in range(8) if f"p{i}" in flat]
    assert len(shown) == 5
    assert "+3 more" in flat
    assert "errorta projects" in flat


def test_terminal_bad_stop_reason_renders_in_failure_style(monkeypatch) -> None:
    import errorta_cli.render as _render

    monkeypatch.setattr(_render, "_color_enabled", lambda: True)
    # list_status "active" would normally style green (cli.ok); the _TERMINAL_BAD
    # stop reason must override it to the failure style — this is the case the
    # list-status derivation under-classifies.
    projects = [{
        "id": "boom", "list_status": "active",
        "list_status_reason": "gate_not_improving",
    }]
    out = render_status(_payload(projects), Verbosity())
    row = next(ln for ln in out.splitlines() if "boom" in ln)
    assert "\x1b[31m" in row  # cli.bad, not the green an "active" row would get
    assert "\x1b[32m" not in row  # cli.ok (green) is overridden


# --------------------------------------------------------------------------- #
# Regression lock — the BOUND render must stay byte-identical to main.
# --------------------------------------------------------------------------- #

def test_bound_render_is_byte_identical() -> None:
    """Spec 18 is CLI-only and must not perturb the bound-status rendering.

    A representative bound payload (health + a failed run with caps, backlog,
    counters, resumable + last_error) locked to its exact string. If this drifts,
    the unbound work leaked into the bound path.
    """
    payload = {
        "project_id": "demo",
        "health": {
            "service": "errorta", "version": "0.1.0", "python": "3.14",
            "build": {"commit": "abc123", "dirty": True},
            "residency": {"mode": "local"},
        },
        "run": {
            "running": False, "can_resume": True,
            "state": {
                "status": "failed", "stop_reason": "gate_not_improving",
                "last_error": "boom",
                "counters": {"iterations": 3, "model_calls": 10, "tasks_done": 2},
            },
            "caps": {
                "max_iterations": 40, "max_model_calls": None,
                "max_parallel_workers": None, "delivery_review_round_limit": 6,
                "defaulted": ["max_model_calls"],
            },
            "backlog": {"todo": 5, "dispatchable": 0},
        },
    }
    expected = (
        "sidecar: errorta v0.1.0 (python 3.14)\n"
        "build:   abc123 (dirty)\n"
        "residency: local\n"
        "project: demo\n"
        "run:     failed\n"
        "stop:    gate_not_improving\n"
        "         (resumable)\n"
        "error:   boom\n"
        "caps: iterations 40  model_calls ∞ (default)  parallel auto  delivery_rounds 6\n"
        "todo:    5 (dispatchable: 0)\n"
        "  iterations    3\n"
        "  model_calls  10\n"
        "  tasks_done    2"
    )
    assert render_status(payload, Verbosity()) == expected


def test_json_mode_returns_full_unfiltered_list(make_ctx) -> None:
    items = [{"id": "a", "list_status": "running", "list_status_reason": "live_run",
              "north_star": "ship"}]
    client = RouteClient(responses=_projects_resp(items))
    payload, text = registry.dispatch("status", client, make_ctx(), ["--json"])
    assert payload["projects"] == items
    assert '"north_star"' in text
    assert '"ship"' in text
