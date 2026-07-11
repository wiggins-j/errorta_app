"""S6 mid-run steering — interject / pm-control / governance / attention / file-edit
+ worktree accept (F147 §8.3/§8.4).

Grounded against the real ``coding.py`` routes + Pydantic bodies (``_ResolveSignalBody``,
``_UpdateProjectFile``, ``_GovernanceSettingsBody``, ``_NewTask``). HTTP is either a
``RouteClient`` recorder or a real ``SidecarClient`` over ``httpx.MockTransport``; the
sidecar is never booted. The autouse ``_neutralize_sole_owner_guard`` fixture pins the
guard to a no-op; tests that assert the guard fired re-``setattr`` a spy.
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import registry
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import CliError, LockBusy

from .conftest import RouteClient

PID = "proj-1"


def _capture(handler):
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


class _Recorder:
    """Capture every request's method/path/body/params, returning canned bodies."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.seen: list[dict] = []
        self.responses = responses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append({
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
            "origin": request.headers.get(ORIGIN_HEADER),
            "body": body,
        })
        for key, resp in self.responses.items():
            if key in request.url.path and (
                    key.endswith(request.method) or "|" not in key):
                return httpx.Response(200, json=resp)
        return httpx.Response(200, json={})

    def last(self, method: str) -> dict:
        for entry in reversed(self.seen):
            if entry["method"] == method:
                return entry
        raise AssertionError(f"no {method} recorded in {self.seen}")


# --------------------------------------------------------------------------- #
# 1. Route + body: each steering command hits the real route with real fields.
# --------------------------------------------------------------------------- #

