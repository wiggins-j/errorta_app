"""S5 — project create/import + management + north-star/focus (F147 §5.1, §8.1, §8.3).

Grounded against the real routes in ``routes/coding.py``. The sidecar is never
booted: HTTP is a ``RouteClient`` fake (route-only assertions) or a real
``SidecarClient`` over ``httpx.MockTransport`` (request-body assertions). The
autouse ``_neutralize_sole_owner_guard`` fixture (conftest) pins the sole-owner
guard to a no-op; tests that assert the guard is *invoked* re-``setattr`` a spy.

Directory binding: ``make_ctx`` pins ``ctx.cwd`` to an isolated tmp dir, so any
``.errorta-project`` pointer a command writes lands there — never in the repo.
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import config, registry
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import EXIT_LOCK_BUSY, EXIT_NOT_FOUND, CliError, LockBusy, NotFound

from .conftest import RouteClient

PID = "acme"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 1. new → POST _NewProject with the right fields + writes the pointer + binds.
# --------------------------------------------------------------------------- #

def test_new_posts_new_project_body_and_writes_pointer(make_ctx, tmp_path) -> None:
    ctx = make_ctx()  # cwd == tmp_path
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, request.headers.get(ORIGIN_HEADER),
                       json.loads(request.content)))
        return httpx.Response(200, json={"project": {"id": "snake", "status": "active"}})

    with _mock_client(handler) as client:
        payload, _text = registry.dispatch(
            "new", client, ctx,
            ["snake", "--north-star", "build snake", "--dod", "it runs", "--yes"])

    path, origin, body = bodies[0]
    assert path == "/coding/projects"
    assert origin == ORIGIN_VALUE                       # invariant #2
    assert body["project_id"] == "snake"
    assert body["target"] == "new"
    assert body["north_star"] == "build snake"
    assert body["definition_of_done"] == "it runs"
    # No delivery_root unless --here / --delivery-root was given.
    assert "delivery_root" not in body
    # The pointer bound this dir to the project, and ctx switched to it.
    assert config.read_pointer(tmp_path) == "snake"
    assert ctx.project_id == "snake"
    assert payload["_kind"] == "created"


def test_new_here_sets_delivery_root_to_cwd(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"project": {"id": "g"}})

    with _mock_client(handler) as client:
        registry.dispatch("new", client, ctx, ["g", "--here", "--yes"])
    assert bodies[0]["delivery_root"] == str(tmp_path)


def test_new_delivery_root_flag(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"project": {"id": "g"}})

    with _mock_client(handler) as client:
        registry.dispatch("new", client, ctx,
                          ["g", "--delivery-root", "/tmp/x", "--yes"])
    assert bodies[0]["delivery_root"] == "/tmp/x"


def test_new_requires_yes_non_interactive(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("new", client, make_ctx(), ["snake"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []  # nothing created without confirmation


def test_new_invokes_sole_owner_guard(make_ctx, monkeypatch) -> None:
    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append(1))
    registry.dispatch("new", RouteClient(default={"project": {"id": "g"}}),
                      make_ctx(), ["g", "--yes"])
    assert spy, "sole-owner guard not invoked on new"


def test_new_without_id_is_parser_error_no_call(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError, match="missing required argument.*id"):
        registry.dispatch("new", client, make_ctx(), ["--yes"])
    assert client.calls == []


# --------------------------------------------------------------------------- #
# 2. projects → GET list, renders derived list_status.
# --------------------------------------------------------------------------- #

def test_projects_lists_with_status(make_ctx) -> None:
    client = RouteClient(responses={"/coding/projects": {"projects": [
        {"id": "a", "list_status": "running", "list_status_reason": "",
         "north_star": "ship it"},
        {"id": "b", "list_status": "needs attention",
         "list_status_reason": "auth_failed", "north_star": ""},
    ]}})
    _payload, text = registry.dispatch("projects", client, make_ctx(), [])
    assert ("GET", "/coding/projects") in client.calls
    assert "a" in text and "b" in text
    assert "running" in text and "needs attention" in text


def test_projects_does_not_guard(make_ctx, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: called.append(1))
    registry.dispatch("projects", RouteClient(default={"projects": []}),
                      make_ctx(), [])
    assert called == []


# --------------------------------------------------------------------------- #
# 3. open / switch → GET project, bind pointer + ctx.
# --------------------------------------------------------------------------- #

def test_open_binds_and_renders(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    client = RouteClient(responses={
        f"/coding/projects/{PID}": {"project": {"id": PID, "north_star": "n"}}})
    _payload, _text = registry.dispatch("open", client, ctx, [PID])
    assert ("GET", f"/coding/projects/{PID}") in client.calls
    assert config.read_pointer(tmp_path) == PID
    assert ctx.project_id == PID


def test_switch_is_same_as_open(make_ctx) -> None:
    ctx = make_ctx()
    client = RouteClient(default={"project": {"id": PID}})
    registry.dispatch("switch", client, ctx, [PID])
    assert ("GET", f"/coding/projects/{PID}") in client.calls
    assert ctx.project_id == PID


def test_open_unknown_project_does_not_write_pointer(make_ctx, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "project not found"})

    with _mock_client(handler) as client:
        with pytest.raises(NotFound) as ei:
            registry.dispatch("open", client, make_ctx(), ["ghost"])
    assert ei.value.exit_code == EXIT_NOT_FOUND
    # No binding written on a 404 (the GET raises before we bind).
    assert config.read_pointer(tmp_path) is None


def test_open_reads_do_not_guard(make_ctx, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: called.append(1))
    registry.dispatch("open", RouteClient(default={"project": {"id": PID}}),
                      make_ctx(), [PID])
    assert called == []


# --------------------------------------------------------------------------- #
# 4. delete → DELETE route, sole-owner + --yes gated; 409-active → LockBusy.
# --------------------------------------------------------------------------- #

def test_delete_deletes_when_confirmed(make_ctx, monkeypatch) -> None:
    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append(1))
    client = RouteClient(default={"deleted": True, "project_id": PID})
    ctx = make_ctx(project_id=PID)
    payload, _text = registry.dispatch("delete", client, ctx, [PID, "--yes"])
    assert ("DELETE", f"/coding/projects/{PID}") in client.calls
    assert spy, "sole-owner guard not invoked on delete"
    assert payload["_kind"] == "deleted"
    assert ctx.project_id is None  # unbound after deleting the bound project


def test_delete_requires_yes_non_interactive(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("delete", client, make_ctx(), [PID])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_delete_while_run_active_is_lockbusy(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "project run is still active"})

    with _mock_client(handler) as client:
        with pytest.raises(LockBusy) as ei:
            registry.dispatch("delete", client, make_ctx(), [PID, "--yes"])
    assert ei.value.exit_code == EXIT_LOCK_BUSY
    assert "still active" in ei.value.message


# --------------------------------------------------------------------------- #
# 5. import local → POST _LocalImport + writes the pointer at the repo path.
# --------------------------------------------------------------------------- #

def test_import_local_posts_body_and_binds(make_ctx, tmp_path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    ctx = make_ctx()
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"project": {
            "id": "myrepo", "target": "existing", "repo_path": str(repo)}})

    with _mock_client(handler) as client:
        registry.dispatch("import", client, ctx, ["local", str(repo), "--yes"])
    path, body = bodies[0]
    assert path == "/coding/projects/import/local"
    assert body["project_id"] == "myrepo"     # derived from the folder name
    assert body["folder_path"] == str(repo)
    assert "git_init" not in body             # not requested
    # The pointer was written into the imported repo dir.
    assert config.read_pointer(repo) == "myrepo"


def test_import_local_git_init_flag(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"project": {"id": "x", "repo_path": str(tmp_path)}})

    with _mock_client(handler) as client:
        registry.dispatch("import", client, ctx,
                          ["local", str(tmp_path), "--id", "x", "--git-init", "--yes"])
    assert bodies[0]["git_init"] is True and bodies[0]["confirm"] is True


def test_import_local_not_a_git_repo_is_friendly(make_ctx, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": {
            "error": "not_a_git_repo", "detail": "set git_init=true to initialize"}})

    with _mock_client(handler) as client:
        with pytest.raises(CliError) as ei:
            registry.dispatch("import", client, make_ctx(),
                              ["local", str(tmp_path), "--yes"])
    assert ei.value.code == "not_a_git_repo"
    assert "--git-init" in ei.value.message


def test_import_local_requires_yes(make_ctx, tmp_path) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("import", client, make_ctx(), ["local", str(tmp_path)])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# 6. import github (no url) → auth-status; a token is NEVER printed (#4).
# --------------------------------------------------------------------------- #

def test_import_github_auth_status_never_prints_token(make_ctx) -> None:
    # The real route returns only {gh_present, login, token_in_keychain}. Even if
    # a stray token-shaped field leaked into the payload, the renderer selects
    # fields and must not surface it.
    client = RouteClient(responses={
        "/coding/projects/import/github/auth-status": {
            "gh_present": True, "login": "octocat", "token_in_keychain": True,
            "token": "ghp_SENTINEL_TOKEN_DO_NOT_LEAK"}})
    _payload, text = registry.dispatch("import", client, make_ctx(), ["github"])
    assert ("GET", "/coding/projects/import/github/auth-status") in client.calls
    assert "octocat" in text
    assert "ghp_SENTINEL_TOKEN_DO_NOT_LEAK" not in text


def test_import_github_auth_status_is_a_read_no_guard(make_ctx, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: called.append(1))
    registry.dispatch("import",
                      RouteClient(default={"gh_present": False}),
                      make_ctx(), ["github"])
    assert called == []


# --------------------------------------------------------------------------- #
# 7. import github <url> → branches + clone + poll to done + GET + bind.
# --------------------------------------------------------------------------- #

def test_import_github_clone_polls_to_done_and_binds(make_ctx, tmp_path) -> None:
    dest = tmp_path / "clone"
    dest.mkdir()
    ctx = make_ctx()
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        seen.append((request.method, p))
        if p.endswith("/branches"):
            return httpx.Response(200, json={"ok": True, "branches": ["main"],
                                             "default_branch": "main"})
        if p.endswith("/import/github/clone"):
            return httpx.Response(200, json={"job_id": "job-9", "status": "cloning"})
        if p == "/coding/projects/import/github/clone/job-9":
            return httpx.Response(200, json={"status": "done", "project_id": "acme__widget"})
        if p == "/coding/projects/acme__widget":
            return httpx.Response(200, json={"project": {
                "id": "acme__widget", "repo_path": str(dest)}})
        return httpx.Response(200, json={})

    with _mock_client(handler) as client:
        payload, _text = registry.dispatch(
            "import", client, ctx,
            ["github", "https://github.com/acme/widget", "--branch", "main", "--yes"])
    methods = [(m, p) for m, p in seen]
    assert ("POST", "/coding/projects/import/github/branches") in methods
    assert ("POST", "/coding/projects/import/github/clone") in methods
    assert ("GET", "/coding/projects/import/github/clone/job-9") in methods
    assert ("GET", "/coding/projects/acme__widget") in methods
    assert payload["_kind"] == "cloned"
    assert config.read_pointer(dest) == "acme__widget"
    assert ctx.project_id == "acme__widget"


def test_import_github_clone_error_raises(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/branches"):
            return httpx.Response(200, json={"ok": False, "error": "x"})
        if p.endswith("/import/github/clone"):
            return httpx.Response(200, json={"job_id": "j", "status": "cloning"})
        if p.endswith("/clone/j"):
            return httpx.Response(200, json={"status": "error", "message": "repo not found"})
        return httpx.Response(200, json={})

    with _mock_client(handler) as client:
        with pytest.raises(CliError) as ei:
            registry.dispatch("import", client, make_ctx(),
                              ["github", "https://github.com/a/b", "--yes"])
    assert ei.value.code == "clone_error"
    assert "repo not found" in ei.value.message


def test_import_github_clone_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("import", client, make_ctx(),
                          ["github", "https://github.com/a/b"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# 8. cwd → project binding resolves through the pointer.
# --------------------------------------------------------------------------- #

def test_pointer_binding_resolves_project(tmp_path) -> None:
    config.write_pointer(tmp_path, "bound-proj")
    assert config.resolve_project_id(tmp_path, tmp_path) == "bound-proj"


# --------------------------------------------------------------------------- #
# 9. north-star show/set/proposal/accept.
# --------------------------------------------------------------------------- #

def test_north_star_show_reads_project(make_ctx) -> None:
    client = RouteClient(responses={
        f"/coding/projects/{PID}": {"project": {
            "id": PID, "north_star": "ship v1", "definition_of_done": "tests pass"}}})
    _payload, text = registry.dispatch("north-star", client, make_ctx(project_id=PID), [])
    assert ("GET", f"/coding/projects/{PID}") in client.calls
    assert "ship v1" in text and "tests pass" in text


def test_north_star_set_puts_body(make_ctx, monkeypatch) -> None:
    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append(1))
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"project": {"id": PID, "north_star": "new ns"}})

    with _mock_client(handler) as client:
        payload, text = registry.dispatch(
            "north-star", client, make_ctx(project_id=PID),
            ["set", "--north-star", "new ns", "--dod", "done", "--yes"])
    m, p, body = bodies[0]
    assert (m, p) == ("PUT", f"/coding/projects/{PID}/north-star")
    assert body == {"north_star": "new ns", "definition_of_done": "done"}
    assert spy, "sole-owner guard not invoked on north-star set"
    # The response is unwrapped exactly once (not {"project": {"project": ...}}),
    # so the rendered view shows the new North Star rather than a blank.
    assert payload["project"]["id"] == PID
    assert "new ns" in text


def test_north_star_proposal_reads(make_ctx) -> None:
    client = RouteClient(responses={
        f"/coding/projects/{PID}/north-star-proposal": {"proposal": {
            "north_star": "inferred", "definition_of_done": "x", "accepted": False}}})
    _payload, text = registry.dispatch("north-star", client, make_ctx(project_id=PID),
                                       ["proposal"])
    assert ("GET", f"/coding/projects/{PID}/north-star-proposal") in client.calls
    assert "inferred" in text


def test_north_star_accept_posts(make_ctx) -> None:
    client = RouteClient(responses={
        "/north-star-proposal/accept": {"project": {"id": PID}, "proposal": {}}})
    _payload, _text = registry.dispatch("north-star", client, make_ctx(project_id=PID),
                                        ["accept", "--yes"])
    assert ("POST", f"/coding/projects/{PID}/north-star-proposal/accept") in client.calls


def test_north_star_accept_while_live_renders_clear_refusal(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "project run is still active"})

    with _mock_client(handler) as client:
        with pytest.raises(LockBusy) as ei:
            registry.dispatch("north-star", client, make_ctx(project_id=PID),
                              ["accept", "--yes"])
    assert ei.value.exit_code == EXIT_LOCK_BUSY
    assert "cancel the run" in ei.value.message  # the friendly gate hint


def test_north_star_needs_project(make_ctx) -> None:
    _payload, text = registry.dispatch("north-star", RouteClient(), make_ctx(), [])
    assert "no project" in text.lower()


# --------------------------------------------------------------------------- #
# 10. focus list/add/edit/reorder/accept/work-request.
# --------------------------------------------------------------------------- #

def test_focus_list_reads_with_status(make_ctx) -> None:
    client = RouteClient(responses={
        f"/coding/projects/{PID}/focus": {"focuses": [
            {"id": "f1", "title": "graphics", "status": "active"}]}})
    _payload, text = registry.dispatch("focus", client, make_ctx(project_id=PID),
                                       ["list", "--status", "active"])
    assert ("GET", f"/coding/projects/{PID}/focus") in client.calls
    assert "graphics" in text


def test_focus_add_posts(make_ctx, monkeypatch) -> None:
    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append(1))
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"focus": {"id": "f2", "title": "add sprites",
                                                    "status": "active"}})

    with _mock_client(handler) as client:
        registry.dispatch("focus", client, make_ctx(project_id=PID),
                          ["add", "add sprites", "--body", "pixel art", "--yes"])
    path, body = bodies[0]
    assert path == f"/coding/projects/{PID}/focus"
    assert body["title"] == "add sprites"   # single quoted-arg title
    assert body["body"] == "pixel art"
    assert spy, "sole-owner guard not invoked on focus add"


def test_focus_edit_puts_patch(make_ctx) -> None:
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"focus": {"id": "f1", "status": "archived"}})

    with _mock_client(handler) as client:
        registry.dispatch("focus", client, make_ctx(project_id=PID),
                          ["edit", "f1", "--status", "archived", "--yes"])
    m, p, body = bodies[0]
    assert (m, p) == ("PUT", f"/coding/projects/{PID}/focus/f1")
    assert body == {"status": "archived"}


def test_focus_reorder_puts_ordered_ids(make_ctx) -> None:
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"focuses": []})

    with _mock_client(handler) as client:
        registry.dispatch("focus", client, make_ctx(project_id=PID),
                          ["reorder", "f2,f1,f3", "--yes"])
    m, p, body = bodies[0]
    assert (m, p) == ("PUT", f"/coding/projects/{PID}/focus/reorder")
    assert body == {"ordered_ids": ["f2", "f1", "f3"]}


def test_focus_accept_posts(make_ctx) -> None:
    client = RouteClient(responses={"/focus/f1/accept": {"focus": {"id": "f1"}}})
    registry.dispatch("focus", client, make_ctx(project_id=PID),
                      ["accept", "f1", "--yes"])
    assert ("POST", f"/coding/projects/{PID}/focus/f1/accept") in client.calls


def test_focus_accept_while_live_renders_clear_refusal(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "project run is still active"})

    with _mock_client(handler) as client:
        with pytest.raises(LockBusy) as ei:
            registry.dispatch("focus", client, make_ctx(project_id=PID),
                              ["accept", "f1", "--yes"])
    assert ei.value.exit_code == EXIT_LOCK_BUSY
    assert "cancel the run" in ei.value.message


def test_focus_work_request_puts(make_ctx) -> None:
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"project": {"id": PID}})

    with _mock_client(handler) as client:
        registry.dispatch("focus", client, make_ctx(project_id=PID),
                          ["work-request", "focus on tests", "--yes"])
    m, p, body = bodies[0]
    assert (m, p) == ("PUT", f"/coding/projects/{PID}/work-request")
    assert body == {"work_request": "focus on tests"}


def test_focus_add_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("focus", client, make_ctx(project_id=PID), ["add", "hi"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_focus_list_is_a_read_no_guard(make_ctx, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: called.append(1))
    registry.dispatch("focus", RouteClient(default={"focuses": []}),
                      make_ctx(project_id=PID), [])
    assert called == []


def test_focus_needs_project(make_ctx) -> None:
    _payload, text = registry.dispatch("focus", RouteClient(), make_ctx(), [])
    assert "no project" in text.lower()


# --------------------------------------------------------------------------- #
# 11. Registry parity + registration for the S5 commands.
# --------------------------------------------------------------------------- #

def test_s5_commands_are_registered() -> None:
    for name in ("projects", "new", "open", "switch", "delete", "import",
                 "north-star", "focus"):
        assert registry.get(name) is not None, name


def test_s5_reads_hit_identical_routes_via_both_surfaces(make_ctx) -> None:
    for name, args, expected in (
        ("projects", [], ("GET", "/coding/projects")),
        ("north-star", [], ("GET", "/coding/projects/p")),
        ("focus", [], ("GET", "/coding/projects/p/focus")),
        ("import", ["github"], ("GET", "/coding/projects/import/github/auth-status")),
    ):
        argv_client = RouteClient(default={"projects": [], "project": {},
                                           "focuses": [], "gh_present": False})
        slash_client = RouteClient(default={"projects": [], "project": {},
                                            "focuses": [], "gh_present": False})
        registry.dispatch(name, argv_client, make_ctx(project_id="p"), list(args))
        n_s, base = registry.split_slash("/" + name + " " + " ".join(args))
        registry.dispatch(n_s, slash_client, make_ctx(project_id="p"), base)
        assert argv_client.calls == slash_client.calls, name
        assert expected in argv_client.calls, (name, argv_client.calls)