def test_interject_posts_message_body_with_origin(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("interject", client, make_ctx(project_id=PID),
                          ["steer left", "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/interject"
    assert post["body"] == {"message": "steer left"}
    assert post["origin"] == ORIGIN_VALUE


def test_interject_carries_artifact_id(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("interject", client, make_ctx(project_id=PID),
                          ["fix it", "--artifact-id", "a1", "--yes"])
    assert rec.last("POST")["body"] == {"message": "fix it", "artifact_id": "a1"}


def test_pm_ask_posts_message(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("pm", client, make_ctx(project_id=PID),
                          ["ask", "how far along?", "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/pm-ask"
    assert post["body"] == {"message": "how far along?"}


def test_pm_bare_question_is_pm_ask(make_ctx) -> None:
    """A non-subcommand first token is a question routed to pm-ask."""
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("pm", client, make_ctx(project_id=PID),
                          ["what is left to do", "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/pm-ask"
    assert post["body"] == {"message": "what is left to do"}


def test_pm_control_directive_body(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("pm", client, make_ctx(project_id=PID),
                          ["control", "use sonnet for dev", "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/pm-control"
    assert post["body"] == {"directive": "use sonnet for dev"}


def test_pm_control_actions_json_uses_real_catalog_shape(make_ctx) -> None:
    """--actions passes the REAL control-action catalog shapes through verbatim."""
    actions = [
        {"type": "assign_models", "role_routes": {"dev": "sonnet"}},
        {"type": "set_autonomy", "knobs": {"max_iterations": 50}},
        {"type": "set_governance", "fields": {"block_on_problems": True}},
        {"type": "create_task", "title": "add x", "detail": "d", "role": "dev"},
        {"type": "start_run"},
    ]
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("pm", client, make_ctx(project_id=PID),
                          ["control", "--actions", json.dumps(actions), "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/pm-control"
    assert post["body"] == {"actions": actions}


def test_pm_control_bad_actions_json_errors(make_ctx) -> None:
    with pytest.raises(CliError) as ei:
        registry.dispatch("pm", RouteClient(), make_ctx(project_id=PID),
                          ["control", "--actions", "{not json", "--yes"])
    assert ei.value.code == "bad_actions_json"


@pytest.mark.parametrize("sub", ["accept", "decline"])
def test_pm_change_accept_decline_routes(make_ctx, sub) -> None:
    client = RouteClient()
    registry.dispatch("pm", client, make_ctx(project_id=PID), [sub, "c-9", "--yes"])
    assert ("POST", f"/coding/projects/{PID}/pm-changes/c-9/{sub}") in client.calls


@pytest.mark.parametrize("sub", ["chat", "changes"])
def test_pm_reads_hit_get_routes(make_ctx, sub) -> None:
    client = RouteClient()
    registry.dispatch("pm", client, make_ctx(project_id=PID), [sub])
    route = "pm-changes" if sub == "changes" else "pm-chat"
    assert ("GET", f"/coding/projects/{PID}/{route}") in client.calls


def test_task_new_posts_new_task_body(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("task", client, make_ctx(project_id=PID),
                          ["new", "add login", "--role", "dev", "--detail", "d",
                           "--depends-on", "t1,t2", "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/tasks"
    assert post["body"] == {"title": "add login", "role": "dev", "detail": "d",
                            "depends_on": ["t1", "t2"]}


def test_task_new_defaults_role_dev(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("task", client, make_ctx(project_id=PID),
                          ["new", "thing", "--yes"])
    assert rec.last("POST")["body"] == {"title": "thing", "role": "dev", "detail": ""}


def test_task_set_patches_only_given_fields(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("task", client, make_ctx(project_id=PID),
                          ["set", "t-3", "--state", "done", "--yes"])
    patch = rec.last("PATCH")
    assert patch["path"] == f"/coding/projects/{PID}/tasks/t-3"
    assert patch["body"] == {"state": "done"}


def test_governance_settings_sends_only_set_fields(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("governance", client, make_ctx(project_id=PID),
                          ["settings", "--mode", "strict",
                           "--block-on-problems", "false",
                           "--max-review-rounds", "3", "--yes"])
    put = rec.last("PUT")
    assert put["path"] == f"/coding/projects/{PID}/governance/settings"
    # ONLY the three fields the user set — no phase/human_code_approval/monitor.
    assert put["body"] == {"mode": "strict", "block_on_problems": False,
                           "max_review_rounds": 3}


def test_governance_approve_reject_bodies(make_ctx) -> None:
    for sub in ("approve", "reject"):
        rec = _Recorder()
        with _capture(rec) as client:
            registry.dispatch("governance", client, make_ctx(project_id=PID),
                              [sub, "ap-1", "--feedback", "lgtm", "--yes"])
        post = rec.last("POST")
        assert post["path"] == (
            f"/coding/projects/{PID}/governance/approvals/ap-1/{sub}")
        assert post["body"] == {"feedback": "lgtm", "actor": "user"}


def test_governance_artifact_accept_sends_confirm_true(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("governance", client, make_ctx(project_id=PID),
                          ["artifact", "accept", "art-1", "--yes"])
    post = rec.last("POST")
    assert post["path"] == (
        f"/coding/projects/{PID}/governance/artifacts/art-1/accept")
    assert post["body"] == {"confirm": True}


def test_governance_artifact_export_task_body(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("governance", client, make_ctx(project_id=PID),
                          ["artifact", "export-task", "art-2",
                           "--target-path", "docs/spec.md", "--title", "T", "--yes"])
    post = rec.last("POST")
    assert post["path"] == (
        f"/coding/projects/{PID}/governance/artifacts/art-2/export-task")
    assert post["body"] == {"target_path": "docs/spec.md", "title": "T"}


def test_attention_resolve_sends_resolve_signal_body(make_ctx, tmp_path) -> None:
    correction = tmp_path / "fix.txt"
    correction.write_text("do it this way", encoding="utf-8")
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("attention", client, make_ctx(project_id=PID),
                          ["resolve", "sig-7", "--action", "apply_correction",
                           "--suggestion-id", "s1",
                           "--correction-file", str(correction), "--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/attention/sig-7/resolve"
    # Exactly the _ResolveSignalBody field names.
    assert post["body"] == {"action": "apply_correction", "suggestion_id": "s1",
                            "correction_text": "do it this way"}


def test_attention_resolve_requires_action(make_ctx) -> None:
    client = RouteClient()
    payload, _ = registry.dispatch("attention", client, make_ctx(project_id=PID),
                                   ["resolve", "sig-1", "--yes"])
    assert payload.get("_usage")
    assert client.calls == []  # never fired without an --action


# --------------------------------------------------------------------------- #
# 2. edit — GET then PUT with expected_sha256; 409 stale / run_active conflicts.
# --------------------------------------------------------------------------- #

_SHA = "a" * 64


def _edit_handler(*, put_status=200, put_detail=None, current_sha=_SHA):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={
                "path": "app.py", "content": "old\n", "content_sha256": current_sha,
                "encoding": "utf-8", "bytes": 4, "on_master": True})
        # PUT
        if put_status != 200:
            return httpx.Response(put_status, json={"detail": put_detail})
        return httpx.Response(200, json={
            "path": "app.py", "content_sha256": "b" * 64, "bytes": 4,
            "head": "deadbeef1234", "on_master": True})
    return handler


def test_edit_reads_then_puts_with_expected_sha(make_ctx, tmp_path) -> None:
    new = tmp_path / "new.py"
    new.write_text("new content\n", encoding="utf-8")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "path": request.url.path,
                     "params": dict(request.url.params),
                     "body": json.loads(request.content) if request.content else None})
        if request.method == "GET":
            return httpx.Response(200, json={
                "path": "app.py", "content": "old\n", "content_sha256": _SHA,
                "encoding": "utf-8", "bytes": 4, "on_master": True})
        return httpx.Response(200, json={"path": "app.py", "head": "h", "bytes": 12,
                                         "content_sha256": "b" * 64})

    with _capture(handler) as client:
        registry.dispatch("edit", client, make_ctx(project_id=PID),
                          ["app.py", "--content-file", str(new), "--yes"])
    get, put = seen[0], seen[1]
    assert get["method"] == "GET" and get["params"] == {"path": "app.py"}
    assert put["method"] == "PUT"
    assert put["path"] == f"/coding/projects/{PID}/files"
    assert put["params"] == {"path": "app.py"}
    # _UpdateProjectFile: content (from the file) + the sha the GET returned.
    assert put["body"] == {"content": "new content\n", "expected_sha256": _SHA}


def test_edit_stale_409_renders_conflict(make_ctx, tmp_path) -> None:
    new = tmp_path / "new.py"
    new.write_text("new\n", encoding="utf-8")
    handler = _edit_handler(put_status=409,
                            put_detail={"reason": "stale_file", "content_sha256": "c" * 64})
    with _capture(handler) as client:
        with pytest.raises(LockBusy) as ei:
            registry.dispatch("edit", client, make_ctx(project_id=PID),
                              ["app.py", "--content-file", str(new), "--yes"])
    assert "stale" in ei.value.message.lower()
    assert ei.value.exit_code == 3


def test_edit_run_active_409_renders_conflict(make_ctx, tmp_path) -> None:
    new = tmp_path / "new.py"
    new.write_text("new\n", encoding="utf-8")
    handler = _edit_handler(put_status=409, put_detail={
        "reason": "run_active",
        "message": "cannot edit files while a coding run is active"})
    with _capture(handler) as client:
        with pytest.raises(LockBusy) as ei:
            registry.dispatch("edit", client, make_ctx(project_id=PID),
                              ["app.py", "--content-file", str(new), "--yes"])
    assert "run is live" in ei.value.message.lower()


def test_edit_no_content_source_bails_after_read(make_ctx) -> None:
    """Non-interactive + no --content-file: read current, then bail (no PUT)."""
    rec = _Recorder(responses={"files": {
        "content": "x\n", "content_sha256": _SHA, "encoding": "utf-8"}})
    with _capture(rec) as client:
        payload, _ = registry.dispatch("edit", client, make_ctx(project_id=PID),
                                       ["app.py"])
    assert payload["_kind"] == "needs_content"
    assert [e["method"] for e in rec.seen] == ["GET"]  # only the read


def test_edit_unchanged_content_is_noop(make_ctx, tmp_path) -> None:
    same = tmp_path / "same.py"
    same.write_text("old\n", encoding="utf-8")
    rec = _Recorder(responses={"files": {
        "content": "old\n", "content_sha256": _SHA, "encoding": "utf-8"}})
    with _capture(rec) as client:
        payload, _ = registry.dispatch("edit", client, make_ctx(project_id=PID),
                                       ["app.py", "--content-file", str(same), "--yes"])
    assert payload["_kind"] == "unchanged"
    assert [e["method"] for e in rec.seen] == ["GET"]  # never PUT an identical blob


# --------------------------------------------------------------------------- #
# 3. accept — confirm/--yes gated; POST {confirm:true}; merge-gate conflict.
# --------------------------------------------------------------------------- #

def test_accept_posts_confirm_true(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("accept", client, make_ctx(project_id=PID), ["--yes"])
    post = rec.last("POST")
    assert post["path"] == f"/coding/projects/{PID}/worktree/accept"
    assert post["body"] == {"confirm": True}


def test_accept_override_and_allow_conflicts(make_ctx) -> None:
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("accept", client, make_ctx(project_id=PID),
                          ["--override", "--allow-conflicts", "--yes"])
    assert rec.last("POST")["body"] == {
        "confirm": True, "override": True, "allow_conflicts": True}


def test_accept_merge_gate_blocked_surfaces_hint(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {
            "error": "merge_gate_blocked", "gate": {"blockers": []}}})

    with _capture(handler) as client:
        with pytest.raises(LockBusy) as ei:
            registry.dispatch("accept", client, make_ctx(project_id=PID), ["--yes"])
    assert ei.value.code == "merge_gate_blocked"
    assert "--override" in ei.value.message


def test_accept_requires_yes_non_interactive(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("accept", client, make_ctx(project_id=PID), [])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_accept_prompts_interactively_and_decline_skips_merge(make_ctx, monkeypatch) -> None:
    """A bare interactive `accept` must prompt (M1); declining does NOT merge-back."""
    monkeypatch.setattr("errorta_cli.commands._mutate.is_interactive", lambda: True)
    monkeypatch.setattr("errorta_cli.commands._mutate.prompt_yes_no",
                        lambda *a, **k: False)
    client = RouteClient()
    registry.dispatch("accept", client, make_ctx(project_id=PID), [])
    assert client.calls == []  # user declined the y/N → no worktree/accept POST


def test_steering_mutation_never_reads_run_state(make_ctx) -> None:
    """No steering command issues a request on the side-effecting /run route (L2).

    Mid-run steering must not gate on run state; a stray /run call would also
    trip the recovery side-effect the sole-owner model forbids.
    """
    rec = _Recorder()
    with _capture(rec) as client:
        registry.dispatch("interject", client, make_ctx(project_id=PID),
                          ["steer the team now", "--yes"])
    assert rec.seen, "interject issued no request"
    assert all("/run" not in e["path"] for e in rec.seen), rec.seen


# --------------------------------------------------------------------------- #
# 4. files / diff reads.
# --------------------------------------------------------------------------- #

def test_files_read_gets_with_path_param(make_ctx) -> None:
    rec = _Recorder(responses={"files": {"content": "hi", "encoding": "utf-8",
                                         "bytes": 2, "content_sha256": _SHA}})
    with _capture(rec) as client:
        registry.dispatch("files", client, make_ctx(project_id=PID), ["src/a.py"])
    get = rec.last("GET")
    assert get["path"] == f"/coding/projects/{PID}/files"
    assert get["params"] == {"path": "src/a.py"}


def test_diff_reads_worktree(make_ctx) -> None:
    client = RouteClient()
    registry.dispatch("diff", client, make_ctx(project_id=PID), [])
    assert ("GET", f"/coding/projects/{PID}/worktree") in client.calls


# --------------------------------------------------------------------------- #
# 5. Sole-owner guard: fired on every mutation, NOT on reads (#5).
# --------------------------------------------------------------------------- #

_S6_MUTATIONS = [
    ("interject", ["hi", "--yes"]),
    ("pm", ["ask", "q", "--yes"]),
    ("pm", ["control", "d", "--yes"]),
    ("pm", ["accept", "c1", "--yes"]),
    ("pm", ["decline", "c1", "--yes"]),
    ("task", ["new", "t", "--yes"]),
    ("task", ["set", "t1", "--state", "done", "--yes"]),
    ("governance", ["settings", "--mode", "strict", "--yes"]),
    ("governance", ["approve", "a1", "--yes"]),
    ("governance", ["reject", "a1", "--yes"]),
    ("governance", ["artifact", "accept", "art1", "--yes"]),
    ("governance", ["artifact", "export-task", "art1", "--target-path", "x.md", "--yes"]),
    ("attention", ["resolve", "s1", "--action", "dismiss", "--yes"]),
    ("accept", ["--yes"]),
]

_S6_READS = [
    ("pm", ["chat"]),
    ("pm", ["changes"]),
    ("governance", []),
    ("attention", []),
    ("files", ["a.py"]),
    ("diff", []),
]


def test_guard_invoked_on_every_s6_mutation(make_ctx, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: calls.append((home, handle)))
    for name, args in _S6_MUTATIONS:
        client = RouteClient(default={"content": "x", "encoding": "utf-8",
                                      "content_sha256": _SHA})
        registry.dispatch(name, client, make_ctx(project_id=PID), args)
    assert len(calls) == len(_S6_MUTATIONS), calls


def test_guard_not_invoked_on_s6_reads(make_ctx, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    for name, args in _S6_READS:
        client = RouteClient(default={"content": "x", "encoding": "utf-8",
                                      "content_sha256": _SHA})
        registry.dispatch(name, client, make_ctx(project_id=PID), args)
    assert calls == []


# --------------------------------------------------------------------------- #
# 6. --yes gating: every mutation refuses non-interactively without --yes (#7).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, args", _S6_MUTATIONS)
def test_s6_mutation_requires_yes_non_interactive(make_ctx, name, args) -> None:
    without_yes = [a for a in args if a != "--yes"]
    client = RouteClient(default={"content": "x", "encoding": "utf-8",
                                  "content_sha256": _SHA})
    with pytest.raises(CliError) as ei:
        registry.dispatch(name, client, make_ctx(project_id=PID), without_yes)
    assert ei.value.code == "confirmation_required"
    # The mutating request never fired (the confirm gate is BEFORE the POST/PUT).
    mutations = [c for c in client.calls if c[0] in ("POST", "PUT", "PATCH")]
    assert mutations == [], (name, client.calls)


# --------------------------------------------------------------------------- #
# 7. --watch on a steering subcommand is refused (would re-fire every tick).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, args", [
    ("pm", ["control", "d", "--yes", "--watch"]),
    ("governance", ["settings", "--mode", "strict", "--yes", "--watch"]),
    ("attention", ["resolve", "s1", "--action", "x", "--yes", "--watch"]),
])
def test_watch_on_steering_subcommand_refused(make_ctx, name, args) -> None:
    with pytest.raises(CliError) as ei:
        registry.dispatch(name, RouteClient(), make_ctx(project_id=PID), args)
    assert ei.value.code == "watch_on_mutation"


# --------------------------------------------------------------------------- #
# 8. No-project short-circuit: no route call on any S6 command when unbound.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["interject", "pm", "task", "governance",
                                  "attention", "files", "edit", "diff", "accept"])
def test_no_project_short_circuits_before_any_route(make_ctx, name) -> None:
    client = RouteClient()
    payload, _ = registry.dispatch(name, client, make_ctx(project_id=None),
                                   ["--yes"])
    assert client.calls == []
    from errorta_cli.render import is_no_project
    assert is_no_project(payload)


# --------------------------------------------------------------------------- #
# 9. Renderers don't leak a planted secret into human output (#4).
# --------------------------------------------------------------------------- #

SECRET = "sk-ant-S6-SECRET-DO-NOT-RENDER"


@pytest.mark.parametrize("name, args, resp", [
    ("interject", ["hi", "--yes"], {"applied": [], "refusals": [
        {"code": "x", "reason": "no", "_secret": SECRET}], "run_started": False}),
    ("accept", ["--yes"], {"delivered_to": "/tmp/out", "run_hint": "python x.py",
                           "_secret": SECRET}),
])
def test_s6_render_never_leaks_secret(make_ctx, name, args, resp) -> None:
    client = RouteClient(default=resp)
    _p, text = registry.dispatch(name, client, make_ctx(project_id=PID), args)
    assert SECRET not in text


# --------------------------------------------------------------------------- #
# 10. Registry parity: every new S6 command is registered.
# --------------------------------------------------------------------------- #

def test_s6_commands_registered() -> None:
    for name in ("interject", "task", "files", "edit", "diff", "accept"):
        assert registry.get(name) is not None, name
    # pm / governance / attention keep their names but gained steering subs.
    assert registry.get("pm") is not None
    assert registry.get("governance") is not None
    assert registry.get("attention") is not None
